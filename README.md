# AI-Powered Website Link Analysis API

A FastAPI service that takes a website URL, safely fetches and cleans its content, and uses an LLM (**Google Gemini**, with automatic fallback to **Groq**) to return a structured JSON analysis in five sections.

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
cp .env.example .env          # then set GEMINI_API_KEY and/or GROQ_API_KEY
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
| `GEMINI_API_KEY` | – | Google Gemini key ([free](https://aistudio.google.com/apikey)). Never committed (`.env` is git-ignored) |
| `GROQ_API_KEY` | – | Groq key ([free](https://console.groq.com/keys)). At least one of the two keys is required |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Primary model |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Fallback model (open-weights model hosted on Groq) |
| `LLM_PRIMARY_PROVIDER` / `LLM_FALLBACK_PROVIDER` | `gemini` / `groq` | Provider order. Providers without a key are skipped |
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

```json
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
    "provider": "gemini",
    "model": "gemini-2.5-flash-lite",
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
| 502 | `website_unreachable` | Timeout, connection error, HTTP 4xx/5xx, bot blocking (401/403/429), non-HTML content |
| 503 | `llm_error` | Every configured provider failed (rate limit, timeout, bad key, invalid output); the message lists each provider's reason |
| 500 | `llm_not_configured` | Neither `GEMINI_API_KEY` nor `GROQ_API_KEY` is set on the server |

Real log of these cases: [`docs/samples/error-cases.log`](docs/samples/error-cases.log)

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
   ├─ 8. One LLM call to Gemini with a response schema generated from Pydantic .. services/llm_service.py
   ├─ 9. Validate with Pydantic; on error / 60 s timeout / invalid output retry on Groq (strict JSON schema)
   ├─10. Post-process competitors (remove self/duplicates, label as AI suggestion) .. services/competitor_service.py
   └─11. Return JSON + meta (model, pages, tokens, cost, timing, notes)
```

```
app/
├── main.py                    FastAPI app, routes, error handlers
├── config.py                  Settings from environment / .env
├── schemas.py                 Request, response and LLM output models
├── errors.py                  Domain errors → HTTP status codes
├── services/
│   ├── website_extractor.py   Safe fetching + HTML extraction + context building
│   ├── llm_service.py         Gemini + Groq providers, fallback chain, token/cost tracking
│   ├── analysis_service.py    Orchestration, prompt, caching, request de-duplication
│   └── competitor_service.py  Competitor clean-up and labelling
└── utils/
    └── validators.py          URL validation + SSRF protection
tests/test_link_analysis.py    43 offline tests
scripts/benchmark.py           Measures latency / tokens / cost per provider on real sites
```

### Design decisions

* **Grounded, not generic.** The prompt requires real product names and wording from the site, services are tagged `website_content` vs `inferred`, and missing facts are stated ("Not specified on website") instead of invented.
* **Honest about data limits.** Competitors come from model knowledge (no live market-research API), so each is labelled `source: "ai_suggestion"` and a disclaimer is added to `meta.notes`. No search volumes are produced.
* **Schema-enforced JSON.** One Pydantic model drives both providers: Gemini gets it as `response_schema`, Groq gets a strict JSON schema (`additionalProperties: false`, all fields required). Every response is validated with Pydantic again before it is returned. Fields like platforms and content types are enums.
* **Two providers, one interface.** `LLMProvider` subclasses for Gemini and Groq; the service tries them in order and reports which one answered in `meta.provider`. If one provider is down, rate-limited or returns invalid JSON, the request still succeeds.
* **SSRF protection.** Only `http/https` on ports 80/443, no embedded credentials, blocked internal hostnames, and every resolved IP must be globally routable (blocks `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254.169.254`, `100.64/10`, `::1`, IPv4-mapped IPv6, etc.). Redirects are followed manually so a public URL cannot redirect to an internal one. *Known limitation:* a DNS-rebinding attack between the check and the connection is not fully prevented; pinning the resolved IP in a custom transport would close that gap.
* **JavaScript-rendered sites.** Many SPAs ship their only static text in `<noscript>`; that content is used. If little text is available the response includes a note saying so. A headless browser (Playwright) was deliberately not added to keep the service light.

---

## LLM platform & model research

All prices are official paid-tier list prices (USD per 1M tokens) **checked on 2026-11-19**. Both Gemini and Groq also offer a **free tier**, which this project runs on.

### A. Comparison

| Provider | Model | Input $/1M | Output $/1M | Context window | Speed (provider-published) | Structured JSON | Free tier | Suitability |
|---|---|---:|---:|---|---|---|---|---|
| Google | **`gemini-2.5-flash-lite`** | 0.10 | 0.40 | 1,048,576 | not published | Yes – `response_schema` | Yes | **Recommended primary** – cheapest, reliable in testing |
| Google | `gemini-3.5-flash-lite` | 0.30 | 2.50 | 1,048,576 | not published | Yes – `response_schema` | Yes | Newer generation; repeatedly overloaded (HTTP 503) in testing |
| Google | `gemini-3.8-flash` | 0.75 (1.50 from 2027-01-01) | 3.75 (7.50 from 2027-01-01) | 1,048,576 | not published | Yes – `response_schema` | Yes | Stronger Flash model; overloaded in testing; price doubles in January |
| Groq | **`openai/gpt-oss-120b`** | 0.15 | 0.60 | 131,072 | ~500 tokens/s | Yes – strict `json_schema` | Yes (8K tokens/min) | **Recommended fallback** – fastest in testing |
| Groq | `openai/gpt-oss-20b` | 0.075 | 0.30 | 131,072 | ~1,000 tokens/s | Yes – strict `json_schema` | Yes (8K tokens/min) | Cheapest overall; smaller model, less world knowledge for competitors |
| Groq | `qwen/qwen3.8-27b` | 0.80 | 4.00 | 131,072 | ~450 tokens/s | Yes – `json_schema` | Yes | Preview model – not for production |

Sources: [Gemini pricing](https://aistudio.google.com/docs/pricing) · [Gemini models](https://aistudio.google.com/docs/models) · [Gemini structured output](https://aistudio.google.com/docs/structured-output) · [Groq models & pricing](https://console.groq.com/docs/models) · [Groq structured outputs](https://console.groq.com/docs/structured-outputs) · [Groq rate limits](https://console.groq.com/docs/rate-limits)

`openai/gpt-oss-120b` is an open-weights model served by Groq; it needs only a Groq key.

**Speed & accuracy:** the speed column is what the provider publishes, not a measurement. No provider publishes accuracy for this task, so the table does not claim it. Measured results from this application are in section B; "accuracy" there means judged output quality on real websites, not a formal benchmark.

### B. Measured results (this application)

Measured on **2026-11-19** from a home connection in India on the free tiers, with [`scripts/benchmark.py`](scripts/benchmark.py) (`python -m scripts.benchmark <urls>`). Each provider was run on its own (no fallback). Times are end-to-end: website fetch + extraction + LLM call. Full responses: [`docs/samples/`](docs/samples).

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

### C. Token & cost estimate per analysis

Measured: **~1,000–3,600 input tokens** and **~3,200–4,100 output tokens** per website. For a conservative estimate use 5k in / 4k out:

| Model | Estimated cost per analysis (5k in / 4k out) at paid list price |
|---|---:|
| `openai/gpt-oss-20b` (Groq) | ≈ $0.0016 |
| **`gemini-2.5-flash-lite`** | **≈ $0.0021** |
| `openai/gpt-oss-120b` (Groq) | ≈ $0.0032 |
| `gemini-3.5-flash-lite` | ≈ $0.0115 |
| `gemini-3.8-flash` | ≈ $0.0188 |
| `qwen/qwen3.8-27b` (Groq) | ≈ $0.0200 |

On the free tiers the cost is **$0**. The API still reports `meta.token_usage.estimated_cost_usd` (what the call would cost on a paid plan) so production cost can be projected.

### D. Final recommendation

| Item | Value |
|---|---|
| Recommended provider | **Google Gemini** (primary) + **Groq** (automatic fallback) |
| Recommended model | **`gemini-2.5-flash-lite`** |
| Input tokens | Measured ~1,000–3,600 per website |
| Output tokens | Measured ~3,700–4,100 per website |
| Cost per analysis | **$0 on the free tier**; measured **~$0.002** at paid list price (≈ 500 analyses per $1) |
| Response time | Measured **14–26 s** end-to-end (3 sites) |
| Fallback | **Groq `openai/gpt-oss-120b`** – measured 8.5–10.7 s, ~$0.0025; used automatically on error, timeout (60 s), bad key, quota or invalid output |

**Why `gemini-2.5-flash-lite`:** in testing it was the only Gemini model that answered reliably. It is the cheapest Gemini model (≈ $0.002 per analysis at paid prices), has a 1M-token context so large websites are never cut off by the model, and returns schema-constrained JSON natively. Its free tier also allows far more than Groq's one analysis per minute, so it suits being the primary model. Newer Gemini 3.x models are not ruled out: switching is one setting (`GEMINI_MODEL`) once their capacity improves.

**Why Groq as fallback:** a different provider means an outage or quota limit at Google does not take the API down. `gpt-oss-120b` was the fastest model measured (≈ 9 s) with strict JSON-schema output and good instruction following. Its free tier allows only ~8K tokens per minute, so the Groq request gets a smaller content budget (5,000 chars) and an output cap of 4,400 tokens.

**Balancing accuracy, speed and cost:** use `gemini-2.5-flash-lite` by default; set `LLM_PRIMARY_PROVIDER=groq` when the lowest latency matters most (on a paid Groq plan to remove the per-minute limit); try `gemini-3.8-flash` for premium reports once it is not overloaded. All of this is configuration, not code changes.

### E. Cost optimisation (implemented ✔ / recommended ➜)

* ✔ **Clean before sending** – scripts, styles, SVG, nav, footer and forms removed; only text, headings, meta and structured data are sent.
* ✔ **De-duplicate** – repeated lines across homepage and sub-pages are removed; sub-pages adding nothing new (SPA shells) are dropped.
* ✔ **Per-provider character budget** with a fair share for the homepage and each sub-page (14,000 chars for Gemini, 5,000 for Groq).
* ✔ **One LLM call for all five sections** instead of five calls – shared context is paid for once.
* ✔ **Static prompt first** – the system prompt and schema never change, so providers' implicit prompt caching can apply.
* ✔ **Result cache** – the same URL within 1 h returns instantly with no LLM call; concurrent identical requests are merged into one call.
* ✔ **Fail fast** – invalid/blocked URLs and missing API keys are rejected before any fetch or LLM call.
* ✔ **Free tiers and cheap models by default**; a second provider only when the first fails.
* ➜ Use Gemini's **Batch API (~50% off)** for bulk/offline analyses.
* ➜ Use a persistent cache (Redis) in multi-instance deployments.
* ➜ Use `openai/gpt-oss-20b` for simple extraction-only tasks.

---

## Deployment

The repo includes a `Procfile` for platforms that use one (Railway, Heroku, etc.).

**Render (free):** New → Web Service → select this repo, then set:

* Build command: `pip install -r requirements.txt`
* Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
* Environment variables: `GEMINI_API_KEY`, `GROQ_API_KEY`, `PYTHON_VERSION=3.13.3`
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

Verified on 2026-11-19: `/health` reports both providers, `/docs` loads, and live analyses of dxora.com (14.6 s) and stripe.com (17.7 s) returned all five sections via Gemini.

> The free Render instance sleeps after 15 minutes without traffic; the first request after that takes ~30–50 s extra while it wakes up. Some websites block requests from cloud/data-centre IPs and will return `website_unreachable` on the hosted demo even though they work locally.

---

## Security

* No secrets in the repository – `.env` is git-ignored; `.env.example` contains placeholders only.
* SSRF protection on every outbound request (see above).
* Response size limit (3 MB), timeouts on fetch and LLM, maximum 5 redirects.
* Internal errors are logged server-side; clients receive generic messages without stack traces.
