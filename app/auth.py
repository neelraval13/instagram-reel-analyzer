"""Authentication dependency.

Resolves an incoming `Authorization: Bearer <key>` header to an
AuthContext, which the endpoint and middleware use for attribution.

Lookup order:
    1. The keystore (hashed lookup against api_keys table).
    2. The legacy bearer token in settings.api_bearer_token, if set.
       This exists so the original single-token deployment keeps
       working during the migration to per-user keys; remove the
       env var to disable it.

A failed lookup returns 401 with no detail about which path failed -
we don't want to leak whether a given prefix is legacy or real.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.keys import AuthContext, get_keystore

security = HTTPBearer()


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthContext:
    bearer = credentials.credentials

    # Path 1: real per-user key
    auth = await get_keystore().verify(bearer)
    if auth is not None:
        return auth

    # Path 2: legacy fallback
    if settings.api_bearer_token and bearer == settings.api_bearer_token:
        return AuthContext(
            user_id="legacy",
            key_id=None,
            is_legacy=True,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing bearer token",
    )
