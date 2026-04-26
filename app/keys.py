# pyright: reportGeneralTypeIssues=false
"""API key generation, hashing, and Redis-backed storage.

Keys are issued in Stripe-style format:

    ra_live_<43 random URL-safe chars>

The plaintext key is shown to the admin exactly once, at creation time.
Only a SHA256 hash is persisted - if our Redis is ever leaked, the
keys themselves cannot be recovered. Verification on the request path
hashes the incoming bearer and looks up the hash.

Redis data model:

    next_key_id           - INCR'd counter for assigning new key_ids
    keyhash:<sha256>      - HASH containing the key's metadata
                            (key_id, user_id, name, created_at,
                             last_used_at, active="1"|"0")
    keyid:<key_id>        - STRING holding the sha256, so we can
                            revoke by key_id without scanning
    keys:all              - SET of all key_ids, used by /admin/keys list

The dual indexing (by hash AND by id) costs a little extra storage but
saves a full keyspace scan on every revoke. Worth it.

Why HSET instead of one JSON blob: HGET/HSET let us update a single
field (e.g. last_used_at on every auth) without read-modify-write
race conditions, and Redis is bytes-efficient at this.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Length of the random tail. 43 chars of base64 ~= 32 bytes of entropy,
# more than enough to defeat any brute-force or guessing attack.
_KEY_RANDOM_BYTES = 32
_KEY_PREFIX = "ra_live_"

# Redis key prefixes. Centralized so we can audit them at a glance.
_K_NEXT_ID = "next_key_id"
_K_BY_HASH = "keyhash:"
_K_BY_ID = "keyid:"
_K_ALL = "keys:all"


@dataclass(frozen=True)
class AuthContext:
    """Carried on the request after successful auth. Populated by the
    auth dependency; consumed by handlers and middleware for attribution.
    """

    user_id: str
    key_id: int | None  # None for the legacy fallback user
    is_legacy: bool = False


@dataclass(frozen=True)
class IssuedKey:
    """Returned from create() exactly once, at key creation time.

    The plaintext field is the only place the user-visible key value
    ever appears. It is never persisted; once this dataclass is
    discarded the key is unrecoverable except from whoever holds it.
    """

    plaintext: str
    key_id: int
    user_id: str
    name: str
    created_at: str


def generate_api_key() -> str:
    """Mint a fresh API key string. Cryptographically random."""
    return _KEY_PREFIX + secrets.token_urlsafe(_KEY_RANDOM_BYTES)


def hash_key(plaintext: str) -> str:
    """One-way hash used as the storage key.

    SHA256 is fine here (not bcrypt/argon2) because API keys have
    far more entropy than human passwords - brute-forcing 256 bits
    of randomness is computationally impossible regardless of how
    fast the hash is.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class KeyStore:
    """Redis-backed store for hashed API keys.

    All four operations are async and round-trip to Redis once or twice.
    Latency is dominated by network RTT, so the in-process work is
    negligible.
    """

    def __init__(self) -> None:
        # The Redis client is lazily resolved per-call rather than held
        # as an instance attr. This lets get_redis()'s singleton handle
        # connection pooling globally instead of us caching a stale
        # client across reconnects.
        pass

    async def create(self, user_id: str, name: str) -> IssuedKey:
        """Mint a new key for a user. Returns the plaintext exactly once.

        Three writes (atomic via pipeline): the hash record, the id-to-hash
        index, and the all-keys set. We INCR first to allocate the id,
        then write everything else.
        """
        client = get_redis()
        plaintext = generate_api_key()
        digest = hash_key(plaintext)
        created_at = datetime.utcnow().isoformat(timespec="seconds")

        key_id = await client.incr(_K_NEXT_ID)

        # Pipeline batches the writes into one round trip. Without this
        # we'd pay 3x the latency.
        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(
                f"{_K_BY_HASH}{digest}",
                mapping={
                    "key_id": str(key_id),
                    "user_id": user_id,
                    "name": name,
                    "created_at": created_at,
                    "last_used_at": "",  # empty string = never used yet
                    "active": "1",
                },
            )
            pipe.set(f"{_K_BY_ID}{key_id}", digest)
            pipe.sadd(_K_ALL, str(key_id))
            await pipe.execute()

        logger.info(
            "api_key_created",
            extra={
                "key_id": key_id,
                "user_id": user_id,
                "key_name": name,
            },
        )

        return IssuedKey(
            plaintext=plaintext,
            key_id=key_id,
            user_id=user_id,
            name=name,
            created_at=created_at,
        )

    async def verify(self, plaintext: str) -> AuthContext | None:
        """Look up a bearer string, return AuthContext if valid and active."""
        client = get_redis()
        digest = hash_key(plaintext)
        record = await client.hgetall(f"{_K_BY_HASH}{digest}")

        if not record:
            return None
        if record.get("active") != "1":
            return None

        key_id = int(record["key_id"])
        user_id = record["user_id"]

        # Best-effort touch of last_used_at. We don't await it strictly -
        # if it fails, log and proceed; auth has already succeeded.
        try:
            await client.hset(
                f"{_K_BY_HASH}{digest}",
                "last_used_at",
                datetime.utcnow().isoformat(timespec="seconds"),
            )
        except Exception:  # noqa: BLE001 - last_used is best-effort
            logger.exception("api_key_last_used_update_failed")

        return AuthContext(user_id=user_id, key_id=key_id, is_legacy=False)

    async def list(self) -> list[dict[str, Any]]:
        """Return all keys (without hashes or plaintexts).

        Iterates the all-keys set and HGETALLs each. For our scale
        (single-digit to low-double-digit keys) this is fine; if we
        ever had thousands of keys we'd switch to paginated SCAN.
        """
        client = get_redis()
        ids = await client.smembers(_K_ALL)

        if not ids:
            return []

        # Fetch all keys' metadata in one round trip via pipeline.
        async with client.pipeline(transaction=False) as pipe:
            for kid in ids:
                pipe.get(f"{_K_BY_ID}{kid}")
            digests = await pipe.execute()

        async with client.pipeline(transaction=False) as pipe:
            for digest in digests:
                if digest:
                    pipe.hgetall(f"{_K_BY_HASH}{digest}")
                else:
                    pipe.hgetall("__never_exists__")  # placeholder slot
            records = await pipe.execute()

        result: list[dict[str, Any]] = []
        for record in records:
            if not record:
                continue
            result.append(
                {
                    "id": int(record["key_id"]),
                    "user_id": record["user_id"],
                    "name": record["name"],
                    "created_at": record["created_at"],
                    "last_used_at": record.get("last_used_at") or None,
                    "active": int(record.get("active", "0")),
                }
            )

        # Sort ascending by id so output matches the SQLite version
        # and is stable across calls.
        result.sort(key=lambda r: r["id"])
        return result

    async def revoke(self, key_id: int) -> bool:
        """Mark a key inactive. Returns False if no such active key existed."""
        client = get_redis()
        digest = await client.get(f"{_K_BY_ID}{key_id}")

        if digest is None:
            return False

        # Check current state and flip atomically. We can't do this in
        # one Redis command, so we use HGET + HSET. Race window is tiny
        # (single-admin use case) and the worst outcome is a "revoked
        # twice = first one wins" which is what we want anyway.
        current = await client.hget(f"{_K_BY_HASH}{digest}", "active")
        if current != "1":
            return False

        await client.hset(f"{_K_BY_HASH}{digest}", "active", "0")
        logger.info("api_key_revoked", extra={"key_id": key_id})
        return True


# --- Module-level singleton ------------------------------------------------

_keystore_instance: KeyStore | None = None


def get_keystore() -> KeyStore:
    """Return the singleton KeyStore. Initialised lazily on first use.

    Unlike the cache, this is never disabled - auth always needs storage.
    """
    global _keystore_instance
    if _keystore_instance is None:
        _keystore_instance = KeyStore()
        logger.info("keystore_initialized")
    return _keystore_instance
