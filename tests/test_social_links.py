"""Social-media and low-evidence links: no invented analyses, web research fallback."""

import pytest

from app.errors import WebsiteFetchError
from app.services.platforms import detect_platform
from app.services.research_service import ResearchResult, ResearchSource, parse_research
from app.services.website_extractor import WebsiteContent, WebsiteExtractor, evidence_chars
from tests.test_link_analysis import FakeLLM, FakeResearch, make_client, public_dns  # noqa: F401  (public_dns is a fixture)

INSTAGRAM_LOGIN_WALL = """<html lang="en"><head><title>Instagram</title>
<meta name="viewport" content="width=device-width"></head>
<body><div id="root"></div><img src="a.png"><img src="b.png">
<a href="/accounts/login/">Log in</a><a href="/accounts/emailsignup/">Sign up</a>
<script>window.__data = {"big": "bundle"}</script></body></html>"""


class LoginWallExtractor:
    async def extract(self, url, **kw):  # noqa: D102
        return WebsiteContent(url, url, WebsiteExtractor.parse(url, INSTAGRAM_LOGIN_WALL), [])


class BlockedExtractor:
    async def extract(self, url, **kw):
        raise WebsiteFetchError("Website blocked automated access (HTTP 403).")


def reliable_research():
    return ResearchResult(
        confidence="high",
        text="SUMMARY: Acme Studio is a Bengaluru design agency.\nFACTS:\n- Offers branding and UI design.",
        sources=[ResearchSource("acmestudio.in", "https://example.org/r1"), ResearchSource("clutch.co", "https://example.org/r2")],
        queries=["acmestudio instagram"],
    )


# ----------------------------------------------------------------- platform detection
@pytest.mark.parametrize("url,name,handle,kind", [
    ("https://www.instagram.com/crestspot", "Instagram", "crestspot", "profile"),
    ("https://instagram.com/crestspot/", "Instagram", "crestspot", "profile"),
    ("https://www.instagram.com/p/ABC123/", "Instagram", None, "post"),
    ("https://x.com/elonmusk", "X", "elonmusk", "profile"),
    ("https://twitter.com/nasa/status/123", "X", "nasa", "post"),
    ("https://www.youtube.com/@MrBeast", "YouTube", "MrBeast", "profile"),
    ("https://www.youtube.com/channel/UC123", "YouTube", "UC123", "profile"),
    ("https://m.facebook.com/nike", "Facebook", "nike", "profile"),
    ("https://www.linkedin.com/company/dxora-technologies", "LinkedIn", "dxora-technologies", "page"),
    ("https://www.linkedin.com/in/someone", "LinkedIn", "someone", "profile"),
    ("https://www.tiktok.com/@nike", "TikTok", "nike", "profile"),
])
def test_detect_platform(url, name, handle, kind):
    p = detect_platform(url)
    assert (p.name, p.handle, p.profile_kind) == (name, handle, kind)


@pytest.mark.parametrize("url", ["https://stripe.com", "https://notinstagram.com/x", "https://dxora.com/instagram"])
def test_regular_sites_are_not_platforms(url):
    assert detect_platform(url) is None


# ----------------------------------------------------------------- evidence score
def test_login_wall_has_no_evidence():
    page = WebsiteExtractor.parse("https://www.instagram.com/crestspot", INSTAGRAM_LOGIN_WALL)
    assert evidence_chars(WebsiteContent("u", "u", page, [])) == 0
    assert evidence_chars(None) == 0


def test_url_only_description_is_not_evidence():
    html = '<html><head><title>Elon Musk (@elonmusk) / X</title><meta name="description" content="https://t.co/abc"></head><body><h1>Elon Musk</h1></body></html>'
    page = WebsiteExtractor.parse("https://x.com/elonmusk", html)
    assert evidence_chars(WebsiteContent("u", "u", page, [])) == 0


