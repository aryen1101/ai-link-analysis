"""Tests run fully offline: DNS, website fetching and the LLM are mocked."""

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import BlockedURLError, InvalidURLError, LLMError, WebsiteFetchError
from app.schemas import LLMAnalysis
from app.services import analysis_service as analysis_module
from app.services.analysis_service import AnalysisService, TTLCache
from app.services.competitor_service import refine_competitors
from app.services.llm_service import LLMResult
from app.services.website_extractor import WebsiteContent, WebsiteExtractor, build_llm_context
from app.utils import validators
from app.utils.validators import ensure_public_url, normalize_url

SAMPLE_HTML = """
<html lang="en"><head>
<title>Acme Cloud Accounting | Bookkeeping for Small Business</title>
<meta name="description" content="Acme helps small businesses automate bookkeeping, invoicing and payroll.">
<meta property="og:site_name" content="Acme">
<script type="application/ld+json">{"@type":"Organization","name":"Acme Inc","areaServed":"United Kingdom"}</script>
</head><body>
<header><nav><a href="/about">About us</a><a href="/services">Services</a><a href="/login">Login</a></nav></header>
<main>
<h1>Accounting software for small businesses</h1>
<h2>Automated invoicing</h2>
<p>Send professional invoices in seconds and get paid twice as fast with automatic reminders.</p>
<h2>Payroll made simple</h2>
<p>Run payroll for up to 50 employees with automatic tax filing and payslips.</p>
<img src="a.png"><img src="b.png" alt="dashboard">
</main>
<footer><p>Copyright Acme Inc. All rights reserved worldwide forever.</p></footer>
<script>var tracking = "should not appear in output";</script>
</body></html>
"""


def fake_analysis() -> LLMAnalysis:
    return LLMAnalysis.model_validate({
        "brand": {"name": "Acme", "category": "Accounting Software", "summary": "Cloud bookkeeping for SMBs."},
        "competitor_analysis": {
            "brand_overview": "Acme offers invoicing and payroll.",
            "main_category": "Accounting Software",
            "main_products_services": ["Invoicing", "Payroll"],
            "competitors": [
                {"name": "Xero", "website": "xero.com", "target_market": "SMB", "main_service": "Accounting",
                 "strengths": ["Brand"], "weaknesses_or_gaps": ["Price"], "source": "ai_suggestion"},
                {"name": "Acme", "website": "https://acme.example", "target_market": "SMB", "main_service": "x",
                 "strengths": [], "weaknesses_or_gaps": [], "source": "ai_suggestion"},
                {"name": "xero", "website": "", "target_market": "SMB", "main_service": "dup",
                 "strengths": [], "weaknesses_or_gaps": [], "source": "ai_suggestion"},
                {"name": "QuickBooks", "website": "https://quickbooks.intuit.com", "target_market": "SMB",
                 "main_service": "Accounting", "strengths": ["Features"], "weaknesses_or_gaps": ["Complex"],
                 "source": "ai_suggestion"},
            ],
            "comparison_table": [{"aspect": "Main Service", "your_brand": "Invoicing", "competitors": ["A", "B"]}],
            "competitive_advantages": ["Simple"],
            "market_positioning_opportunities": ["UK focus"],
        },
        "target_search_queries": [
            {"query": f"q{i}", "platforms": ["Google"], "intent": "Service Search",
             "business_relevance": "high", "target_audience": "SMB"} for i in range(5)
        ],
        "seo_improvements": {
            "current_title": "t", "current_meta_description": "d",
            "keywords": {"primary_keyword": "accounting software", "secondary_keywords": ["invoicing"],
                         "long_tail_keywords": ["payroll for small business"], "business_keywords": ["bookkeeping"]},
            "recommended_title": "Accounting Software for Small Business | Acme",
            "title_structure": "Primary | Brand", "meta_description": "m", "issues_found": ["1 image missing alt"],
            "supporting_content": [{"type": "faq", "title": "FAQ", "reason": "r"}],
        },
        "top_services": [
            {"service_name": f"s{i}", "description": "d", "target_industry": "i", "target_customer": "c",
             "main_benefit": "b", "source": "website_content" if i < 3 else "inferred"} for i in range(5)
        ],
        "target_audience": {
            "primary_audience": ["Small businesses"], "secondary_audience": ["Accountants"],
            "industries": ["Retail"], "business_types": ["SMB"], "customer_profiles": ["Owner"],
            "geographic_focus": "United Kingdom", "customer_needs_and_challenges": ["Late payments"],
            "customer_interests": ["Automation"],
            "recommended_marketing_segments": [{"audience": "A", "business_type": "SMB", "requirement": "Invoicing"}],
        },
    })


