# pyright: reportGeneralTypeIssues=false
"""Usage history querying.

Reads the permanent daily usage counters written by app/rate_limit.py.
The schema:

    usage:<user_id>:<YYYY-MM-DD>  - INTEGER count of analyses that day
    usage:users                    - SET of all user_ids ever seen

These keys have no TTL - they're permanent history. Each entry is ~50
bytes; 5 friends x 365 days = ~90KB/year, trivially small for Upstash's
256MB free tier.

The dashboard uses these to render trend tables: today, last 7 days,
all-time. We could compute heavier aggregations (monthly, percentiles)
later if needed; for now the surface is intentionally minimal.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

_K_USAGE = "usage:"
_K_USAGE_USERS = "usage:users"


def _date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


async def get_usage_per_user(days: int = 7) -> list[dict[str, Any]]:
    """Return per-user usage counts for the last N days, including today.

    Output shape:
        [
          {
            "user_id": "alice",
            "today": 4,
            "last_7_days": 18,
            "all_time": 142,
            "by_day": {"2026-04-26": 4, "2026-04-25": 3, ...}
          },
          ...
        ]

    Sorted by today's count descending (so most-active-now bubbles up).
    Users who have never used the service are not included.
    """
    client = get_redis()
    user_ids = await client.smembers(_K_USAGE_USERS)

    if not user_ids:
        return []

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_str = _date_str(now)

    # Pre-compute the date range for the window.
    window_dates = [_date_str(now - timedelta(days=i)) for i in range(days)]

    # Fetch every user's per-day counts in one big pipeline. For 50 users
    # x 7 days = 350 GETs in one round trip. Still fast.
    async with client.pipeline(transaction=False) as pipe:
        for user_id in user_ids:
            for date_str in window_dates:
                pipe.get(f"{_K_USAGE}{user_id}:{date_str}")
        window_results = await pipe.execute()

    result: list[dict[str, Any]] = []
    cursor = 0
    for user_id in user_ids:
        by_day: dict[str, int] = {}
        for date_str in window_dates:
            count_raw = window_results[cursor]
            cursor += 1
            count = int(count_raw) if count_raw else 0
            if count > 0:
                by_day[date_str] = count

        today = by_day.get(today_str, 0)
        last_7 = sum(by_day.values())

        # All-time: SCAN all usage:<user>:* keys. Cheap since users
        # have at most ~365 keys/year. Async generator pattern.
        all_time = 0
        async for key in client.scan_iter(match=f"{_K_USAGE}{user_id}:*"):
            count_raw = await client.get(key)
            if count_raw:
                all_time += int(count_raw)

        result.append(
            {
                "user_id": user_id,
                "today": today,
                "last_7_days": last_7,
                "all_time": all_time,
                "by_day": by_day,
            }
        )

    # Sort: most-active-today first, then most-active-this-week, then alpha.
    result.sort(key=lambda r: (-r["today"], -r["last_7_days"], r["user_id"]))
    return result


async def get_totals() -> dict[str, int]:
    """Aggregate usage counts across all users."""
    per_user = await get_usage_per_user(days=7)
    return {
        "today": sum(u["today"] for u in per_user),
        "last_7_days": sum(u["last_7_days"] for u in per_user),
        "all_time": sum(u["all_time"] for u in per_user),
        "active_users_today": sum(1 for u in per_user if u["today"] > 0),
        "total_users_ever": len(per_user),
    }
