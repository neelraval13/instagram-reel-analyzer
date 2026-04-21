"""Gemini video analysis provider.

Uses the native async client at `client.aio`. Supports both freeform
text output and schema-guided structured output via Gemini's
response_schema feature.
"""

import asyncio
import json
import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
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
from app.schemas import ReelAnalysis

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
            err = ProviderError(msg)
            err.retryable = False
            return err
        return ProviderError(msg)
    return ProviderError(f"Unexpected Gemini error: {e}")


class GeminiAnalyzer(BaseAnalyzer):
    supports_structured = True

    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise ProviderConfigError(
                "GEMINI_API_KEY is required when using the Gemini provider"
            )
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    async def _upload_and_wait(self, video_path: str) -> genai_types.File:
        """Upload a file and block until Gemini finishes processing it.

        Shared between freeform and structured paths so we don't
        duplicate the polling loop.
        """
        video_file = await self._client.aio.files.upload(file=video_path)

        assert video_file.state is not None
        assert video_file.name is not None
        file_name: str = video_file.name

        while video_file.state.name == "PROCESSING":
            await asyncio.sleep(2)
            video_file = await self._client.aio.files.get(name=file_name)
            assert video_file.state is not None

        if video_file.state.name == "FAILED":
            raise ProviderError("Gemini failed to process the video file")

        return video_file

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=9),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def analyze(self, video_path: str, prompt: str) -> str:
        logger.info("gemini_analyze_start", extra={"model": self._model})

        try:
            video_file = await self._upload_and_wait(video_path)
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=9),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def analyze_structured(self, video_path: str, prompt: str) -> ReelAnalysis:
        logger.info(
            "gemini_analyze_structured_start",
            extra={"model": self._model},
        )

        try:
            video_file = await self._upload_and_wait(video_path)
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=[video_file, prompt],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ReelAnalysis,
                ),
            )
        except ReelAnalyzerError:
            raise
        except asyncio.TimeoutError as e:
            raise ProviderTimeoutError(f"Gemini request timed out: {e}") from e
        except genai_errors.APIError as e:
            raise _translate_genai_error(e) from e
        except Exception as e:
            raise ProviderError(f"Unexpected Gemini failure: {e}") from e

        # The SDK usually hands us a parsed Pydantic instance via .parsed,
        # but fall back to manually parsing .text if it's not populated.
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, ReelAnalysis):
            logger.info("gemini_analyze_structured_success")
            return parsed

        if response.text is None:
            raise ProviderError("Gemini returned an empty structured response")

        try:
            data = json.loads(response.text)
            result = ReelAnalysis.model_validate(data)
        except (json.JSONDecodeError, ValueError) as e:
            # Schema-guided decoding should prevent this, but defend against
            # the rare edge case where the model still produces invalid JSON.
            raise ProviderError(
                f"Gemini returned malformed structured output: {e}"
            ) from e

        logger.info("gemini_analyze_structured_success")
        return result
