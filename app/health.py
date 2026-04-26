"""Health and readiness endpoints.

Two endpoints with deliberately different semantics:

    GET /health  - "is the process alive and accepting requests?"
                   Always 200 if the process can answer at all. No
                   external checks. Used by Railway/load balancers
                   to decide whether to restart the container.

    GET /ready   - "can this instance actually serve requests?"
                   Returns 200 if all critical dependencies are
                   working, 503 with details if anything is broken.
                   Used for deploy gates and debugging.

The split matters: a transient cache hiccup should NOT trigger a
container restart (which would make things worse). It should mark
the instance unready so traffic can drain elsewhere if available,
and so an alert fires for investigation - while /health stays 200
because the process itself is fine.
"""

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.analyzer import get_analyzer
from app.cache import get_cache
from app.config import settings
from app.keys import get_keystore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def _check_sqlite(db_path: str, table: str) -> str:
    """Open a fresh connection and run a tiny query against the named
    table. Returns 'ok' or a short error description.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        finally:
            conn.close()
        return "ok"
    except sqlite3.Error as e:
        return f"sqlite error: {e}"
    except Exception as e:  # noqa: BLE001 - defensive
        return f"unexpected error: {e}"


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

    # Cache (the table only exists if cache is enabled, so skip if not).
    if settings.cache_enabled:
        checks["cache"] = _check_sqlite(settings.cache_db_path, "analysis_cache")
    else:
        checks["cache"] = "disabled"

    # Keystore - same DB file, separate table.
    checks["keystore"] = _check_sqlite(settings.cache_db_path, "api_keys")

    # Analyzer constructibility.
    checks["analyzer"] = _check_analyzer()

    # Healthy if every check is "ok" or "disabled".
    healthy = all(v in ("ok", "disabled") for v in checks.values())
    status = "ready" if healthy else "degraded"
    code = 200 if healthy else 503

    if not healthy:
        logger.warning("readiness_check_failed", extra={"checks": checks})

    return JSONResponse(
        status_code=code,
        content={"status": status, "checks": checks},
    )
