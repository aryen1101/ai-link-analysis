# AI-Powered Website Link Analysis API

A FastAPI service that takes a website URL, safely fetches and cleans its content, and uses an LLM (OpenAI) to return a structured JSON analysis in five sections:

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
cp .env.example .env          # then set OPENAI_API_KEY in .env
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
| `OPENAI_API_KEY` | – | **Required.** Never committed (`.env` is git-ignored) |
| `OPENAI_MODEL` | `gpt-6-luna` | Primary model |
| `OPENAI_FALLBACK_MODEL` | `gpt-5.4-mini` | Used automatically if the primary fails (rate limit, timeout, invalid output, model unavailable) |
| `OPENAI_REASONING_EFFORT` | `low` | Reasoning effort for reasoning models (`none` = fastest) |
| `MAX_EXTRA_PAGES` | `3` | Internal pages (about/services/products…) fetched in addition to the homepage |
| `MAX_CONTENT_CHARS` | `14000` | Character budget for website content sent to the LLM |
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
    "model": "gpt-6-luna",
    "pages_analyzed": ["https://stripe.com/in", "https://stripe.com/in/about", "..."],
    "content_chars_sent": 13882,
    "cached": false,
    "processing_time_ms": 0,
    "token_usage": { "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0 },
    "notes": ["Competitors are AI-generated suggestions ...", "Search queries are AI-generated; no search-volume data is reported."]
  }
}
```

Full real responses are saved in [`docs/samples/`](docs/samples).

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
| 503 | `llm_error` | LLM rate limit, timeout or invalid output after fallback |
| 500 | `llm_not_configured` | `OPENAI_API_KEY` missing on the server |

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
   ├─ 8. One LLM call with OpenAI Structured Outputs (JSON schema from Pydantic) .. services/llm_service.py
   ├─ 9. Validate with Pydantic, fall back to secondary model on failure
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
│   ├── llm_service.py         OpenAI structured outputs, fallback, token/cost tracking
│   ├── analysis_service.py    Orchestration, prompt, caching, request de-duplication
│   └── competitor_service.py  Competitor clean-up and labelling
└── utils/
    └── validators.py          URL validation + SSRF protection
tests/test_link_analysis.py    35 offline tests
```

### Design decisions

* **Grounded, not generic.** The prompt requires real product names and wording from the site, services are tagged `website_content` vs `inferred`, and missing facts are stated ("Not specified on website") instead of invented.
* **Honest about data limits.** Competitors come from model knowledge (no live market-research API), so each is labelled `source: "ai_suggestion"` and a disclaimer is added to `meta.notes`. No search volumes are produced.
* **Guaranteed JSON.** OpenAI Structured Outputs (`response_format` = Pydantic model, strict JSON schema), then Pydantic validation. Fields like platforms and content types are enums.
* **SSRF protection.** Only `http/https` on ports 80/443, no embedded credentials, blocked internal hostnames, and every resolved IP must be globally routable (blocks `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254.169.254`, `100.64/10`, `::1`, IPv4-mapped IPv6, etc.). Redirects are followed manually so a public URL cannot redirect to an internal one. *Known limitation:* a DNS-rebinding attack between the check and the connection is not fully prevented; pinning the resolved IP in a custom transport would close that gap.
* **JavaScript-rendered sites.** Many SPAs ship their only static text in `<noscript>`; that content is used. If little text is available the response includes a note saying so. A headless browser (Playwright) was deliberately not added to keep the service light.

---

## LLM platform & model research

All prices are official list prices (USD per 1M tokens, standard tier) **checked on 2026-11-18** from the providers' pricing/model pages linked below.

### A. Comparison

