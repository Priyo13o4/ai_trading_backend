import asyncio
import json
import logging
from typing import Callable, Any
from functools import wraps
from .auth import REDIS

logger = logging.getLogger(__name__)

_singleflight_locks = {}
_singleflight_guard = asyncio.Lock()

def singleflight_cache(key_prefix: str = None, ttl: int = 300, ttl_func: Callable[[Any], int] = None, key_builder: Callable = None):
    """
    Decorator that applies Singleflight (local concurrency coalescing) 
    and Cache-Aside pattern.

    Assumes the decorated function is async. Use key_builder to customize the cache key.
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if key_builder:
                key = key_builder(*args, **kwargs)
            else:
                # Try to build a unique key suffix from args/kwargs (excluding db session)
                # We assume 'db' is usually passed as first arg or kwarg and should be ignored
                key_args = []
                for arg in args:
                    if hasattr(arg, 'execute') or 'Session' in str(type(arg)):
                        continue # Skip DB session
                    key_args.append(str(arg))
                for k, v in sorted(kwargs.items()):
                    if k == 'db' or 'Session' in str(type(v)):
                        continue
                    key_args.append(f"{k}={v}")
                    
                suffix = ":".join(key_args)
                key = f"{key_prefix}:{suffix}" if suffix else key_prefix

            # 1. Fast path cache check
            cached = await REDIS.get(key)
            if cached:
                try:
                    return json.loads(cached)
                except Exception as e:
                    logger.warning(f"Singleflight cache parse error for {key}: {e}")
            
            # 2. Local Coalescing (Singleflight)
            async with _singleflight_guard:
                if key not in _singleflight_locks:
                    _singleflight_locks[key] = asyncio.Event()
                    is_leader = True
                    event = _singleflight_locks[key]
                else:
                    is_leader = False
                    event = _singleflight_locks[key]
                    
            if not is_leader:
                await event.wait() # Wait for leader to finish
                cached = await REDIS.get(key)
                if cached:
                    try:
                        return json.loads(cached)
                    except Exception:
                        pass
                # If cache still misses (e.g. error in leader), proceed to query
                
            # 3. Execution (Only 1 request per worker node makes it here)
            try:
                data = await func(*args, **kwargs)
                if data is not None:
                    from app.utils import json_dumps
                    try:
                        serialized = json_dumps(data)
                        final_ttl = ttl_func(data) if ttl_func else ttl
                        await REDIS.setex(key, final_ttl, serialized)
                    except Exception as e:
                        logger.error(f"Failed to serialize singleflight cache {key}: {e}")
                return data
            finally:
                if is_leader:
                    async with _singleflight_guard:
                        if key in _singleflight_locks:
                            event = _singleflight_locks.pop(key)
                            event.set() # Wake up waiters
        return wrapper
    return decorator
