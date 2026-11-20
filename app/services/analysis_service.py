"""Orchestrates: validate -> (cache) -> extract -> build context -> LLM -> post-process."""

import asyncio
import logging
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import AppError, InsufficientContentError, InsufficientDataError, WebsiteFetchError
from app.schemas import (
    Analysis,
    AnalysisMeta,
    LinkAnalysisResponse,
    LLMAnalysis,
    TokenUsage,
    WebSource,
)
from app.services.competitor_service import COMPETITOR_DISCLAIMER, refine_competitors
from app.services.llm_service import LLMService
from app.services.platforms import PlatformInfo, detect_platform
from app.services.research_service import (
    ResearchResult,
    ResearchService,
    ResearchSource,
    handle_domains,
    website_matches_handle,
)
from app.services.website_extractor import (
    WebsiteContent,
    WebsiteExtractor,
    build_llm_context,
    evidence_chars,
    links_to_account,
)
from app.utils.text import TEXT_FIXES
from app.utils.validators import ensure_public_url

logger = logging.getLogger(__name__)


def _insufficient_message(
    url: str, platform: PlatformInfo | None, fetch_error: AppError | None, research: ResearchResult | None
) -> str:
    if platform:
        who = f"@{platform.handle}" if platform.handle else "this account"
        first = (f"{platform.name} does not show this {platform.profile_kind}'s content to anonymous visitors"
                 if not fetch_error else f"{platform.name} blocked access to this page ({fetch_error.message})")
    else:
        who = "this brand"
        first = (fetch_error.message.rstrip(".") if fetch_error
                 else "The page contains too little readable content (likely JavaScript-only)")
    second = ("web research is not available" if research is None
              else f"public web research found no reliable information about {who} (confidence: {research.confidence})")
    return (f"{first}, and {second}. No analysis was generated to avoid inventing facts. "
            f"Submit the brand's own website URL for a full analysis.")


def _insufficient_details(
    platform: PlatformInfo | None, content: WebsiteContent | None, quality: str,
    fetch_error: AppError | None, research: ResearchResult | None,
) -> dict:
    details: dict = {"content_quality": quality}
    if platform:
        details.update(platform=platform.name, handle=platform.handle, source_type="social_profile")
    if content:
        details["page_title"] = content.homepage.title
    if fetch_error:
        details["fetch_error"] = fetch_error.message
    if research is not None:
        details["web_research"] = {
            "confidence": research.confidence,
            "provider": research.provider,
            "website": research.website,
            "unconfirmed_candidates": research.candidates,
            "summary": research.text[:800],
            "queries": research.queries,
            "sources": [{"title": s.title, "url": s.url} for s in research.sources[:8]],
        }
    return details

SYSTEM_PROMPT = """You are a senior digital-marketing strategist and SEO analyst.
You receive evidence about ONE brand - content extracted from its website or social-media profile, and sometimes <web_research> gathered with Google Search - and must produce a structured business analysis.

Rules:
- Ground everything in the provided evidence. Quote real product/service names, wording and audience signals from it. Never produce generic filler that could apply to any company.
- NEVER infer what a business does from its name, handle or domain alone (e.g. a brand called "Crest" is not a toothpaste company unless the evidence says so). Every claim about the brand must be supported by the page content or the web research.
- <web_research> contains facts from search results with sources. Use it as evidence, but ignore anything about a different organisation that merely shares a similar name.
- Social-media profiles: the brand is the ACCOUNT OWNER, never the platform itself (do not analyse Instagram, X, YouTube ...). Interpret SEO as profile and search optimisation: current_title/current_meta_description = the profile's actual name and bio if available; recommended_title = an optimised profile name/headline; meta_description = an optimised bio; issues_found = concrete profile issues (bio clarity, keywords, link in bio, consistency with the website). Supporting content = posts, highlights, website pages.
- Set analysis_confidence: "high" when the evidence clearly describes the business, "medium" when partly, "low" when thin; explain in confidence_reason which evidence was used.
- Brand name: use the name shown in the evidence (title, og:site_name, structured data, H1, profile name).
- Competitors: list 3-5 real, currently operating companies that compete in the same category and market (match geography and customer size where evident). They come from your general knowledge, so always set source="ai_suggestion". Only include companies you are confident exist; give their real homepage or "" if unsure. Never list the analysed brand itself.
- comparison_table: rows for Main Service, Target Market, Key Strength, Weakness/Gap and Opportunity; one value per competitor in the same order as the competitors list.
- target_search_queries: exactly 5 realistic queries a potential customer would type. Collectively cover Google, Facebook, Instagram, LinkedIn and X; each query lists every platform it suits. Do NOT state or estimate search volumes or rankings.
- SEO: base issues_found on the observed SEO signals (title length, missing/weak meta description, H1 count, alt text, canonical, viewport). recommended_title 50-60 characters; meta_description 140-160 characters. Provide 4-6 secondary, 4-6 long-tail and 4-6 business keywords, and 5-8 supporting content ideas.
- top_services: exactly 5. Mark source="website_content" only when the service is explicitly described in the content; otherwise source="inferred".
- target_audience.recommended_marketing_segments: exactly 5 segments.
- If something is not stated on the website, say so (e.g. geographic_focus "Not specified on website") instead of inventing facts.
- Write concise, specific, professional English regardless of the website language. Keep each list to 3-6 items and each text field to one or two short sentences."""


