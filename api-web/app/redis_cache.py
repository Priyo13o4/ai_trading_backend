"""Backward-compatible cache Redis shim.

This module keeps the historical ``app.redis_cache`` import path working while
delegating client construction to the segmented Redis pool.
"""

import os

from .redis_pool import CACHE_REDIS, RedisPool


def _build_url(prefix: str) -> str:
    host = os.getenv(f"{prefix}_HOST", "redis")
    port = os.getenv(f"{prefix}_PORT", "6379")
    db = os.getenv(f"{prefix}_DB", "0")
    password = os.getenv(f"{prefix}_PASSWORD")
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


CACHE_REDIS_URL = (
    os.getenv("APP_REDIS_URL")
    or os.getenv("CACHE_REDIS_URL")
    or _build_url("REDIS")
)

_SAFE_UNLOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) "
    "else return 0 end"
)


async def safe_unlock(redis_client, lock_key: str, lock_token: str) -> bool:
    """Delete a lock only when the caller still owns it."""
    if not lock_key or not lock_token:
        return False
    result = await redis_client.eval(_SAFE_UNLOCK_LUA, 1, lock_key, lock_token)
    return bool(result)


__all__ = ["CACHE_REDIS", "CACHE_REDIS_URL", "RedisPool", "safe_unlock"]
