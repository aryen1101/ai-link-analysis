"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM ---
    openai_api_key: str = Field(default="", description="OpenAI API key (never commit this)")
    openai_model: str = "gpt-6-luna"
    openai_fallback_model: str = "gpt-5.4-mini"
    llm_timeout_seconds: float = 90.0
    llm_max_output_tokens: int = 8000
    # Reasoning effort sent to reasoning-capable models ("none" | "low" | "medium" ...).
    # Leave blank to use the model default. Temperature is intentionally never sent.
    openai_reasoning_effort: str = "low"

    # --- Website extraction ---
    fetch_timeout_seconds: float = 15.0
    max_response_bytes: int = 3_000_000
    max_redirects: int = 5
    max_extra_pages: int = 3
    max_content_chars: int = 14_000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 LinkAnalysisBot/1.0"
    )

    # --- Caching ---
    cache_ttl_seconds: int = 3600
    cache_max_entries: int = 256

    # --- Server ---
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
