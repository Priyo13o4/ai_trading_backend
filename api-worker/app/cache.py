"""API Worker - Cache configuration.

Creates service-specific Redis client and cache utilities using shared library.
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
from trading_common.timeframes import DERIVED_CAGG_TIMEFRAMES, normalize_timeframe

logger = logging.getLogger(__name__)

# Service-specific Redis client
redis_client = create_redis_client()

# Cache TTLs (re-exported for convenience)
CACHE_TTL = DEFAULT_CACHE_TTL


# Original cache utilities adapted to use service instance

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


def invalidate_historical_cache(symbol: str, timeframe: str) -> None:
    """Invalidate request-shaped historical response caches.

    Keys are shaped like:
      historical:{symbol}:{timeframe}:{start_date}:{end_date}:{before}:{limit}:{include_indicators}:{include_forming}

    Invalidation is intentionally broad: any cache entry for a symbol+TF is deleted.
    For M1 closes, derived TF caches are also invalidated because their truth source
    (Timescale CAGGs) depends on M1.

    Why M1 invalidates derived TF caches:
    - M5–H4 candles are *derived* from M1 via Timescale continuous aggregates.
    - When a new *closed* M1 candle lands, it can change the most recent derived bucket(s)
        after refresh/materialization, so any cached derived history may become stale.

    Why only closed candles invalidate:
    - Forming candles are ephemeral and are not written to Postgres.
    - Invalidating on every forming update would thrash caches and create load spikes.
    - Closed candles are the only events that definitively change persisted history.
    """
    try:
        sym = str(symbol or "").upper()
        tf = normalize_timeframe(timeframe)
        if not sym or not tf:
            return

        patterns: list[str] = [f"historical:{sym}:{tf}:*"]
        if tf == "M1":
            for derived in sorted(DERIVED_CAGG_TIMEFRAMES):
                patterns.append(f"historical:{sym}:{derived}:*")

        for pattern in patterns:
            invalidate_cache_pattern(redis_client, pattern)
    except Exception:
        # Invalidation should never break the main publish path.
        return


def _last_candle_ttl_seconds(is_forming: bool) -> int:
    """Get TTL for last candle snapshot."""
    if is_forming:
        return int(os.getenv("FORMING_LAST_CANDLE_TTL_SECONDS", "300"))
    return int(os.getenv("CLOSED_LAST_CANDLE_TTL_SECONDS", "86400"))


def set_last_candle_update(message: Dict[str, Any]) -> None:
    """Persist the latest candle_update message for fast replay on SSE connect."""
    try:
        if message.get("type") != "candle_update":
            return
        symbol = str(message.get("symbol") or "").upper()
        timeframe = str(message.get("timeframe") or "").upper()
        if not symbol or not timeframe:
            return
        is_forming = bool(message.get("is_forming"))
        key = last_candle_key(symbol, timeframe, is_forming=is_forming)
        redis_client.setex(key, _last_candle_ttl_seconds(is_forming), json.dumps(message))
    except Exception:
        # Snapshot caching should never break publish.
        return


def get_last_candle_update(symbol: str, timeframe: str, *, prefer_forming: bool = True) -> Optional[Dict[str, Any]]:
    """Get last candle update from cache."""
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
    """Cache manager for candlestick data."""
    
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
    
    @staticmethod
    def set(symbol: str, timeframe: str, candles: List[Dict], ttl: int = None):
        """Cache candlestick data."""
        try:
            key = candles_key(symbol, timeframe)
            ttl = ttl or CACHE_TTL['candles']
            
            redis_client.setex(key, ttl, json.dumps(candles))
            logger.info(f"Cached {len(candles)} candles for {symbol} {timeframe}")
            return True
        except Exception as e:
            logger.error(f"Cache set error for {symbol} {timeframe}: {e}")
            return False
    
    @staticmethod
    def append(symbol: str, timeframe: str, new_candle: Dict):
        """Append new candle to cached data."""
        try:
            key = candles_key(symbol, timeframe)
            data = redis_client.get(key)
            
            if data:
                candles = json.loads(data)
                candles.insert(0, new_candle)
                candles = candles[:1000]
                
                redis_client.setex(key, CACHE_TTL['candles'], json.dumps(candles))
                logger.info(f"Appended new candle to {symbol} {timeframe}")
                return True
            
            return False
        except Exception as e:
            logger.error(f"Cache append error for {symbol} {timeframe}: {e}")
            return False
    
    @staticmethod
    def invalidate(symbol: str = None, timeframe: str = None):
        """Invalidate cached data."""
        try:
            if symbol and timeframe:
                key = candles_key(symbol, timeframe)
                redis_client.delete(key)
            elif symbol:
                pattern = f"candles:{symbol}:*"
                invalidate_cache_pattern(redis_client, pattern)
            else:
                invalidate_cache_pattern(redis_client, "candles:*")

            if symbol is None and timeframe is None:
                logger.info("Invalidated cache: all")
            else:
                logger.debug(f"Invalidated cache: {symbol or 'all'} {timeframe or 'all'}")
            return True
        except Exception as e:
            logger.error(f"Cache invalidate error: {e}")
            return False


class PubSubManager:
    """Redis Pub/Sub manager for SSE broadcasting"""
    
    CHANNELS = {
        'candles': 'updates:candles',
        'news': 'updates:news',
        'strategies': 'updates:strategies',
    }
    
    # Stats tracking for periodic logging
    _publish_stats = {
        'last_log_time': datetime.now(),
        'forming_success': {},  # {symbol:tf: count}
        'forming_failures': {},  # {symbol:tf: count}
        'closed_success': 0,
        'closed_failures': 0,
    }
    
    @staticmethod
    def _log_publish_stats():
        """Log aggregated publish statistics every 5 minutes"""
        now = datetime.now()
        elapsed = (now - PubSubManager._publish_stats['last_log_time']).total_seconds()
        
        # Log every 5 minutes (300 seconds)
        if elapsed >= 300:
            forming_success = PubSubManager._publish_stats['forming_success']
            forming_failures = PubSubManager._publish_stats['forming_failures']
            closed_success = PubSubManager._publish_stats['closed_success']
            closed_failures = PubSubManager._publish_stats['closed_failures']
            
            if forming_success or forming_failures or closed_success or closed_failures:
                # Count unique symbol:tf combinations
                forming_success_count = sum(forming_success.values())
                forming_failure_count = sum(forming_failures.values())
                unique_symbols_tfs = len(forming_success) + len(forming_failures)
                
                logger.info(
                    f"[PubSub] 5min summary: "
                    f"Forming candles: {forming_success_count} success, {forming_failure_count} failures "
                    f"across {unique_symbols_tfs} symbol:TF pairs | "
                    f"Closed candles: {closed_success} success, {closed_failures} failures"
                )
                
                # Log any failures in detail
                if forming_failures:
                    logger.warning(f"[PubSub] Forming candle failures: {dict(forming_failures)}")
            
            # Reset stats
            PubSubManager._publish_stats['last_log_time'] = now
            PubSubManager._publish_stats['forming_success'].clear()
            PubSubManager._publish_stats['forming_failures'].clear()
            PubSubManager._publish_stats['closed_success'] = 0
            PubSubManager._publish_stats['closed_failures'] = 0
    
    @staticmethod
    def publish_candle_update(symbol: str, timeframe: str, candle: Dict[str, Any], is_forming: bool = False):
        """Publish new candle update"""
        try:
            message = {
                'type': 'candle_update',
                'symbol': symbol,
                'timeframe': timeframe,
                'candle': candle,
                'is_forming': is_forming,
                'timestamp': datetime.now().isoformat()
            }
            
            # Publish to Redis pub/sub channel for SSE
            redis_client.publish(
                PubSubManager.CHANNELS['candles'],
                json.dumps(message)
            )
            
            # Cache the last candle snapshot for SSE initial payload
            set_last_candle_update(message)
            
            # Track stats (aggregated logging every 5 minutes)
            key = f"{symbol}:{timeframe}"
            if is_forming:
                PubSubManager._publish_stats['forming_success'][key] = \
                    PubSubManager._publish_stats['forming_success'].get(key, 0) + 1
            else:
                PubSubManager._publish_stats['closed_success'] += 1
            
            # Check if it's time to log stats
            PubSubManager._log_publish_stats()
            
            return True
        except Exception as e:
            logger.error(f"[PubSub] Publish error for {symbol} {timeframe}: {e}")
            
            # Track failure
            key = f"{symbol}:{timeframe}"
            if is_forming:
                PubSubManager._publish_stats['forming_failures'][key] = \
                    PubSubManager._publish_stats['forming_failures'].get(key, 0) + 1
            else:
                PubSubManager._publish_stats['closed_failures'] += 1
            
            return False
    
    @staticmethod
    def publish_news_update(news: Dict[str, Any]):
        """Publish new news update"""
        try:
            from datetime import datetime
            message = {
                'type': 'news_update',
                'news': news,
                'timestamp': datetime.now().isoformat()
            }
            
            redis_client.publish(
                PubSubManager.CHANNELS['news'],
                json.dumps(message)
            )
            
            logger.info(f"Published news update")
            return True
        except Exception as e:
            logger.error(f"Publish error: {e}")
            return False
    
    @staticmethod
    def publish_strategy_update(strategy: Dict[str, Any]):
        """Publish new strategy update"""
        try:
            from datetime import datetime
            message = {
                'type': 'strategy_update',
                'strategy': strategy,
                'timestamp': datetime.now().isoformat()
            }
            
            redis_client.publish(
                PubSubManager.CHANNELS['strategies'],
                json.dumps(message)
            )
            
            logger.info(f"Published strategy update")
            return True
        except Exception as e:
            logger.error(f"Publish error: {e}")
            return False
    
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
