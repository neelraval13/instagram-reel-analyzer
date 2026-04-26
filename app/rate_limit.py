"""Per-user rate limiting backed by Redis.

Two limits enforced in parallel:

    burst (per-minute):  defends against runaway clients (looping
                         iOS Shortcuts, buggy retry loops)
    daily (per-day):     defends against leaked keys hammering us
                         and caps Gemini cost exposure

A request must pass BOTH to proceed. If either is exceeded, we raise
RateLimitExceeded which the FastAPI exception handler maps to HTTP 429
with a Retry-After header.

Algorithm: fixed-window counters using Redis INCR + EXPIRE. A bucket is
the wall-clock minute or day the request lands in (UTC). Two atomic ops:
INCR creates-or-increments the counter; EXPIRE sets a TTL so old buckets
self-clean.

The TTL slightly exceeds the window length so we don't accidentally
forget the count if a request lands in the very last microsecond of a
bucket (would only matter under extreme contention; harmless either way).

Counting policy: this dependency runs AFTER verify_api_key, so pre-auth
failures (401, missing header, malformed JSON before our handler even
runs) do not consume quota. Post-auth failures DO consume quota - we
already did the work of authenticating and validating, and the user
got their answer (even a 502 is a real resource use).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import settings
from app.redis_client import get_redis

logger = logging.getLogger(__name__)


WindowKind = Literal["minute", "day"]

# Redis key prefixes
_K_RL_MINUTE = "rl:m:"  # rl:m:<user>:<YYYY-MM-DDTHH:MM>
_K_RL_DAY = "rl:d:"  # rl:d:<user>:<YYYY-MM-DD>
_K_USAGE = "usage:"  # usage:<user>:<YYYY-MM-DD>  (permanent, no TTL)
_K_USAGE_USERS = "usage:users"  # SET of user_ids that have ever used the API

# TTLs are slightly longer than the window to avoid edge-case off-by-one
# wraparound. Redis applies expirations lazily anyway, so a few extra
# seconds are inconsequential.
_TTL_MINUTE_SECONDS = 90  # window is 60s, give 30s grace
_TTL_DAY_SECONDS = 90_000  # window is 86400s, give ~1h grace


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
    """Truncate to the start of the current minute."""
    return now.strftime("%Y-%m-%dT%H:%M")


def _day_window(now: datetime) -> str:
    """Truncate to UTC date."""
    return now.strftime("%Y-%m-%d")


def _seconds_until_minute_rollover(now: datetime) -> int:
    return 60 - now.second


def _seconds_until_day_rollover(now: datetime) -> int:
    seconds_today = now.hour * 3600 + now.minute * 60 + now.second
    return max(1, 86400 - seconds_today)


class RateLimiter:
    """Redis-backed rate limit counters.

    One key per (user, window). INCR is naturally atomic (no read-modify-
    write race), and EXPIRE auto-cleans old windows so we don't leak
    keys forever the way SQLite would have without a cron job.
    """

    def __init__(self, burst_limit: int, daily_limit: int) -> None:
        self._burst_limit = burst_limit
        self._daily_limit = daily_limit

    async def check_and_increment(self, user_id: str) -> RateLimitStatus:
        """Increment counters for the current request and check both limits.

        Raises RateLimitExceeded if either limit would be breached.
        Otherwise returns the post-increment status for logging.

        Both buckets are incremented before either is checked. We want
        attempts to count toward the limit, even if the limit is the
        thing they're hitting - this is standard rate-limiter semantics
        (Stripe, GitHub, AWS all behave this way).
        """
        client = get_redis()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        minute_key = f"{_K_RL_MINUTE}{user_id}:{_minute_window(now)}"
        day_key = f"{_K_RL_DAY}{user_id}:{_day_window(now)}"
        usage_key = f"{_K_USAGE}{user_id}:{_day_window(now)}"

        # Pipeline both increments + their TTLs + the permanent usage
        # counter + the user-set membership. One round trip.
        # EXPIRE is idempotent - calling it on an existing key resets
        # the TTL, which is fine since we want it to outlive the window.
        # The usage_key has NO TTL - it's permanent history.
        async with client.pipeline(transaction=False) as pipe:
            pipe.incr(minute_key)
            pipe.expire(minute_key, _TTL_MINUTE_SECONDS)
            pipe.incr(day_key)
            pipe.expire(day_key, _TTL_DAY_SECONDS)
            pipe.incr(usage_key)
            pipe.sadd(_K_USAGE_USERS, user_id)
            results = await pipe.execute()

        # results is [minute_count, expire_ok, day_count, expire_ok,
        #             usage_count, sadd_added]
        minute_count = int(results[0])
        day_count = int(results[2])

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


# --- Module-level singleton ------------------------------------------------

_limiter_instance: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter_instance
    if _limiter_instance is None:
        _limiter_instance = RateLimiter(
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
