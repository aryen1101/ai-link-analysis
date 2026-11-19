"""Domain exceptions mapped to HTTP status codes in ``app.main``."""


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


class InvalidURLError(AppError):
    status_code = 400
    code = "invalid_url"


class BlockedURLError(AppError):
    status_code = 400
    code = "blocked_url"


class WebsiteFetchError(AppError):
    status_code = 502
    code = "website_unreachable"


class InsufficientContentError(AppError):
    status_code = 422
    code = "insufficient_content"


class InsufficientDataError(AppError):
    """Neither the page nor public web research gave enough evidence for a grounded analysis."""

    status_code = 422
    code = "insufficient_public_data"


class LLMError(AppError):
    status_code = 503
    code = "llm_error"


class LLMConfigError(AppError):
    status_code = 500
    code = "llm_not_configured"
