"""API Web - Cache configuration.

Creates service-specific Redis client using shared utilities.
Provides cache utilities for web service needs.
"""

import json
import logging
import os
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from trading_common.cache import (
    create_redis_client,
    build_cache_key,
    invalidate_cache_pattern,
    get_cache_ttl,
    DEFAULT_CACHE_TTL
)

logger = logging.getLogger(__name__)

# Service-specific Redis client
redis_client = create_redis_client()

# Cache TTLs (re-exported for convenience)
CACHE_TTL = DEFAULT_CACHE_TTL


# Cache key builders
def candles_key(symbol: str, timeframe: str) -> str:
    """Generate Redis key for candlestick data."""
    return build_cache_key("candles", symbol, timeframe)


def news_key(symbol: str = "all") -> str:
    """Generate Redis key for news data."""
    return build_cache_key("news", symbol)


def strategies_key(symbol: str = "all") -> str:
    """Generate Redis key for strategies."""
    return build_cache_key("strategies", symbol)


def performance_key(symbol: str) -> str:
    """Generate Redis key for performance data."""
    return build_cache_key("performance", symbol)


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


class CandleCache:
    """Cache manager for candlestick data (read-only for api-web)."""
    
    @staticmethod
    def get(symbol: str, timeframe: str, limit: int = 500) -> Optional[List[Dict]]:
        """Get cached candlestick data."""
        try:
            key = candles_key(symbol, timeframe)
            data = redis_client.get(key)
            
            if data:
                candles = json.loads(data)
                return candles[:limit]
            
            return None
        except Exception as e:
            logger.error(f"Cache get error for {symbol} {timeframe}: {e}")
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
            
            redis_client.setex(key, ttl, json.dumps(news))
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
            
            redis_client.setex(key, ttl, json.dumps(markers))
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
            
            redis_client.setex(key, ttl, json.dumps(strategies))
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


def check_redis_connection():
    """Check Redis connection health."""
    try:
        redis_client.ping()
        return True
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        return False