_TEXT_FIXES = TEXT_FIXES  # kept for callers / tests that import it from here


def normalize_text(data: LLMAnalysis) -> LLMAnalysis:
    return LLMAnalysis.model_validate_json(data.model_dump_json().translate(TEXT_FIXES))


def _same_site(a: str, b: str) -> bool:
    def host(u: str) -> str:
        return (urlsplit(u).hostname or "").lower().removeprefix("www.")
    return host(a) == host(b)


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
    def __init__(
        self,
        settings: Settings,
        extractor: WebsiteExtractor | None = None,
        llm: LLMService | None = None,
        research: ResearchService | None = None,
    ):
        self.settings = settings
        self.extractor = extractor or WebsiteExtractor(settings)
        self.llm = llm or LLMService(settings)
        self.research = research or ResearchService(settings)
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

    async def _read_site(self, url: str, with_subpages: bool = True) -> WebsiteContent | None:
        """Fetch a related website (official / candidate). Failures are logged, never fatal."""
        try:
            return await self.extractor.extract(url, with_subpages=with_subpages)
        except AppError as exc:
            logger.info("Related website %s could not be read: %s", url, exc.message)
            return None

    async def _verify_account_website(
        self, platform: PlatformInfo, research: ResearchResult | None
    ) -> tuple[WebsiteContent, str] | None:
        """Find the brand website that owns a social account.

        Candidates: websites named by research (confirmed or not) and the obvious domains for the
        handle (crestspot -> crestspot.com / .cloud / ...). Their homepages are fetched in parallel
        (SSRF-checked) and a candidate is accepted only if it links to this exact account - so an
        unrelated same-name brand or a parked domain can never be accepted.
        """
        candidates = [u for u in ([research.website] if research else []) + (research.candidates if research else []) if u]
        candidates += handle_domains(platform.handle)
        candidates = list(dict.fromkeys(candidates))[:12]
        sites = await asyncio.gather(*(self._read_site(u, with_subpages=False) for u in candidates))
        for candidate, site in zip(candidates, sites):
            link = links_to_account(site, platform)
            if site is not None and link:
                logger.info("Account %s verified: %s links to %s", platform.label, site.final_url, link)
                return site, link
        logger.info("No candidate website links to %s (checked %d)", platform.label, len(candidates))
        return None

    async def _run(self, url: str) -> LinkAnalysisResponse:
        started = time.perf_counter()
        self.llm.ensure_configured()  # fail fast before fetching the website
        platform = detect_platform(url)

        # 1. Fetch the page. Blocked / empty pages are not fatal any more: web research may still help.
        content: WebsiteContent | None = None
        fetch_error: AppError | None = None
        try:
            content = await self.extractor.extract(url)
        except (WebsiteFetchError, InsufficientContentError) as exc:
            fetch_error = exc
            logger.info("Page fetch for %s gave no usable content: %s", url, exc.message)
        if content is not None and platform and platform.handle:
            # Platforms often redirect anonymous visitors (region pages, login, "about" pages).
            # Such a page describes the platform, not the account, so it is not evidence.
            landed = detect_platform(content.final_url)
            if not landed or (landed.handle or "").lower() != platform.handle.lower():
                fetch_error = WebsiteFetchError(
                    f"{platform.name} redirected to {content.final_url}, which does not show this account")
                logger.info("Discarding %s: %s", url, fetch_error.message)
                content = None

        # 2. How much real, page-specific evidence did we get?
        evidence = evidence_chars(content)
        if evidence < self.settings.min_evidence_chars:
            quality = "insufficient"
        elif evidence < self.settings.good_evidence_chars:
            quality = "limited"
        else:
            quality = "good"
        logger.info("Evidence for %s: %d chars (%s)%s", url, evidence, quality,
                    f", platform={platform.label}" if platform else "")

        # 3. Thin or blocked page -> public web research (search tools with cited sources).
        research: ResearchResult | None = None
        if quality != "good":
            title = content.homepage.title if content else ""
            desc = content.homepage.meta_description if content else ""
            research = await self.research.lookup(url, platform, title, desc)

        # 3a. Research found a same-name brand but could not confirm it owns this account?
        #     Check it ourselves: a brand website that links to this exact account is hard evidence.
        official: WebsiteContent | None = None
        if platform and platform.handle and research and research.reliable and research.website:
            # A model's self-rated confidence is not proof. For social accounts the website named by
            # research must belong to the account: it links back, or its domain matches the handle.
            if not website_matches_handle(research.website, platform.handle):
                site = await self._read_site(research.website, with_subpages=False)
                link = links_to_account(site, platform)
                if link:
                    research.verified_by = link
                else:
                    logger.info("Research named %s for %s but it neither links to the account nor matches the "
                                "handle - treating the research as a different brand", research.website, platform.label)
                    research.confidence = "low"
                    research.candidates = [research.website, *research.candidates]
                    research.website = None

        if quality == "insufficient" and platform and platform.handle and not (research and research.reliable):
            verified = await self._verify_account_website(platform, research)
            if verified is not None:
                site, link = verified
                if research is None:
                    research = ResearchResult(confidence="medium", text="", provider="link_verification")
                if research.confidence in ("low", "none"):
                    # The search findings did not identify this account; do not pass them on as facts.
                    research.text = ("Search results did not clearly identify this account. The brand was identified "
                                     "because its official website links to the account.")
                    research.sources = []
                    research.confidence = "medium"
                research.verified_by = link
                research.website = site.final_url
                research.sources.append(ResearchSource(
                    title=f"{site.homepage.title or site.final_url} (links to {link})", url=site.final_url))
                official = await self._read_site(site.final_url) or site  # full read incl. key sub-pages
        use_research = bool(research and research.reliable)

        # 3b. If research names the brand's official website, read it too (same SSRF-safe extractor).
        if (official is None and use_research and research.website and self.settings.follow_official_website
                and not _same_site(research.website, url)):
            official = await self._read_site(research.website)
            if official is not None and platform and not research.verified_by:
                research.verified_by = links_to_account(official, platform)
        if official is not None and evidence_chars(official) < self.settings.min_evidence_chars:
            official = None
        if official is not None:
            logger.info("Using official website %s (%d evidence chars)", official.final_url, evidence_chars(official))

        # 4. Refuse to invent an analysis when there is no evidence at all.
        if quality == "insufficient" and not use_research:
            if fetch_error and not platform and research is None:
                raise fetch_error  # plain website that could not be fetched and no research available
            raise InsufficientDataError(
                _insufficient_message(url, platform, fetch_error, research), details=_insufficient_details(
                    platform, content, quality, fetch_error, research)
            )

        sent_chars: dict[str, int] = {}

        def build_user(max_chars: int) -> str:
            # Each provider gets the evidence trimmed to its own budget.
            sections = [f"Source: {platform.label if platform else 'website'} - {url}"]
            research_block = ""
            if use_research:
                src = "\n".join(f"- {s.title}" for s in research.sources[:8])
                verified = (f"Verified: the website {research.website} links to this exact account ({research.verified_by}), "
                            f"so the account belongs to that brand.\n" if research.verified_by else "")
                research_block = (f"<web_research confidence=\"{research.confidence}\">\n" + verified
                                  + (f"Official website: {research.website}\n" if research.website else "")
                                  + f"{research.text}\nSources:\n{src}\n</web_research>")
            budget = max(max_chars - len(research_block) - 300, 1000)
            page_has_text = bool(content and evidence)
            if official is not None:
                # Split the budget: the official website is usually the richest evidence.
                share = budget // 2 if page_has_text else budget
                sections.append(f"<official_website_content url=\"{official.final_url}\">\n"
                                f"{build_llm_context(official, share)}\n</official_website_content>")
                budget -= share
            if page_has_text:
                sections.append(f"<page_content>\n{build_llm_context(content, budget)}\n</page_content>")
            elif content:
                sections.append(f"(The submitted page shows no public content - page title: '{content.homepage.title}'.)")
            elif fetch_error:
                sections.append(f"(The submitted page could not be read: {fetch_error.message})")
            if research_block:
                sections.append(research_block)
            text = "\n\n".join(sections)
            sent_chars["n"] = len(text)
            return "Analyse this brand and return the structured analysis.\n\n" + text

        result = await self.llm.structured(SYSTEM_PROMPT, build_user, LLMAnalysis)
        data = normalize_text(result.data)  # type: ignore[arg-type]
        context_len = sent_chars.get("n", 0)
        final_url = content.final_url if content else url

        data.competitor_analysis = refine_competitors(data.competitor_analysis, data.brand.name, final_url)

        data_sources = (
            (["website_content"] if content and evidence else [])
            + (["official_website"] if official is not None else [])
            + (["web_search"] if use_research else [])
        )
        notes = [COMPETITOR_DISCLAIMER, "Search queries are AI-generated; no search-volume data is reported."]
        if platform:
            article = "an" if platform.label[:1].lower() in "aeiou" else "a"
            notes.append(f"Source is {article} {platform.label}; the analysis covers the account owner, not {platform.name}.")
        if fetch_error:
            notes.append(f"The page could not be read directly ({fetch_error.message}).")
        elif evidence == 0:
            notes.append("The page showed no public content"
                         f"{' (login wall for anonymous visitors)' if platform else ''}.")
        elif quality != "good":
            notes.append(f"The page showed only limited public content ({evidence} characters of real text"
                         f"{', likely a login wall' if platform else ', likely JavaScript-rendered'}).")
        if use_research:
            notes.append(f"Supplemented with public web research ({len(research.sources)} sources, "
                         f"confidence: {research.confidence}); see meta.web_research_sources.")
        if official is not None:
            notes.append(f"The brand's official website ({official.final_url}), identified by web research, was also analysed.")
        if research is not None and research.verified_by:
            notes.append(f"Account ownership verified: {research.website} links to {research.verified_by}.")
        if research is not None and not use_research and quality != "good":
            notes.append(f"Web research found no reliable additional information (confidence: {research.confidence}).")
        inferred = sum(1 for s in data.top_services if s.source == "inferred")
        if inferred:
            notes.append(f"{inferred} of {len(data.top_services)} services are inferred (not explicitly stated in the evidence).")
        if len(data.competitor_analysis.competitors) < 3:
            notes.append("Fewer than 3 reliable competitors could be identified.")

        return LinkAnalysisResponse(
            website=final_url,
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
                source_type="social_profile" if platform else "website",
                platform=platform.name if platform else None,
                handle=platform.handle if platform else None,
                content_quality=quality,
                account_verified_by=research.verified_by if research is not None else None,
                data_sources=data_sources,
                analysis_confidence=data.analysis_confidence,
                confidence_reason=data.confidence_reason,
                web_research_sources=[WebSource(title=s.title, url=s.url) for s in research.sources] if use_research else [],
                pages_analyzed=(content.pages if content else []) + (official.pages if official else []),
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
