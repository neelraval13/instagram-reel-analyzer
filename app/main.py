import os
import time

from fastapi import Depends, FastAPI
from pydantic import BaseModel, HttpUrl

from app.analyzer import get_analyzer
from app.auth import verify_token
from app.downloader import download_reel

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
    duration_seconds: float | None = None


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(
    request: AnalyzeRequest,
    _token: str = Depends(verify_token),
) -> AnalyzeResponse:
    start = time.time()
    video_path: str | None = None

    try:
        video_path = download_reel(str(request.url))
        analyzer = get_analyzer()
        result = analyzer.analyze(video_path, request.prompt)
        duration = round(time.time() - start, 2)

        return AnalyzeResponse(
            success=True,
            analysis=result,
            duration_seconds=duration,
        )

    except Exception as e:
        duration = round(time.time() - start, 2)
        return AnalyzeResponse(
            success=False,
            error=str(e),
            duration_seconds=duration,
        )

    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
