import os

import redis.asyncio as aioredis


def _build_url(prefix: str) -> str:
    host = os.getenv(f"{prefix}_HOST", "redis")
    port = os.getenv(f"{prefix}_PORT", "6379")
    db = os.getenv(f"{prefix}_DB", "0")
    password = os.getenv(f"{prefix}_PASSWORD")
    if password is None or password == "":
        raise RuntimeError(f"{prefix}_PASSWORD is required")
    return f"redis://:{password}@{host}:{port}/{db}"


CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL")
if not CACHE_REDIS_URL:
    CACHE_REDIS_URL = _build_url("REDIS")

CACHE_REDIS = aioredis.from_url(CACHE_REDIS_URL, decode_responses=True)
