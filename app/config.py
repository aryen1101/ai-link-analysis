"""Application configuration loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM providers (tried in this order; only providers with a key are used) ---
    llm_primary_provider: str = "gemini"
    llm_fallback_provider: str = "groq"
    # Hard per-provider deadline; after it the next provider is tried.
    llm_timeout_seconds: float = 60.0

    # Google Gemini - https://aistudio.google.com/apikey (free tier available)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    # v1beta supports system instructions + response schemas for every current model.
    gemini_api_version: str = "v1beta"
    gemini_max_output_tokens: int = 8192
    gemini_max_content_chars: int = 14_000
    # Optional thinking budget in tokens (0 disables thinking where supported). None = model default.
    gemini_thinking_budget: int | None = None

    # Groq - https://console.groq.com/keys (free tier available)
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    # Free tier allows ~8K tokens/minute (prompt + output), so Groq gets a smaller budget.
    groq_max_output_tokens: int = 4400
    groq_max_content_chars: int = 5_000
    groq_reasoning_effort: str = "low"

    # --- Web research (Gemini + Google Search grounding) ---
    # Used when the page itself has too little evidence: social profiles behind a login wall,
    # sites that block automated requests, empty JavaScript shells.
    web_research_enabled: bool = True
    # Tried in order; providers without an API key are skipped.
    #   groq   = gpt-oss + built-in browser_search tool (free tier ~1K requests/day)
    #   gemini = Google Search grounding (free tier only ~20 requests/day per model)
    research_providers: str = "groq,gemini"
    research_groq_model: str = "openai/gpt-oss-120b"
    research_gemini_model: str = "gemini-2.5-flash-lite"
    research_timeout_seconds: float = 30.0
    # Fetch and analyse the brand's official website when research names one.
    follow_official_website: bool = True
    # Evidence thresholds (characters of page-specific text, see website_extractor.evidence_chars)
    min_evidence_chars: int = 300     # below: no analysis without reliable web research
    good_evidence_chars: int = 2000   # below: page content is supplemented with web research

    # --- Website extraction ---
    fetch_timeout_seconds: float = 15.0
    max_response_bytes: int = 3_000_000
    max_redirects: int = 5
    max_extra_pages: int = 3
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