def test_parse_research():
    r = parse_research("CONFIDENCE: **Medium**\nSUMMARY: A shop.\nFACTS:\n- Sells shoes.")
    assert r.confidence == "medium" and r.text.startswith("SUMMARY") and "CONFIDENCE" not in r.text
    assert parse_research("no header here").confidence == "low"


# ----------------------------------------------------------------- API behaviour
def test_instagram_login_wall_without_reliable_research_is_refused(public_dns):
    research = FakeResearch(ResearchResult(confidence="low", text="SUMMARY: Only unrelated matches.", sources=[
        ResearchSource("reddit.com", "https://example.org/x")], queries=["crestspot instagram"]))
    llm = FakeLLM()
    client = make_client(llm=llm, extractor=LoginWallExtractor(), research=research)
    r = client.post("/link-analysis", json={"url": "https://www.instagram.com/crestspot"})
    assert r.status_code == 422
    body = r.json()
    assert body["success"] is False and body["error"]["code"] == "insufficient_public_data"
    assert "Instagram" in body["error"]["message"] and "@crestspot" in body["error"]["message"]
    assert body["details"]["platform"] == "Instagram" and body["details"]["handle"] == "crestspot"
    assert body["details"]["web_research"]["confidence"] == "low"
    assert llm.providers[0].prompts == []  # the LLM was never asked to invent an analysis


