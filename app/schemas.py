"""Structured output schema for reel analysis.

When a client requests `structured: true`, the analyzer returns an
instance of ReelAnalysis (as a dict) instead of a freeform string.
Gemini's response_schema feature constrains model output to valid
JSON matching this shape at decode time - not post-hoc parsing.

Schema versioning is encoded in SCHEMA_VERSION. The cache keys on
this string, so when we evolve the schema we bump the version and
old structured cache entries naturally stop matching without needing
to delete anything.
"""

from enum import Enum

from pydantic import BaseModel, Field

# Bump this string when the ReelAnalysis shape changes in a way that
# would invalidate previously-cached structured results.
SCHEMA_VERSION = "structured-v1"


class Sentiment(str, Enum):
    positive = "positive"
    negative = "negative"
    neutral = "neutral"
    mixed = "mixed"
    enthusiastic = "enthusiastic"
    critical = "critical"


class ReelAnalysis(BaseModel):
    """Structured analysis of an Instagram reel.

    Designed to be rich enough for downstream apps (search, filters,
    triggered actions in PWAs/shortcuts) without asking the model
    for fields it would have to hallucinate.
    """

    summary: str = Field(description="1-2 sentence summary of what the reel is about.")
    transcript: str | None = Field(
        default=None,
        description=(
            "Verbatim transcript of spoken audio, if any. "
            "Null when the reel has no speech (music-only, ambient, silent)."
        ),
    )
    visual_description: str = Field(
        description=(
            "Description of what is happening visually, independent of audio. "
            "Covers setting, actions, on-screen text, notable visual elements."
        ),
    )
    topics: list[str] = Field(
        description=(
            "3-7 short lowercase tags describing the subject matter. "
            "Examples: 'cooking', 'f1', 'entrepreneurship', 'fashion'."
        ),
    )
    key_points: list[str] = Field(
        description=(
            "3-8 bullet-worthy takeaways or claims made in the reel. "
            "Each point is a complete sentence."
        ),
    )
    sentiment: Sentiment = Field(description="Overall tone of the reel.")
    has_call_to_action: bool = Field(
        description=(
            "Whether the reel explicitly asks the viewer to do something "
            "(buy, follow, subscribe, visit a link, etc)."
        ),
    )
    estimated_duration_seconds: int = Field(
        description=(
            "Best-guess estimate of the reel's duration in seconds. "
            "Instagram reels are typically 15-90 seconds."
        ),
    )
