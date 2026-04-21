import asyncio
import os
import tempfile

import yt_dlp


def _download_sync(url: str) -> str:
    """Blocking yt-dlp download; called via asyncio.to_thread."""
    fd, output_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    os.remove(output_path)

    ydl_opts = {
        "outtmpl": output_path,
        "format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        ydl.download([url])

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Download failed: file missing or empty at {output_path}")

    return output_path


async def download_reel(url: str) -> str:
    """Download an Instagram reel and return the file path.

    yt-dlp is synchronous, so we run it in a worker thread to avoid
    blocking the event loop.
    """
    return await asyncio.to_thread(_download_sync, url)
