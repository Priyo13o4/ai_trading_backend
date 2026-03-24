"""
Redis Connection Pool Manager with Role Segmentation
Backward-compatible - falls back to single Redis if new env vars not set.

This module provides role-based Redis connection management:
- queue: n8n queue operations (isolated to prevent queue pressure affecting app)
- app: api-web/api-sse cache, pubsub, soft locks
- session: auth sessions (already separate via SESSION_REDIS_*)

Usage:
    from app.redis_pool import RedisPool, CACHE_REDIS

    # Get specific Redis client by role
    app_redis = RedisPool.get_app_redis()
    session_redis = RedisPool.get_session_redis()

    # Or use the backward-compatible alias
    await CACHE_REDIS.get("key")

Environment Variables:
    # Existing (backward compatible):
    REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD
    CACHE_REDIS_URL

    # New (optional, for explicit segmentation):
    QUEUE_REDIS_URL    # n8n queue (defaults to main Redis if not set)
    APP_REDIS_URL      # api-web/sse cache, pubsub, soft locks (defaults to main)
    SESSION_REDIS_URL  # Sessions (defaults to SESSION_REDIS_* env vars)
"""
import os
import logging
from typing import Optional
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Redis key namespace prefixes by service
# Used to enforce logical separation even within shared Redis
REDIS_NAMESPACES = {
    "api_web": "api:",
    "api_sse": "sse:",
    "n8n": "n8n:",
    "session": "sess:",
    "webhook": "wh:",
    "janitor": "jan:",
    "lock": "lock:",
}


def _build_redis_url(prefix: str) -> str:
    """
    Build Redis URL from individual env vars.

    Args:
        prefix: Environment variable prefix (e.g., "REDIS", "SESSION_REDIS")

    Returns:
        Redis URL string
    """
    host = os.getenv(f"{prefix}_HOST", "redis")
    port = os.getenv(f"{prefix}_PORT", "6379")
    db = os.getenv(f"{prefix}_DB", "0")
    password = os.getenv(f"{prefix}_PASSWORD", "")

    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


def namespaced_key(service: str, key: str) -> str:
    """
    Ensure a Redis key is properly namespaced.

    Args:
        service: Service name (e.g., "api_web", "session")
        key: The key to namespace

    Returns:
        Namespaced key string

    Example:
        namespaced_key("api_web", "user:123") -> "api:user:123"
    """
    prefix = REDIS_NAMESPACES.get(service, "misc:")
    if key.startswith(prefix):
        return key  # Already namespaced
    return f"{prefix}{key}"


class RedisPool:
    """
    Manages Redis connections with role-based segmentation.

    Roles:
    - queue: n8n queue operations (can be separate Redis for isolation)
    - app: api-web/sse cache, pubsub, soft locks
    - session: auth sessions (already separate via SESSION_REDIS_*)

    Thread Safety:
        Redis clients are lazily initialized on first access.
        The connection pool is managed by redis-py internally.
    """

    _queue_redis: Optional[aioredis.Redis] = None
    _app_redis: Optional[aioredis.Redis] = None
    _session_redis: Optional[aioredis.Redis] = None
    _initialized: bool = False

    @classmethod
    def get_queue_redis(cls) -> aioredis.Redis:
        """
        Get Redis client for queue operations (n8n).

        Falls back to main Redis if QUEUE_REDIS_URL not set.
        """
        if cls._queue_redis is None:
            url = (
                os.getenv("QUEUE_REDIS_URL")
                or os.getenv("CACHE_REDIS_URL")
                or _build_redis_url("REDIS")
            )
            cls._queue_redis = aioredis.from_url(
                url,
                decode_responses=True,
                max_connections=20
            )
            logger.info("Queue Redis initialized: %s", url.split("@")[-1] if "@" in url else url)
        return cls._queue_redis

    @classmethod
    def get_app_redis(cls) -> aioredis.Redis:
        """
        Get Redis client for app cache/pubsub/locks.

        Falls back to main Redis if APP_REDIS_URL not set.
        """
        if cls._app_redis is None:
            url = (
                os.getenv("APP_REDIS_URL")
                or os.getenv("CACHE_REDIS_URL")
                or _build_redis_url("REDIS")
            )
            cls._app_redis = aioredis.from_url(
                url,
                decode_responses=True,
                max_connections=50
            )
            logger.info("App Redis initialized: %s", url.split("@")[-1] if "@" in url else url)
        return cls._app_redis

    @classmethod
    def get_session_redis(cls) -> aioredis.Redis:
        """
        Get Redis client for session storage.

        Uses SESSION_REDIS_* env vars by default.
        """
        if cls._session_redis is None:
            url = (
                os.getenv("SESSION_REDIS_URL")
                or _build_redis_url("SESSION_REDIS")
            )
            cls._session_redis = aioredis.from_url(
                url,
                decode_responses=True,
                max_connections=30
            )
            logger.info("Session Redis initialized: %s", url.split("@")[-1] if "@" in url else url)
        return cls._session_redis

    @classmethod
    async def close_all(cls):
        """
        Close all Redis connections.

        Call this on application shutdown.
        """
        pools = [
            ("queue", cls._queue_redis),
            ("app", cls._app_redis),
            ("session", cls._session_redis),
        ]
        for name, pool in pools:
            if pool:
                try:
                    await pool.aclose()
                    logger.info("%s Redis connection closed", name)
                except Exception as e:
                    logger.error("Error closing %s Redis: %s", name, e)

        cls._queue_redis = None
        cls._app_redis = None
        cls._session_redis = None

    @classmethod
    async def health_check(cls) -> dict:
        """
        Check health of all Redis connections.

        Returns:
            Dict with health status for each Redis role
        """
        results = {}
        for name, getter in [
            ("queue", cls.get_queue_redis),
            ("app", cls.get_app_redis),
            ("session", cls.get_session_redis),
        ]:
            try:
                redis_client = getter()
                await redis_client.ping()
                results[name] = {"status": "healthy"}
            except Exception as e:
                results[name] = {"status": "unhealthy", "error": str(e)[:100]}
        return results


# ============================================================================
# BACKWARD COMPATIBLE ALIASES
# ============================================================================

# Existing code uses CACHE_REDIS directly - this maintains compatibility
def _get_cache_redis() -> aioredis.Redis:
    """Lazy initialization wrapper for backward compatibility."""
    return RedisPool.get_app_redis()


# Create a lazy wrapper that defers initialization
class _LazyRedis:
    """Wrapper that defers Redis initialization until first use."""

    def __init__(self, getter):
        self._getter = getter
        self._client = None

    def __getattr__(self, name):
        if self._client is None:
            self._client = self._getter()
        return getattr(self._client, name)


# Backward-compatible alias - existing code continues to work
CACHE_REDIS = _LazyRedis(_get_cache_redis)


# ============================================================================
# PUBSUB CHANNEL NAMING
# ============================================================================

# Standardized channel names with namespace prefixes
PUBSUB_CHANNELS = {
    "candles": "sse:updates:candles",
    "news": "sse:updates:news",
    "strategies": "sse:updates:strategies",
    "webhook_processed": "api:events:webhook_processed",
    "subscription_updated": "api:events:subscription_updated",
}


def get_pubsub_channel(name: str) -> str:
    """
    Get the standardized pubsub channel name.

    Args:
        name: Logical channel name (e.g., "candles", "news")

    Returns:
        Namespaced channel name
    """
    return PUBSUB_CHANNELS.get(name, f"misc:{name}")
