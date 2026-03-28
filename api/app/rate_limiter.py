#!/usr/bin/env python3
"""
Redis-Based API Rate Limiter
============================
Centralized rate limiting for Twelve Data API calls.
Shared across all scripts, persists across restarts.

Rules:
- 8 calls/min per API key
- Auto-switch to next key when limit hit
- 2 keys available = 16 calls/min total
- Tracks usage in Redis with TTL
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger("rate_limiter")

# API Keys from environment
API_KEYS = [k for k in [
    os.getenv("TWELVE_DATA_API_KEY"),
    os.getenv("TWELVE_DATA_API_KEY_2")
] if k]

CALLS_PER_MINUTE_PER_KEY = 8
WINDOW_SECONDS = 60

# Redis client (lazy init)
_redis_client = None


def _get_redis():
    """Get Redis client (lazy initialization)"""
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.Redis(
                host=os.getenv('REDIS_HOST', 'n8n-redis'),
                port=int(os.getenv('REDIS_PORT', 6379)),
                password=os.getenv('REDIS_PASSWORD'),
                db=0,
                decode_responses=True
            )
            _redis_client.ping()
            logger.info("Rate limiter connected to Redis")
        except Exception as e:
            logger.warning(f"Redis not available for rate limiting: {e}")
            _redis_client = None
    return _redis_client


def _get_key_usage(api_key: str) -> int:
    """Get current usage count for an API key in the current minute window"""
    redis = _get_redis()
    if not redis:
        return 0
    
    # Key format: rate_limit:{api_key_hash}:{minute_timestamp}
    key_hash = api_key[-8:]  # Use last 8 chars as identifier
    current_minute = int(time.time() // WINDOW_SECONDS)
    redis_key = f"rate_limit:{key_hash}:{current_minute}"
    
    try:
        count = redis.get(redis_key)
        return int(count) if count else 0
    except Exception as e:
        logger.warning(f"Failed to get rate limit count: {e}")
        return 0


def _increment_key_usage(api_key: str) -> int:
    """Increment usage count for an API key, returns new count"""
    redis = _get_redis()
    if not redis:
        return 1
    
    key_hash = api_key[-8:]
    current_minute = int(time.time() // WINDOW_SECONDS)
    redis_key = f"rate_limit:{key_hash}:{current_minute}"
    
    try:
        # Increment and set TTL (auto-expire after 2 minutes)
        pipe = redis.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, WINDOW_SECONDS * 2)
        results = pipe.execute()
        return results[0]  # New count after increment
    except Exception as e:
        logger.warning(f"Failed to increment rate limit: {e}")
        return 1


def get_available_api_key() -> Tuple[Optional[str], dict]:
    """
    Get an API key that has available quota.
    Auto-switches to next key if current is exhausted.
    
    Returns:
        Tuple of (api_key, status_dict)
        status_dict contains: {
            'key_index': int,
            'usage': int,
            'limit': int,
            'all_exhausted': bool
        }
    """
    if not API_KEYS:
        logger.error("No API keys configured!")
        return None, {'error': 'No API keys configured'}
    
    status = {
        'key_index': -1,
        'usage': 0,
        'limit': CALLS_PER_MINUTE_PER_KEY,
        'all_exhausted': False,
        'keys_checked': []
    }
    
    # Try each key
    for idx, key in enumerate(API_KEYS):
        usage = _get_key_usage(key)
        key_status = {
            'index': idx,
            'usage': usage,
            'available': usage < CALLS_PER_MINUTE_PER_KEY
        }
        status['keys_checked'].append(key_status)
        
        if usage < CALLS_PER_MINUTE_PER_KEY:
            status['key_index'] = idx
            status['usage'] = usage
            logger.debug(f"Using API key #{idx+1} (usage: {usage}/{CALLS_PER_MINUTE_PER_KEY})")
            return key, status
    
    # All keys exhausted
    status['all_exhausted'] = True
    logger.warning(f"All API keys exhausted! Keys status: {status['keys_checked']}")
    return None, status


def record_api_call(api_key: str) -> dict:
    """
    Record that an API call was made with the given key.
    Call this AFTER a successful API request.
    
    Returns:
        Status dict with current usage info
    """
    new_count = _increment_key_usage(api_key)
    key_hash = api_key[-8:]
    
    status = {
        'key': f"...{key_hash}",
        'usage_after': new_count,
        'limit': CALLS_PER_MINUTE_PER_KEY,
        'remaining': max(0, CALLS_PER_MINUTE_PER_KEY - new_count)
    }
    
    if new_count >= CALLS_PER_MINUTE_PER_KEY:
        logger.info(f"API key ...{key_hash} exhausted ({new_count}/{CALLS_PER_MINUTE_PER_KEY}), will switch on next call")
    
    return status


def wait_for_quota(max_wait_seconds: int = 65) -> bool:
    """
    Wait until at least one API key has available quota.
    
    Args:
        max_wait_seconds: Maximum time to wait (default 65s, just over 1 minute)
    
    Returns:
        True if quota available, False if timeout
    """
    start_time = time.time()
    
    while time.time() - start_time < max_wait_seconds:
        key, status = get_available_api_key()
        if key:
            return True
        
        # Calculate time until next minute window
        current_second = int(time.time() % WINDOW_SECONDS)
        wait_time = WINDOW_SECONDS - current_second + 1
        
        logger.info(f"All API keys exhausted, waiting {wait_time}s for quota reset...")
        time.sleep(min(wait_time, max_wait_seconds - (time.time() - start_time)))
    
    return False


def get_rate_limit_status() -> dict:
    """Get current rate limit status for all keys"""
    status = {
        'keys': [],
        'total_usage': 0,
        'total_limit': len(API_KEYS) * CALLS_PER_MINUTE_PER_KEY,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    
    for idx, key in enumerate(API_KEYS):
        usage = _get_key_usage(key)
        key_status = {
            'index': idx,
            'key_hint': f"...{key[-8:]}",
            'usage': usage,
            'limit': CALLS_PER_MINUTE_PER_KEY,
            'available': CALLS_PER_MINUTE_PER_KEY - usage
        }
        status['keys'].append(key_status)
        status['total_usage'] += usage
    
    return status


# Convenience function for scripts
def get_api_key_with_limit() -> Optional[str]:
    """
    Simple function to get an available API key.
    Waits if all keys exhausted (up to 65 seconds).
    
    Returns:
        API key string, or None if failed after waiting
    """
    key, status = get_available_api_key()
    
    if key:
        return key
    
    # All exhausted, wait for quota
    if wait_for_quota():
        key, _ = get_available_api_key()
        return key
    
    logger.error("Failed to get API key after waiting")
    return None
