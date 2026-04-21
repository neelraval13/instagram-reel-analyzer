"""Gemini video analysis provider.

Uses the native async client at `client.aio`. Translates Google's
exception types into our taxonomy so upstream callers only see
ReelAnalyzerError subclasses.
"""

import asyncio
import logging

from google import genai
from google.genai import errors as genai_errors
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.analyzer.base import BaseAnalyzer
from app.config import settings
from app.errors import (
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ReelAnalyzerError,
)

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ReelAnalyzerError) and exc.retryable


def _translate_genai_error(e: Exception) -> ReelAnalyzerError:
    """Map a google-genai APIError to our error taxonomy by HTTP code."""
    if isinstance(e, genai_errors.APIError):
        code = getattr(e, "code", None)
        msg = f"Gemini API error ({code}): {e}"
        if code == 429:
            return ProviderRateLimitError(msg)
        if code in (408, 504):
            return ProviderTimeoutError(msg)
        if code in (400, 403, 404):
            # Client-side problem; don't retry forever
            err = ProviderError(msg)
            err.retryable = False
            return err
        return ProviderError(msg)
    return ProviderError(f"Unexpected Gemini error: {e}")


class GeminiAnalyzer(BaseAnalyzer):
    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise ProviderConfigError(
                "GEMINI_API_KEY is required when using the Gemini provider"
            )
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=9),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def analyze(self, video_path: str, prompt: str) -> str:
        logger.info("gemini_analyze_start", extra={"model": self._model})

        try:
            video_file = await self._client.aio.files.upload(file=video_path)

            assert video_file.state is not None
            assert video_file.name is not None
            file_name: str = video_file.name

            while video_file.state.name == "PROCESSING":
                await asyncio.sleep(2)
                video_file = await self._client.aio.files.get(name=file_name)
                assert video_file.state is not None

            if video_file.state.name == "FAILED":
                # Gemini-side processing failure - not our bug, might succeed
                # on retry with a fresh upload.
                raise ProviderError("Gemini failed to process the video file")

            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=[video_file, prompt],
            )
        except ReelAnalyzerError:
            raise
        except asyncio.TimeoutError as e:
            raise ProviderTimeoutError(f"Gemini request timed out: {e}") from e
        except genai_errors.APIError as e:
            raise _translate_genai_error(e) from e
        except Exception as e:
            raise ProviderError(f"Unexpected Gemini failure: {e}") from e

        if response.text is None:
            raise ProviderError("Gemini returned an empty response")

        logger.info("gemini_analyze_success")
        return response.text
