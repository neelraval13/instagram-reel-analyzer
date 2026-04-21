"""Exception hierarchy for Reel Analyzer.

Every internal error raised by the service inherits from ReelAnalyzerError.
Each subclass carries two pieces of metadata:

    http_status: the HTTP status code this error should map to
    retryable:   whether the retry layer should attempt this again

Keep these two concepts separate. "Is this retryable?" and "how do I
report it?" are different questions and the answer to one doesn't
imply the other (e.g. a 429 is retryable but reported as 429, a 500
from a config error is not retryable and reported as 500).
"""


class ReelAnalyzerError(Exception):
    """Base class for every error the service raises intentionally."""

    http_status: int = 500
    retryable: bool = False


# --- Input errors -----------------------------------------------------------


class InvalidReelURLError(ReelAnalyzerError):
    """The URL is not a recognized Instagram reel URL."""

    http_status = 400
    retryable = False


# --- Download errors --------------------------------------------------------


class DownloadError(ReelAnalyzerError):
    """yt-dlp failed to download the reel (network, extractor, Instagram)."""

    http_status = 502
    retryable = True


# --- Provider errors (Gemini / Qwen / future) -------------------------------


class ProviderError(ReelAnalyzerError):
    """Generic upstream failure from the analysis provider."""

    http_status = 502
    retryable = True


class ProviderTimeoutError(ProviderError):
    """Provider took too long to respond."""

    http_status = 504
    retryable = True


class ProviderRateLimitError(ProviderError):
    """Provider returned a rate-limit signal (429 or equivalent)."""

    http_status = 429
    retryable = True


class ProviderConfigError(ReelAnalyzerError):
    """Provider is misconfigured (missing API key, unknown model, etc.).

    Not retryable - this will not fix itself by trying again.
    """

    http_status = 500
    retryable = False
