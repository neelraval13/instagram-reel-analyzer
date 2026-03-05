import base64

import httpx

from app.analyzer.base import BaseAnalyzer
from app.config import settings


class QwenAnalyzer(BaseAnalyzer):
    """Qwen2.5-VL via Ollama's OpenAI-compatible API."""

    def __init__(self) -> None:
        self._base_url = settings.ollama_base_url
        self._model = settings.qwen_model

    def analyze(self, video_path: str, prompt: str) -> str:
        with open(video_path, "rb") as f:
            video_b64 = base64.b64encode(f.read()).decode("utf-8")

        # Ollama exposes an OpenAI-compatible /v1/chat/completions
        response = httpx.post(
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
            timeout=300.0,
        )
        response.raise_for_status()

        data = response.json()
        text: str = data["choices"][0]["message"]["content"]
        if not text:
            raise ValueError("Qwen returned an empty response")
        return text