| Provider | Model | Input $/1M | Output $/1M | Cached input $/1M | Context window | Structured JSON | Suitability |
|---|---|---:|---:|---:|---|---|---|
| OpenAI | **`gpt-6-luna`** | **0.10** | **0.50** | 0.01 | 1.05M (922K input) | Yes – strict JSON schema (Structured Outputs) | **Recommended**: lowest cost, native strict JSON, large context |
| OpenAI | `gpt-5.4-mini` | 0.75 | 4.50 | 0.075 | 400K (272K input) | Yes – strict JSON schema | **Fallback**: different model family, ~9× the price |
| OpenAI | `gpt-6.1-terra` | 1.00 | 5.00 | 0.05 | 1.05M (922K input) | Yes – strict JSON schema | Higher tier, for deeper competitor reasoning if quality needs it |
| Google | `gemini-3.5-flash-lite` | 0.30 | 2.50 | 0.03 | 1,048,576 | Yes – JSON schema response format | Good low-cost alternative provider |
| Google | `gemini-3.8-flash` | 0.75 (1.50 from 2027-01-01) | 3.75 (7.50 from 2027-01-01) | 0.075 | 1,048,576 | Yes – JSON schema response format | Capable, but price doubles in January |

Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing) · [OpenAI models](https://developers.openai.com/api/docs/models) · [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) · [Gemini pricing](https://aistudio.google.com/docs/pricing) · [Gemini models](https://aistudio.google.com/docs/models) · [Gemini structured output](https://aistudio.google.com/docs/structured-output)

**Response speed & accuracy:** providers do not publish latency or accuracy for this specific task, so the table does not claim them. Measured results from this application are in the next section. Accuracy here means judged quality of the output on real websites, not a formal benchmark.

### B. Measured results (this application)

| Website | Model | Input tokens | Output tokens | LLM + extraction time | Cost (calculated from usage) |
|---|---|---:|---:|---:|---:|
| _pending – run with an API key, see `docs/samples/`_ | | | | | |

### C. Token & cost estimate per analysis

Typical request = system prompt (~600 tokens) + JSON schema (~2,000 tokens) + cleaned website content (≤14,000 chars ≈ 3,500 tokens) → **≈ 4,000–6,500 input tokens**. The five-section output is **≈ 2,500–4,500 output tokens** (incl. reasoning tokens at `low` effort).

| Model | Estimated cost per analysis (5k in / 4k out) |
|---|---:|
| `gpt-6-luna` | **≈ $0.0025** |
| `gemini-3.5-flash-lite` | ≈ $0.0115 |
| `gpt-5.4-mini` | ≈ $0.0218 |
| `gpt-6.1-terra` | ≈ $0.0250 |

*(Estimates from list prices; the API returns the real cost of each call in `meta.token_usage.estimated_cost_usd`.)*

### D. Final recommendation

| Item | Value |
|---|---|
| Recommended provider | **OpenAI** |
| Recommended model | **`gpt-6-luna`** with `reasoning_effort=low` |
| Estimated input tokens | ~4,000–6,500 per website |
| Estimated output tokens | ~2,500–4,500 per website |
| Estimated cost per analysis | **~$0.002–0.003** (≈ 400 analyses per $1) |
| Expected response time | Estimate: 15–40 s end-to-end (extraction 1–3 s + generation); see measured table |
| Fallback model | `gpt-5.4-mini` (automatic on rate limit / timeout / invalid output / unavailability) |

**Why `gpt-6-luna`:** it is the cheapest model in the comparison by a wide margin (5× cheaper output than Gemini Flash-Lite, 9× cheaper than gpt-5.4-mini), supports strict JSON-schema structured outputs so responses always match our Pydantic schema, has a 1M-token context (no risk of truncating large sites), and supports prompt caching – our static system prompt + schema prefix is cached at $0.01/1M. Competitor suggestions benefit from the broad world knowledge of a current-generation model, and the `low` reasoning effort gives better comparisons and keyword choices than `none` at a small latency cost.

**Balancing accuracy, speed and cost:** start with `gpt-6-luna` + `low` effort; switch to `reasoning_effort=none` if latency matters more than depth; move to `gpt-6.1-terra` only for premium/enterprise reports where deeper competitor reasoning is worth ~10× the cost.

### E. Cost optimisation (implemented ✔ / recommended ➜)

* ✔ **Clean before sending** – scripts, styles, SVG, nav, footer and forms removed; only text, headings, meta and structured data are sent.
* ✔ **De-duplicate** – repeated lines across homepage and sub-pages are removed; sub-pages adding nothing new (SPA shells) are dropped.
* ✔ **Hard character budget** (`MAX_CONTENT_CHARS`) with a fair share for the homepage and each sub-page.
* ✔ **One LLM call for all five sections** instead of five calls – shared context is paid for once.
* ✔ **Prompt caching** – static system prompt + schema first, `prompt_cache_key` set, so the repeated prefix is billed at the cached rate.
* ✔ **Result cache** – the same URL within 1 h returns instantly with no LLM call; concurrent identical requests are merged into one call.
* ✔ **Fail fast** – invalid/blocked URLs and missing API key are rejected before any fetch or LLM call.
* ✔ **Cheapest capable model by default**, larger model only as fallback.
* ➜ Use the **Batch API (50% off)** for bulk/offline analyses.
* ➜ Use a persistent cache (Redis) in multi-instance deployments.
* ➜ Use `reasoning_effort=none` for simple extraction-only tasks.

---

## Deployment

The repo includes a `Procfile` for platforms that use one (Railway, Heroku, etc.).

**Render (free):** New → Web Service → select this repo, then set:

* Build command: `pip install -r requirements.txt`
* Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
* Environment variable: `OPENAI_API_KEY`

| | URL |
|---|---|
| Live demo | _to be added_ |
| API endpoint | _to be added_ `/link-analysis` |
| API docs | _to be added_ `/docs` |

---

## Security

* No secrets in the repository – `.env` is git-ignored; `.env.example` contains placeholders only.
* SSRF protection on every outbound request (see above).
* Response size limit (3 MB), timeouts on fetch and LLM, maximum 5 redirects.
* Internal errors are logged server-side; clients receive generic messages without stack traces.


#   a i - l i n k - a n a l y s i s  
 