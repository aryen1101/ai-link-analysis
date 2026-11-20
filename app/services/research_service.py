"""Public web research for brands whose page gives too little evidence.

Used only when the submitted page itself is thin: social profiles behind a login
wall, sites that block automated requests, empty JavaScript shells.

Three documented search tools are supported and tried in order (``RESEARCH_PROVIDERS``):

* ``openai`` - OpenAI Responses API with the built-in ``web_search`` tool
* ``groq``   - gpt-oss on Groq with the built-in ``browser_search`` tool
* ``gemini`` - Gemini with Google Search grounding

The model must report only facts about *this* brand, rate its confidence and name
the official website if one is found. Answers without any search sources are never
treated as reliable, so unrelated matches or model guesses are not passed on as facts.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.config import Settings
from app.services.platforms import PLATFORM_DOMAINS, PlatformInfo
from app.utils.text import clean_text

logger = logging.getLogger(__name__)


@dataclass
class ResearchSource:
    title: str
    url: str


@dataclass
class ResearchResult:
    confidence: str                 # high | medium | low | none
    text: str                       # findings (summary + facts), header lines removed
    website: str | None = None      # official website named by the research, if any
    candidates: list[str] = field(default_factory=list)  # same-name brand websites not yet confirmed
    verified_by: str | None = None  # link on the brand's website that proves the account match
    sources: list[ResearchSource] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    provider: str = ""

    @property
    def reliable(self) -> bool:
        return self.confidence in ("high", "medium") and bool(self.sources)


RESEARCH_PROMPT = """Search the web to find factual public information about the brand, organisation or person behind this {subject}:
{url}
{hints}
Find out what they are, what they offer (products/services/content), their market or location, their official website, and who their audience is.

How to search:
- Search for the exact URL, and also for the handle / name as a brand name on its own (e.g. "{handle}" and "{handle}" + website).
- A website, brand directory or company page that links to this exact account (same handle on the same platform) is a confirmed match; use it.
- If a candidate brand with the same name appears (a company page, product listing or domain such as {handle}.com / .cloud / .io / .in), open its website and check whether it links to this account or uses the same handle. If it does, that is a confirmed match.
- Check the brand's own website if you find one.

