"""Analysis result cache backed by SQLite.

Stores LLM outputs keyed on (shortcode, provider, model, output_mode, prompt_hash).

output_mode distinguishes freeform text from structured JSON, and
different structured schema versions from each other - so asking for
structured output after a cached freeform run correctly misses, and
so do old structured entries after a schema version bump.

No Instagram content is stored, no user association - this cache
cannot tell you "who asked what", only "has anyone ever asked this
exact question about this exact reel with this exact model in this
exact output shape".
"""

import asyncio
import hashlib
import logging
import re
import sqlite3
from datetime import datetime

from app.config import settings

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


class AnalysisCache:
    """SQLite-backed cache for analysis results (freeform or structured)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    shortcode     TEXT NOT NULL,
                    provider      TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    output_mode   TEXT NOT NULL,
                    prompt_hash   TEXT NOT NULL,
                    prompt        TEXT NOT NULL,
                    analysis      TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    hit_count     INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (shortcode, provider, model, output_mode, prompt_hash)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shortcode ON analysis_cache (shortcode)"
            )

            # Schema migration for databases created before output_mode existed.
            # Safe to run unconditionally because "ADD COLUMN" fails silently
            # if the column already exists (we swallow that specific error).
            try:
                conn.execute(
                    "ALTER TABLE analysis_cache ADD COLUMN output_mode TEXT NOT NULL DEFAULT 'freeform'"
                )
            except sqlite3.OperationalError:
                # Column already present, or table just created with it.
                pass

            conn.commit()

    def _get_sync(
        self,
        shortcode: str,
        provider: str,
        model: str,
        output_mode: str,
        prompt_hash: str,
    ) -> str | None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT analysis FROM analysis_cache
                WHERE shortcode = ? AND provider = ?
                  AND model = ? AND output_mode = ? AND prompt_hash = ?
                """,
                (shortcode, provider, model, output_mode, prompt_hash),
            )
            row = cur.fetchone()
            if row is None:
                return None

            conn.execute(
                """
                UPDATE analysis_cache SET hit_count = hit_count + 1
                WHERE shortcode = ? AND provider = ?
                  AND model = ? AND output_mode = ? AND prompt_hash = ?
                """,
                (shortcode, provider, model, output_mode, prompt_hash),
            )
            conn.commit()
            return row[0]

    def _put_sync(
        self,
        shortcode: str,
        provider: str,
        model: str,
        output_mode: str,
        prompt_hash: str,
        prompt: str,
        analysis: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO analysis_cache
                    (shortcode, provider, model, output_mode, prompt_hash,
                     prompt, analysis, created_at, hit_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    shortcode,
                    provider,
                    model,
                    output_mode,
                    prompt_hash,
                    prompt,
                    analysis,
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

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
        prompt_hash = hash_prompt(prompt)
        result = await asyncio.to_thread(
            self._get_sync,
            shortcode,
            provider,
            model,
            output_mode,
            prompt_hash,
        )
        if result is not None:
            logger.info(
                "cache_hit",
                extra={
                    "shortcode": shortcode,
                    "provider": provider,
                    "model": model,
                    "output_mode": output_mode,
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
        """Store an analysis result. For structured mode, pass a JSON string."""
        prompt_hash = hash_prompt(prompt)
        await asyncio.to_thread(
            self._put_sync,
            shortcode,
            provider,
            model,
            output_mode,
            prompt_hash,
            prompt,
            analysis,
        )
        logger.info(
            "cache_write",
            extra={
                "shortcode": shortcode,
                "provider": provider,
                "model": model,
                "output_mode": output_mode,
            },
        )


# --- Module-level singleton ------------------------------------------------

_cache_instance: AnalysisCache | None = None


def get_cache() -> AnalysisCache | None:
    global _cache_instance
    if not settings.cache_enabled:
        return None
    if _cache_instance is None:
        _cache_instance = AnalysisCache(settings.cache_db_path)
        logger.info("cache_initialized", extra={"path": settings.cache_db_path})
    return _cache_instance
