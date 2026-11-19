"""Fetch a website safely and extract clean, LLM-ready content.

Steps:
1. Fetch the homepage (redirects followed manually so each hop is SSRF-checked,
   response size capped, only HTML accepted).
2. Parse with BeautifulSoup: title, meta tags, Open Graph, JSON-LD organisation
   data, headings, navigation labels, main body text.
3. Discover up to N high-value internal pages (about / services / products ...)
   and fetch them concurrently for richer context.
4. Strip boilerplate, de-duplicate lines and trim to a character budget so the
   LLM receives only useful tokens.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.errors import AppError, InsufficientContentError, WebsiteFetchError
from app.utils.validators import ensure_public_url

logger = logging.getLogger(__name__)

NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "template", "canvas", "video", "audio"]
SUBPAGE_KEYWORDS = (
    "about", "service", "solution", "product", "what-we-do", "offering",
    "pricing", "industr", "platform", "feature", "company", "who-we-are",
)
SHORT_OK_TAGS = {"li", "h1", "h2", "h3", "h4", "h5", "dt"}
LOW_CONTENT_CHARS = 2500
SKIP_LINK_PATTERNS = re.compile(
    r"(login|signin|sign-in|signup|register|cart|checkout|privacy|terms|cookie|careers|jobs|"
    r"\.pdf$|\.jpg$|\.png$|\.zip$|mailto:|tel:|javascript:)",
    re.I,
)


@dataclass
class PageContent:
    url: str
    title: str = ""
    meta_description: str = ""
    meta_keywords: str = ""
    og: dict[str, str] = field(default_factory=dict)
    lang: str = ""
    headings: dict[str, list[str]] = field(default_factory=dict)
    nav_items: list[str] = field(default_factory=list)
    structured_data: list[str] = field(default_factory=list)
    text_blocks: list[str] = field(default_factory=list)
    internal_links: list[tuple[str, str]] = field(default_factory=list)
    image_count: int = 0
    images_missing_alt: int = 0
    has_canonical: bool = False
    has_viewport: bool = False


@dataclass
class WebsiteContent:
    url: str
    final_url: str
    homepage: PageContent
    subpages: list[PageContent]

    @property
    def pages(self) -> list[str]:
        return [self.homepage.url] + [p.url for p in self.subpages]


class WebsiteExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ fetch
    async def _fetch_html(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        """Return (final_url, html). Every redirect hop is re-validated against SSRF rules."""
        current = await ensure_public_url(url)
        for _ in range(self.settings.max_redirects + 1):
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise WebsiteFetchError("Website returned a redirect without a location.")
                        current = await ensure_public_url(urljoin(current, location))
                        continue

                    if resp.status_code in (401, 403, 429):
                        raise WebsiteFetchError(
                            f"Website blocked automated access (HTTP {resp.status_code})."
                        )
                    if resp.status_code >= 400:
                        raise WebsiteFetchError(f"Website returned HTTP {resp.status_code}.")

                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and "html" not in ctype and "xml" not in ctype:
                        raise WebsiteFetchError(f"URL is not an HTML page (content-type: {ctype}).")

                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_response_bytes:
                            break  # keep what we have; the top of the page is the most useful part
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = resp.charset_encoding or "utf-8"
                    return current, raw.decode(encoding, errors="replace")
            except httpx.TimeoutException as exc:
                raise WebsiteFetchError("Website took too long to respond.") from exc
            except httpx.HTTPError as exc:
                raise WebsiteFetchError(f"Could not connect to website: {exc.__class__.__name__}.") from exc
        raise WebsiteFetchError("Too many redirects.")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.settings.fetch_timeout_seconds, connect=8.0),
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            },
            limits=httpx.Limits(max_connections=8),
        )

    # ------------------------------------------------------------------ parse
    @staticmethod
    def parse(url: str, html: str) -> PageContent:
        soup = BeautifulSoup(html, "lxml")
        page = PageContent(url=url)

        if soup.html and soup.html.get("lang"):
            page.lang = str(soup.html.get("lang"))
        if soup.title and soup.title.string:
            page.title = _clean(soup.title.string)

        for meta in soup.find_all("meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            content = _clean(meta.get("content") or "")
            if not name:
                continue
            if name == "description":
                page.meta_description = content
            elif name == "keywords":
                page.meta_keywords = content
            elif name == "viewport":
                page.has_viewport = True
            elif name.startswith("og:") and name in ("og:title", "og:description", "og:site_name", "og:type"):
                page.og[name] = content
        page.has_canonical = soup.find("link", rel="canonical") is not None

        # JSON-LD (Organization, LocalBusiness, Product, Service ...)
        for script in soup.find_all("script", type="application/ld+json"):
            page.structured_data.extend(_summarize_jsonld(script.string or ""))

        imgs = soup.find_all("img")
        page.image_count = len(imgs)
        page.images_missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

        # Internal links (before removing nav/footer)
        base_host = urlsplit(url).hostname
        seen = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if urlsplit(href).hostname != base_host or href in seen or SKIP_LINK_PATTERNS.search(href):
                continue
            seen.add(href)
            page.internal_links.append((href, _clean(a.get_text(" "))[:80]))

        nav = soup.find_all(["nav", "header"])
        nav_items = []
        for n in nav:
            for a in n.find_all("a"):
                t = _clean(a.get_text(" "))
                if 1 < len(t) < 40 and t not in nav_items:
                    nav_items.append(t)
        page.nav_items = nav_items[:40]

        # JS-rendered sites (React/Vue SPAs) often ship their only static text inside <noscript>.
        for ns in soup.find_all("noscript"):
            if len(_clean(ns.get_text(" "))) > 40:
                ns.unwrap()
        for tag in soup(NOISE_TAGS):
            tag.decompose()

        for level in ("h1", "h2", "h3"):
            items = []
            for h in soup.find_all(level):
                t = _clean(h.get_text(" "))
                if t and t not in items and len(t) < 200:
                    items.append(t)
            page.headings[level] = items[:25]

        # Body text: prefer <main>, drop chrome
        root = soup.find("main") or soup.body or soup
        for tag in root.find_all(["nav", "footer", "header", "aside"]):
            tag.decompose()
        seen_lines: set[str] = set()
        for el in root.find_all(
            ["p", "li", "h1", "h2", "h3", "h4", "h5", "dt", "td", "blockquote", "dd", "span", "div", "a"]
        ):
            if el.name in ("div", "span", "a") and el.find(["p", "li", "div", "h1", "h2", "h3", "h4"]):
                continue  # only leaf-ish containers
            t = _clean(el.get_text(" "))
            # Short items (service names in lists / headings) are valuable; short generic divs are noise.
            min_len = 4 if el.name in SHORT_OK_TAGS else 25
            if len(t) < min_len or t.lower() in seen_lines:
                continue
            seen_lines.add(t.lower())
            page.text_blocks.append(t[:600])
        return page

    # ------------------------------------------------------------------ public
    async def extract(self, url: str) -> WebsiteContent:
        async with self._client() as client:
            final_url, html = await self._fetch_html(client, url)
            homepage = self.parse(final_url, html)

            candidates = self._pick_subpages(homepage)
            results = await asyncio.gather(
                *(self._fetch_html(client, u) for u in candidates), return_exceptions=True
            )
            subpages = []
            for cand, res in zip(candidates, results):
                if isinstance(res, BaseException):
                    logger.info("Skipping sub-page %s: %s", cand, getattr(res, "message", res))
                    continue
                sub_url, sub_html = res
                subpages.append(self.parse(sub_url, sub_html))

        content = WebsiteContent(url=url, final_url=final_url, homepage=homepage, subpages=subpages)
        total_text = sum(len(b) for p in [homepage, *subpages] for b in p.text_blocks)
        if total_text < 150 and not homepage.meta_description and not homepage.title:
            raise InsufficientContentError(
                "Website returned too little readable content to analyze "
                "(it may be JavaScript-rendered or blocking bots)."
            )
        return content

    def _pick_subpages(self, page: PageContent) -> list[str]:
        scored = []
        home_path = urlsplit(page.url).path.rstrip("/")
        for href, text in page.internal_links:
            path = urlsplit(href).path.lower().rstrip("/")
            if not path or path == home_path or path.count("/") > 2:
                continue
            hay = f"{path} {text.lower()}"
            score = sum(1 for k in SUBPAGE_KEYWORDS if k in hay)
            if score:
                scored.append((score, -len(path), href))
        scored.sort(reverse=True)
        picked, seen_paths = [], set()
        for _, _, href in scored:
            p = urlsplit(href).path.rstrip("/")
            if p in seen_paths:
                continue
            seen_paths.add(p)
            picked.append(href)
            if len(picked) >= self.settings.max_extra_pages:
                break
        return picked


# ---------------------------------------------------------------------- helpers
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _summarize_jsonld(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        typ = item.get("@type")
        typ = ", ".join(typ) if isinstance(typ, list) else str(typ or "")
        if not typ or typ in ("WebPage", "BreadcrumbList", "WebSite", "ImageObject", "SiteNavigationElement"):
            continue
        fields = []
        for key in ("name", "legalName", "description", "slogan", "areaServed", "address", "serviceType", "category"):
            val = item.get(key)
            if isinstance(val, dict):
                val = ", ".join(str(v) for k, v in val.items() if not k.startswith("@"))
            if val:
                fields.append(f"{key}: {_clean(str(val))[:200]}")
        if fields:
            out.append(f"[{typ}] " + "; ".join(fields))
    return out[:5]


def build_llm_context(content: WebsiteContent, max_chars: int) -> str:
    """Compact, de-duplicated, budgeted text representation of the website for the LLM."""
    h = content.homepage
    parts = [
        f"URL: {content.final_url}",
        f"Language: {h.lang or 'unknown'}",
        f"Title: {h.title or '(missing)'}",
        f"Meta description: {h.meta_description or '(missing)'}",
    ]
    if h.meta_keywords:
        parts.append(f"Meta keywords: {h.meta_keywords}")
    for k, v in h.og.items():
        parts.append(f"{k}: {v}")
    parts.append(
        f"SEO signals: canonical={'yes' if h.has_canonical else 'no'}, "
        f"viewport={'yes' if h.has_viewport else 'no'}, H1 count={len(h.headings.get('h1', []))}, "
        f"images={h.image_count}, images missing alt={h.images_missing_alt}"
    )
    if h.structured_data:
        parts.append("Structured data:\n" + "\n".join(h.structured_data))
    if h.nav_items:
        parts.append("Navigation: " + " | ".join(h.nav_items))
    for level in ("h1", "h2", "h3"):
        if h.headings.get(level):
            parts.append(f"{level.upper()}: " + " | ".join(h.headings[level]))

    header = "\n".join(parts)
    budget = max_chars - len(header)
    seen: set[str] = set()

    def section(page: PageContent, label: str, share: int) -> str:
        lines, used = [], 0
        for block in page.text_blocks:
            key = block.lower()
            if key in seen:
                continue
            if used + len(block) > share:
                break
            seen.add(key)
            lines.append(f"- {block}")
            used += len(block) + 3
        if not lines:
            return ""
        title = f" ({page.title})" if page.title and label != "HOMEPAGE" else ""
        return f"\n\n## {label}{title}: {page.url}\n" + "\n".join(lines)

    home_share = int(budget * (0.55 if content.subpages else 1.0))
    body = section(h, "HOMEPAGE", home_share)
    if content.subpages:
        per_sub = (budget - len(body)) // len(content.subpages)
        for sp in content.subpages:
            # Skip sub-pages that add nothing new (e.g. SPA routes serving the same static shell).
            sub = section(sp, "SUBPAGE", per_sub)
            if sub:
                body += sub
    return (header + body)[:max_chars]


_URL_ONLY = re.compile(r"^(\s*https?://\S+\s*)+$", re.I)
_BOILERPLATE = re.compile(
    r"(log ?in|sign ?up|create (an )?account|forgot (your )?password|cookie|javascript is (not available|disabled)|"
    r"enable javascript|see instagram photos|terms of (use|service)|privacy policy|all rights reserved|"
    r"posts replies|something went wrong)",
    re.I,
)


def evidence_chars(content: "WebsiteContent | None") -> int:
    """Characters of real, page-specific information (descriptions, structured data, body text).

    Generic platform chrome - login walls, cookie banners, bare URLs, nav labels - is excluded,
    so an Instagram login page scores ~0 even though its HTML is half a megabyte.
    """
    if content is None:
        return 0
    seen: set[str] = set()
    total = 0
    for page in [content.homepage, *content.subpages]:
        candidates = [page.meta_description, page.og.get("og:description", ""), *page.structured_data]
        candidates += [b for b in page.text_blocks if len(b) >= 20]
        for text in candidates:
            key = text.strip().lower()
            if not key or key in seen or _URL_ONLY.match(text) or _BOILERPLATE.search(text):
                continue
            seen.add(key)
            total += len(text)
    return total


__all__ = ["WebsiteExtractor", "WebsiteContent", "PageContent", "build_llm_context", "evidence_chars", "AppError"]