Rules:
- Only include facts from search results that clearly refer to THIS exact account/brand. Ignore unrelated organisations, people or products that merely share a similar name.
- Do not guess from the name, handle or domain.
- Answer in exactly this format:
CONFIDENCE: high | medium | low | none
WEBSITE: <official website URL of this brand, or none>
CANDIDATES: <up to 3 websites of same-name brands you found but could NOT confirm as this account's owner, comma-separated, or none>
SUMMARY: <2-3 sentences>
FACTS:
- <fact>
(high = several results clearly describe this exact brand; medium = at least one clear, specific match; low = only ambiguous or possibly unrelated matches; none = nothing relevant found.)
Keep it under 250 words."""

# Sites that are never a brand's "official website" (aggregators, social networks, directories).
_NOT_OFFICIAL = set(PLATFORM_DOMAINS) | {
    "brandfetch.com", "boldfruit.com", "crunchbase.com", "wikipedia.org", "socialblade.com", "linktr.ee",
    "google.com", "bing.com", "yelp.com", "glassdoor.com", "indeed.com", "zoominfo.com", "medium.com",
    "github.com", "play.google.com", "apps.apple.com", "amazon.com", "t.co", "bit.ly",
}


class ResearchService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._gemini = None
        self._groq = None
        self._openai = None

    @property
    def providers(self) -> list[str]:
        order = [p.strip().lower() for p in self.settings.research_providers.split(",") if p.strip()]
        keys = {
            "openai": self.settings.openai_api_key,
            "groq": self.settings.groq_api_key,
            "gemini": self.settings.gemini_api_key,
        }
        return [p for p in order if keys.get(p)]

    @property
    def enabled(self) -> bool:
        return bool(self.settings.web_research_enabled and self.providers)

    async def lookup(
        self, url: str, platform: PlatformInfo | None, page_title: str = "", page_description: str = ""
    ) -> ResearchResult | None:
        """Return findings from the first provider that answers, or ``None`` if research is unavailable."""
        if not self.enabled:
            return None
        prompt = build_prompt(url, platform, page_title, page_description)
        best: ResearchResult | None = None
        for provider in self.providers:
            try:
                call = {"openai": self._openai_search, "groq": self._groq_search}.get(provider, self._gemini_search)
                result = await asyncio.wait_for(call(prompt), timeout=self.settings.research_timeout_seconds)
            except Exception as exc:  # best-effort: try the next provider
                logger.warning("Web research via %s failed for %s: %s", provider, url, f"{type(exc).__name__}: {str(exc)[:200]}")
                continue
            result.provider = provider
            if not result.sources and result.confidence in ("high", "medium"):
                result.confidence = "low"  # ungrounded claims are not treated as reliable
            logger.info("Web research via %s for %s: confidence=%s, website=%s, %d sources",
                        provider, url, result.confidence, result.website, len(result.sources))
            if result.reliable:
                return result
            # Keep the most useful unconfirmed answer: one naming a website can still be verified by the caller.
            if best is None or (not (best.website or best.candidates) and (result.website or result.candidates)):
                best = result
        return best

    # ------------------------------------------------------------------ OpenAI
    async def _openai_search(self, prompt: str) -> ResearchResult:
        if self._openai is None:
            from openai import AsyncOpenAI

            self._openai = AsyncOpenAI(api_key=self.settings.openai_api_key, max_retries=0,
                                       timeout=self.settings.research_timeout_seconds)
        kwargs: dict = {}
        if self.settings.research_openai_effort:
            kwargs["reasoning"] = {"effort": self.settings.research_openai_effort}
        resp = await self._openai.responses.create(
            model=self.settings.research_openai_model,
            input=prompt,
            tools=[{"type": "web_search"}],
            max_output_tokens=2500,
            **kwargs,
        )
        result = parse_research(resp.output_text or "")
        seen: set[str] = set()
        for item in resp.output or []:
            if getattr(item, "type", "") != "message":
                continue
            for part in getattr(item, "content", None) or []:
                for ann in getattr(part, "annotations", None) or []:
                    link = _strip_tracking(getattr(ann, "url", "") or "")
                    if getattr(ann, "type", "") == "url_citation" and link and link not in seen:
                        seen.add(link)
                        result.sources.append(ResearchSource(title=clean_text(getattr(ann, "title", "") or ""), url=link))
        return result

    # ------------------------------------------------------------------ Groq
    async def _groq_search(self, prompt: str) -> ResearchResult:
        if self._groq is None:
            from groq import AsyncGroq

            self._groq = AsyncGroq(api_key=self.settings.groq_api_key, timeout=self.settings.research_timeout_seconds,
                                   max_retries=0)
        resp = await self._groq.chat.completions.create(
            model=self.settings.research_groq_model,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"type": "browser_search"}],
            tool_choice="required",
            reasoning_effort="low",
            max_completion_tokens=1500,
        )
        msg = resp.choices[0].message
        result = parse_research(msg.content or "")
        seen: set[str] = set()
        for tool in getattr(msg, "executed_tools", None) or []:
            search = getattr(tool, "search_results", None)
            for item in (getattr(search, "results", None) or []) if search else []:
                link = getattr(item, "url", "") or ""
                if link and link not in seen and urlsplit(link).scheme in ("http", "https"):
                    seen.add(link)
                    title = clean_text(getattr(item, "title", "") or "")
                    # Groq's page-open tool reports titles like "example.com - viewing lines [0 - 471] of 471".
                    title = re.sub(r"\s*-\s*viewing lines \[.*$", "", title) or urlsplit(link).hostname or link
                    result.sources.append(ResearchSource(title=title, url=link))
        return result

    # ------------------------------------------------------------------ Gemini
    async def _gemini_search(self, prompt: str) -> ResearchResult:
        from google.genai import types

        if self._gemini is None:
            from google import genai

            self._gemini = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(api_version=self.settings.gemini_api_version or None,
                                               timeout=int(self.settings.research_timeout_seconds * 1000)),
            )
        resp = await self._gemini.aio.models.generate_content(
            model=self.settings.research_gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                max_output_tokens=900,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        result = parse_research(resp.text or "")
        cand = resp.candidates[0] if resp.candidates else None
        meta = getattr(cand, "grounding_metadata", None)
        if meta:
            seen: set[str] = set()
            for chunk in meta.grounding_chunks or []:
                web = getattr(chunk, "web", None)
                if web and web.uri and web.uri not in seen:
                    seen.add(web.uri)
                    result.sources.append(ResearchSource(title=web.title or "", url=web.uri))
            result.queries = list(meta.web_search_queries or [])
        return result


def build_prompt(url: str, platform: PlatformInfo | None, page_title: str, page_description: str) -> str:
    hints = []
    if platform and platform.handle:
        hints.append(f"Account handle: @{platform.handle} on {platform.name}.")
    generic = {"", "instagram", "x", "facebook", "linkedin", "tiktok", "youtube", "log in", "login"}
    if page_title and page_title.strip().lower() not in generic:
        hints.append(f"Page title: {page_title}")
    if page_description and not page_description.lower().startswith("http"):
        hints.append(f"Page description: {page_description[:300]}")
    handle = platform.handle if platform and platform.handle else (urlsplit(url).hostname or "").removeprefix("www.")
    return RESEARCH_PROMPT.format(
        subject=platform.label if platform else "website", url=url, hints="\n".join(hints), handle=handle
    )


def parse_research(text: str) -> ResearchResult:
    text = clean_text(text).replace("**", "")
    conf = re.search(r"CONFIDENCE:\s*(high|medium|low|none)", text, re.I)
    site = re.search(r"WEBSITE:\s*(\S+)", text, re.I)
    cand = re.search(r"CANDIDATES?:\s*(.+)", text, re.I)
    body = re.sub(r"^\s*(CONFIDENCE|WEBSITE|CANDIDATES?):.*$\n?", "", text, flags=re.I | re.M).strip()
    # Drop inline citation links like "([example.com](https://...))" - sources are listed separately.
    body = re.sub(r"\s*\(\[[^\]]*\]\([^)]*\)\)", "", body)
    return ResearchResult(
        confidence=conf.group(1).lower() if conf else "low",
        text=body,
        website=official_website(site.group(1)) if site else None,
        candidates=_candidate_list(cand.group(1)) if cand else [],
    )


def _candidate_list(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        site = official_website(part)
        if site and site not in out:
            out.append(site)
    return out[:3]


def handle_domains(handle: str | None) -> list[str]:
    """Obvious brand domains for a handle (crestspot -> crestspot.com, .cloud ...). Only ever used
    as candidates that must prove themselves by linking back to the account."""
    name = re.sub(r"[^a-z0-9-]", "", (handle or "").lower().replace("_", "").replace(".", ""))
    if len(name) < 3:
        return []
    return [f"https://{name}.{tld}/" for tld in ("com", "cloud", "io", "in", "co", "ai", "net", "app")]


def website_matches_handle(website: str | None, handle: str | None) -> bool:
    """True when the website's domain and the account handle name the same brand
    (nike.in ~ @nike, dxora.com ~ @dxora-technologies, but not crestsport.com ~ @crestspot)."""
    if not website or not handle:
        return False

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    h = norm(handle)
    labels = [norm(x) for x in (urlsplit(website).hostname or "").split(".")[:-1] if x not in ("www", "m")]
    return len(h) >= 3 and any(len(lab) >= 3 and (lab in h or h in lab) for lab in labels)


def _strip_tracking(url: str) -> str:
    return re.sub(r"[?&]utm_source=openai$", "", url)


def official_website(raw: str) -> str | None:
    """Normalise a website named by the research; reject 'none' and non-official domains."""
    raw = raw.strip().strip("<>()[].,;*`'\"")
    if not raw or raw.lower() in ("none", "n/a", "unknown", "-"):
        return None
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    if any(host == d or host.endswith("." + d) for d in _NOT_OFFICIAL):
        return None
    return f"https://{host}/"
