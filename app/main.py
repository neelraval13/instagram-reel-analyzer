import json
import logging
import os
import time
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from app.analyzer import get_analyzer
from app.auth import verify_token
from app.cache import FREEFORM_MODE, extract_shortcode, get_cache
from app.config import settings
from app.downloader import download_reel
from app.errors import ReelAnalyzerError
from app.schemas import SCHEMA_VERSION, ReelAnalysis
from app.validators import validate_reel_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Reel Analyzer",
    description="Analyze Instagram Reels with AI",
)


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
        both `analysis` (string) is populated and `structured_available`
        is False with a `fallback_reason` string. Clients that asked for
        structured can detect this and render the freeform instead.
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


def _is_strict(request: Request) -> bool:
    return request.query_params.get("strict", "").lower() == "true"


def _is_nocache(request: Request) -> bool:
    return request.query_params.get("nocache", "").lower() == "true"


@app.exception_handler(ReelAnalyzerError)
async def handle_reel_error(request: Request, exc: ReelAnalyzerError) -> JSONResponse:
    logger.warning(
        "request_failed",
        extra={
            "error_type": exc.__class__.__name__,
            "error": str(exc),
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


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: AnalyzeRequest,
    fastapi_request: Request,
    _token: str = Depends(verify_token),
) -> AnalyzeResponse:
    start = time.time()
    video_path: str | None = None
    nocache = _is_nocache(fastapi_request)

    try:
        url = validate_reel_url(str(request.url))

        analyzer = get_analyzer()
        # Decide what mode we can actually serve. If the client asked
        # for structured and the provider can't, we fall back to
        # freeform and flag it in the response.
        wants_structured = request.structured
        will_serve_structured = wants_structured and analyzer.supports_structured
        fallback_reason: str | None = None
        if wants_structured and not will_serve_structured:
            fallback_reason = (
                f"Provider {settings.analyzer_provider!r} does not support "
                "structured output; returning freeform instead."
            )
            logger.info(
                "structured_fallback",
                extra={"provider": settings.analyzer_provider},
            )

        output_mode = SCHEMA_VERSION if will_serve_structured else FREEFORM_MODE

        # --- Cache lookup -----------------------------------------------
        cache = get_cache()
        shortcode = extract_shortcode(url)

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

        # --- Cache miss: full pipeline ----------------------------------
        video_path = await download_reel(url)

        if will_serve_structured:
            structured_result = await analyzer.analyze_structured(
                video_path, request.prompt
            )
            analysis_str: str | None = None
            structured_dict: dict[str, Any] | None = structured_result.model_dump(
                mode="json"
            )
            # Cache the JSON-serialized form. Pydantic's model_dump_json
            # handles Enum/datetime serialization for us.
            cache_value = structured_result.model_dump_json()
        else:
            freeform = await analyzer.analyze(video_path, request.prompt)
            analysis_str = freeform
            structured_dict = None
            cache_value = freeform

        # --- Cache write ------------------------------------------------
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
                logger.exception("cache_write_failed")

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
    """Turn a cached-string result back into the right response shape."""
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

    # Structured: cached value is JSON - parse and re-validate so we
    # never ship malformed data to clients even if the cache is corrupt.
    try:
        data = json.loads(cached)
        validated = ReelAnalysis.model_validate(data)
    except Exception:
        logger.exception("cache_value_corrupt")
        # Treat as cache miss - return error path and let the retry
        # happen naturally on the next request.
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
