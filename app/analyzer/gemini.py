import time

from google import genai

from app.analyzer.base import BaseAnalyzer
from app.config import settings


class GeminiAnalyzer(BaseAnalyzer):
    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is required when using the Gemini provider"
            )
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    def analyze(self, video_path: str, prompt: str) -> str:
        video_file = self._client.files.upload(file=video_path)

        assert video_file.state is not None
        assert video_file.name is not None
        file_name: str = video_file.name

        while video_file.state.name == "PROCESSING":
            time.sleep(2)
            video_file = self._client.files.get(name=file_name)
            assert video_file.state is not None

        if video_file.state.name == "FAILED":
            raise ValueError("Gemini failed to process the video file")

        response = self._client.models.generate_content(
            model=self._model,
            contents=[video_file, prompt],
        )

        if response.text is None:
            raise ValueError("Gemini returned an empty response")

        return response.text
