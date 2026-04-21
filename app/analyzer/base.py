from abc import ABC, abstractmethod

from app.schemas import ReelAnalysis


class BaseAnalyzer(ABC):
    """Abstract base for all video analysis providers."""

    # Providers override this to declare whether they support schema-
    # guided structured output. Clients can query this through the
    # factory to decide whether to downgrade or error.
    supports_structured: bool = False

    @abstractmethod
    async def analyze(self, video_path: str, prompt: str) -> str:
        """Analyze a video file and return a freeform text response."""
        ...

    async def analyze_structured(self, video_path: str, prompt: str) -> ReelAnalysis:
        """Analyze a video and return a ReelAnalysis.

        Default implementation raises NotImplementedError. Providers
        that support schema-guided decoding (like Gemini) override this.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support structured output"
        )
