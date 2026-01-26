"""
Redis Cache Layer for Trading Bot
Handles caching of candlestick data, news, and strategies
"""

import json
import os
import redis
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging

from .timeframes import DERIVED_CAGG_TIMEFRAMES, normalize_timeframe

logger = logging.getLogger(__name__)

# Redis connection


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"{name} is required")
    return value


redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'redis'),
    port=int(os.getenv('REDIS_PORT', '6379')),
    password=_required_env('REDIS_PASSWORD'),
    db=int(os.getenv('REDIS_DB', '0')),
    decode_responses=True
)

# Cache TTLs (in seconds)
CACHE_TTL = {
    'candles': 300,      # 5 minutes (matches update interval)
    'news': 600,         # 10 minutes
    'news_markers': 3600, # 1 hour (news doesn't change often)
    'strategies': 300,   # 5 minutes
    'performance': 600,  # 10 minutes
}

# Redis key patterns
def candles_key(symbol: str, timeframe: str) -> str:
    """Generate Redis key for candlestick data"""
    return f"candles:{symbol}:{timeframe}"

def news_key(symbol: str = "all") -> str:
    """Generate Redis key for news data"""
    return f"news:{symbol}"

def strategies_key(symbol: str = "all") -> str:
    """Generate Redis key for strategies"""
    return f"strategies:{symbol}"

def performance_key(symbol: str) -> str:
    """Generate Redis key for performance data"""
    return f"performance:{symbol}"


def last_candle_key(symbol: str, timeframe: str, *, is_forming: bool) -> str:
    suffix = "forming" if is_forming else "closed"
    return f"candles:last:{suffix}:{symbol}:{timeframe}"


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
            for key in redis_client.scan_iter(match=pattern):
                redis_client.delete(key)
    except Exception:
        # Invalidation should never break the main publish path.
        return


def _last_candle_ttl_seconds(is_forming: bool) -> int:
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
    """Cache manager for candlestick data"""
    
    @staticmethod
    def get(symbol: str, timeframe: str, limit: int = 500) -> Optional[List[Dict]]:
        """Get cached candlestick data"""
        try:
            key = candles_key(symbol, timeframe)
            data = redis_client.get(key)
            
            if data:
                candles = json.loads(data)
                # Return only requested limit
                return candles[:limit]
            
            return None
        except Exception as e:
            logger.error(f"Cache get error for {symbol} {timeframe}: {e}")
            return None
    
    @staticmethod
    def set(symbol: str, timeframe: str, candles: List[Dict], ttl: int = None):
        """Cache candlestick data"""
        try:
            key = candles_key(symbol, timeframe)
            ttl = ttl or CACHE_TTL['candles']
            
            # Store as JSON
            redis_client.setex(
                key,
                ttl,
                json.dumps(candles)
            )
            
            logger.info(f"Cached {len(candles)} candles for {symbol} {timeframe}")
            return True
        except Exception as e:
            logger.error(f"Cache set error for {symbol} {timeframe}: {e}")
            return False
    
    @staticmethod
    def append(symbol: str, timeframe: str, new_candle: Dict):
        """Append new candle to cached data"""
        try:
            key = candles_key(symbol, timeframe)
            data = redis_client.get(key)
            
            if data:
                candles = json.loads(data)
                # Add new candle at the beginning (newest first)
                candles.insert(0, new_candle)
                # Keep only last 1000 candles in cache
                candles = candles[:1000]
                
                # Update cache
                redis_client.setex(
                    key,
                    CACHE_TTL['candles'],
                    json.dumps(candles)
                )
                
                logger.info(f"Appended new candle to {symbol} {timeframe}")
                return True
            
            return False
        except Exception as e:
            logger.error(f"Cache append error for {symbol} {timeframe}: {e}")
            return False
    
    @staticmethod
    def invalidate(symbol: str = None, timeframe: str = None):
        """Invalidate cached data"""
        try:
            if symbol and timeframe:
                # Invalidate specific pair
                key = candles_key(symbol, timeframe)
                redis_client.delete(key)
            elif symbol:
                # Invalidate all timeframes for symbol
                pattern = f"candles:{symbol}:*"
                for key in redis_client.scan_iter(match=pattern):
                    redis_client.delete(key)
            else:
                # Invalidate all candles
                for key in redis_client.scan_iter(match="candles:*"):
                    redis_client.delete(key)

            # Per-minute invalidations (e.g., M1 closes) can be very noisy; keep global wipes visible.
            if symbol is None and timeframe is None:
                logger.info("Invalidated cache: all")
            else:
                logger.debug(f"Invalidated cache: {symbol or 'all'} {timeframe or 'all'}")
            return True
        except Exception as e:
            logger.error(f"Cache invalidate error: {e}")
            return False


