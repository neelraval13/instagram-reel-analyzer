"""Instagram reel download via yt-dlp.

yt-dlp is synchronous and somewhat flaky (Instagram breaks extractors
every few weeks), so we:

    1. Run it in a worker thread so it doesn't block the event loop.
    2. Retry transient failures with exponential backoff.
    3. Translate yt-dlp's exception types into our own taxonomy so
       callers never need to know which downloader we're using.
"""

import asyncio
import logging
import os
import tempfile

import yt_dlp
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# yt-dlp re-exports DownloadError at the package root; using this
# path keeps type checkers (Pyright/Pylance) happy while resolving
# to the same class as yt_dlp.utils.DownloadError at runtime.
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from app.errors import DownloadError, ReelAnalyzerError

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Tenacity predicate: retry only errors marked retryable."""
    return isinstance(exc, ReelAnalyzerError) and exc.retryable


def _download_sync(url: str) -> str:
    """Blocking yt-dlp download. Called via asyncio.to_thread."""
    fd, output_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    os.remove(output_path)

    ydl_opts = {
        "outtmpl": output_path,
        "format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            ydl.download([url])
    except YtDlpDownloadError as e:
        raise DownloadError(f"yt-dlp failed: {e}") from e
    except Exception as e:
        # Any other failure from yt-dlp's innards - treat as retryable
        # download error rather than letting it leak out as a bare Exception.
        raise DownloadError(f"Unexpected download failure: {e}") from e

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise DownloadError(f"Download produced no output at {output_path}")

    return output_path


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=9),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def download_reel(url: str) -> str:
    """Download an Instagram reel and return the file path.

    Retries transient failures up to 3 times with 1s/3s/9s backoff.
    Non-retryable errors (if any reach this layer) bubble up immediately.
    """
    logger.info("reel_download_start", extra={"url": url})
    try:
        path = await asyncio.to_thread(_download_sync, url)
    except DownloadError:
        logger.warning("reel_download_attempt_failed", extra={"url": url})
        raise
    logger.info("reel_download_success", extra={"url": url, "path": path})
    return path
