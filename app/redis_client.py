"""Redis connection management.

Single async client used by the keystore, rate limiter, and analysis cache.
The client uses a connection pool (default size = 50) so concurrent requests
share TCP connections rather than each opening a new one.

Why a singleton: Upstash and most Redis services charge per-connection or
limit total open connections. Pooling means our 100 concurrent in-flight
requests use a few real TCP connections, not 100.

The client speaks the standard Redis protocol over TLS (rediss://) when
the URL specifies it. redis-py auto-detects TLS from the URL scheme.

Timeouts: socket_timeout=5s caps how long any single command will block.
This includes PING from /ready, so a stuck Redis server can't hang the
healthcheck. socket_connect_timeout caps the initial handshake.
"""

import logging
from typing import Optional

import redis.asyncio as redis_async

from app.config import settings

logger = logging.getLogger(__name__)

_client: Optional[redis_async.Redis] = None


def get_redis() -> redis_async.Redis:
    """Return the shared async Redis client.

    Lazy: constructs on first call so import-time failures (bad URL, etc.)
    don't crash the whole app before logging is set up.
    """
    global _client
    if _client is None:
        _client = redis_async.from_url(
            settings.redis_url,
            decode_responses=True,  # str in/out, no manual .decode()
            socket_timeout=5.0,  # cap per-command latency
            socket_connect_timeout=5.0,  # cap initial connection
            health_check_interval=30,  # ping every 30s to detect dead conns
            socket_keepalive=True,
            retry_on_timeout=True,
        )
        logger.info(
            "redis_client_initialized",
            extra={
                # Don't log the password. Only the host portion.
                "redis_host": settings.redis_url.split("@")[-1].split(":")[0]
                if "@" in settings.redis_url
                else settings.redis_url,
            },
        )
    return _client


async def close_redis() -> None:
    """Close the connection pool. Called from the lifespan shutdown hook."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("redis_client_closed")
