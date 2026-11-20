"""LLM integration with two providers: Google Gemini (primary) and Groq (fallback).

* Both providers are asked for schema-constrained JSON generated from the same
  Pydantic model, and every response is validated with Pydantic.
* Providers are tried in order (``LLM_PRIMARY_PROVIDER`` then ``LLM_FALLBACK_PROVIDER``);
  only providers with an API key configured are used.
* Each provider has its own content budget, because Groq's free tier allows only
  ~8K tokens per minute while Gemini accepts a 1M-token context.
* Token usage is returned so the API can report the cost of every call.
"""

import asyncio
import copy
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import LLMConfigError, LLMError

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output) at paid-tier list prices, checked 2026-11-19:
#   Gemini: aistudio.google.com/docs/pricing   Groq: console.groq.com/docs/models
# Both providers also offer a free tier; the figure shows what the call would cost on a paid plan.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI: developers.openai.com/api/docs/pricing
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-6.1-terra": (1.00, 5.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.8-flash": (0.75, 3.75),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.80, 4.00),
}


@dataclass
class LLMResult:
    data: BaseModel
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    content_chars: int = 0

    @property
    def estimated_cost_usd(self) -> float | None:
        price = MODEL_PRICING.get(self.model)
        if not price:
            return None
        return round((self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000, 6)


class ProviderError(Exception):
    """A provider-level failure; ``fatal`` errors (bad key) are still reported but the next provider is tried."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.message = message
        self.fatal = fatal


class LLMProvider(ABC):
    name: str

    def __init__(self, api_key: str, model: str, max_content_chars: int, max_output_tokens: int, timeout: float):
        self.api_key = api_key
        self.model = model
        self.max_content_chars = max_content_chars
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    @abstractmethod
    async def generate(self, system: str, user: str, schema: type[BaseModel]) -> tuple[BaseModel, int, int, str]:
        """Return (parsed model, input tokens, output tokens, model name)."""


# --------------------------------------------------------------------- Gemini
class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions with Structured Outputs (strict JSON schema from the Pydantic model)."""

    name = "openai"

    def __init__(self, *args, reasoning_effort: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.reasoning_effort = reasoning_effort
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout, max_retries=1)
        return self._client

    async def generate(self, system, user, schema):
        import openai

        kwargs: dict = {}
        if self.reasoning_effort and self.model.startswith(("gpt-5", "gpt-6", "o")):
            kwargs["reasoning_effort"] = self.reasoning_effort
        try:
            completion = await self._get_client().chat.completions.parse(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
                max_completion_tokens=self.max_output_tokens,
                prompt_cache_key="link-analysis-v2",
                **kwargs,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise ProviderError("OpenAI rejected the API key.", fatal=True) from exc
        except openai.RateLimitError as exc:
            raise ProviderError("OpenAI rate limit or quota exceeded.") from exc
        except openai.APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out.") from exc
        except openai.LengthFinishReasonError as exc:
            raise ProviderError("OpenAI output was truncated (max tokens reached).") from exc
        except openai.APIStatusError as exc:
            raise ProviderError(f"OpenAI request failed (HTTP {exc.status_code}).") from exc
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI API error: {exc.__class__.__name__}.") from exc
        except ValidationError as exc:
            raise ProviderError(f"openai returned JSON that does not match the schema ({exc.error_count()} errors).") from exc

        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            raise ProviderError(f"OpenAI refused the request: {message.refusal}")
        if message.parsed is None:
            raise ProviderError("OpenAI returned no parsable structured output.")
        usage = completion.usage
        return message.parsed, (usage.prompt_tokens if usage else 0), (usage.completion_tokens if usage else 0), self.model


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, *args, thinking_budget: int | None = None, api_version: str = "v1beta", **kwargs):
        super().__init__(*args, **kwargs)
        self.thinking_budget = thinking_budget
        self.api_version = api_version
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.timeout * 1000),
                    api_version=self.api_version or None,
                    # One quick retry for transient overload; quota errors (429) go straight to the fallback.
                    retry_options=types.HttpRetryOptions(
                        attempts=2, initial_delay=2.0, max_delay=4.0, http_status_codes=[500, 502, 503, 504]
                    ),
                ),
            )
        return self._client

    async def generate(self, system, user, schema):
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=self.max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if self.thinking_budget is not None:
            config.thinking_config = types.ThinkingConfig(thinking_budget=self.thinking_budget)

        try:
            resp = await self._get_client().aio.models.generate_content(
                model=self.model, contents=user, config=config
            )
        except errors.ClientError as exc:
            code = getattr(exc, "code", None)
            if code in (401, 403) or "API_KEY_INVALID" in str(exc):
                raise ProviderError("Gemini rejected the API key.", fatal=True) from exc
            if code == 429:
                raise ProviderError("Gemini rate limit or free-tier quota exceeded.") from exc
            if code == 404:
                raise ProviderError(f"Gemini model '{self.model}' is not available.") from exc
            raise ProviderError(f"Gemini request failed (HTTP {code}).") from exc
        except errors.ServerError as exc:
            raise ProviderError(f"Gemini server error (HTTP {getattr(exc, 'code', '?')}).") from exc
        except errors.APIError as exc:
            raise ProviderError(f"Gemini API error: {exc.__class__.__name__}.") from exc
        except Exception as exc:  # network errors / timeouts raised by the underlying HTTP client
            raise ProviderError(f"Gemini request failed: {exc.__class__.__name__}.") from exc

        text = resp.text or ""
        if not text:
            reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
            raise ProviderError(f"Gemini returned no content (finish reason: {reason}).")
        parsed = _validate(schema, text, self.name)

        usage = resp.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0
        return parsed, input_tokens, output_tokens, self.model