class NewsCache:
    """Cache manager for news data"""
    
    @staticmethod
    def get(symbol: str = "all") -> Optional[List[Dict]]:
        """Get cached news"""
        try:
            key = news_key(symbol)
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"News cache get error: {e}")
            return None
    
    @staticmethod
    def set(news: List[Dict], symbol: str = "all", ttl: int = None):
        """Cache news data"""
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
    """Cache manager for news markers (chart annotations)"""
    
    @staticmethod
    def get(symbol: str, hours: int = 168) -> Optional[List[Dict]]:
        """Get cached news markers for symbol and time range
        
        Args:
            symbol: Trading pair (e.g., 'XAUUSD')
            hours: Time range in hours (default 168 = 1 week)
        """
        try:
            key = f"news_markers:{symbol}:{hours}h"
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"News markers cache get error for {symbol}: {e}")
            return None
    
    @staticmethod
    def set(symbol: str, markers: List[Dict], hours: int = 168, ttl: int = None):
        """Cache news markers
        
        Args:
            symbol: Trading pair
            markers: List of news marker objects
            hours: Time range cached
            ttl: Cache expiration in seconds
        """
        try:
            key = f"news_markers:{symbol}:{hours}h"
            ttl = ttl or CACHE_TTL['news_markers']
            
            redis_client.setex(key, ttl, json.dumps(markers))
            logger.info(f"Cached {len(markers)} news markers for {symbol} ({hours}h range)")
            return True
        except Exception as e:
            logger.error(f"News markers cache set error for {symbol}: {e}")
            return False
    
    @staticmethod
    def invalidate(symbol: str = None):
        """Invalidate cached news markers"""
        try:
            pattern = f"news_markers:{symbol}:*" if symbol else "news_markers:*"
            
            deleted = 0
            for key in redis_client.scan_iter(match=pattern):
                redis_client.delete(key)
                deleted += 1
            
            logger.info(f"Invalidated {deleted} news markers cache entries for {symbol or 'all symbols'}")
            return True
        except Exception as e:
            logger.error(f"News markers cache invalidate error: {e}")
            return False


