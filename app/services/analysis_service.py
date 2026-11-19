"""Orchestrates: validate -> (cache) -> extract -> build context -> LLM -> post-process."""

import asyncio
import logging
import time
from collections import OrderedDict

from app.config import Settings
from app.schemas import (
    Analysis,
    AnalysisMeta,
    LinkAnalysisResponse,
    LLMAnalysis,
    TokenUsage,
)
from app.services.competitor_service import COMPETITOR_DISCLAIMER, refine_competitors
from app.services.llm_service import LLMService
from app.services.website_extractor import LOW_CONTENT_CHARS, WebsiteExtractor, build_llm_context
from app.utils.validators import ensure_public_url

logger = logging.getLogger(__name__)
MAX_CONTEXT_CHARS = 14_000  # upper bound used for logging / low-content detection

SYSTEM_PROMPT = """You are a senior digital-marketing strategist and SEO analyst.
You receive content extracted from ONE website and must produce a structured business analysis.

Rules:
- Ground everything in the provided website content. Quote real product/service names, wording and audience signals from it. Never produce generic filler that could apply to any company.
- Brand name: use the name shown on the site (title, og:site_name, structured data, H1).
- Competitors: list 3-5 real, currently operating companies that compete in the same category and market (match geography and customer size where evident). They come from your general knowledge, so always set source="ai_suggestion". Only include companies you are confident exist; give their real homepage or "" if unsure. Never list the analysed brand itself.
- comparison_table: rows for Main Service, Target Market, Key Strength, Weakness/Gap and Opportunity; one value per competitor in the same order as the competitors list.
- target_search_queries: exactly 5 realistic queries a potential customer would type. Collectively cover Google, Facebook, Instagram, LinkedIn and X; each query lists every platform it suits. Do NOT state or estimate search volumes or rankings.
- SEO: base issues_found on the observed SEO signals (title length, missing/weak meta description, H1 count, alt text, canonical, viewport). recommended_title 50-60 characters; meta_description 140-160 characters. Provide 4-6 secondary, 4-6 long-tail and 4-6 business keywords, and 5-8 supporting content ideas.
- top_services: exactly 5. Mark source="website_content" only when the service is explicitly described in the content; otherwise source="inferred".
- target_audience.recommended_marketing_segments: exactly 5 segments.
- If something is not stated on the website, say so (e.g. geographic_focus "Not specified on website") instead of inventing facts.
- Write concise, specific, professional English regardless of the website language. Keep each list to 3-6 items and each text field to one or two short sentences."""


# Typographic characters LLMs like to emit (non-breaking hyphens/spaces, zero-width chars)
# that render badly in many clients. Replaced with plain equivalents.
_TEXT_FIXES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", "​": "", "⁠": "", "﻿": "",
})


def normalize_text(data: LLMAnalysis) -> LLMAnalysis:
    return LLMAnalysis.model_validate_json(data.model_dump_json().translate(_TEXT_FIXES))


class TTLCache:
    """Small in-memory LRU cache with TTL (per process). Avoids repeated LLM calls for the same URL."""

    def __init__(self, ttl: int, max_entries: int):
        self.ttl, self.max_entries = ttl, max_entries
        self._data: OrderedDict[str, tuple[float, LinkAnalysisResponse]] = OrderedDict()

    def get(self, key: str) -> LinkAnalysisResponse | None:
        item = self._data.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: LinkAnalysisResponse) -> None:
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


class AnalysisService:
    def __init__(self, settings: Settings, extractor: WebsiteExtractor | None = None, llm: LLMService | None = None):
        self.settings = settings
        self.extractor = extractor or WebsiteExtractor(settings)
        self.llm = llm or LLMService(settings)
        self.cache = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries)
        self._inflight: dict[str, asyncio.Future] = {}

    async def analyze(self, raw_url: str, use_cache: bool = True) -> LinkAnalysisResponse:
        url = await ensure_public_url(raw_url)
        key = url.rstrip("/").lower()

        if use_cache and (hit := self.cache.get(key)):
            logger.info("Cache hit for %s", url)
            return hit.model_copy(update={"meta": hit.meta.model_copy(update={"cached": True})})

        # Collapse concurrent identical requests into one LLM call.
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await self._run(url)
            self.cache.set(key, result)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(key, None)

    async def _run(self, url: str) -> LinkAnalysisResponse:
        started = time.perf_counter()
        self.llm.ensure_configured()  # fail fast before fetching the website
        content = await self.extractor.extract(url)
        full_context = build_llm_context(content, MAX_CONTEXT_CHARS)
        logger.info("Extracted %d chars from %d page(s) for %s", len(full_context), len(content.pages), url)

        sent_chars: dict[str, int] = {}

        def build_user(max_chars: int) -> str:
            # Each provider gets the website content trimmed to its own budget.
            context = build_llm_context(content, max_chars)
            sent_chars["n"] = len(context)
            return f"Analyse this website and return the structured analysis.\n\n<website_content>\n{context}\n</website_content>"

        result = await self.llm.structured(SYSTEM_PROMPT, build_user, LLMAnalysis)
        data = normalize_text(result.data)  # type: ignore[arg-type]
        context_len = sent_chars.get("n", len(full_context))

        data.competitor_analysis = refine_competitors(data.competitor_analysis, data.brand.name, content.final_url)

        notes = [COMPETITOR_DISCLAIMER, "Search queries are AI-generated; no search-volume data is reported."]
        inferred = sum(1 for s in data.top_services if s.source == "inferred")
        if inferred:
            notes.append(f"{inferred} of {len(data.top_services)} services are inferred (not explicitly on the website).")
        if len(full_context) < LOW_CONTENT_CHARS:
            notes.append(
                "Limited static content was available (the site is likely JavaScript-rendered); "
                "the analysis is based on the title, meta tags, headings and visible text only."
            )
        if len(data.competitor_analysis.competitors) < 3:
            notes.append("Fewer than 3 reliable competitors could be identified.")

        return LinkAnalysisResponse(
            website=content.final_url,
            brand=data.brand,
            analysis=Analysis(
                competitor_analysis=data.competitor_analysis,
                target_search_queries=data.target_search_queries,
                seo_improvements=data.seo_improvements,
                top_services=data.top_services,
                target_audience=data.target_audience,
            ),
            meta=AnalysisMeta(
                provider=result.provider,
                model=result.model,
                pages_analyzed=content.pages,
                content_chars_sent=context_len,
                cached=False,
                processing_time_ms=int((time.perf_counter() - started) * 1000),
                token_usage=TokenUsage(
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.input_tokens + result.output_tokens,
                    estimated_cost_usd=result.estimated_cost_usd,
                ),
                notes=notes,
            ),
        )
