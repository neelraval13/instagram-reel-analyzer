"""Admin endpoints for API key management.

Three endpoints, all gated by a separate ADMIN_TOKEN env var:

    POST   /admin/keys           - issue a new key (returns plaintext once)
    GET    /admin/keys           - list keys (no plaintexts)
    DELETE /admin/keys/{key_id}  - revoke a key

This is a deliberately separate auth surface from /analyze. User keys
(ra_live_...) authorize calling the analysis API; the admin token
authorizes managing those user keys. A user with an analysis key
cannot escalate to issue more keys - they're entirely different
namespaces.

When ADMIN_TOKEN is empty (the default), every /admin/* endpoint
returns 404. This makes the admin surface invisible until you
deliberately enable it via env var.
"""

import logging
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import settings
from app.invites import get_invite_store
from app.keys import get_keystore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Separate HTTPBearer instance from the user-auth one. Same scheme,
# different validation logic.
_admin_security = HTTPBearer()


async def verify_admin_token(
    credentials: HTTPAuthorizationCredentials = Depends(_admin_security),
) -> None:
    """Authorize an admin call.

    Hides the entire admin surface (404, not 401) when ADMIN_TOKEN is
    unset - so probes can't tell the difference between "admin disabled
    here" and "wrong token". Once enabled, wrong tokens get 401.
    """
    if not settings.admin_token:
        # Admin disabled. Pretend the route doesn't exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    if credentials.credentials != settings.admin_token:
        logger.warning("admin_auth_failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
        )


# --- Models ---------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)


class CreateKeyResponse(BaseModel):
    """Returned exactly once at key creation. The api_key field is the
    only place the plaintext key ever appears - clients must save it
    immediately or revoke and re-issue.
    """

    key_id: int
    user_id: str
    name: str
    created_at: str
    api_key: str
    warning: str


class KeyMetadata(BaseModel):
    """Read-only view of a key. No plaintexts, no hashes - just metadata."""

    id: int
    user_id: str
    name: str
    created_at: str
    last_used_at: str | None
    active: int


class RevokeResponse(BaseModel):
    revoked: bool
    key_id: int


# --- Endpoints ------------------------------------------------------------


@router.post(
    "/keys",
    response_model=CreateKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    request: CreateKeyRequest,
    _admin: None = Depends(verify_admin_token),
) -> CreateKeyResponse:
    issued = await get_keystore().create(
        user_id=request.user_id,
        name=request.name,
    )
    return CreateKeyResponse(
        key_id=issued.key_id,
        user_id=issued.user_id,
        name=issued.name,
        created_at=issued.created_at,
        api_key=issued.plaintext,
        warning=(
            "This key will not be shown again. Save it now. "
            "If lost, revoke and re-issue."
        ),
    )


@router.get("/keys", response_model=list[KeyMetadata])
async def list_keys(
    _admin: None = Depends(verify_admin_token),
) -> list[dict[str, Any]]:
    return await get_keystore().list()


@router.delete("/keys/{key_id}", response_model=RevokeResponse)
async def revoke_key(
    key_id: int,
    _admin: None = Depends(verify_admin_token),
) -> RevokeResponse:
    revoked = await get_keystore().revoke(key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active key with id {key_id}",
        )
    return RevokeResponse(revoked=True, key_id=key_id)


# --- Invite endpoints ------------------------------------------------------


class CreateInviteRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)


class CreateInviteResponse(BaseModel):
    code: str
    user_id: str
    created_at: str
    onboard_url: str


class InviteMetadata(BaseModel):
    code: str
    user_id: str
    used: int
    created_at: str
    redeemed_at: str | None
    redeemed_by_key_id: int | None


@router.post(
    "/invites",
    response_model=CreateInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    request: CreateInviteRequest,
    fastapi_request: Request,
    _admin: None = Depends(verify_admin_token),
) -> CreateInviteResponse:
    """Generate a single-use invite code for a specific user_id."""
    invite = await get_invite_store().create(user_id=request.user_id)
    base_url = str(fastapi_request.base_url).rstrip("/")
    onboard_url = f"{base_url}/onboard?code={invite.code}"
    return CreateInviteResponse(
        code=invite.code,
        user_id=invite.user_id,
        created_at=invite.created_at,
        onboard_url=onboard_url,
    )


@router.get("/invites", response_model=list[InviteMetadata])
async def list_invites(
    _admin: None = Depends(verify_admin_token),
) -> list[dict[str, Any]]:
    return await get_invite_store().list()