@pytest.fixture
def public_dns(monkeypatch):
    """Make every hostname resolve to a public IP (93.184.216.34) without network access."""

    async def fake_getaddrinfo(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)


# ----------------------------------------------------------------- validators
@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com/"),
    ("HTTP://Example.COM/path?q=1#frag", "http://example.com/path?q=1"),
    ("https://bücher.de", "https://xn--bcher-kva.de/"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://example.com", "https://user:pw@example.com", "http://intranet",
                                 "javascript:alert(1)", "https://exa mple.com"])
def test_invalid_urls(raw):
    with pytest.raises((InvalidURLError, BlockedURLError)):
        normalize_url(raw)


@pytest.mark.parametrize("raw", [
    "http://127.0.0.1", "http://10.0.0.5", "http://192.168.1.1", "http://169.254.169.254/latest/meta-data",
    "http://[::1]", "http://[::ffff:127.0.0.1]", "http://0.0.0.0", "http://100.64.0.1",
    "http://localhost", "http://api.internal", "http://example.com:8080",
])
def test_ssrf_blocked(raw):
    with pytest.raises(BlockedURLError):
        asyncio.run(ensure_public_url(raw))


def test_dns_resolving_to_private_ip_is_blocked(monkeypatch):
    async def fake(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake)
    with pytest.raises(BlockedURLError):
        asyncio.run(ensure_public_url("https://evil-rebind.example"))


def test_public_url_allowed(public_dns):
    assert asyncio.run(ensure_public_url("example.com")) == "https://example.com/"


# ----------------------------------------------------------------- extraction
def test_parse_extracts_relevant_content():
    page = WebsiteExtractor.parse("https://acme.example/", SAMPLE_HTML)
    assert page.title.startswith("Acme Cloud Accounting")
    assert "automate bookkeeping" in page.meta_description
    assert page.og["og:site_name"] == "Acme"
    assert any("Organization" in s and "Acme Inc" in s for s in page.structured_data)
    assert page.headings["h1"] == ["Accounting software for small businesses"]
    assert page.images_missing_alt == 1
    text = " ".join(page.text_blocks)
    assert "payroll for up to 50 employees" in text
    assert "tracking" not in text and "Copyright" not in text
    links = [u for u, _ in page.internal_links]
    assert "https://acme.example/services" in links
    assert not any("login" in u for u in links)


def test_spa_noscript_fallback_is_used_and_duplicate_subpages_skipped():
    html = ('<html><body><div id="root"></div><noscript><h1>Acme Platform</h1><ul>'
            '<li>Mobile App Development</li><li>Cloud DevOps</li></ul>'
            '<p>We build custom software for enterprises across the globe.</p></noscript></body></html>')
    page = WebsiteExtractor.parse("https://spa.example/", html)
    assert "Mobile App Development" in page.text_blocks
    sub = WebsiteExtractor.parse("https://spa.example/services", html)
    ctx = build_llm_context(WebsiteContent("u", "https://spa.example/", page, [sub]), 5000)
    assert ctx.count("Mobile App Development") == 1 and "SUBPAGE" not in ctx


