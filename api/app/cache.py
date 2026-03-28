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
            
            logger.info(f"Invalidated cache: {symbol or 'all'} {timeframe or 'all'}")
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
    
    @staticmethod
    def publish_candle_update(symbol: str, timeframe: str, candle: Dict):
        """Publish new candle update"""
        try:
            message = {
                'type': 'candle_update',
                'symbol': symbol,
                'timeframe': timeframe,
                'candle': candle,
                'timestamp': datetime.now().isoformat()
            }
            
            redis_client.publish(
                PubSubManager.CHANNELS['candles'],
                json.dumps(message)
            )
            
            logger.info(f"Published candle update: {symbol} {timeframe}")
            return True
        except Exception as e:
            logger.error(f"Publish error: {e}")
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
