# pyright: reportGeneralTypeIssues=false
"""Invite codes for self-service onboarding.

Invite codes let admins generate single-use tokens that a friend or
colleague can redeem on the /onboard page to receive their own
ra_live_ API key. Without an invite code, /onboard is closed.

Why single-use: prevents one shared link from being passed around to
arbitrary people. Each invite ties to a specific user_id, so we get
clean attribution: "this code was for Alice; if it gets redeemed,
the resulting key belongs to Alice."

Redis data model:

    invite:<code>   - HASH {user_id, used: "0"|"1", created_at,
                            redeemed_at, redeemed_by_key_id}
    invites:all     - SET of all invite codes (for admin listing)

The code itself is 16 chars of base32 (no confusing 0/O/1/I), giving
~80 bits of entropy. Plenty unguessable, also short enough to type
or share over a casual chat.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Crockford-style base32 alphabet: removes 0/O and 1/I/L confusion.
# Codes are still uppercase for visual distinctness from ra_live_ keys.
_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_CODE_LENGTH = 16

_K_INVITE = "invite:"
_K_INVITES_ALL = "invites:all"


@dataclass(frozen=True)
class IssuedInvite:
    """Returned from create_invite() at generation time."""

    code: str
    user_id: str
    created_at: str


@dataclass(frozen=True)
class RedeemedInvite:
    """Returned from redeem() on a successful claim."""

    user_id: str


class InviteAlreadyUsedError(Exception):
    """Raised when a code has already been redeemed."""


class InviteNotFoundError(Exception):
    """Raised when a code doesn't exist."""


def generate_code() -> str:
    """Mint a fresh invite code. Cryptographically random, easy to read."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


class InviteStore:
    """Redis-backed store for invite codes."""

    async def create(self, user_id: str) -> IssuedInvite:
        """Mint a new invite tied to a user_id."""
        client = get_redis()
        code = generate_code()
        created_at = datetime.utcnow().isoformat(timespec="seconds")

        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(
                f"{_K_INVITE}{code}",
                mapping={
                    "user_id": user_id,
                    "used": "0",
                    "created_at": created_at,
                    "redeemed_at": "",
                    "redeemed_by_key_id": "",
                },
            )
            pipe.sadd(_K_INVITES_ALL, code)
            await pipe.execute()

        logger.info(
            "invite_created",
            extra={"code": code, "user_id": user_id},
        )

        return IssuedInvite(code=code, user_id=user_id, created_at=created_at)

    async def redeem(self, code: str, key_id: int) -> RedeemedInvite:
        """Mark an invite as used. Raises if code is missing or already used."""
        client = get_redis()
        code = code.strip().upper()
        record = await client.hgetall(f"{_K_INVITE}{code}")

        if not record:
            raise InviteNotFoundError(f"Invite code not found: {code}")
        if record.get("used") != "0":
            raise InviteAlreadyUsedError(f"Invite code already used: {code}")

        user_id = record["user_id"]
        redeemed_at = datetime.utcnow().isoformat(timespec="seconds")

        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(
                f"{_K_INVITE}{code}",
                mapping={
                    "used": "1",
                    "redeemed_at": redeemed_at,
                    "redeemed_by_key_id": str(key_id),
                },
            )
            await pipe.execute()

        logger.info(
            "invite_redeemed",
            extra={"code": code, "user_id": user_id, "key_id": key_id},
        )

        return RedeemedInvite(user_id=user_id)

    async def list(self) -> list[dict[str, Any]]:
        """Return all invites with metadata."""
        client = get_redis()
        codes = await client.smembers(_K_INVITES_ALL)

        if not codes:
            return []

        async with client.pipeline(transaction=False) as pipe:
            for code in codes:
                pipe.hgetall(f"{_K_INVITE}{code}")
            records = await pipe.execute()

        result: list[dict[str, Any]] = []
        for code, record in zip(codes, records):
            if not record:
                continue
            result.append(
                {
                    "code": code,
                    "user_id": record.get("user_id"),
                    "used": int(record.get("used", "0")),
                    "created_at": record.get("created_at"),
                    "redeemed_at": record.get("redeemed_at") or None,
                    "redeemed_by_key_id": (
                        int(record["redeemed_by_key_id"])
                        if record.get("redeemed_by_key_id")
                        else None
                    ),
                }
            )

        result.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return result


# --- Module-level singleton ------------------------------------------------

_invite_store: InviteStore | None = None


def get_invite_store() -> InviteStore:
    global _invite_store
    if _invite_store is None:
        _invite_store = InviteStore()
        logger.info("invite_store_initialized")
    return _invite_store
