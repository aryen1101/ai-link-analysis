"""Run real analyses and record measured latency / tokens / cost per provider.

Usage:  python -m scripts.benchmark https://dxora.com https://stripe.com
Writes each full response to docs/samples/<provider>-<host>.json and prints a Markdown table.
Requires GEMINI_API_KEY and/or GROQ_API_KEY in .env.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings
from app.services.analysis_service import AnalysisService

OUT = Path("docs/samples")


async def run(urls: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for provider in ("gemini", "groq"):
        settings = Settings(llm_primary_provider=provider, llm_fallback_provider="none")
        service = AnalysisService(settings)
        if not service.llm.active_providers:
            print(f"skip {provider}: no API key")
            continue
        for i, url in enumerate(urls):
            if provider == "groq" and i:
                await asyncio.sleep(65)  # Groq free tier: ~8K tokens per minute
            start = time.perf_counter()
            try:
                res = await service.analyze(url, use_cache=False)
            except Exception as exc:  # report and continue
                print(f"{provider} {url}: FAILED {getattr(exc, 'message', exc)}")
                continue
            secs = time.perf_counter() - start
            host = urlsplit(res.website).hostname.removeprefix("www.")
            (OUT / f"{provider}-{host}.json").write_text(
                json.dumps(res.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            u = res.meta.token_usage
            rows.append(f"| {host} | {res.meta.provider} / `{res.meta.model}` | {u.input_tokens:,} | "
                        f"{u.output_tokens:,} | {secs:.1f} s | ${u.estimated_cost_usd} |")
            print(rows[-1], flush=True)
    print("\n| Website | Provider / model | Input tokens | Output tokens | End-to-end time | Cost at paid list price |")
    print("|---|---|---:|---:|---:|---:|")
    print("\n".join(rows))


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:] or ["https://dxora.com"]))
