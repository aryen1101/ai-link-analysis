# AI-Powered Website Link Analysis API

A FastAPI service that takes a website URL, safely fetches and cleans its content, and uses an LLM (**OpenAI**, with automatic fallback to **Google Gemini** and **Groq**) to return a structured JSON analysis in five sections.

It works with **business websites and social-media links** (Instagram, X, Facebook, LinkedIn, YouTube, TikTok ...). When a page hides its content (login walls, bot blocking), the service researches the brand with web-search tools, verifies which website owns the account, and analyses that - and if no reliable information exists it says so instead of inventing an analysis. See [Social media & blocked links](#social-media--blocked-links).

**Live demo:** https://ai-link-analysis-nv5l.onrender.com · **API docs (Swagger):** [https://ai-link-analysis-nv5l.onrender.com/docs](https://ai-link-analysis-nv5l.onrender.com/docs) · **Endpoint:** `POST https://ai-link-analysis-nv5l.onrender.com/link-analysis`

The five sections:

1. **Competitor vs Your Brand** – brand overview, category, products/services, 3+ competitors, strengths, gaps, comparison table, advantages, positioning opportunities
2. **Top 5 Target Search Queries** – query, platforms (Google / Facebook / Instagram / LinkedIn / X), intent, business relevance, audience
3. **SEO Improvements** – primary / secondary / long-tail / business keywords, recommended title + structure, meta description, observed on-page issues, supporting content ideas
4. **Top 5 Services** – name, description, target industry, target customer, main benefit, and whether it was found on the site or `inferred`
5. **Target Audience** – primary/secondary audiences, industries, business types, profiles, geography, needs, interests, 5 marketing segments

---

## Quick start

```bash
git clone https://github.com/aryen1101/ai-link-analysis.git
cd ai-link-analysis
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set OPENAI_API_KEY (and optionally GEMINI_API_KEY / GROQ_API_KEY)
uvicorn app.main:app --host 0.0.0.0 --port 4000
```

* API: `POST http://localhost:4000/link-analysis`
* Swagger docs: http://localhost:4000/docs
* Health check: `GET http://localhost:4000/health`

Run the tests (fully offline – DNS, HTTP and the LLM are mocked):

```bash
pytest -q
```

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | – | OpenAI key. Never committed (`.env` is git-ignored). At least one LLM key is required |
| `GEMINI_API_KEY` | – | Google Gemini key ([free tier](https://aistudio.google.com/apikey)) – fallback |
| `GROQ_API_KEY` | – | Groq key ([free tier](https://console.groq.com/keys)) – fallback and web research |
| `OPENAI_MODEL` | `gpt-6-luna` | Primary model |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Fallback model |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Fallback model (open-weights model hosted on Groq) |
| `LLM_PROVIDERS` | `openai,gemini,groq` | Provider order. Providers without a key are skipped |
| `WEB_RESEARCH_ENABLED` | `true` | Research brands whose page shows no content (social login walls, blocked sites) |
| `RESEARCH_PROVIDERS` | `openai,groq` | Search tools used for research, in order (OpenAI `web_search`, Groq `browser_search`, Gemini Google Search) |
| `GEMINI_MAX_CONTENT_CHARS` / `GROQ_MAX_CONTENT_CHARS` | `14000` / `5000` | Website-content budget per provider (Groq free tier allows ~8K tokens/min) |
| `GROQ_REASONING_EFFORT` | `low` | Reasoning effort for gpt-oss models |
| `MAX_EXTRA_PAGES` | `3` | Internal pages (about/services/products…) fetched in addition to the homepage |
| `CACHE_TTL_SECONDS` | `3600` | In-memory result cache per URL |

---

## API

### Request

```http
POST /link-analysis
Content-Type: application/json

{ "url": "https://example.com" }
```

Optional query parameter `?refresh=true` bypasses the cache.

```bash
curl -X POST http://localhost:4000/link-analysis \
  -H "Content-Type: application/json" \
  -d '{"url": "https://stripe.com"}'
```

### Success response (shape)

```jsonc
{
  "success": true,
  "website": "https://stripe.com/in",
  "brand": { "name": "...", "category": "...", "summary": "..." },
  "analysis": {
    "competitor_analysis": {
      "brand_overview": "...", "main_category": "...", "main_products_services": [],
      "competitors": [{ "name": "...", "website": "...", "target_market": "...", "main_service": "...",
                        "strengths": [], "weaknesses_or_gaps": [], "source": "ai_suggestion" }],
      "comparison_table": [{ "aspect": "Main Service", "your_brand": "...", "competitors": [] }],
      "competitive_advantages": [], "market_positioning_opportunities": []
    },
    "target_search_queries": [{ "query": "...", "platforms": ["Google", "LinkedIn"], "intent": "...",
                                "business_relevance": "...", "target_audience": "..." }],
    "seo_improvements": {
      "current_title": "...", "current_meta_description": "...",
      "keywords": { "primary_keyword": "...", "secondary_keywords": [], "long_tail_keywords": [], "business_keywords": [] },
      "recommended_title": "...", "title_structure": "...", "meta_description": "...",
      "issues_found": [], "supporting_content": [{ "type": "faq", "title": "...", "reason": "..." }]
    },
    "top_services": [{ "service_name": "...", "description": "...", "target_industry": "...",
                       "target_customer": "...", "main_benefit": "...", "source": "website_content" }],
    "target_audience": {
      "primary_audience": [], "secondary_audience": [], "industries": [], "business_types": [],
      "customer_profiles": [], "geographic_focus": "...", "customer_needs_and_challenges": [],
      "customer_interests": [], "recommended_marketing_segments": [{ "audience": "...", "business_type": "...", "requirement": "..." }]
    }
  },
  "meta": {
    "provider": "openai",
    "model": "gpt-6-luna",
    "source_type": "website",              // or "social_profile"
    "platform": null, "handle": null,      // e.g. "Instagram", "crestspot"
    "content_quality": "good",             // good | limited | insufficient (real page text found)
    "account_verified_by": null,           // link on the brand website that points to the social account
    "data_sources": ["website_content"],   // + "official_website", "web_search"
    "analysis_confidence": "high",         // high | medium | low, with confidence_reason
    "confidence_reason": "...",
    "web_research_sources": [],            // [{ "title": "...", "url": "..." }] when research was used
    "pages_analyzed": ["https://stripe.com/in", "https://stripe.com/in/about", "..."],
    "content_chars_sent": 13882,
    "cached": false,
    "processing_time_ms": 0,
    "token_usage": { "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0 },
    "notes": ["Competitors are AI-generated suggestions ...", "Search queries are AI-generated; no search-volume data is reported."]
  }
}
```

Full real responses from both providers are saved in [`docs/samples/`](docs/samples) (e.g. [`gemini-stripe.com.json`](docs/samples/gemini-stripe.com.json), [`groq-basecamp.com.json`](docs/samples/groq-basecamp.com.json)).

### Error responses

All errors use the same structure:

```json
{ "success": false, "website": "<submitted url>", "error": { "code": "...", "message": "..." } }
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_url` | Empty / malformed URL, unsupported scheme, unresolvable domain |
| 400 | `blocked_url` | SSRF protection: private/loopback/link-local/metadata IPs, internal hostnames, non-standard ports |
| 422 | `invalid_request` | Missing or wrong request body |
| 422 | `insufficient_content` | Page has almost no readable content |
| 422 | `insufficient_public_data` | The page shows no content (e.g. a social login wall or a blocked page) **and** web research found no reliable, verifiable information about the brand. Nothing is invented; `details` shows what was found (platform, handle, research summary, sources, unconfirmed candidates) |
| 502 | `website_unreachable` | Timeout, connection error, HTTP 4xx/5xx, bot blocking (401/403/429), non-HTML content |
| 503 | `llm_error` | Every configured provider failed (rate limit, timeout, bad key, invalid output); the message lists each provider's reason |
| 500 | `llm_not_configured` | No LLM API key (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`) is set on the server |

Real log of these cases: [`docs/samples/error-cases.log`](docs/samples/error-cases.log)

---

## Social media & blocked links

Social platforms show anonymous visitors a login wall: for `https://www.instagram.com/crestspot` the page contains only the title "Instagram". An earlier version passed that to the model, which guessed from the word *crest* and described a dental-care brand. The service now handles these links explicitly:

1. **Detect the platform and handle** (`platforms.py`) – Instagram, X/Twitter, Facebook, LinkedIn, YouTube, TikTok, Threads, Pinterest, Reddit. Pages the platform redirects to (login, region or "about" pages) are discarded, and the platform's own sub-pages are never crawled.
2. **Score the evidence.** Login walls, cookie banners, bare links and boilerplate count as zero.
3. **Research the account** with documented web-search tools (OpenAI `web_search`, then Groq `browser_search`). The model must cite sources, name the official website and rate its confidence; answers without sources are never trusted.
4. **Verify ownership.** A model's confidence is not proof. A website is accepted as the account owner only if it **links to the exact account** (e.g. a footer icon to `instagram.com/crestspot`) or its domain matches the handle. Candidate websites – from research and the obvious domains for the handle (`crestspot.com`, `.cloud`, `.io` ...) – are checked in parallel with the same SSRF protection. Research about a same-name but different brand is discarded.
5. **Analyse the verified website + cited research**, report `source_type`, `platform`, `handle`, `account_verified_by`, `data_sources`, `analysis_confidence` and `web_research_sources` in `meta`.
6. **Otherwise refuse** with `422 insufficient_public_data` and the details of what was found.

The same research path also rescues normal websites that block automated requests (e.g. `freshworks.com` returns HTTP 403).

**Results on real links** (OpenAI `gpt-6-luna`, 2026-11-20):

| Link | What the page showed | Path taken | Result |
|---|---|---|---|
| `instagram.com/crestspot` | Login wall (title "Instagram") | Research → `crest.dxora.com` **links to the account** → website analysed | **CREST – AI-powered brand promotion & visibility platform**; services from its plans (brand profile, outreach, #1 spot placement, Competitor Watch); high confidence. Same result in 5 of 5 runs, 46–54 s |
| `x.com/elonmusk` | Login wall | Research with cited sources | Elon Musk – personal account of a public figure; 44 s |
| `facebook.com/nike` | Blocked (HTTP 400) | Research → official site `nike.in` | Nike – athletic footwear and apparel; 43–52 s |
| `linkedin.com/company/dxora-technologies` | Blocked (HTTP 403) | Research → `dxora.com` | Dxora Technologies – software engineering & AI services; 42 s |
| `tiktok.com/@nike` | Redirect to TikTok's own "about" page (discarded) | Research → `nike.in` | Nike; 45–50 s |
| `youtube.com/@MrBeast` | Channel description | Page content (plus research when YouTube shows a consent page) | MrBeast – creator-led entertainment; 34–51 s |
| `instagram.com/zzq_no_such_brand_8812x` | Login wall | Research found nothing; no website links to the account | **422 `insufficient_public_data`** – no invented analysis; 13 s |

Sample responses: [`openai-instagram-crestspot.json`](docs/samples/openai-instagram-crestspot.json),
[`openai-x-elonmusk.json`](docs/samples/openai-x-elonmusk.json),
[`openai-instagram-zzq_no_such_brand_8812x.json`](docs/samples/openai-instagram-zzq_no_such_brand_8812x.json) (refusal).

**Limits:** research depends on what public search engines know about an account. A small or new account whose website does not link to it may correctly end in `insufficient_public_data`; submitting the brand's own website URL always works. Social-link analyses take ~40–55 s because they add a research call and website checks before the analysis.


---

## How it works

```
POST /link-analysis
   │
   ├─ 1. Validate + normalise URL ............ utils/validators.py
   ├─ 2. SSRF check (DNS → every IP must be public)
   ├─ 3. Cache lookup (URL → result, TTL 1h) . services/analysis_service.py
   ├─ 4. Fetch homepage (redirects followed manually; every hop re-checked, 3 MB cap, HTML only)
   ├─ 5. Parse: title, meta, Open Graph, JSON-LD, headings, nav, body text, <noscript> fallback
   ├─ 6. Pick up to 3 high-value internal pages (about / services / products ...) → fetch in parallel
   ├─ 7. Clean: drop scripts/styles/nav/footer, de-duplicate, trim to character budget
   ├─ 8. Score real evidence (login walls, cookie banners, bare URLs score 0) ... website_extractor.evidence_chars
   ├─ 9. Thin / blocked page → web research with cited sources ........ services/research_service.py
   │      social accounts: verify the brand website links back to the account, then read that website
   │      still no reliable evidence → 422 insufficient_public_data (no invented analysis)
   ├─10. One LLM call (OpenAI structured outputs; JSON schema from Pydantic) .. services/llm_service.py
   ├─11. Validate with Pydantic; on error / timeout / invalid output retry on Gemini, then Groq
   ├─12. Post-process competitors (remove self/duplicates, label as AI suggestion) .. services/competitor_service.py
   └─13. Return JSON + meta (sources, confidence, model, pages, tokens, cost, timing, notes)
```

```
app/
├── main.py                    FastAPI app, routes, error handlers
├── config.py                  Settings from environment / .env
├── schemas.py                 Request, response and LLM output models
├── errors.py                  Domain errors → HTTP status codes
├── services/
│   ├── website_extractor.py   Safe fetching + HTML extraction + evidence scoring + context building
│   ├── platforms.py           Social-platform / handle detection
│   ├── research_service.py    Web research (OpenAI web_search, Groq browser_search, Gemini Google Search)
│   ├── llm_service.py         OpenAI + Gemini + Groq providers, fallback chain, token/cost tracking
│   ├── analysis_service.py    Orchestration, prompt, evidence policy, account verification, caching
│   └── competitor_service.py  Competitor clean-up and labelling
└── utils/
    ├── validators.py          URL validation + SSRF protection
    └── text.py                Unicode clean-up of model output
tests/                         77 offline tests (test_link_analysis.py, test_social_links.py)
scripts/benchmark.py           Measures latency / tokens / cost per provider on real sites
```

### Design decisions

* **Grounded, not generic.** The prompt requires real product names and wording from the site, services are tagged `website_content` vs `inferred`, and missing facts are stated ("Not specified on website") instead of invented.
* **Honest about data limits.** Competitors come from model knowledge (no live market-research API), so each is labelled `source: "ai_suggestion"` and a disclaimer is added to `meta.notes`. No search volumes are produced.
* **Schema-enforced JSON.** One Pydantic model drives both providers: Gemini gets it as `response_schema`, Groq gets a strict JSON schema (`additionalProperties: false`, all fields required). Every response is validated with Pydantic again before it is returned. Fields like platforms and content types are enums.
* **Three providers, one interface.** `LLMProvider` subclasses for OpenAI, Gemini and Groq; the service tries them in order and reports which one answered in `meta.provider`. If one provider is down, rate-limited or returns invalid JSON, the request still succeeds.
* **No evidence, no analysis.** Every request measures how much real, page-specific text was found. A login wall or a blocked page is never sent to the model as if it were evidence; the model is told never to infer a business from its name, and if neither the page nor verified research describes the brand, the API returns `insufficient_public_data` instead of a plausible-looking but invented analysis.
* **SSRF protection.** Only `http/https` on ports 80/443, no embedded credentials, blocked internal hostnames, and every resolved IP must be globally routable (blocks `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254.169.254`, `100.64/10`, `::1`, IPv4-mapped IPv6, etc.). Redirects are followed manually so a public URL cannot redirect to an internal one. *Known limitation:* a DNS-rebinding attack between the check and the connection is not fully prevented; pinning the resolved IP in a custom transport would close that gap.
* **JavaScript-rendered sites.** Many SPAs ship their only static text in `<noscript>`; that content is used. If little text is available the response includes a note saying so. A headless browser (Playwright) was deliberately not added to keep the service light.

---

## LLM platform & model research

All prices are official paid-tier list prices (USD per 1M tokens) **checked on 2026-11-19**. Gemini and Groq also offer a **free tier**; in testing those limits turned out to be very small (Gemini: 20 requests/day per model, Groq: 200K tokens/day), which is why the final setup uses a paid OpenAI key as the primary provider.

### A. Comparison

| Provider | Model | Input $/1M | Output $/1M | Context window | Speed (provider-published) | Structured JSON | Free tier | Suitability |
|---|---|---:|---:|---|---|---|---|---|
| OpenAI | **`gpt-6-luna`** | 0.10 | 0.50 | 1.05M (922K input) | not published | Yes – strict Structured Outputs | No | **Recommended primary** – cheapest OpenAI model, strict JSON, built-in `web_search` |
| OpenAI | `gpt-5.4-mini` | 0.75 | 4.50 | 400K (272K input) | not published | Yes – strict Structured Outputs | No | 7–9× the price of `gpt-6-luna` |
| OpenAI | `gpt-6.1-terra` | 1.00 | 5.00 | 1.05M (922K input) | not published | Yes – strict Structured Outputs | No | Higher tier for premium reports |
| Google | **`gemini-2.5-flash-lite`** | 0.10 | 0.40 | 1,048,576 | not published | Yes – `response_schema` | Yes (20 req/day) | **Fallback** – cheapest, reliable in testing |
| Google | `gemini-3.5-flash-lite` | 0.30 | 2.50 | 1,048,576 | not published | Yes – `response_schema` | Yes | Newer generation; repeatedly overloaded (HTTP 503) in testing |
| Google | `gemini-3.8-flash` | 0.75 (1.50 from 2027-01-01) | 3.75 (7.50 from 2027-01-01) | 1,048,576 | not published | Yes – `response_schema` | Yes | Stronger Flash model; overloaded in testing; price doubles in January |
| Groq | **`openai/gpt-oss-120b`** | 0.15 | 0.60 | 131,072 | ~500 tokens/s | Yes – strict `json_schema` | Yes (8K tokens/min, 200K/day) | **Fallback + web research** – fastest in testing |
| Groq | `openai/gpt-oss-20b` | 0.075 | 0.30 | 131,072 | ~1,000 tokens/s | Yes – strict `json_schema` | Yes (8K tokens/min) | Cheapest overall; smaller model, less world knowledge for competitors |
| Groq | `qwen/qwen3.8-27b` | 0.80 | 4.00 | 131,072 | ~450 tokens/s | Yes – `json_schema` | Yes | Preview model – not for production |

Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing) · [OpenAI models](https://developers.openai.com/api/docs/models) · [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search) · [Gemini pricing](https://aistudio.google.com/docs/pricing) · [Gemini models](https://aistudio.google.com/docs/models) · [Gemini structured output](https://aistudio.google.com/docs/structured-output) · [Groq models & pricing](https://console.groq.com/docs/models) · [Groq structured outputs](https://console.groq.com/docs/structured-outputs) · [Groq rate limits](https://console.groq.com/docs/rate-limits)

`openai/gpt-oss-120b` is an open-weights model served by Groq; it needs only a Groq key.

**Speed & accuracy:** the speed column is what the provider publishes, not a measurement. No provider publishes accuracy for this task, so the table does not claim it. Measured results from this application are in section B; "accuracy" there means judged output quality on real websites, not a formal benchmark.

### B. Measured results (this application)

**Primary model (OpenAI `gpt-6-luna`, reasoning effort `low`)** – measured on 2026-11-20 through the full pipeline (fetch, evidence check, research when needed, analysis):

| Link | Path taken | Input tokens | Output tokens | End-to-end time | Cost |
|---|---|---:|---:|---:|---:|
| stripe.com | website content only | 5,040 | 3,684 | 33.5 s | $0.0023 |
| dxora.com | thin JS site → website + research | 2,974 | 3,684 | 37.3 s | $0.0021 |
| youtube.com/@MrBeast | channel description (no research needed) | 3,722 | 3,815 | 34.2 s | $0.0023 |
| instagram.com/crestspot | login wall → research → website verified by link → crest.dxora.com | 4,767 | 3,922 | 46–54 s | $0.0024 |
| facebook.com/nike | blocked (HTTP 400) → research → nike.in | 3,602 | 3,669 | 43.2 s | $0.0022 |
| linkedin.com/company/dxora-technologies | blocked (HTTP 403) → research → dxora.com | 3,002 | 3,714 | 41.7 s | $0.0022 |
| x.com/elonmusk | login wall → research | 2,468 | 3,497 | 45.0 s | $0.0020 |
| freshworks.com | blocked (HTTP 403) → research | 2,489 | 3,541 | 37.7 s | $0.0020 |

Cost covers the analysis call; a research call adds roughly $0.002 (≈ 20K input tokens of search results at $0.10/1M). Time is dominated by the model (≈ 20–25 s for the five-section output); research and verification add 10–20 s for social links.

**Fallback models** – measured on **2026-11-19** from a home connection in India on the free tiers, with [`scripts/benchmark.py`](scripts/benchmark.py) (`python -m scripts.benchmark <urls>`). Each provider was run on its own (no fallback). Times are end-to-end: website fetch + extraction + LLM call. Full responses: [`docs/samples/`](docs/samples).

| Website | Provider / model | Input tokens | Output tokens | End-to-end time | Cost at paid list price |
|---|---|---:|---:|---:|---:|
| dxora.com | gemini / `gemini-2.5-flash-lite` | 967 | 3,731 | 25.8 s | $0.0016 |
| stripe.com | gemini / `gemini-2.5-flash-lite` | 3,533 | 4,080 | 16.9 s | $0.0020 |
| basecamp.com | gemini / `gemini-2.5-flash-lite` | 3,607 | 4,100 | 14.5 s | $0.0020 |
| dxora.com | groq / `openai/gpt-oss-120b` | 2,712 | 3,471 | 8.6 s | $0.0025 |
| stripe.com | groq / `openai/gpt-oss-120b` | 3,276 | 3,497 | 10.7 s | $0.0026 |
| basecamp.com | groq / `openai/gpt-oss-120b` | 3,372 | 3,195 | 8.5 s | $0.0024 |

Gemini does not count the response schema as input tokens; Groq does (~2,200 tokens), which is why Groq's input counts are higher for the small dxora.com page.

**Other observations (same day, small sample – not a benchmark):**

* `gemini-3.5-flash-lite` and `gemini-3.8-flash` returned **HTTP 503 "model is currently experiencing high demand"** on most attempts; one successful `gemini-3.5-flash-lite` run took 80 s. `gemini-3.1-flash-lite` timed out after 120 s. `gemini-2.5-flash-lite` succeeded every time.
* Groq's free tier (8K tokens/minute) fits **one analysis per minute**; back-to-back requests get HTTP 429. Groq also occasionally rejects its own output with `json_validate_failed`; the service retries once automatically.
* **Output quality** (manual review): both models identified the brand, category and services from the site content, marked inferred services, and suggested plausible competitors (for dxora.com: Ahmedabad-based IT firms Hidden Brains, TatvaSoft and Radixweb; for basecamp.com: Asana, Monday.com, Trello / ClickUp). `gpt-oss-120b` followed the length rules more closely (55-character title vs 65 from Gemini, against a 50–60 target).
* **Fallback demo:** [`docs/samples/fallback-demo.log`](docs/samples/fallback-demo.log) – Gemini forced to fail, Groq answers, the second request is served from cache.
* **Free-tier limits:** after ~20 Gemini requests the key returned `429 RESOURCE_EXHAUSTED` ("20 requests per day per model"), and Groq's `gpt-oss-120b` hit its 200K tokens/day limit after a few dozen analyses. That is too little for a demo that reviewers test, so OpenAI is now the primary provider.
* **Web research quality:** for `@crestspot`, Gemini's Google Search grounding twice matched the account to a same-name clothing shop with "high" confidence; OpenAI and Groq were more careful. Gemini is therefore not in the default research list, and every research result about a social account must pass the website-ownership check.

### C. Token & cost estimate per analysis

Measured: **~1,000–3,600 input tokens** and **~3,200–4,100 output tokens** per website. For a conservative estimate use 5k in / 4k out:

| Model | Estimated cost per analysis (5k in / 4k out) at paid list price |
|---|---:|
| `openai/gpt-oss-20b` (Groq) | ≈ $0.0016 |
| **`gpt-6-luna`** (OpenAI) | **≈ $0.0025** |
| `gemini-2.5-flash-lite` | ≈ $0.0021 |
| `openai/gpt-oss-120b` (Groq) | ≈ $0.0032 |
| `gemini-3.5-flash-lite` | ≈ $0.0115 |
| `gemini-3.8-flash` | ≈ $0.0188 |
| `qwen/qwen3.8-27b` (Groq) | ≈ $0.0200 |

On the Gemini/Groq free tiers the cost is **$0**. The API still reports `meta.token_usage.estimated_cost_usd` (what the call would cost on a paid plan) so production cost can be projected.

### D. Final recommendation

| Item | Value |
|---|---|
| Recommended provider | **OpenAI** (primary) + **Google Gemini** and **Groq** (automatic fallbacks) |
| Recommended model | **`gpt-6-luna`** with `reasoning_effort=low` |
| Input tokens | Measured ~2,500–5,000 per analysis (+ ~20K for a web-research call on social links) |
| Output tokens | Measured ~3,500–3,900 per analysis |
| Cost per analysis | Measured **~$0.002** (website) to **~$0.004** (social link with research) |
| Response time | Measured **33–38 s** for websites, **42–54 s** for social links with research and verification |
| Fallbacks | `gemini-2.5-flash-lite` (14–26 s, free tier) and Groq `gpt-oss-120b` (8.5–10.7 s, free tier) – used automatically on error, timeout, quota or invalid output |

**Why `gpt-6-luna`:** it is the cheapest OpenAI model ($0.10 / $0.50 per 1M tokens – the same input price as `gemini-2.5-flash-lite`), returns strictly schema-valid JSON, has a 1M-token context, and has a built-in `web_search` tool with citations, which the social-link research needs. In testing it was the most careful researcher (it did not confuse same-name brands) and produced specific, well-grounded analyses for every link type. Unlike the free tiers it has no small daily cap, so reviewers can test many links in a row. At ~$0.002–0.004 per analysis, 1,000 analyses cost about $2–4.

**Why keep Gemini and Groq:** `gemini-2.5-flash-lite` was the only Gemini model that answered reliably in testing (newer 3.x models returned HTTP 503 "high demand"), it is the cheapest per token, and it has a free tier. Groq's `gpt-oss-120b` was the fastest model measured (≈ 9 s). Both keep the API working if OpenAI is down or rate-limited.

Groq's free tier allows only ~8K tokens per minute, so the Groq request gets a smaller content budget (5,000 chars) and an output cap of 4,400 tokens.

**Balancing accuracy, speed and cost:** use `gpt-6-luna` by default; set `LLM_PROVIDERS=groq,openai` when the lowest latency matters most (on a paid Groq plan); set `LLM_PROVIDERS=gemini,openai` to minimise cost on the Gemini free tier for light traffic; use `OPENAI_MODEL=gpt-6.1-terra` for premium reports. All of this is configuration, not code changes.

### E. Cost optimisation (implemented ✔ / recommended ➜)

* ✔ **Clean before sending** – scripts, styles, SVG, nav, footer and forms removed; only text, headings, meta and structured data are sent.
* ✔ **De-duplicate** – repeated lines across homepage and sub-pages are removed; sub-pages adding nothing new (SPA shells) are dropped.
* ✔ **Per-provider character budget** with a fair share for the homepage and each sub-page (14,000 chars for OpenAI/Gemini, 5,000 for Groq).
* ✔ **Research only when needed** – web research runs only when the page has too little evidence; content-rich websites go straight to one analysis call.
* ✔ **Prompt caching** – static system prompt + schema first, `prompt_cache_key` set for OpenAI, so the repeated prefix is billed at the cached rate.
* ✔ **One LLM call for all five sections** instead of five calls – shared context is paid for once.
* ✔ **Static prompt first** – the system prompt and schema never change, so providers' implicit prompt caching can apply.
* ✔ **Result cache** – the same URL within 1 h returns instantly with no LLM call; concurrent identical requests are merged into one call.
* ✔ **Fail fast** – invalid/blocked URLs and missing API keys are rejected before any fetch or LLM call.
* ✔ **Cheapest capable model by default** (`gpt-6-luna`); other providers only when the first fails.
* ➜ Use Gemini's **Batch API (~50% off)** for bulk/offline analyses.
* ➜ Use a persistent cache (Redis) in multi-instance deployments.
* ➜ Use `openai/gpt-oss-20b` for simple extraction-only tasks.

---

## Deployment

The repo includes a `Procfile` for platforms that use one (Railway, Heroku, etc.).

**Render (free):** New → Web Service → select this repo, then set:

* Build command: `pip install -r requirements.txt`
* Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
* Environment variables: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `PYTHON_VERSION=3.13.3`
* Health check path: `/health`

| | URL |
|---|---|
| Live demo | https://ai-link-analysis-nv5l.onrender.com |
| API endpoint | `POST https://ai-link-analysis-nv5l.onrender.com/link-analysis` |
| API docs | https://ai-link-analysis-nv5l.onrender.com/docs |
| Health check | https://ai-link-analysis-nv5l.onrender.com/health |

Try it:

```bash
curl -X POST https://ai-link-analysis-nv5l.onrender.com/link-analysis \
  -H "Content-Type: application/json" \
  -d '{"url": "https://stripe.com"}'
```

Verified on 2026-11-19 (previous Gemini-based version): `/health` responded, `/docs` loaded, and live analyses of dxora.com (14.6 s) and stripe.com (17.7 s) returned all five sections. After deploying this version, set `OPENAI_API_KEY` in the Render environment.

> The free Render instance sleeps after 15 minutes without traffic; the first request after that takes ~30–50 s extra while it wakes up. Some websites block requests from cloud/data-centre IPs and will return `website_unreachable` on the hosted demo even though they work locally.

---

## Security

* No secrets in the repository – `.env` is git-ignored; `.env.example` contains placeholders only.
* SSRF protection on every outbound request (see above).
* Response size limit (3 MB), timeouts on fetch and LLM, maximum 5 redirects.
* Internal errors are logged server-side; clients receive generic messages without stack traces.
