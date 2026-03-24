"""API Web - Cache configuration.

Creates service-specific Redis client using shared utilities.
Provides cache utilities for web service needs.
"""

import hashlib
import json
import logging
import os
from typing import Optional, List, Dict, Any, Iterable
from app.utils import json_dumps
from datetime import datetime, timezone

from trading_common.cache import (
    create_redis_client,
    build_cache_key,
    DEFAULT_CACHE_TTL
)

logger = logging.getLogger(__name__)

# Service-specific Redis client
redis_client = create_redis_client()

# Cache TTLs (re-exported for convenience)
CACHE_TTL = DEFAULT_CACHE_TTL


# Cache key builders
def news_key(symbol: str = "all") -> str:
    """Generate Redis key for news data."""
    return build_cache_key("news", symbol)


def strategies_key(symbol: str = "all") -> str:
    """Generate Redis key for strategies."""
    normalized_symbol = str(symbol or "all").strip()
    if not normalized_symbol:
        normalized_symbol = "all"
    elif normalized_symbol.lower() != "all":
        normalized_symbol = normalized_symbol.upper()
    else:
        normalized_symbol = "all"

    return build_cache_key("strategies", normalized_symbol)


def last_candle_key(symbol: str, timeframe: str, *, is_forming: bool) -> str:
    """Generate key for last candle snapshot."""
    suffix = "forming" if is_forming else "closed"
    return build_cache_key("candles", "last", suffix, symbol, timeframe)


def get_last_candle_update(symbol: str, timeframe: str, *, prefer_forming: bool = True) -> Optional[Dict[str, Any]]:
    """Get last candle update from cache (for SSE initial payload)."""
    try:
        symbol = str(symbol or "").upper()
        timeframe = str(timeframe or "").upper()
        if not symbol or not timeframe:
            return None

        keys: list[str] = []
        if prefer_forming:
            keys.append(last_candle_key(symbol, timeframe, is_forming=True))
        keys.append(last_candle_key(symbol, timeframe, is_forming=False))

        for key in keys:
            raw = redis_client.get(key)
            if raw:
                return json.loads(raw)
    except Exception:
        return None
    return None


class NewsCache:
    """Cache manager for news data."""
    
    @staticmethod
    def get(symbol: str = "all") -> Optional[List[Dict]]:
        """Get cached news."""
        try:
            key = news_key(symbol)
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"News cache get error: {e}")
            return None
    
    @staticmethod
    def set(news: List[Dict], symbol: str = "all", ttl: int = None):
        """Cache news data."""
        try:
            key = news_key(symbol)
            ttl = ttl or CACHE_TTL['news']
            
            redis_client.setex(key, ttl, json_dumps(news))
            logger.info(f"Cached {len(news)} news items")
            return True
        except Exception as e:
            logger.error(f"News cache set error: {e}")
            return False


class NewsMarkersCache:
    """Cache manager for news markers (chart annotations)."""
    
    @staticmethod
    def get(symbol: str, hours: int = 168) -> Optional[List[Dict]]:
        """Get cached news markers for symbol and time range."""
        try:
            key = f"news_markers:{symbol}:{hours}h"
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"News markers cache get error for {symbol}: {e}")
            return None
    
    @staticmethod
    def set(symbol: str, markers: List[Dict], hours: int = 168, ttl: int = None):
        """Cache news markers."""
        try:
            key = f"news_markers:{symbol}:{hours}h"
            ttl = ttl or CACHE_TTL['news_markers']
            
            redis_client.setex(key, ttl, json_dumps(markers))
            logger.info(f"Cached {len(markers)} news markers for {symbol}")
            return True
        except Exception as e:
            logger.error(f"News markers cache set error for {symbol}: {e}")
            return False


