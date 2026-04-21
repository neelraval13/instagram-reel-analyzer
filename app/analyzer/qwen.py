"""Qwen2.5-VL video analysis via Ollama's OpenAI-compatible API.

Translates httpx exception types into our taxonomy.
"""

import asyncio
import base64
import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.analyzer.base import BaseAnalyzer
from app.config import settings
from app.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ReelAnalyzerError,
)

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ReelAnalyzerError) and exc.retryable


def _read_and_encode(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


class QwenAnalyzer(BaseAnalyzer):
    """Qwen2.5-VL via Ollama's OpenAI-compatible API."""

    def __init__(self) -> None:
        self._base_url = settings.ollama_base_url
        self._model = settings.qwen_model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=9),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def analyze(self, video_path: str, prompt: str) -> str:
        logger.info("qwen_analyze_start", extra={"model": self._model})
        video_b64 = await asyncio.to_thread(_read_and_encode, video_path)

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    json={
                        "model": self._model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "video_url",
                                        "video_url": {
                                            "url": f"data:video/mp4;base64,{video_b64}"
                                        },
                                    },
                                    {"type": "text", "text": prompt},
                                ],
                            }
                        ],
                    },
                )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Qwen request timed out: {e}") from e
        except httpx.RequestError as e:
            # Network-level error (DNS, connection refused, etc.)
            raise ProviderError(f"Qwen network error: {e}") from e

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            msg = f"Qwen HTTP {status}: {e.response.text[:200]}"
            if status == 429:
                raise ProviderRateLimitError(msg) from e
            if status in (408, 504):
                raise ProviderTimeoutError(msg) from e
            if 500 <= status < 600:
                raise ProviderError(msg) from e
            # 4xx other than 429 - likely a config/request problem, don't retry
            err = ProviderError(msg)
            err.retryable = False
            raise err from e

        try:
            data = response.json()
            text: str = data["choices"][0]["message"]["content"]
        except (KeyError, ValueError, TypeError) as e:
            raise ProviderError(f"Malformed Qwen response: {e}") from e

        if not text:
            raise ProviderError("Qwen returned an empty response")

        logger.info("qwen_analyze_success")
        return text
