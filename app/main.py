import logging
import os
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from app.analyzer import get_analyzer
from app.auth import verify_token
from app.cache import extract_shortcode, get_cache
from app.config import settings
from app.downloader import download_reel
from app.errors import ReelAnalyzerError
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


class AnalyzeResponse(BaseModel):
    success: bool
    analysis: str | None = None
    error: str | None = None
    error_type: str | None = None
    duration_seconds: float | None = None
    cache_hit: bool = False
    cache_layer: str | None = None  # "analysis" | None


def _is_strict(request: Request) -> bool:
    """Strict mode: map ReelAnalyzerError to its native HTTP status code."""
    return request.query_params.get("strict", "").lower() == "true"


def _is_nocache(request: Request) -> bool:
    """Client-requested cache bypass via ?nocache=true."""
    return request.query_params.get("nocache", "").lower() == "true"


@app.exception_handler(ReelAnalyzerError)
async def handle_reel_error(request: Request, exc: ReelAnalyzerError) -> JSONResponse:
    """Central handler for every known service error."""
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
        # Validate first so bad URLs never reach yt-dlp or the cache.
        url = validate_reel_url(str(request.url))

        # --- Cache lookup -------------------------------------------------
        cache = get_cache()
        shortcode = extract_shortcode(url)

        if cache is not None and shortcode is not None and not nocache:
            cached = await cache.get(
                shortcode=shortcode,
                provider=settings.analyzer_provider,
                model=_current_model(),
                prompt=request.prompt,
            )
            if cached is not None:
                duration = round(time.time() - start, 2)
                return AnalyzeResponse(
                    success=True,
                    analysis=cached,
                    duration_seconds=duration,
                    cache_hit=True,
                    cache_layer="analysis",
                )

        # --- Cache miss: full pipeline -----------------------------------
        video_path = await download_reel(url)
        analyzer = get_analyzer()
        result = await analyzer.analyze(video_path, request.prompt)

        # --- Cache write -------------------------------------------------
        if cache is not None and shortcode is not None:
            # Fire-and-forget: don't let a cache write failure kill a
            # successful analysis. Log and swallow.
            try:
                await cache.put(
                    shortcode=shortcode,
                    provider=settings.analyzer_provider,
                    model=_current_model(),
                    prompt=request.prompt,
                    analysis=result,
                )
            except Exception:
                logger.exception("cache_write_failed")

        duration = round(time.time() - start, 2)
        return AnalyzeResponse(
            success=True,
            analysis=result,
            duration_seconds=duration,
            cache_hit=False,
            cache_layer=None,
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


def _current_model() -> str:
    """The model identifier to use in cache keys, per configured provider."""
    provider = settings.analyzer_provider.lower()
    if provider == "gemini":
        return settings.gemini_model
    if provider == "qwen":
        return settings.qwen_model
    return provider