class StrategyCache:
    """Cache manager for strategy data."""
    
    @staticmethod
    def get(symbol: str = "all") -> Optional[List[Dict]]:
        """Get cached strategies."""
        try:
            key = strategies_key(symbol)
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Strategy cache get error: {e}")
            return None
    
    @staticmethod
    def set(strategies: List[Dict], symbol: str = "all", ttl: int = None):
        """Cache strategy data."""
        try:
            key = strategies_key(symbol)
            ttl = ttl or CACHE_TTL['strategies']
            
            redis_client.setex(key, ttl, json_dumps(strategies))
            logger.info(f"Cached {len(strategies)} strategies")
            return True
        except Exception as e:
            logger.error(f"Strategy cache set error: {e}")
            return False


class PubSubManager:
    """Redis Pub/Sub manager for SSE broadcasting"""
    
    CHANNELS = {
        'candles': 'updates:candles',
        'news': 'updates:news',
        'strategies': 'updates:strategies',
    }
    
    @staticmethod
    def subscribe(channel: str):
        """Subscribe to a channel"""
        pubsub = redis_client.pubsub()
        pubsub.subscribe(channel)
        return pubsub


class MutationGatedPublisher:
    """Skip duplicate SSE publishes when the payload has not changed."""

    def __init__(self):
        self._last_hashes: Dict[str, str] = {}

    @staticmethod
    def _compute_hash(data: str) -> str:
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def publish_if_changed(self, channel: str, payload: Dict[str, Any]) -> bool:
        serialized = json_dumps(payload)
        payload_hash = self._compute_hash(serialized)
        if self._last_hashes.get(channel) == payload_hash:
            return False

        self._last_hashes[channel] = payload_hash
        redis_client.publish(channel, serialized)
        return True


_mutation_gated_publisher = MutationGatedPublisher()


def _publish_payload(channel: str, payload: Dict[str, Any]) -> bool:
    try:
        return _mutation_gated_publisher.publish_if_changed(channel, payload)
    except Exception as e:
        logger.error(f"PubSub publish error: {e}")
        return False


def publish_news_snapshot(news: List[Dict[str, Any]]) -> bool:
    """Publish a news snapshot event for SSE consumers."""
    payload = {
        "type": "news_snapshot",
        "news": news,
        "server_ts": datetime.now(timezone.utc).isoformat(),
    }
    return _publish_payload(PubSubManager.CHANNELS["news"], payload)


def publish_strategies_snapshot(strategies: List[Dict[str, Any]]) -> bool:
    """Publish a strategies snapshot event for SSE consumers."""
    payload = {
        "type": "strategies_snapshot",
        "strategies": strategies,
        "server_ts": datetime.now(timezone.utc).isoformat(),
    }
    return _publish_payload(PubSubManager.CHANNELS["strategies"], payload)


def publish_strategy_update(strategy: Dict[str, Any]) -> bool:
    """Publish a single strategy update event for SSE consumers."""
    payload = {
        "type": "strategy_update",
        "strategy": strategy,
        "server_ts": datetime.now(timezone.utc).isoformat(),
    }
    return _publish_payload(PubSubManager.CHANNELS["strategies"], payload)


def invalidate_strategy_cache_domain(strategy_ids: Iterable[int]) -> Dict[str, int]:
    """Invalidate strategy detail keys and strategy list keys under latest:strategies:*."""
    deleted_detail = 0
    deleted_list = 0

    try:
        normalized_ids = sorted({int(x) for x in strategy_ids if x is not None})
        if not normalized_ids:
            return {"deleted_detail": 0, "deleted_list": 0}

        for strategy_id in normalized_ids:
            key = f"latest:strategy:id:{strategy_id}"
            try:
                deleted_detail += int(redis_client.delete(key) or 0)
            except Exception:
                logger.warning("Failed to delete strategy detail key %s", key, exc_info=True)

        # Selective domain invalidation for list/read models.
        list_keys = list(redis_client.scan_iter(match="latest:strategies:*"))
        if list_keys:
            deleted_list = int(redis_client.delete(*list_keys) or 0)

        return {"deleted_detail": deleted_detail, "deleted_list": deleted_list}
    except Exception as e:
        logger.error("Strategy cache invalidation failed: %s", e, exc_info=True)
        return {"deleted_detail": deleted_detail, "deleted_list": deleted_list}
