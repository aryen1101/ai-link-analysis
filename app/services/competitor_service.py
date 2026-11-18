"""Post-processing for competitor data returned by the LLM.

Competitors are produced from the model's general knowledge (no live search API
is used), so every competitor is explicitly labelled ``ai_suggestion``. This
module also removes the analysed brand from its own competitor list, removes
duplicates and normalises competitor URLs.
"""

from urllib.parse import urlsplit

from app.schemas import CompetitorAnalysis

COMPETITOR_DISCLAIMER = (
    "Competitors are AI-generated suggestions based on the website content and the model's "
    "general market knowledge. They are not verified by live market research."
)


def _host(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def refine_competitors(analysis: CompetitorAnalysis, brand_name: str, brand_url: str) -> CompetitorAnalysis:
    brand_host = _host(brand_url)
    brand_key = brand_name.strip().lower()
    seen: set[str] = set()
    kept = []
    for comp in analysis.competitors:
        name_key = comp.name.strip().lower()
        host = _host(comp.website)
        if not name_key or name_key == brand_key or (host and host == brand_host):
            continue
        if name_key in seen:
            continue
        seen.add(name_key)
        comp.source = "ai_suggestion"
        if comp.website and "://" not in comp.website:
            comp.website = "https://" + comp.website
        kept.append(comp)
    analysis.competitors = kept
    return analysis
