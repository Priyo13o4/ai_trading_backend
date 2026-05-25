import uuid
import asyncio
import logging
from typing import Optional

from ..auth import REDIS

logger = logging.getLogger(__name__)

_events_singleflight_local_locks: dict[str, asyncio.Lock] = {}
_events_singleflight_local_locks_guard = asyncio.Lock()

def _cache_key_token(value) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)

def _singleflight_lock_key(cache_key: str) -> str:
    return f"lock:{cache_key}"

async def _redis_get_best_effort(key: str) -> Optional[str]:
    try:
        return await REDIS.get(key)
    except Exception as exc:
        logger.warning("[API] Redis GET failed for key=%s: %s", key, exc)
        return None

async def _redis_setex_best_effort(key: str, ttl_seconds: int, value: str) -> bool:
    try:
        await REDIS.setex(key, ttl_seconds, value)
        return True
    except Exception as exc:
        logger.warning("[API] Redis SETEX failed for key=%s: %s", key, exc)
        return False

async def _redis_exists_best_effort(key: str) -> Optional[bool]:
    try:
        return bool(await REDIS.exists(key))
    except Exception as exc:
        logger.warning("[API] Redis EXISTS failed for key=%s: %s", key, exc)
        return None

async def _acquire_redis_lock(lock_key: str, lock_ttl_seconds: int) -> tuple[bool, str]:
    token = uuid.uuid4().hex
    try:
        acquired = bool(await REDIS.set(lock_key, token, nx=True, ex=lock_ttl_seconds))
        return acquired, token
    except Exception as exc:
        logger.warning("[API] Redis lock acquire failed for key=%s: %s", lock_key, exc)
        return False, token

async def _release_redis_lock_best_effort(lock_key: str, token: str) -> None:
    # Atomic compare-and-del so one request cannot release another request's lock.
    try:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        await REDIS.eval(script, 1, lock_key, token)
    except Exception as exc:
        logger.warning("[API] Redis lock release failed for key=%s: %s", lock_key, exc)
        return

async def _get_events_local_singleflight_lock(cache_key: str) -> asyncio.Lock:
    async with _events_singleflight_local_locks_guard:
        lock = _events_singleflight_local_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            _events_singleflight_local_locks[cache_key] = lock
        return lock

async def _cleanup_events_local_singleflight_lock(cache_key: str, lock: asyncio.Lock) -> None:
    async with _events_singleflight_local_locks_guard:
        current = _events_singleflight_local_locks.get(cache_key)
        if current is lock and not lock.locked():
            _events_singleflight_local_locks.pop(cache_key, None)