def test_subpage_selection_and_context_budget():
    settings = Settings(openai_api_key="x", max_extra_pages=2)
    ex = WebsiteExtractor(settings)
    page = ex.parse("https://acme.example/", SAMPLE_HTML)
    assert set(ex._pick_subpages(page)) == {"https://acme.example/about", "https://acme.example/services"}
    content = WebsiteContent("https://acme.example/", "https://acme.example/", page, [])
    ctx = build_llm_context(content, 600)
    assert len(ctx) <= 600
    assert "Title: Acme Cloud Accounting" in ctx and "images missing alt=1" in ctx


# ----------------------------------------------------------------- services
def test_refine_competitors_removes_self_and_duplicates():
    comp = fake_analysis().competitor_analysis
    out = refine_competitors(comp, "Acme", "https://acme.example/")
    assert [c.name for c in out.competitors] == ["Xero", "QuickBooks"]
    assert out.competitors[0].website == "https://xero.com"


def test_ttl_cache_expiry(monkeypatch):
    cache = TTLCache(ttl=10, max_entries=1)
    now = [1000.0]
    monkeypatch.setattr(analysis_module.time, "time", lambda: now[0])
    cache.set("a", "A")
    cache.set("b", "B")
    assert cache.get("a") is None and cache.get("b") == "B"
    now[0] += 11
    assert cache.get("b") is None


# ----------------------------------------------------------------- API
class FakeExtractor:
    calls = 0

    async def extract(self, url):
        FakeExtractor.calls += 1
        page = WebsiteExtractor.parse(url, SAMPLE_HTML)
        return WebsiteContent(url, url, page, [])


class FakeLLM:
    def __init__(self, fail=False):
        self.fail = fail

    def ensure_configured(self):
        pass

    async def structured(self, system, user, schema):
        if self.fail:
            raise LLMError("LLM provider timed out.")
        assert "<website_content>" in user and "payroll" in user
        return LLMResult(fake_analysis(), "gpt-6-luna", 4000, 0, 2500, 1200)


def make_client(llm=None, extractor=None):
    from app.main import app

    client = TestClient(app)
    client.__enter__()
    app.state.analysis = AnalysisService(
        Settings(openai_api_key="x"), extractor=extractor or FakeExtractor(), llm=llm or FakeLLM()
    )
    return client


def test_link_analysis_success_and_cache(public_dns):
    FakeExtractor.calls = 0
    client = make_client()
    r = client.post("/link-analysis", json={"url": "https://acme.example"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["brand"]["name"] == "Acme"
    assert set(body["analysis"]) == {"competitor_analysis", "target_search_queries", "seo_improvements",
                                     "top_services", "target_audience"}
    assert len(body["analysis"]["target_search_queries"]) == 5
    assert all(c["source"] == "ai_suggestion" for c in body["analysis"]["competitor_analysis"]["competitors"])
    assert body["meta"]["token_usage"]["estimated_cost_usd"] == pytest.approx(0.00165)
    assert body["meta"]["cached"] is False

    r2 = client.post("/link-analysis", json={"url": "acme.example/"})
    assert r2.json()["meta"]["cached"] is True
    assert FakeExtractor.calls == 1


@pytest.mark.parametrize("url,status,code", [
    ("http://127.0.0.1/admin", 400, "blocked_url"),
    ("ftp://example.com", 400, "invalid_url"),
    ("http://169.254.169.254/latest/meta-data", 400, "blocked_url"),
])
def test_link_analysis_rejects_bad_urls(url, status, code):
    r = make_client().post("/link-analysis", json={"url": url})
    assert r.status_code == status
    assert r.json() == {"success": False, "website": url, "error": {"code": code, "message": r.json()["error"]["message"]}}


def test_missing_body_returns_structured_error():
    r = make_client().post("/link-analysis", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_unreachable_website(public_dns):
    class Down:
        async def extract(self, url):
            raise WebsiteFetchError("Could not connect to website: ConnectError.")

    r = make_client(extractor=Down()).post("/link-analysis", json={"url": "https://down.example"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "website_unreachable"


def test_llm_failure(public_dns):
    r = make_client(llm=FakeLLM(fail=True)).post("/link-analysis", json={"url": "https://acme.example"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_error"


def test_health():
    r = make_client().get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
