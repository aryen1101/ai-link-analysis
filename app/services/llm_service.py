"""OpenAI integration using Structured Outputs (JSON schema generated from Pydantic).

* The response is parsed and validated directly into ``LLMAnalysis``.
* On a retryable failure (rate limit, timeout, 5xx, invalid output) the request
  is retried once with the configured fallback model.
* Token usage is returned so the API can report the real cost of every call.
"""

import logging
import time
from dataclasses import dataclass

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import LLMConfigError, LLMError

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, cached input, output). Source: developers.openai.com/api/docs/pricing,
# checked 2026-11-18. Used only to report an estimated cost per request.
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
}


@dataclass
class LLMResult:
    data: BaseModel
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    latency_ms: int

    @property
    def estimated_cost_usd(self) -> float | None:
        price = next((p for name, p in MODEL_PRICING.items() if self.model.startswith(name)), None)
        if not price:
            return None
        inp, cached, out = price
        uncached = max(self.input_tokens - self.cached_tokens, 0)
        return round((uncached * inp + self.cached_tokens * cached + self.output_tokens * out) / 1_000_000, 6)


class LLMService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: AsyncOpenAI | None = None

    def ensure_configured(self) -> None:
        if not self.settings.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not configured on the server.")

    @property
    def client(self) -> AsyncOpenAI:
        self.ensure_configured()
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url or None,
                timeout=self.settings.llm_timeout_seconds,
                max_retries=1,
            )
        return self._client

    async def structured(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        models = [self.settings.openai_model]
        if self.settings.openai_fallback_model and self.settings.openai_fallback_model not in models:
            models.append(self.settings.openai_fallback_model)

        last_error: Exception | None = None
        for model in models:
            try:
                return await self._call(model, system, user, schema)
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                raise LLMError("LLM provider rejected the API key.") from exc
            except openai.BadRequestError as exc:
                # e.g. model not available for this account / unsupported parameter -> try fallback
                logger.warning("LLM bad request on %s: %s", model, exc)
                last_error = exc
            except (
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.InternalServerError,
                openai.NotFoundError,
                openai.LengthFinishReasonError,
                ValidationError,
                LLMError,
            ) as exc:
                logger.warning("LLM call failed on %s (%s); trying fallback if available", model, type(exc).__name__)
                last_error = exc

        if isinstance(last_error, openai.RateLimitError):
            raise LLMError("LLM provider rate limit or quota exceeded. Please retry later.") from last_error
        if isinstance(last_error, openai.APITimeoutError):
            raise LLMError("LLM provider timed out.") from last_error
        raise LLMError(f"LLM analysis failed: {type(last_error).__name__}.") from last_error

    async def _call(self, model: str, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        kwargs: dict = {}
        if self.settings.openai_reasoning_effort and _supports_reasoning(model):
            kwargs["reasoning_effort"] = self.settings.openai_reasoning_effort

        start = time.perf_counter()
        completion = await self.client.chat.completions.parse(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            max_completion_tokens=self.settings.llm_max_output_tokens,
            prompt_cache_key="link-analysis-v1",
            **kwargs,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMError(f"Model refused the request: {message.refusal}")
        if message.parsed is None:
            raise LLMError("Model returned no parsable structured output.")

        usage = completion.usage
        cached = 0
        if usage and getattr(usage, "prompt_tokens_details", None):
            cached = usage.prompt_tokens_details.cached_tokens or 0
        result = LLMResult(
            data=message.parsed,
            model=completion.model or model,
            input_tokens=usage.prompt_tokens if usage else 0,
            cached_tokens=cached,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
        )
        logger.info(
            "LLM %s: %d in / %d out tokens, %d ms, ~$%s",
            result.model, result.input_tokens, result.output_tokens, latency_ms, result.estimated_cost_usd,
        )
        return result


def _supports_reasoning(model: str) -> bool:
    return model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
