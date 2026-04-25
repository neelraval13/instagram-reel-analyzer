"""Per-user rate limiting backed by SQLite.

Two limits enforced in parallel:

    burst (per-minute):  defends against runaway clients (looping
                         iOS Shortcuts, buggy retry loops)
    daily (per-day):     defends against leaked keys hammering us
                         and caps Gemini cost exposure

A request must pass BOTH to proceed. If either is exceeded, we raise
RateLimitExceeded which the FastAPI exception handler maps to HTTP 429
with a Retry-After header.

Algorithm: fixed-window counters. A bucket is the wall-clock minute
or day the request lands in (UTC). The (user_id, kind, window_start)
row carries a count; we INSERT-or-UPDATE-add-one atomically. Boundary
bursts are theoretically possible (last second of minute X plus first
second of minute X+1) but irrelevant at our scale.

Counting policy: this dependency runs AFTER verify_api_key, so pre-auth
failures (401, missing header, malformed JSON before our handler even
runs) do not consume quota. Post-auth failures DO consume quota - we
already did the work of authenticating and validating, and the user
got their answer (even a 502 is a real resource use).
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import settings

logger = logging.getLogger(__name__)


WindowKind = Literal["minute", "day"]


class RateLimitExceeded(Exception):
    """Raised when a check fails. Carries the info needed to render
    a 429 response with a usable Retry-After header.
    """

    def __init__(
        self,
        user_id: str,
        kind: WindowKind,
        limit: int,
        current: int,
        retry_after_seconds: int,
    ) -> None:
        self.user_id = user_id
        self.kind = kind
        self.limit = limit
        self.current = current
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Rate limit exceeded: user={user_id!r} kind={kind!r} "
            f"limit={limit} current={current}"
        )


@dataclass(frozen=True)
class RateLimitStatus:
    """Returned from check_and_increment so callers can log usage."""

    minute_count: int
    minute_limit: int
    day_count: int
    day_limit: int


def _minute_window(now: datetime) -> str:
    """Truncate to the start of the current minute, ISO formatted."""
    return now.replace(second=0, microsecond=0).isoformat()


def _day_window(now: datetime) -> str:
    """Truncate to UTC midnight of the current day, ISO formatted."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _seconds_until_minute_rollover(now: datetime) -> int:
    """How many seconds until the current minute bucket resets."""
    return 60 - now.second


def _seconds_until_day_rollover(now: datetime) -> int:
    """How many seconds until UTC midnight."""
    seconds_today = now.hour * 3600 + now.minute * 60 + now.second
    return max(1, 86400 - seconds_today)


class RateLimiter:
    """SQLite-backed rate limit counters.

    One row per (user_id, kind, window_start). Atomic upsert via SQLite's
    INSERT ... ON CONFLICT DO UPDATE keeps increments race-safe even
    under concurrent requests for the same user.
    """

    def __init__(
        self,
        db_path: str,
        burst_limit: int,
        daily_limit: int,
    ) -> None:
        self._db_path = db_path
        self._burst_limit = burst_limit
        self._daily_limit = daily_limit
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_id      TEXT NOT NULL,
                    window_kind  TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    count        INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, window_kind, window_start)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_limits_user_kind "
                "ON rate_limits (user_id, window_kind, window_start)"
            )
            conn.commit()

    def _check_and_increment_sync(self, user_id: str, now: datetime) -> RateLimitStatus:
        minute_start = _minute_window(now)
        day_start = _day_window(now)

        with self._connect() as conn:
            # Atomic upsert + increment for the minute bucket.
            conn.execute(
                """
                INSERT INTO rate_limits (user_id, window_kind, window_start, count)
                VALUES (?, 'minute', ?, 1)
                ON CONFLICT (user_id, window_kind, window_start)
                DO UPDATE SET count = count + 1
                """,
                (user_id, minute_start),
            )
            cur = conn.execute(
                """
                SELECT count FROM rate_limits
                WHERE user_id = ? AND window_kind = 'minute' AND window_start = ?
                """,
                (user_id, minute_start),
            )
            row = cur.fetchone()
            minute_count = row[0] if row else 1

            # Same for the day bucket.
            conn.execute(
                """
                INSERT INTO rate_limits (user_id, window_kind, window_start, count)
                VALUES (?, 'day', ?, 1)
                ON CONFLICT (user_id, window_kind, window_start)
                DO UPDATE SET count = count + 1
                """,
                (user_id, day_start),
            )
            cur = conn.execute(
                """
                SELECT count FROM rate_limits
                WHERE user_id = ? AND window_kind = 'day' AND window_start = ?
                """,
                (user_id, day_start),
            )
            row = cur.fetchone()
            day_count = row[0] if row else 1

            conn.commit()

        # Check both limits AFTER incrementing. If we're over, the increment
        # stands - we want the count to reflect attempts, not just successes.
        if minute_count > self._burst_limit:
            raise RateLimitExceeded(
                user_id=user_id,
                kind="minute",
                limit=self._burst_limit,
                current=minute_count,
                retry_after_seconds=_seconds_until_minute_rollover(now),
            )
        if day_count > self._daily_limit:
            raise RateLimitExceeded(
                user_id=user_id,
                kind="day",
                limit=self._daily_limit,
                current=day_count,
                retry_after_seconds=_seconds_until_day_rollover(now),
            )

        return RateLimitStatus(
            minute_count=minute_count,
            minute_limit=self._burst_limit,
            day_count=day_count,
            day_limit=self._daily_limit,
        )

    async def check_and_increment(self, user_id: str) -> RateLimitStatus:
        """Increment counters for the current request and check both limits.

        Raises RateLimitExceeded if either limit would be breached.
        Otherwise returns the post-increment status for logging.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return await asyncio.to_thread(self._check_and_increment_sync, user_id, now)


# --- Module-level singleton ------------------------------------------------

_limiter_instance: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter_instance
    if _limiter_instance is None:
        _limiter_instance = RateLimiter(
            db_path=settings.cache_db_path,
            burst_limit=settings.rate_limit_per_minute,
            daily_limit=settings.rate_limit_per_day,
        )
        logger.info(
            "rate_limiter_initialized",
            extra={
                "burst_limit": settings.rate_limit_per_minute,
                "daily_limit": settings.rate_limit_per_day,
            },
        )
    return _limiter_instance
