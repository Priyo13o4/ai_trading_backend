import hashlib
import logging
import time
from fastapi import HTTPException, Request

from ..redis_cache import CACHE_REDIS

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _hash_key(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:24]


async def rate_limit(request: Request, bucket: str, limit_per_minute: int) -> None:
    ip = _client_ip(request)
    minute = int(time.time() // 60)
    key = f"rl:{bucket}:{_hash_key(ip)}:{minute}"

    try:
        pipe = CACHE_REDIS.pipeline()
        pipe.incr(key)
        pipe.expire(key, 120)
        count, _ = await pipe.execute()
    except Exception as err:
        logger.warning("rate_limit redis error: %s", err)
        return

    if int(count) > int(limit_per_minute):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
