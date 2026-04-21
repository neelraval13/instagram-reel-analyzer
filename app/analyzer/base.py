from abc import ABC, abstractmethod


class BaseAnalyzer(ABC):
    """Abstract base for all video analysis providers."""

    @abstractmethod
    async def analyze(self, video_path: str, prompt: str) -> str:
        """Analyze a video file and return the text response."""
        ...
