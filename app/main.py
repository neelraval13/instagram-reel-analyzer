import json
import logging
import os
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.analyzer import get_analyzer
from app.auth import verify_api_key
from app.cache import FREEFORM_MODE, extract_shortcode, get_cache
from app.config import settings
from app.downloader import download_reel
from app.errors import ReelAnalyzerError
from app.keys import AuthContext
from app.logging_config import (
    Event,
    bind_request_context,
    clear_request_context,
    configure_logging,
)
from app.rate_limit import RateLimitExceeded, get_rate_limiter
from app.schemas import SCHEMA_VERSION, ReelAnalysis
from app.validators import validate_reel_url

# Configure logging FIRST so even import-time errors come out as JSON.
configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Reel Analyzer",
    description="Analyze Instagram Reels with AI",
)


# --- Middleware ------------------------------------------------------------


def _generate_request_id() -> str:
    """8-char hex request identifier."""
    return secrets.token_hex(4)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Generate a request ID, bind it to log context, time the request,
    add an X-Request-ID response header, and emit start/completed events.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        request_id = _generate_request_id()
        start = time.time()

        # Bind context BEFORE any handler code runs, so every log line
        # emitted during this request automatically carries request_id
        # and the request basics.
        bind_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            user_id=None,  # populated once per-user keys land
        )

        logger.info(Event.REQUEST_STARTED)

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.time() - start) * 1000)
            logger.exception(
                Event.REQUEST_FAILED,
                extra={"duration_ms": duration_ms, "status": "exception"},
            )
            clear_request_context()
            raise

        duration_ms = int((time.time() - start) * 1000)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            Event.REQUEST_COMPLETED,
            extra={
                "duration_ms": duration_ms,
                "status_code": response.status_code,
            },
        )
        clear_request_context()
        return response


app.add_middleware(RequestContextMiddleware)


# --- Models ----------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    url: HttpUrl
    prompt: str
    structured: bool = False


class AnalyzeResponse(BaseModel):
    """Response shape.

    In freeform mode:  `analysis` is a string, `analysis_structured` is None.
    In structured mode (when supported): `analysis_structured` is a dict,
                                         `analysis` is None.
    Hybrid fallback (structured requested but provider can't):
        `analysis` (string) is populated and `structured_available`
        is False with a `fallback_reason` string.
    """

    success: bool
    analysis: str | None = None
    analysis_structured: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None
    duration_seconds: float | None = None
    cache_hit: bool = False
    cache_layer: str | None = None
    structured_available: bool = True
    fallback_reason: str | None = None


# --- Query param helpers ---------------------------------------------------


def _is_strict(request: Request) -> bool:
    return request.query_params.get("strict", "").lower() == "true"


def _is_nocache(request: Request) -> bool:
    return request.query_params.get("nocache", "").lower() == "true"


# --- Exception handler -----------------------------------------------------


@app.exception_handler(ReelAnalyzerError)
async def handle_reel_error(request: Request, exc: ReelAnalyzerError) -> JSONResponse:
    logger.warning(
        Event.REQUEST_FAILED,
        extra={
            "error_type": exc.__class__.__name__,
            "error_message": str(exc),
            "retryable": exc.retryable,
        },
    )
    status = exc.http_status if _is_strict(request) else 200
    return JSONResponse(
        status_code=status,
        content={
            "success": False,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        },
    )


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning(
        "rate_limit_exceeded",
        extra={
            "user_id": exc.user_id,
            "kind": exc.kind,
            "limit": exc.limit,
            "current": exc.current,
            "retry_after_seconds": exc.retry_after_seconds,
        },
    )
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": (
                f"Rate limit exceeded ({exc.kind}): "
                f"{exc.current} requests, limit {exc.limit}. "
                f"Retry in {exc.retry_after_seconds}s."
            ),
            "error_type": "RateLimitExceeded",
            "limit_kind": exc.kind,
            "limit": exc.limit,
            "current": exc.current,
            "retry_after_seconds": exc.retry_after_seconds,
        },
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


# --- Rate limit dependency ------------------------------------------------


async def enforce_rate_limit(
    auth: AuthContext = Depends(verify_api_key),
) -> AuthContext:
    """Increment + check the caller's rate limit counters.

    Raises RateLimitExceeded (mapped to 429) if either limit is over.
    Returns AuthContext on success so the endpoint can keep using it -
    FastAPI dedupes Depends(verify_api_key) within one request, so this
    is the same AuthContext the endpoint sees.
    """
    status = await get_rate_limiter().check_and_increment(auth.user_id)
    bind_request_context(
        rate_limit_minute=status.minute_count,
        rate_limit_day=status.day_count,
    )
    return auth


