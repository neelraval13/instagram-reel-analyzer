import asyncio
import base64

import httpx

from app.analyzer.base import BaseAnalyzer
from app.config import settings


def _read_and_encode(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


class QwenAnalyzer(BaseAnalyzer):
    """Qwen2.5-VL via Ollama's OpenAI-compatible API."""

    def __init__(self) -> None:
        self._base_url = settings.ollama_base_url
        self._model = settings.qwen_model

    async def analyze(self, video_path: str, prompt: str) -> str:
        video_b64 = await asyncio.to_thread(_read_and_encode, video_path)

        # Ollama exposes an OpenAI-compatible /v1/chat/completions
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
        response.raise_for_status()

        data = response.json()
        text: str = data["choices"][0]["message"]["content"]
        if not text:
            raise ValueError("Qwen returned an empty response")
        return text