# ----------------------------------------------------------------------- Groq
class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, *args, reasoning_effort: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.reasoning_effort = reasoning_effort
        self._client = None

    def _get_client(self):
        if self._client is None:
            from groq import AsyncGroq

            self._client = AsyncGroq(api_key=self.api_key, timeout=self.timeout, max_retries=1)
        return self._client

    async def generate(self, system, user, schema):
        import groq

        kwargs: dict = {}
        if self.reasoning_effort and "gpt-oss" in self.model:
            kwargs["reasoning_effort"] = self.reasoning_effort
        request = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_json_schema(schema)},
            },
            max_completion_tokens=self.max_output_tokens,
            **kwargs,
        )
        # Groq occasionally rejects its own generation with HTTP 400 "json_validate_failed"
        # (documented behaviour of structured outputs); one retry usually succeeds.
        for attempt in (1, 2):
            try:
                resp = await self._get_client().chat.completions.create(**request)
                break
            except groq.AuthenticationError as exc:
                raise ProviderError("Groq rejected the API key.", fatal=True) from exc
            except groq.RateLimitError as exc:
                raise ProviderError("Groq rate limit or free-tier token limit exceeded.") from exc
            except groq.APITimeoutError as exc:
                raise ProviderError("Groq request timed out.") from exc
            except groq.APIStatusError as exc:
                detail = _groq_error_code(exc)
                if attempt == 1 and exc.status_code == 400 and detail == "json_validate_failed":
                    logger.info("Groq output failed schema validation; retrying once")
                    continue
                logger.warning("Groq HTTP %s: %s", exc.status_code, str(exc)[:300])
                raise ProviderError(f"Groq request failed (HTTP {exc.status_code}, {detail or 'no detail'}).") from exc
            except groq.APIError as exc:
                raise ProviderError(f"Groq API error: {exc.__class__.__name__}.") from exc

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise ProviderError("Groq output was truncated (max tokens reached).")
        parsed = _validate(schema, choice.message.content or "", self.name)
        usage = resp.usage
        return parsed, (usage.prompt_tokens if usage else 0), (usage.completion_tokens if usage else 0), self.model