# --- Endpoint --------------------------------------------------------------


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: AnalyzeRequest,
    fastapi_request: Request,
    auth: AuthContext = Depends(enforce_rate_limit),
) -> AnalyzeResponse:
    start = time.time()
    video_path: str | None = None
    nocache = _is_nocache(fastapi_request)

    # Re-bind user_id now that auth has resolved. The middleware bound it
    # as None earlier; this overwrites with the actual value.
    bind_request_context(
        user_id=auth.user_id,
        key_id=auth.key_id,
        is_legacy_auth=auth.is_legacy,
    )

    try:
        url = validate_reel_url(str(request.url))

        analyzer = get_analyzer()
        wants_structured = request.structured
        will_serve_structured = wants_structured and analyzer.supports_structured
        fallback_reason: str | None = None
        if wants_structured and not will_serve_structured:
            fallback_reason = (
                f"Provider {settings.analyzer_provider!r} does not support "
                "structured output; returning freeform instead."
            )
            logger.info(
                Event.PROVIDER_FALLBACK,
                extra={
                    "from_mode": "structured",
                    "to_mode": "freeform",
                    "provider": settings.analyzer_provider,
                },
            )

        output_mode = SCHEMA_VERSION if will_serve_structured else FREEFORM_MODE

        # Add per-request fields to log context so subsequent log lines
        # in this request carry them too.
        shortcode = extract_shortcode(url)
        bind_request_context(
            shortcode=shortcode,
            output_mode=output_mode,
            provider=settings.analyzer_provider,
        )

        # --- Cache lookup ----------------------------------------------
        cache = get_cache()
        if cache is not None and shortcode is not None and not nocache:
            cached = await cache.get(
                shortcode=shortcode,
                provider=settings.analyzer_provider,
                model=_current_model(),
                prompt=request.prompt,
                output_mode=output_mode,
            )
            if cached is not None:
                duration = round(time.time() - start, 2)
                return _build_cached_response(
                    cached=cached,
                    output_mode=output_mode,
                    duration=duration,
                    structured_available=will_serve_structured,
                    fallback_reason=fallback_reason,
                )
            else:
                logger.info(
                    Event.CACHE_MISS,
                    extra={"reason": "no_entry"},
                )
        elif nocache:
            logger.info(Event.CACHE_MISS, extra={"reason": "client_bypass"})

        # --- Cache miss: full pipeline ---------------------------------
        video_path = await download_reel(url)

        if will_serve_structured:
            structured_result = await analyzer.analyze_structured(
                video_path, request.prompt
            )
            analysis_str: str | None = None
            structured_dict: dict[str, Any] | None = structured_result.model_dump(
                mode="json"
            )
            cache_value = structured_result.model_dump_json()
        else:
            freeform = await analyzer.analyze(video_path, request.prompt)
            analysis_str = freeform
            structured_dict = None
            cache_value = freeform

        if cache is not None and shortcode is not None:
            try:
                await cache.put(
                    shortcode=shortcode,
                    provider=settings.analyzer_provider,
                    model=_current_model(),
                    prompt=request.prompt,
                    analysis=cache_value,
                    output_mode=output_mode,
                )
            except Exception:
                logger.exception(Event.CACHE_WRITE_FAILED)

        duration = round(time.time() - start, 2)
        return AnalyzeResponse(
            success=True,
            analysis=analysis_str,
            analysis_structured=structured_dict,
            duration_seconds=duration,
            cache_hit=False,
            cache_layer=None,
            structured_available=will_serve_structured,
            fallback_reason=fallback_reason,
        )

    except ReelAnalyzerError:
        raise
    except Exception:
        logger.exception("unexpected_error")
        duration = round(time.time() - start, 2)
        return AnalyzeResponse(
            success=False,
            error="Internal server error",
            error_type="InternalError",
            duration_seconds=duration,
        )

    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)


def _build_cached_response(
    cached: str,
    output_mode: str,
    duration: float,
    structured_available: bool,
    fallback_reason: str | None,
) -> AnalyzeResponse:
    if output_mode == FREEFORM_MODE:
        return AnalyzeResponse(
            success=True,
            analysis=cached,
            duration_seconds=duration,
            cache_hit=True,
            cache_layer="analysis",
            structured_available=structured_available,
            fallback_reason=fallback_reason,
        )

    try:
        data = json.loads(cached)
        validated = ReelAnalysis.model_validate(data)
    except Exception:
        logger.exception("cache_value_corrupt")
        raise

    return AnalyzeResponse(
        success=True,
        analysis_structured=validated.model_dump(mode="json"),
        duration_seconds=duration,
        cache_hit=True,
        cache_layer="analysis",
        structured_available=True,
        fallback_reason=None,
    )


def _current_model() -> str:
    provider = settings.analyzer_provider.lower()
    if provider == "gemini":
        return settings.gemini_model
    if provider == "qwen":
        return settings.qwen_model
    return provider