class StrategyCache:
    """Cache manager for strategy data"""
    
    @staticmethod
    def get(symbol: str = "all") -> Optional[List[Dict]]:
        """Get cached strategies"""
        try:
            key = strategies_key(symbol)
            data = redis_client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Strategy cache get error: {e}")
            return None
    
    @staticmethod
    def set(strategies: List[Dict], symbol: str = "all", ttl: int = None):
        """Cache strategy data"""
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

    # Reduce log noise: aggregate successful publish counts and emit periodically.
    _candle_pub_window_started_at: Optional[datetime] = None
    _candle_pub_last_log_at: Optional[datetime] = None
    _candle_pub_counts: Dict[str, int] = {}
    _candle_pub_symbols: Dict[str, int] = {}
    _candle_pub_total: int = 0
    _candle_pub_forming_total: int = 0
    _candle_pub_fail_counts: Dict[str, int] = {}
    _candle_pub_fail_symbols: Dict[str, int] = {}
    _candle_pub_fail_total: int = 0
    _candle_pub_fail_last_err: Optional[str] = None
    _CANDLE_PUB_LOG_EVERY = timedelta(minutes=5)
    
    @staticmethod
    def publish_candle_update(symbol: str, timeframe: str, candle: Dict, is_forming: bool = False):
        """Publish new candle update"""
        try:
            # Only invalidate response caches on closed candles.
            # Forming candles are ephemeral (Redis/SSE only) and not persisted.
            if not bool(is_forming):
                invalidate_historical_cache(symbol, timeframe)

            message = {
                'type': 'candle_update',
                'symbol': symbol,
                'timeframe': timeframe,
                'candle': candle,
                'is_forming': bool(is_forming),
                'timestamp': datetime.now().isoformat()
            }

            # Cache the latest message for instant replay to new SSE clients.
            set_last_candle_update(message)
            
            redis_client.publish(
                PubSubManager.CHANNELS['candles'],
                json.dumps(message)
            )

            # Success logging (throttled)
            now = datetime.utcnow()
            if PubSubManager._candle_pub_window_started_at is None:
                PubSubManager._candle_pub_window_started_at = now
                PubSubManager._candle_pub_last_log_at = now

            tf_key = normalize_timeframe(timeframe)
            PubSubManager._candle_pub_counts[tf_key] = PubSubManager._candle_pub_counts.get(tf_key, 0) + 1
            sym_key = (symbol or "").strip().upper()
            if sym_key:
                PubSubManager._candle_pub_symbols[sym_key] = PubSubManager._candle_pub_symbols.get(sym_key, 0) + 1
            PubSubManager._candle_pub_total += 1
            if bool(is_forming):
                PubSubManager._candle_pub_forming_total += 1

            last_log = PubSubManager._candle_pub_last_log_at or now
            if (now - last_log) >= PubSubManager._CANDLE_PUB_LOG_EVERY:
                # Requested compact format: Success/Failed with symbols + TFs.
                symbols = list(sorted(PubSubManager._candle_pub_symbols.keys()))
                tfs = list(sorted(PubSubManager._candle_pub_counts.keys()))
                logger.info(
                    "Redis candle publish Success: symbols=%s tfs=%s total=%d forming=%d",
                    symbols,
                    tfs,
                    PubSubManager._candle_pub_total,
                    PubSubManager._candle_pub_forming_total,
                )

                fail_symbols = list(sorted(PubSubManager._candle_pub_fail_symbols.keys()))
                fail_tfs = list(sorted(PubSubManager._candle_pub_fail_counts.keys()))
                logger.info(
                    "Redis candle publish Failed: symbols=%s tfs=%s total=%d last_err=%s",
                    fail_symbols,
                    fail_tfs,
                    PubSubManager._candle_pub_fail_total,
                    PubSubManager._candle_pub_fail_last_err,
                )

                PubSubManager._candle_pub_last_log_at = now
                PubSubManager._candle_pub_window_started_at = now
                PubSubManager._candle_pub_counts = {}
                PubSubManager._candle_pub_symbols = {}
                PubSubManager._candle_pub_total = 0
                PubSubManager._candle_pub_forming_total = 0
                PubSubManager._candle_pub_fail_counts = {}
                PubSubManager._candle_pub_fail_symbols = {}
                PubSubManager._candle_pub_fail_total = 0
                PubSubManager._candle_pub_fail_last_err = None
            return True
        except Exception as e:
            # Track failures for the next throttled summary line.
            tf_key = normalize_timeframe(timeframe)
            PubSubManager._candle_pub_fail_counts[tf_key] = PubSubManager._candle_pub_fail_counts.get(tf_key, 0) + 1
            sym_key = (symbol or "").strip().upper()
            if sym_key:
                PubSubManager._candle_pub_fail_symbols[sym_key] = PubSubManager._candle_pub_fail_symbols.get(sym_key, 0) + 1
            PubSubManager._candle_pub_fail_total += 1
            PubSubManager._candle_pub_fail_last_err = str(e)

            # Requested compact format; keep the exception for debugging.
            logger.error(
                "Redis candle publish Failed: symbols=%s tfs=%s err=%s",
                [(symbol or "").strip().upper()],
                [normalize_timeframe(timeframe)],
                str(e),
            )
            return False
    
    @staticmethod
    def publish_news_update(news: Dict):
        """Publish new news update"""
        try:
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
    def publish_strategy_update(strategy: Dict):
        """Publish new strategy update"""
        try:
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


# Health check
def check_redis_connection() -> bool:
    """Check if Redis is accessible"""
    try:
        redis_client.ping()
        return True
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")
        return False
