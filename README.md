# AI-Powered Website Link Analysis API

A FastAPI service that takes a website URL, safely fetches and cleans its content, and uses an LLM (**Google Gemini**, with automatic fallback to **Groq**) to return a structured JSON analysis in five sections:

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
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Primary model |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Fallback model (open-weights model hosted on Groq) |
| `LLM_PRIMARY_PROVIDER` / `LLM_FALLBACK_PROVIDER` | `gemini` / `groq` | Provider order. Providers without a key are skipped |
| `GEMINI_MAX_CONTENT_CHARS` / `GROQ_MAX_CONTENT_CHARS` | `14000` / `6000` | Website-content budget per provider (Groq free tier allows ~8K tokens/min) |
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
    "model": "gemini-3.5-flash-lite",
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
   ├─ 9. Validate with Pydantic; on any failure retry on Groq (strict JSON schema, smaller content budget)
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
tests/test_link_analysis.py    40 offline tests
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
| Google | **`gemini-3.5-flash-lite`** | 0.30 | 2.50 | 1,048,576 | not published | Yes – `response_schema` | Yes | **Recommended primary** |
| Google | `gemini-3.8-flash` | 0.75 (1.50 from 2027-01-01) | 3.75 (7.50 from 2027-01-01) | 1,048,576 | not published | Yes – `response_schema` | Yes | Stronger Flash model; price doubles in January |
| Google | `gemini-2.5-flash-lite` | 0.10 | 0.40 | 1,048,576 | not published | Yes – `response_schema` | Yes | Cheapest Gemini, older generation |
| Groq | **`openai/gpt-oss-120b`** | 0.15 | 0.60 | 131,072 | ~500 tokens/s | Yes – strict `json_schema` | Yes (8K tokens/min) | **Recommended fallback** |
| Groq | `openai/gpt-oss-20b` | 0.075 | 0.30 | 131,072 | ~1,000 tokens/s | Yes – strict `json_schema` | Yes (8K tokens/min) | Fastest and cheapest; weaker world knowledge for competitors |
| Groq | `qwen/qwen3.8-27b` | 0.80 | 4.00 | 131,072 | ~450 tokens/s | Yes – `json_schema` | Yes | Preview model – not for production |

Sources: [Gemini pricing](https://aistudio.google.com/docs/pricing) · [Gemini models](https://aistudio.google.com/docs/models) · [Gemini structured output](https://aistudio.google.com/docs/structured-output) · [Groq models & pricing](https://console.groq.com/docs/models) · [Groq structured outputs](https://console.groq.com/docs/structured-outputs) · [Groq rate limits](https://console.groq.com/docs/rate-limits)

`openai/gpt-oss-120b` is an open-weights model served by Groq; it needs only a Groq key.

**Speed & accuracy:** the speed column is what the provider publishes, not a measurement. No provider publishes accuracy for this task, so the table does not claim it. Measured results from this application are in section B; "accuracy" there means judged output quality on real websites, not a formal benchmark.

### B. Measured results (this application)

| Website | Provider / model | Input tokens | Output tokens | End-to-end time | Cost at paid list price |
|---|---|---:|---:|---:|---:|
| _pending – run with API keys, see `docs/samples/`_ | | | | | |

### C. Token & cost estimate per analysis

Typical request = system prompt (~600 tokens) + response schema (~2,000 tokens) + cleaned website content (≤14,000 chars ≈ 3,500 tokens) → **≈ 4,000–6,500 input tokens**. The five-section output is **≈ 2,500–4,500 output tokens**.

| Model | Estimated cost per analysis (5k in / 4k out) at paid list price |
|---|---:|
| `openai/gpt-oss-20b` (Groq) | ≈ $0.0016 |
| `gemini-2.5-flash-lite` | ≈ $0.0021 |
| `openai/gpt-oss-120b` (Groq) | ≈ $0.0032 |
| **`gemini-3.5-flash-lite`** | **≈ $0.0115** |
| `gemini-3.8-flash` | ≈ $0.0188 |
| `qwen/qwen3.8-27b` (Groq) | ≈ $0.0200 |

On the free tiers the cost is **$0**. The API still reports `meta.token_usage.estimated_cost_usd` (what the call would cost on a paid plan) so production cost can be projected.

### D. Final recommendation

| Item | Value |
|---|---|
| Recommended provider | **Google Gemini** |
| Recommended model | **`gemini-3.5-flash-lite`** |
| Estimated input tokens | ~4,000–6,500 per website |
| Estimated output tokens | ~2,500–4,500 per website |
| Estimated cost per analysis | **$0 on the free tier**; ~$0.01 on the paid tier (≈ 90 analyses per $1) |
| Expected response time | Estimate: 10–30 s end-to-end (extraction 1–3 s + generation); see measured table |
| Fallback | **Groq `openai/gpt-oss-120b`** – automatic on rate limit, timeout, bad key or invalid output |

**Why `gemini-3.5-flash-lite`:** it is the current-generation Flash-Lite model with a free tier, a 1M-token context (large websites are never truncated by the model), native schema-constrained JSON output, and the broad world knowledge needed for competitor suggestions. `gemini-2.5-flash-lite` is cheaper but an older generation, and `gemini-3.8-flash` costs more and doubles in price on 2027-01-01.

**Why Groq as fallback:** a different provider means an outage or quota limit at Google does not take the API down. `gpt-oss-120b` supports strict JSON-schema output and is very fast on Groq hardware. Its free tier allows only ~8K tokens per minute, so the Groq request gets a smaller content budget (6,000 chars) and output cap.

**Balancing accuracy, speed and cost:** use `gemini-3.5-flash-lite` by default; switch `GEMINI_MODEL` to `gemini-3.8-flash` for premium reports where deeper competitor reasoning matters; set `LLM_PRIMARY_PROVIDER=groq` with `openai/gpt-oss-20b` when the lowest latency matters most. All of this is configuration, not code changes.

### E. Cost optimisation (implemented ✔ / recommended ➜)

* ✔ **Clean before sending** – scripts, styles, SVG, nav, footer and forms removed; only text, headings, meta and structured data are sent.
* ✔ **De-duplicate** – repeated lines across homepage and sub-pages are removed; sub-pages adding nothing new (SPA shells) are dropped.
* ✔ **Per-provider character budget** with a fair share for the homepage and each sub-page (14,000 chars for Gemini, 6,000 for Groq).
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
* Environment variables: `GEMINI_API_KEY`, `GROQ_API_KEY`

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
