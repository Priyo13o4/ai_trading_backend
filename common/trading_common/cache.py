"""Cache utilities - Factory functions only, no global instances."""

import json
import os
import redis
from typing import Optional, Any


def create_redis_client(
    host: Optional[str] = None,
    port: Optional[int] = None,
    password: Optional[str] = None,
    db: Optional[int] = None,
    decode_responses: bool = True
) -> redis.Redis:
    """
    Create Redis client instance.
    
    Args:
        host: Redis host (default: from REDIS_HOST env)
        port: Redis port (default: from REDIS_PORT env)
        password: Redis password (default: from REDIS_PASSWORD env)
        db: Redis database number (default: from REDIS_DB env)
        decode_responses: Decode responses to strings (default: True)
    
    Returns:
        Redis client instance
    """
    return redis.Redis(
        host=host or os.getenv('REDIS_HOST', 'redis'),
        port=int(port or os.getenv('REDIS_PORT', '6379')),
        password=password or os.getenv('REDIS_PASSWORD'),
        db=int(db or os.getenv('REDIS_DB', '0')),
        decode_responses=decode_responses
    )


def build_cache_key(*parts) -> str:
    """
    Build cache key from parts.
    
    Args:
        *parts: Key components
    
    Returns:
        Colon-separated cache key
    """
    return ':'.join(str(p) for p in parts if p)


def invalidate_cache_pattern(redis_client: redis.Redis, pattern: str) -> int:
    """
    Delete keys matching pattern.
    
    Args:
        redis_client: Redis client instance
        pattern: Key pattern to match (e.g., 'historical:XAUUSD:*')
    
    Returns:
        Number of keys deleted
    """
    count = 0
    try:
        for key in redis_client.scan_iter(match=pattern):
            redis_client.delete(key)
            count += 1
    except Exception:
        pass
    return count


# Cache TTL constants (in seconds)
DEFAULT_CACHE_TTL = {
    'candles': 300,      # 5 minutes
    'news': 600,         # 10 minutes
    'news_markers': 3600, # 1 hour
    'strategies': 300,   # 5 minutes
    'performance': 600,  # 10 minutes
    'historical': 300,   # 5 minutes
}


def get_cache_ttl(cache_type: str) -> int:
    """Get default TTL for cache type."""
    return DEFAULT_CACHE_TTL.get(cache_type, 300)
