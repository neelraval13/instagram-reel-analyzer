import logging
import os
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from app.analyzer import get_analyzer
from app.auth import verify_token
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


def _is_strict(request: Request) -> bool:
    """Strict mode: map ReelAnalyzerError to its native HTTP status code.

    Default (non-strict) returns 200 with success:false - this preserves
    backwards compatibility with the existing iOS Shortcut, which only
    checks the JSON body.
    """
    return request.query_params.get("strict", "").lower() == "true"


@app.exception_handler(ReelAnalyzerError)
async def handle_reel_error(request: Request, exc: ReelAnalyzerError) -> JSONResponse:
    """Central handler for every known service error.

    Logs once, renders consistently, respects ?strict= for status code.
    """
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
    _token: str = Depends(verify_token),
) -> AnalyzeResponse:
    start = time.time()
    video_path: str | None = None

    try:
        # Validate first so bad URLs never reach yt-dlp.
        url = validate_reel_url(str(request.url))

        video_path = await download_reel(url)
        analyzer = get_analyzer()
        result = await analyzer.analyze(video_path, request.prompt)
        duration = round(time.time() - start, 2)

        return AnalyzeResponse(
            success=True,
            analysis=result,
            duration_seconds=duration,
        )

    except ReelAnalyzerError:
        # Known errors - let the exception handler format them consistently.
        raise
    except Exception as e:
        # Genuine bugs (not domain errors). Log with stack trace and return
        # a generic 500-equivalent so we don't leak internals to clients.
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