# -------------------------------------------------------------------- service
class LLMService:
    def __init__(self, settings: Settings, providers: list[LLMProvider] | None = None):
        self.settings = settings
        self.providers = providers if providers is not None else self._build_providers(settings)

    @staticmethod
    def _build_providers(s: Settings) -> list[LLMProvider]:
        available: dict[str, LLMProvider] = {
            "openai": OpenAIProvider(
                s.openai_api_key, s.openai_model, s.openai_max_content_chars, s.openai_max_output_tokens,
                s.llm_timeout_seconds, reasoning_effort=s.openai_reasoning_effort,
            ),
            "gemini": GeminiProvider(
                s.gemini_api_key, s.gemini_model, s.gemini_max_content_chars, s.gemini_max_output_tokens,
                s.llm_timeout_seconds, thinking_budget=s.gemini_thinking_budget, api_version=s.gemini_api_version,
            ),
            "groq": GroqProvider(
                s.groq_api_key, s.groq_model, s.groq_max_content_chars, s.groq_max_output_tokens,
                s.llm_timeout_seconds, reasoning_effort=s.groq_reasoning_effort,
            ),
        }
        order = [p.strip().lower() for p in s.llm_providers.split(",") if p.strip()]
        chain = []
        for name in order:
            if name in available and available[name] not in chain:
                chain.append(available[name])
        return chain

    @property
    def active_providers(self) -> list[LLMProvider]:
        return [p for p in self.providers if p.configured]

    def describe(self) -> str:
        return " -> ".join(f"{p.name}:{p.model}" for p in self.active_providers) or "none configured"

    def ensure_configured(self) -> None:
        if not self.active_providers:
            raise LLMConfigError("No LLM API key configured. Set GEMINI_API_KEY and/or GROQ_API_KEY on the server.")

    async def structured(
        self, system: str, build_user: Callable[[int], str], schema: type[BaseModel]
    ) -> LLMResult:
        """Run the prompt on each configured provider in order until one returns valid output.

        ``build_user(max_chars)`` builds the user prompt within that provider's content budget.
        """
        self.ensure_configured()
        failures: list[str] = []
        all_fatal = True
        for provider in self.active_providers:
            user = build_user(provider.max_content_chars)
            start = time.perf_counter()
            try:
                # Hard deadline: SDK-level timeouts are per network read and can be exceeded.
                data, in_tok, out_tok, model = await asyncio.wait_for(
                    provider.generate(system, user, schema), timeout=provider.timeout
                )
            except asyncio.TimeoutError:
                logger.warning("LLM provider %s (%s) timed out after %ss", provider.name, provider.model, provider.timeout)
                failures.append(f"{provider.name}: timed out after {provider.timeout:.0f}s")
                all_fatal = False
                continue
            except ProviderError as exc:
                logger.warning("LLM provider %s (%s) failed: %s", provider.name, provider.model, exc.message)
                failures.append(f"{provider.name}: {exc.message}")
                all_fatal = all_fatal and exc.fatal
                continue
            result = LLMResult(
                data=data, provider=provider.name, model=model, input_tokens=in_tok, output_tokens=out_tok,
                latency_ms=int((time.perf_counter() - start) * 1000), content_chars=len(user),
            )
            logger.info(
                "LLM %s/%s: %d in / %d out tokens, %d ms, ~$%s at paid list price",
                result.provider, result.model, in_tok, out_tok, result.latency_ms, result.estimated_cost_usd,
            )
            return result

        prefix = "LLM provider rejected the API key" if all_fatal else "LLM analysis failed"
        raise LLMError(f"{prefix} ({'; '.join(failures)}).")


# -------------------------------------------------------------------- helpers
def _validate(schema: type[BaseModel], text: str, provider: str) -> BaseModel:
    try:
        return schema.model_validate_json(_strip_fences(text))
    except ValidationError as exc:
        raise ProviderError(f"{provider} returned JSON that does not match the schema ({exc.error_count()} errors).") from exc


def _groq_error_code(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("code") or err.get("type") or "")
    return ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema adapted to strict structured-output rules.

    Every object gets ``additionalProperties: false`` and lists all properties as
    required; ``const`` is rewritten as a one-value ``enum``; defaults are removed.
    """
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if "const" in node:
                node["enum"] = [node.pop("const")]
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


__all__ = ["LLMService", "LLMResult", "LLMProvider", "GeminiProvider", "GroqProvider", "ProviderError",
           "strict_json_schema", "MODEL_PRICING"]
