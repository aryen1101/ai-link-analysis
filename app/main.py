"""FastAPI entrypoint: ``uvicorn app.main:app --host 0.0.0.0 --port 4000``"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.errors import AppError
from app.schemas import ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("link-analysis")


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = AnalysisService(settings)
    app.state.analysis = service
    if not service.llm.active_providers:
        logger.warning("No GEMINI_API_KEY or GROQ_API_KEY set - /link-analysis will return llm_not_configured")
    logger.info("LLM provider chain: %s", service.llm.describe())
    yield


app = FastAPI(
    title="AI Website Link Analysis API",
    version="1.0.0",
    description=(
        "Analyses a website with an LLM and returns competitor, search-query, SEO, services "
        "and target-audience insights as structured JSON."
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


def _error(
    status: int, code: str, message: str, website: str | None = None, details: dict | None = None
) -> JSONResponse:
    body = ErrorResponse(website=website, error={"code": code, "message": message}, details=details)
    return JSONResponse(status_code=status, content=body.model_dump(exclude_none=True))


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    logger.warning("%s: %s", exc.code, exc.message)
    return _error(exc.status_code, exc.code, exc.message, getattr(request.state, "website", None), exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = f"{'.'.join(str(p) for p in first.get('loc', []))}: {first.get('msg', 'invalid request')}"
    return _error(422, "invalid_request", msg)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return _error(500, "internal_error", "An unexpected error occurred.", getattr(request.state, "website", None))


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/health", tags=["system"])
async def health(request: Request):
    providers = request.app.state.analysis.llm.active_providers
    return {
        "status": "ok",
        "llm_configured": bool(providers),
        "llm_providers": [{"provider": p.name, "model": p.model} for p in providers],
    }


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    tags=["analysis"],
    summary="Analyse a website URL",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or blocked URL (SSRF protection)"},
        422: {"model": ErrorResponse, "description": "Invalid request body or not enough content"},
        502: {"model": ErrorResponse, "description": "Website unreachable or blocked access"},
        503: {"model": ErrorResponse, "description": "LLM provider error"},
    },
)
async def link_analysis(
    payload: LinkAnalysisRequest,
    request: Request,
    refresh: bool = Query(False, description="Bypass the cache and re-run the analysis"),
) -> LinkAnalysisResponse:
    request.state.website = payload.url
    service: AnalysisService = request.app.state.analysis
    return await service.analyze(payload.url, use_cache=not refresh)
