"""API key generation, hashing, and storage.

Keys are issued in Stripe-style format:

    ra_live_<43 random URL-safe chars>

The plaintext key is shown to the admin exactly once, at creation time.
Only a SHA256 hash is persisted - if cache.db is ever leaked, the
keys themselves cannot be recovered. Verification on the request path
hashes the incoming bearer and looks up the hash.

The `ra_` prefix exists so leaked keys can be grep'd from logs, git
history, and code. The `live_` segment is forward-compatible with a
future `test_` environment.
"""

import asyncio
import hashlib
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.config import settings

logger = logging.getLogger(__name__)

# Length of the random tail. 43 chars of base64 ~= 32 bytes of entropy,
# more than enough to defeat any brute-force or guessing attack.
_KEY_RANDOM_BYTES = 32
_KEY_PREFIX = "ra_live_"


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
    """Async-friendly store for hashed API keys.

    Shares the SQLite file with the analysis cache. We open a fresh
    connection per operation - same pattern as cache.py - so cross-
    thread access is safe.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash        TEXT NOT NULL UNIQUE,
                    user_id         TEXT NOT NULL,
                    name            TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    last_used_at    TEXT,
                    active          INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id)"
            )
            conn.commit()

    # --- Sync internals (called via to_thread from async wrappers) ----

    def _create_sync(self, user_id: str, name: str) -> IssuedKey:
        plaintext = generate_api_key()
        digest = hash_key(plaintext)
        created_at = datetime.utcnow().isoformat(timespec="seconds")

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO api_keys (key_hash, user_id, name, created_at, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (digest, user_id, name, created_at),
            )
            conn.commit()
            assert cur.lastrowid is not None
            return IssuedKey(
                plaintext=plaintext,
                key_id=cur.lastrowid,
                user_id=user_id,
                name=name,
                created_at=created_at,
            )

    def _verify_sync(self, plaintext: str) -> AuthContext | None:
        digest = hash_key(plaintext)
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, user_id, active FROM api_keys
                WHERE key_hash = ?
                """,
                (digest,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            key_id, user_id, active = row
            if not active:
                return None

            # Touch last_used_at. Best-effort; don't let a write failure
            # block authentication.
            try:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(timespec="seconds"), key_id),
                )
                conn.commit()
            except sqlite3.Error:
                logger.exception("api_key_last_used_update_failed")

            return AuthContext(user_id=user_id, key_id=key_id, is_legacy=False)

    def _list_sync(self) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, user_id, name, created_at, last_used_at, active
                FROM api_keys ORDER BY id ASC
                """
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _revoke_sync(self, key_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET active = 0 WHERE id = ?",
                (key_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    # --- Async public API ---------------------------------------------

    async def create(self, user_id: str, name: str) -> IssuedKey:
        """Mint a new key for a user. Returns the plaintext exactly once."""
        issued = await asyncio.to_thread(self._create_sync, user_id, name)
        logger.info(
            "api_key_created",
            extra={
                "key_id": issued.key_id,
                "user_id": issued.user_id,
                "name": issued.name,
            },
        )
        return issued

    async def verify(self, plaintext: str) -> AuthContext | None:
        """Look up a bearer string, return AuthContext if valid and active."""
        return await asyncio.to_thread(self._verify_sync, plaintext)

    async def list(self) -> list[dict]:
        """Return all keys (without hashes or plaintexts)."""
        return await asyncio.to_thread(self._list_sync)

    async def revoke(self, key_id: int) -> bool:
        """Mark a key inactive. Returns False if no such key existed."""
        revoked = await asyncio.to_thread(self._revoke_sync, key_id)
        if revoked:
            logger.info("api_key_revoked", extra={"key_id": key_id})
        return revoked


# --- Module-level singleton ------------------------------------------------

_keystore_instance: KeyStore | None = None


def get_keystore() -> KeyStore:
    """Return the singleton KeyStore. Initialised lazily on first use.

    Unlike the cache, this is never disabled - auth always needs storage.
    """
    global _keystore_instance
    if _keystore_instance is None:
        _keystore_instance = KeyStore(settings.cache_db_path)
        logger.info(
            "keystore_initialized",
            extra={"path": settings.cache_db_path},
        )
    return _keystore_instance
