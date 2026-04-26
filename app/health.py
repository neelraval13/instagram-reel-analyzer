"""Health and readiness endpoints.

    GET /health  - "is the process alive and accepting requests?"
                   Always 200 if the process can answer at all. No
                   external checks. Used by Render/load balancers
                   to decide whether to restart the container.

    GET /ready   - "can this instance actually serve requests?"
                   Returns 200 if all critical dependencies are
                   working, 503 with details if anything is broken.
                   Used for deploy gates and debugging.

The split matters: a transient Redis hiccup should NOT trigger a
container restart (which would make things worse). It should mark
the instance unready - while /health stays 200 because the process
itself is fine.
"""

import logging
from typing import Any, Awaitable, cast

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.analyzer import get_analyzer
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def _check_redis() -> str:
    """PING Redis. Timeout is enforced at the client level via
    socket_timeout in redis_client.py, so we don't need to wrap this
    in asyncio.wait_for - a stuck connection will raise TimeoutError
    after socket_timeout seconds.

    The cast() is needed because redis-py's type stubs annotate ping()
    as returning Awaitable[bool] | bool (a union covering both sync and
    async clients in one signature). At runtime the async client always
    returns an awaitable, but the type checker can't prove that.

    Returns 'ok' or a short error description.
    """
    try:
        client = get_redis()
        await cast(Awaitable[bool], client.ping())
        return "ok"
    except Exception as e:  # noqa: BLE001 - defensive
        return f"{type(e).__name__}: {e}"


def _check_analyzer() -> str:
    """Confirm the configured analyzer can be constructed.

    Catches missing API keys, unknown providers, etc. without making
    an actual upstream API call (which would cost money and create
    a runtime dependency on the provider's uptime).
    """
    try:
        get_analyzer()
        return "ok"
    except Exception as e:  # noqa: BLE001 - defensive
        return f"{type(e).__name__}: {e}"


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Always 200 if we can answer at all."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe. 200 if all dependencies work, 503 otherwise.

    Body always includes a `checks` dict so callers can see exactly
    what's broken even on success (handy for debugging "I think this
    might be flaky" cases).
    """
    checks: dict[str, Any] = {}

    # Redis is the single backing store for keystore + rate limits + cache.
    # Its health = our health.
    checks["redis"] = await _check_redis()

    # Analyzer constructibility.
    checks["analyzer"] = _check_analyzer()

    # Healthy if every check is "ok" (or "disabled" if we ever add toggles).
    healthy = all(v in ("ok", "disabled") for v in checks.values())
    status = "ready" if healthy else "degraded"
    code = 200 if healthy else 503

    if not healthy:
        logger.warning("readiness_check_failed", extra={"checks": checks})

    return JSONResponse(
        status_code=code,
        content={"status": status, "checks": checks},
    )
