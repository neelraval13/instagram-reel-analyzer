# pyright: reportGeneralTypeIssues=false
"""Admin session management for the dashboard.

The dashboard is browser-based, so we use HttpOnly cookies for auth
instead of Authorization headers. The flow:

    1. User visits /admin and is redirected to /admin/login
    2. /admin/login is a form. User pastes ADMIN_TOKEN, submits.
    3. Server validates the token, generates a random session_id,
       stores session_id -> {created_at, ip} in Redis with 24h TTL,
       and sets an HttpOnly Secure SameSite=Lax cookie with session_id.
    4. Subsequent dashboard requests check the cookie. Valid + unexpired
       session = authorized. No token re-entry needed for 24h.
    5. /admin/logout deletes the session from Redis and clears the cookie.

Why this is more secure than just sending ADMIN_TOKEN as a cookie:

    - The cookie value is a session_id, not the actual token. If a
      cookie leaks (browser extension, XSS bug, malicious browser),
      the attacker gets a 24h session, not the keys to the kingdom.
    - Sessions can be revoked individually (logout, expire, force-clear)
      without rotating ADMIN_TOKEN.
    - HttpOnly means JavaScript can't read it (defends against XSS).
    - SameSite=Lax means most CSRF attacks fail (cookies aren't sent
      on cross-origin POSTs).
    - Secure means it's only sent over HTTPS (defends against passive
      sniffing in transit). Conditionally applied based on request scheme
      so local HTTP dev still works.

24-hour TTL is a deliberate balance: long enough that you don't re-login
constantly, short enough that a stolen cookie has limited blast radius.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Cookie + Redis key constants
COOKIE_NAME = "admin_session"
_K_SESSION = "admin_session:"
_SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_SESSION_TOKEN_BYTES = 32  # 256 bits of entropy


@dataclass(frozen=True)
class AdminSession:
    """Validated session metadata. Returned from validate_session."""

    session_id: str
    created_at: str


async def create_session(ip: Optional[str] = None) -> str:
    """Mint a new session token. Returns the opaque session_id to set
    as a cookie. Stores metadata in Redis with a 24h TTL.

    The session_id is the secret. Treat it like a password.
    """
    client = get_redis()
    session_id = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    created_at = datetime.utcnow().isoformat(timespec="seconds")

    await client.hset(
        f"{_K_SESSION}{session_id}",
        mapping={
            "created_at": created_at,
            "ip": ip or "",
        },
    )
    await client.expire(f"{_K_SESSION}{session_id}", _SESSION_TTL_SECONDS)

    logger.info(
        "admin_session_created",
        # Don't log the session_id itself (it's a credential)
        extra={"created_at": created_at, "ip": ip},
    )

    return session_id


async def validate_session(session_id: Optional[str]) -> Optional[AdminSession]:
    """Check if a session_id is valid + unexpired. Returns the session
    metadata if so, None otherwise.

    The Redis TTL handles expiry naturally - HGETALL on an expired key
    returns {}, which we treat as 'not authenticated'.
    """
    if not session_id:
        return None

    client = get_redis()
    record = await client.hgetall(f"{_K_SESSION}{session_id}")

    if not record:
        return None

    return AdminSession(
        session_id=session_id,
        created_at=record.get("created_at", ""),
    )


async def destroy_session(session_id: Optional[str]) -> None:
    """Delete a session. Used by /admin/logout. Idempotent - missing
    session is fine, just a no-op.
    """
    if not session_id:
        return

    client = get_redis()
    await client.delete(f"{_K_SESSION}{session_id}")

    logger.info("admin_session_destroyed")
