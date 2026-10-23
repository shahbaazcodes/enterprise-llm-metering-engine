"""Async Redis 7 client singleton.

Redis backs two responsibilities of this service:

1. **Distributed idempotency locks** - ``SET key value NX EX 300`` provides
   an atomic compare-and-set across every API replica.
2. **Sub-millisecond quota checks** - a sliding-window log and a reserved /
   settled spend counter, both mutated by server-side Lua scripts so that a
   check and a mutation can never interleave.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis

from app.settings import get_settings

logger = logging.getLogger("llm.metering.redis")

_redis: redis.Redis[bytes] | None = None


def get_redis() -> redis.Redis[bytes]:
    """Return the process-wide async Redis client, creating it on first use."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis.Redis(
            connection_pool=redis.ConnectionPool.from_url(
                settings.redis_url,
                max_connections=50,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_timeout,
                decode_responses=False,
            ),
            encoding="utf-8",
        )
    return _redis


async def ping() -> bool:
    """Health probe - ``False`` when Redis is unreachable."""
    try:
        return bool(await get_redis().ping())
    except Exception:  # noqa: BLE001 - any failure means the dependency is down
        logger.warning("redis ping failed", exc_info=True)
        return False


async def close_redis() -> None:
    """Close the Redis connection pool, if it is open."""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("redis close failed", exc_info=True)
        _redis = None


def set_redis_override(client: redis.Redis[bytes] | None) -> None:
    """Inject an in-process Redis client for tests / LocalStack scenarios.

    When ``None`` is passed (or explicitly set), ``get_redis`` returns the
    real client normally.  This lets the eval harness swap in ``fakeredis``
    and guarantees the Lua scripts execute faithfully under lupa.
    """
    global _redis
    _redis = client