def test_instagram_login_wall_with_reliable_research_is_analysed(public_dns):
    llm = FakeLLM()
    client = make_client(llm=llm, extractor=LoginWallExtractor(), research=FakeResearch(reliable_research()))
    r = client.post("/link-analysis", json={"url": "https://www.instagram.com/acmestudio"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["source_type"] == "social_profile" and meta["platform"] == "Instagram" and meta["handle"] == "acmestudio"
    assert meta["content_quality"] == "insufficient" and meta["data_sources"] == ["web_search"]
    assert [s["title"] for s in meta["web_research_sources"]] == ["acmestudio.in", "clutch.co"]
    prompt = llm.providers[0].prompts[0]
    assert "Instagram profile (@acmestudio)" in prompt and "<web_research" in prompt and "Bengaluru design agency" in prompt


def test_blocked_page_uses_web_research(public_dns):
    client = make_client(extractor=BlockedExtractor(), research=FakeResearch(reliable_research()))
    r = client.post("/link-analysis", json={"url": "https://www.facebook.com/acmestudio"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["platform"] == "Facebook" and meta["data_sources"] == ["web_search"]
    assert any("could not be read directly" in n for n in meta["notes"])


def test_blocked_social_page_without_research_returns_clear_error(public_dns):
    r = make_client(extractor=BlockedExtractor(), research=FakeResearch(None)).post(
        "/link-analysis", json={"url": "https://www.linkedin.com/company/acme"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "insufficient_public_data"
    assert body["details"]["fetch_error"].startswith("Website blocked")


def test_good_website_skips_research(public_dns):
    class RichExtractor:
        async def extract(self, url, **kw):
            html = "<html><head><title>Acme</title></head><body><main>" + "".join(
                f"<p>Acme payroll feature number {i} helps small businesses pay staff on time every month.</p>"
                for i in range(40)) + "</main></body></html>"
            return WebsiteContent(url, url, WebsiteExtractor.parse(url, html), [])

    research = FakeResearch(reliable_research())
    r = make_client(extractor=RichExtractor(), research=research).post("/link-analysis", json={"url": "https://acme.example"})
    assert r.status_code == 200
    assert research.calls == []
    meta = r.json()["meta"]
    assert meta["content_quality"] == "good" and meta["data_sources"] == ["website_content"]
    assert meta["source_type"] == "website" and meta["analysis_confidence"] == "high"


def test_limited_website_is_supplemented(public_dns):
    research = FakeResearch(reliable_research())
    r = make_client(research=research).post("/link-analysis", json={"url": "https://acme.example"})
    assert r.status_code == 200
    assert len(research.calls) == 1
    assert r.json()["meta"]["data_sources"] == ["website_content", "web_search"]


# ----------------------------------------------------------------- research parsing / official website
def test_parse_research_with_website():
    from app.services.research_service import official_website

    r = parse_research("CONFIDENCE: high\nWEBSITE: **crestspot.cloud**\nSUMMARY: An AI brand platform.\nFACTS:\n- x")
    assert r.confidence == "high" and r.website == "https://crestspot.cloud/"
    assert "WEBSITE" not in r.text and "CONFIDENCE" not in r.text
    assert parse_research("CONFIDENCE: low\nWEBSITE: none\nSUMMARY: -").website is None
    assert official_website("https://www.instagram.com/brand") is None
    assert official_website("brandfetch.com/crestspot.cloud") is None
    assert official_website("https://www.Acme.example/about") == "https://acme.example/"


def test_official_website_from_research_is_analysed(public_dns):
    class Extractor:
        def __init__(self):
            self.urls = []

        async def extract(self, url, **kw):
            self.urls.append(url)
            if "instagram.com" in url:
                return WebsiteContent(url, url, WebsiteExtractor.parse(url, INSTAGRAM_LOGIN_WALL), [])
            html = "<html><head><title>Acme Studio | Branding agency</title></head><body><main>" + "".join(
                f"<p>Acme Studio service {i}: brand identity and UI design for Bengaluru startups.</p>" for i in range(20)
            ) + "</main></body></html>"
            return WebsiteContent(url, url, WebsiteExtractor.parse(url, html), [])

    found = reliable_research()
    found.website = "https://acmestudio.example/"
    extractor, llm = Extractor(), FakeLLM()
    r = make_client(llm=llm, extractor=extractor, research=FakeResearch(found)).post(
        "/link-analysis", json={"url": "https://www.instagram.com/acmestudio"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert extractor.urls == ["https://www.instagram.com/acmestudio", "https://acmestudio.example/"]
    assert meta["data_sources"] == ["official_website", "web_search"]
    assert "https://acmestudio.example/" in meta["pages_analyzed"]
    prompt = llm.providers[0].prompts[0]
    assert "<official_website_content" in prompt and "brand identity and UI design" in prompt
    assert "<page_content>" not in prompt  # the empty login wall is not sent as evidence


# ----------------------------------------------------------------- account verification via the brand's website
BRAND_SITE = """<html><head><title>CREST - AI Brand Promotion Platform</title>
<meta name="description" content="CREST helps businesses claim the top spot on Google, social media and AI assistants."></head>
<body><main>{body}</main><footer>{links}</footer></body></html>"""


def brand_site(links: str) -> str:
    body = "".join(f"<p>CREST plan {i}: AI-powered brand syndication and visibility for small businesses in India.</p>"
                   for i in range(15))
    return BRAND_SITE.format(body=body, links=links)


class CandidateExtractor:
    def __init__(self, links):
        self.links, self.urls = links, []

    async def extract(self, url, **kw):
        self.urls.append(url)
        if "instagram.com" in url:
            return WebsiteContent(url, url, WebsiteExtractor.parse(url, INSTAGRAM_LOGIN_WALL), [])
        return WebsiteContent(url, url, WebsiteExtractor.parse(url, brand_site(self.links)), [])


def unconfirmed_research():
    return ResearchResult(confidence="low", text="SUMMARY: A same-name brand exists but no source links it to the account.",
                          candidates=["https://crest.example/"], sources=[ResearchSource("CREST", "https://crest.example/")])


def test_links_to_account():
    from app.services.website_extractor import links_to_account

    page = WebsiteExtractor.parse("https://crest.example/", brand_site(
        '<a href="https://www.instagram.com/crestspot/">IG</a><a href="https://facebook.com/other">FB</a>'))
    content = WebsiteContent("u", "u", page, [])
    assert links_to_account(content, detect_platform("https://instagram.com/CrestSpot")) == "https://www.instagram.com/crestspot/"
    assert links_to_account(content, detect_platform("https://instagram.com/someoneelse")) is None
    assert links_to_account(content, detect_platform("https://x.com/crestspot")) is None


def test_candidate_website_linking_to_account_is_verified_and_analysed(public_dns):
    extractor, llm = CandidateExtractor('<a href="https://instagram.com/crestspot">Instagram</a>'), FakeLLM()
    r = make_client(llm=llm, extractor=extractor, research=FakeResearch(unconfirmed_research())).post(
        "/link-analysis", json={"url": "https://www.instagram.com/crestspot"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["account_verified_by"] == "https://instagram.com/crestspot"
    assert meta["data_sources"] == ["official_website", "web_search"]
    assert any("Account ownership verified" in n for n in meta["notes"])
    prompt = llm.providers[0].prompts[0]
    assert "Verified: the website https://crest.example/ links to this exact account" in prompt
    assert "AI-powered brand syndication" in prompt


def test_candidate_website_not_linking_to_account_is_rejected(public_dns):
    extractor, llm = CandidateExtractor('<a href="https://instagram.com/somebodyelse">Instagram</a>'), FakeLLM()
    r = make_client(llm=llm, extractor=extractor, research=FakeResearch(unconfirmed_research())).post(
        "/link-analysis", json={"url": "https://www.instagram.com/crestspot"})
    assert r.status_code == 422
    assert r.json()["details"]["web_research"]["unconfirmed_candidates"] == ["https://crest.example/"]
    # research candidate first, then the obvious domains for the handle - none links to the account
    assert extractor.urls[:3] == ["https://www.instagram.com/crestspot", "https://crest.example/", "https://crestspot.com/"]
    assert "https://crestspot.cloud/" in extractor.urls
    assert llm.providers[0].prompts == []


def test_platform_pages_do_not_crawl_platform_subpages(monkeypatch):
    import asyncio

    from app.config import Settings

    fetched = []
    html = '<html><head><title>MrBeast - YouTube</title></head><body><a href="/new">New</a><a href="/about">About</a></body></html>'

    async def fake_fetch(self, client, url):
        fetched.append(url)
        return url, html

    monkeypatch.setattr(WebsiteExtractor, "_fetch_html", fake_fetch)
    content = asyncio.run(WebsiteExtractor(Settings()).extract("https://www.youtube.com/@MrBeast"))
    assert fetched == ["https://www.youtube.com/@MrBeast"] and content.subpages == []


def test_platform_redirect_away_from_account_is_not_evidence(public_dns):
    class RedirectingExtractor:
        async def extract(self, url, **kw):
            about = "<html><head><title>About TikTok</title></head><body><main>" + "".join(
                f"<p>TikTok is the leading destination for short-form mobile video, fact {i}.</p>" for i in range(30)
            ) + "</main></body></html>"
            final = "https://www.tiktok.com/in/about"
            return WebsiteContent(url, final, WebsiteExtractor.parse(final, about), [])

    llm = FakeLLM()
    r = make_client(llm=llm, extractor=RedirectingExtractor(), research=FakeResearch(reliable_research())).post(
        "/link-analysis", json={"url": "https://www.tiktok.com/@acmestudio"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["data_sources"] == ["web_search"] and meta["pages_analyzed"] == []
    assert any("redirected to https://www.tiktok.com/in/about" in n for n in meta["notes"])
    assert "short-form mobile video" not in llm.providers[0].prompts[0]


def test_handle_domain_verified_even_without_research(public_dns):
    class Extractor:
        def __init__(self):
            self.calls = []

        async def extract(self, url, with_subpages=True):
            self.calls.append((url, with_subpages))
            if "instagram.com" in url:
                return WebsiteContent(url, url, WebsiteExtractor.parse(url, INSTAGRAM_LOGIN_WALL), [])
            if url == "https://crestspot.cloud/":
                html = brand_site('<a href="https://www.instagram.com/crestspot/">IG</a>')
                return WebsiteContent(url, url, WebsiteExtractor.parse(url, html), [])
            raise WebsiteFetchError("Could not connect to website: ConnectError.")

    extractor, llm = Extractor(), FakeLLM()
    r = make_client(llm=llm, extractor=extractor, research=FakeResearch(None)).post(
        "/link-analysis", json={"url": "https://www.instagram.com/crestspot"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["account_verified_by"] == "https://www.instagram.com/crestspot/"
    assert meta["data_sources"] == ["official_website", "web_search"]
    assert ("https://crestspot.cloud/", False) in extractor.calls   # homepage-only check
    assert ("https://crestspot.cloud/", True) in extractor.calls    # full read after verification
    assert "AI-powered brand syndication" in llm.providers[0].prompts[0]


def test_handle_domains():
    from app.services.research_service import handle_domains

    assert handle_domains("Crest_Spot")[:2] == ["https://crestspot.com/", "https://crestspot.cloud/"]
    assert handle_domains("ab") == [] and handle_domains(None) == []


def test_website_matches_handle():
    from app.services.research_service import website_matches_handle as m

    assert m("https://www.nike.in/", "nike")
    assert m("https://dxora.com/", "dxora-technologies")
    assert m("https://crestspot.cloud/", "crestspot")
    assert not m("https://crestsport.com/", "crestspot")
    assert m("https://crest.dxora.com/", "crestspot")  # subdomain "crest" names the brand
    assert not m(None, "nike") and not m("https://nike.com/", None)


def test_confident_research_about_a_same_name_brand_is_rejected(public_dns):
    """Research claims (high confidence) the account belongs to crestsport.com - a different brand that
    neither links to the account nor matches the handle. It must be discarded; the real brand website
    is then found because it links to the account."""

    class Extractor:
        async def extract(self, url, with_subpages=True):
            if "instagram.com" in url:
                return WebsiteContent(url, url, WebsiteExtractor.parse(url, INSTAGRAM_LOGIN_WALL), [])
            if url == "https://crestsport.com/":
                html = "<html><head><title>CrestSport apparel</title></head><body><main>" + "".join(
                    f"<p>Cargo shorts and thermal clothing collection number {i} for men.</p>" for i in range(20)
                ) + '</main><a href="https://instagram.com/crestsport">IG</a></body></html>'
                return WebsiteContent(url, url, WebsiteExtractor.parse(url, html), [])
            if url == "https://crestspot.cloud/":
                html = brand_site('<a href="https://www.instagram.com/crestspot">IG</a>')
                return WebsiteContent(url, url, WebsiteExtractor.parse(url, html), [])
            raise WebsiteFetchError("Could not connect to website: ConnectError.")

    wrong = ResearchResult(confidence="high", text="SUMMARY: CrestSport sells cargo shorts.", website="https://crestsport.com/",
                           sources=[ResearchSource("crestsport.com", "https://crestsport.com/")])
    llm = FakeLLM()
    r = make_client(llm=llm, extractor=Extractor(), research=FakeResearch(wrong)).post(
        "/link-analysis", json={"url": "https://www.instagram.com/crestspot"})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["account_verified_by"] == "https://www.instagram.com/crestspot"
    prompt = llm.providers[0].prompts[0]
    assert "AI-powered brand syndication" in prompt
    assert "cargo shorts" not in prompt.lower()


def test_notes_wording_for_empty_social_page(public_dns):
    r = make_client(extractor=LoginWallExtractor(), research=FakeResearch(reliable_research())).post(
        "/link-analysis", json={"url": "https://www.instagram.com/acmestudio"})
    notes = r.json()["meta"]["notes"]
    assert "Source is an Instagram profile (@acmestudio); the analysis covers the account owner, not Instagram." in notes
    assert "The page showed no public content (login wall for anonymous visitors)." in notes
