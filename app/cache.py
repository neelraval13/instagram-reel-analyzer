"""Analysis result cache backed by Redis.

Stores LLM outputs keyed on (shortcode, provider, model, output_mode, prompt_hash).

output_mode distinguishes freeform text from structured JSON, and
different structured schema versions from each other - so asking for
structured output after a cached freeform run correctly misses, and
so do old structured entries after a schema version bump.

Redis key shape:

    cache:<shortcode>:<provider>:<model>:<mode>:<prompt_hash>

The composite key encodes the entire identity of the cached entry, so
Redis lookup is a single GET. Each entry has a TTL (config setting,
default 30 days) so the cache self-bounds in size.

No Instagram content is stored, no user association - this cache
cannot tell you "who asked what", only "has anyone ever asked this
exact question about this exact reel with this exact model in this
exact output shape".
"""

import hashlib
import logging
import re

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Ordered most-specific-first so "/nasa/reel/ABC" doesn't get matched
# by the generic "/reel/ABC" pattern.
_SHORTCODE_PATTERNS = [
    re.compile(
        r"^https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+/reel/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:www\.)?instagram\.com/reels/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:www\.)?instagram\.com/reel/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://(?:www\.)?instagram\.com/p/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
]

# Canonical mode identifiers. "freeform" is the default text response;
# anything else is a structured schema version string (see schemas.py).
FREEFORM_MODE = "freeform"

_K_CACHE = "cache:"


def extract_shortcode(url: str) -> str | None:
    """Pull the canonical shortcode out of any accepted reel URL form."""
    for pattern in _SHORTCODE_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group(1)
    return None


def hash_prompt(prompt: str) -> str:
    """128-bit SHA256-derived hash of the prompt text."""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return digest[:32]


def _make_key(
    shortcode: str,
    provider: str,
    model: str,
    output_mode: str,
    prompt_hash: str,
) -> str:
    """Compose the Redis key from the cache key tuple.

    Slashes/colons in model names (e.g. 'org/model:tag') would conflict
    with Redis key conventions, so we replace them. Realistically Gemini's
    model names don't contain these, but defensive against future use.
    """
    safe_model = model.replace(":", "_").replace("/", "_")
    return f"{_K_CACHE}{shortcode}:{provider}:{safe_model}:{output_mode}:{prompt_hash}"


class AnalysisCache:
    """Redis-backed cache for analysis results (freeform or structured)."""

    def __init__(self) -> None:
        self._ttl = settings.analysis_cache_ttl_seconds

    async def get(
        self,
        shortcode: str,
        provider: str,
        model: str,
        prompt: str,
        output_mode: str = FREEFORM_MODE,
    ) -> str | None:
        """Return cached analysis (as a string) or None if not cached.

        For structured outputs, the stored string is JSON-serialized and
        the caller is responsible for deserializing.
        """
        client = get_redis()
        prompt_hash = hash_prompt(prompt)
        key = _make_key(shortcode, provider, model, output_mode, prompt_hash)

        result = await client.get(key)

        if result is not None:
            logger.info(
                "cache_hit",
                extra={
                    "shortcode": shortcode,
                    "provider": provider,
                    "model": model,
                    "output_mode": output_mode,
                    "source": "analysis_cache",
                },
            )
        return result

    async def put(
        self,
        shortcode: str,
        provider: str,
        model: str,
        prompt: str,
        analysis: str,
        output_mode: str = FREEFORM_MODE,
    ) -> None:
        """Store an analysis result. For structured mode, pass a JSON string.

        SET with EX in one command - atomic, no race window between SET
        and EXPIRE that could leak a TTL-less key on crash.
        """
        client = get_redis()
        prompt_hash = hash_prompt(prompt)
        key = _make_key(shortcode, provider, model, output_mode, prompt_hash)

        await client.set(key, analysis, ex=self._ttl)

        logger.info(
            "cache_write",
            extra={
                "shortcode": shortcode,
                "provider": provider,
                "model": model,
                "output_mode": output_mode,
                "ttl_seconds": self._ttl,
            },
        )


# --- Module-level singleton ------------------------------------------------

_cache_instance: AnalysisCache | None = None


def get_cache() -> AnalysisCache | None:
    """Return the singleton cache. Returns None when disabled via config."""
    global _cache_instance
    if not settings.cache_enabled:
        return None
    if _cache_instance is None:
        _cache_instance = AnalysisCache()
        logger.info(
            "cache_initialized",
            extra={"ttl_seconds": settings.analysis_cache_ttl_seconds},
        )
    return _cache_instance
