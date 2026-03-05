from functools import lru_cache

from app.analyzer.base import BaseAnalyzer
from app.config import settings


@lru_cache(maxsize=1)
def get_analyzer() -> BaseAnalyzer:
    """Factory that returns the configured analyzer (singleton)."""
    provider = settings.analyzer_provider.lower()

    if provider == "gemini":
        from app.analyzer.gemini import GeminiAnalyzer

        return GeminiAnalyzer()

    if provider == "qwen":
        from app.analyzer.qwen import QwenAnalyzer

        return QwenAnalyzer()

    raise ValueError(f"Unknown analyzer provider: {provider!r}")
