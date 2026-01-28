"""
Market Status Module - Forex Trading Hours & Holiday Checker
Uses Massive.com API to check if forex markets are open
Implements smart caching to minimize API calls (5 calls/min limit on Basic tier)

V2 IMPROVEMENTS:
- Redis-based persistent cache (shared across gunicorn workers)
- Circuit breaker for 429 rate limit errors
- Startup-only initialization (prevents 4x API calls from 4 workers)
- File-based fallback when Redis unavailable
"""

import os
import json
import redis  # type: ignore
import requests
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from threading import Lock

from .trading_calendar import (MetadataHealth, MarketWindow, TimestampValidation,
                               compute_market_window, validate_timestamp, split_into_trading_windows)

logger = logging.getLogger(__name__)

# Massive.com API configuration
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
MARKET_STATUS_ENDPOINT = "https://api.massive.com/v1/marketstatus/now"
MARKET_HOLIDAYS_ENDPOINT = "https://api.massive.com/v1/marketstatus/upcoming"

# Cache configuration
MARKET_STATUS_CACHE_TTL = 300  # 5 minutes (matches your update frequency)
HOLIDAYS_CACHE_TTL = 345600  # 96 hours (4 days) - covers weekends + holiday weekends
# Example: Friday 5PM EST → Monday 5PM EST = 72 hours (weekend)
#          Christmas Friday → Monday = 96 hours (holiday weekend)
# This ensures cache doesn't expire during multi-day market closures

# Circuit breaker for rate limiting
CIRCUIT_BREAKER_THRESHOLD = 3  # Number of 429 errors before opening circuit
CIRCUIT_BREAKER_TIMEOUT = 600  # 10 minutes cooldown after rate limit
_circuit_breaker_failures = 0
_circuit_breaker_open_until: Optional[datetime] = None
_circuit_lock = Lock()

# Redis connection (shared cache across gunicorn workers)
_redis_client: Optional[redis.Redis] = None
_redis_available = False

# File-based cache fallback
CACHE_DIR = Path("/tmp/market_status_cache")
CACHE_FILE = CACHE_DIR / "market_cache.json"

# In-memory cache (last resort fallback)
_memory_cache: Dict[str, any] = {}


def _normalize_cached_time(value: datetime) -> datetime:
    """Ensure cached timestamps are timezone-aware (UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class MarketStatusError(Exception):
    """Raised when market status API fails"""
    pass


class RateLimitError(MarketStatusError):
    """Raised when API rate limit is hit (429)"""
    pass


# ============================================================================
# CACHE LAYER - Multi-tier: Redis > File > Memory
# ============================================================================

def _init_redis() -> bool:
    """Initialize Redis connection for shared caching"""
    global _redis_client, _redis_available
    
    try:
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_db = int(os.getenv("REDIS_DB", "0"))
        redis_password = os.getenv("REDIS_PASSWORD")
        
        _redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2
        )
        
        # Test connection
        _redis_client.ping()
        _redis_available = True
        logger.info(f"✓ Redis cache connected ({redis_host}:{redis_port})")
        return True
        
    except Exception as e:
        _redis_available = False
        logger.warning(f"Redis unavailable, using file cache: {e}")
        return False


def _init_file_cache():
    """Initialize file-based cache directory"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.debug(f"File cache directory: {CACHE_DIR}")
    except Exception as e:
        logger.warning(f"Could not create cache directory: {e}")


def _get_cache(key: str) -> Optional[Tuple[any, datetime]]:
    """
    Get cached value from multi-tier cache
    Priority: Redis > File > Memory
    
    Returns:
        Tuple of (data, cached_time) or None if not found
    """
    # Tier 1: Redis (shared across workers)
    if _redis_available and _redis_client:
        try:
            cached_json = _redis_client.get(f"market:{key}")
            if cached_json:
                cached_data = json.loads(cached_json)
                cached_time = datetime.fromisoformat(cached_data["timestamp"])
                logger.debug(f"Cache HIT (Redis): {key}")
                return cached_data["value"], cached_time
        except Exception as e:
            logger.debug(f"Redis cache read error: {e}")
    
    # Tier 2: File cache (survives restarts)
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, 'r') as f:
                file_cache = json.load(f)
                if key in file_cache:
                    cached_data = file_cache[key]
                    cached_time = datetime.fromisoformat(cached_data["timestamp"])
                    logger.debug(f"Cache HIT (File): {key}")
                    
                    # Promote to Redis if available
                    if _redis_available:
                        _set_cache(key, cached_data["value"], cached_time)
                    
                    return cached_data["value"], cached_time
    except Exception as e:
        logger.debug(f"File cache read error: {e}")
    
    # Tier 3: Memory (process-local, last resort)
    if key in _memory_cache:
        logger.debug(f"Cache HIT (Memory): {key}")
        return _memory_cache[key]
    
    logger.debug(f"Cache MISS: {key}")
    return None


def _set_cache(key: str, value: any, timestamp: Optional[datetime] = None):
    """
    Store value in multi-tier cache
    Writes to all available tiers for redundancy
    """
    if timestamp is None:
        timestamp = datetime.now()
    
    cache_entry = {
        "value": value,
        "timestamp": timestamp.isoformat()
    }
    
    # Tier 1: Redis (with TTL based on cache type)
    if _redis_available and _redis_client:
        try:
            ttl = HOLIDAYS_CACHE_TTL if "holiday" in key else MARKET_STATUS_CACHE_TTL
            _redis_client.setex(
                f"market:{key}",
                ttl,
                json.dumps(cache_entry, default=str)
            )
            logger.debug(f"Cache SET (Redis): {key} [TTL: {ttl}s]")
        except Exception as e:
            logger.debug(f"Redis cache write error: {e}")
    
    # Tier 2: File cache
    try:
        file_cache = {}
        if CACHE_FILE.exists():
            with open(CACHE_FILE, 'r') as f:
                file_cache = json.load(f)
        
        file_cache[key] = cache_entry
        
        with open(CACHE_FILE, 'w') as f:
            json.dump(file_cache, f, indent=2, default=str)
        
        logger.debug(f"Cache SET (File): {key}")
    except Exception as e:
        logger.debug(f"File cache write error: {e}")
    
    # Tier 3: Memory
    _memory_cache[key] = (value, timestamp)
    logger.debug(f"Cache SET (Memory): {key}")


def _clear_cache(key: Optional[str] = None):
    """Clear cache entries (all tiers)"""
    if key is None:
        # Clear all
        if _redis_available and _redis_client:
            try:
                _redis_client.delete("market:holidays", "market:status")
            except:
                pass
        _memory_cache.clear()
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
        logger.info("All caches cleared")
    else:
        # Clear specific key
        if _redis_available and _redis_client:
            try:
                _redis_client.delete(f"market:{key}")
            except:
                pass
        _memory_cache.pop(key, None)
        logger.info(f"Cache cleared: {key}")


# ============================================================================
# CIRCUIT BREAKER for Rate Limiting
# ============================================================================

def _check_circuit_breaker() -> bool:
    """
    Check if circuit breaker is open (too many 429 errors)
    
    Returns:
        True if circuit is OPEN (requests blocked)
        False if circuit is CLOSED (requests allowed)
    """
    global _circuit_breaker_open_until
    
    with _circuit_lock:
        if _circuit_breaker_open_until is None:
            return False
        
        if datetime.now() < _circuit_breaker_open_until:
            remaining = (_circuit_breaker_open_until - datetime.now()).total_seconds()
            logger.warning(f"Circuit breaker OPEN - API blocked for {remaining:.0f}s more")
            return True
        else:
            # Reset circuit breaker
            logger.info("Circuit breaker CLOSED - resuming API calls")
            _circuit_breaker_open_until = None
            return False


def _record_rate_limit_error():
    """Record a 429 error and potentially open circuit breaker"""
    global _circuit_breaker_failures, _circuit_breaker_open_until
    
    with _circuit_lock:
        _circuit_breaker_failures += 1
        
        if _circuit_breaker_failures >= CIRCUIT_BREAKER_THRESHOLD:
            _circuit_breaker_open_until = datetime.now() + timedelta(seconds=CIRCUIT_BREAKER_TIMEOUT)
            logger.error(
                f"Circuit breaker OPENED after {_circuit_breaker_failures} rate limit errors. "
                f"API calls blocked until {_circuit_breaker_open_until.strftime('%H:%M:%S')}"
            )
            _circuit_breaker_failures = 0  # Reset counter


def _record_success():
    """Record successful API call (resets failure counter)"""
    global _circuit_breaker_failures
    
    with _circuit_lock:
        if _circuit_breaker_failures > 0:
            _circuit_breaker_failures = 0
            logger.debug("Circuit breaker failure counter reset")


def _make_massive_request(endpoint: str, timeout: int = 10) -> Dict:
    """
    Make authenticated request to Massive.com API with circuit breaker
    
    Args:
        endpoint: Full API endpoint URL
        timeout: Request timeout in seconds
        
    Returns:
        JSON response as dictionary
        
    Raises:
        RateLimitError: If rate limit hit (429)
        MarketStatusError: If API request fails
    """
    if not MASSIVE_API_KEY:
        raise MarketStatusError("MASSIVE_API_KEY environment variable not set")
    
    # Check circuit breaker
    if _check_circuit_breaker():
        raise MarketStatusError("Circuit breaker OPEN - API temporarily blocked due to rate limiting")
    
    try:
        response = requests.get(
            endpoint,
            params={"apiKey": MASSIVE_API_KEY},
            timeout=timeout
        )
        
        # Handle rate limiting (429 Too Many Requests)
        if response.status_code == 429:
            _record_rate_limit_error()
            raise RateLimitError(f"Rate limit exceeded (429): {endpoint}")
        
        response.raise_for_status()
        
        # Success - reset circuit breaker
        _record_success()
        
        return response.json()
        
    except requests.exceptions.Timeout:
        raise MarketStatusError(f"Request timeout after {timeout}s")
    except RateLimitError:
        raise  # Re-raise rate limit errors
    except requests.exceptions.RequestException as e:
        raise MarketStatusError(f"API request failed: {str(e)}")
    except ValueError as e:
        raise MarketStatusError(f"Invalid JSON response: {str(e)}")


def fetch_upcoming_holidays(days_ahead: int = 3) -> List[Dict]:
    """
    Fetch upcoming market holidays from Massive.com
    Filters to only relevant holidays (stock exchanges that affect forex/gold)
    Uses persistent cache across workers and restarts
    
    Args:
        days_ahead: Number of days ahead to cache holidays for (default 3 to cover weekends)
        
    Returns:
        List of holiday dictionaries with date, name, exchange, status
        
    Example:
        [
            {
                "date": "2025-12-25",
                "name": "Christmas",
                "exchange": "NYSE",
                "status": "closed"
            }
        ]
    """
    cache_key = "holidays"
    now = datetime.now()
    
    # Check multi-tier cache first
    cached = _get_cache(cache_key)
    if cached:
        cached_data, cached_time = cached
        cache_age = (now - cached_time).total_seconds()
        
        if cache_age < HOLIDAYS_CACHE_TTL:
            logger.debug(f"Using cached holidays (age: {cache_age:.0f}s)")
            return cached_data
    
    # Fetch from API
    logger.info("Fetching upcoming holidays from Massive.com API")
    try:
        all_holidays = _make_massive_request(MARKET_HOLIDAYS_ENDPOINT)
        
        # Filter holidays within next N days
        cutoff_date = (now + timedelta(days=days_ahead)).date()
        relevant_holidays = []
        
        for holiday in all_holidays:
            holiday_date_str = holiday.get("date")
            if not holiday_date_str:
                continue
                
            holiday_date = datetime.strptime(holiday_date_str, "%Y-%m-%d").date()
            
            # Only include holidays in the next N days
            if holiday_date <= cutoff_date:
                relevant_holidays.append(holiday)
        
        # Store in multi-tier cache
        _set_cache(cache_key, relevant_holidays)
        logger.info(f"Cached {len(relevant_holidays)} holidays for next {days_ahead} days")
        
        return relevant_holidays
        
    except (MarketStatusError, RateLimitError) as e:
        logger.error(f"Failed to fetch holidays: {e}")
        
        # Return cached data if available, even if expired
        if cached:
            logger.warning(f"Using stale holiday cache (age: {(now - cached[1]).total_seconds():.0f}s)")
            return cached[0]
        
        return []


def is_market_holiday_today() -> Tuple[bool, Optional[str]]:
    """
    Check if today is a market holiday that affects forex/gold trading
    
    Returns:
        Tuple of (is_holiday: bool, holiday_name: Optional[str])
        
    Example:
        (True, "Christmas") - Today is Christmas
        (False, None) - Not a holiday
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    holidays = fetch_upcoming_holidays(days_ahead=1)  # Only check today
    
    # Check if today is in the holiday list
    # For forex, we care about major exchange closures (NYSE, NASDAQ)
    for holiday in holidays:
        if holiday.get("date") == today_str:
            exchange = holiday.get("exchange", "")
            status = holiday.get("status", "")
            
            # Major exchanges that affect gold/forex liquidity
            if exchange in ["NYSE", "NASDAQ", "CME", "COMEX"] and status == "closed":
                holiday_name = holiday.get("name", "Market Holiday")
                logger.info(f"Market holiday detected: {holiday_name} ({exchange} closed)")
                return True, holiday_name
    
    return False, None


def get_market_status_realtime() -> Dict:
    """
    Get real-time market status from Massive.com with 5-minute caching
    Uses persistent cache across workers and restarts
    
    Returns:
        Dictionary with market status information:
        {
            "fx_open": bool,
            "fx_status": str,
            "crypto_open": bool,
            "server_time": str,
            "after_hours": bool,
            "early_hours": bool
        }
        
    Raises:
        MarketStatusError: If API request fails and no cache available
    """
    cache_key = "status"
    now = datetime.now()
    
    # Check multi-tier cache first
    cached = _get_cache(cache_key)
    if cached:
        cached_data, cached_time = cached
        cache_age = (now - cached_time).total_seconds()
        
        if cache_age < MARKET_STATUS_CACHE_TTL:
            logger.debug(f"Using cached market status (age: {cache_age:.0f}s)")
            return cached_data
    
    # Fetch from API
    logger.info("Fetching real-time market status from Massive.com API")
    try:
        data = _make_massive_request(MARKET_STATUS_ENDPOINT)
        
        # Extract forex-specific data
        currencies = data.get("currencies", {})
        fx_status = currencies.get("fx", "unknown")
        crypto_status = currencies.get("crypto", "unknown")
        
        result = {
            "fx_open": fx_status == "open",
            "fx_status": fx_status,
            "crypto_open": crypto_status == "open",
            "crypto_status": crypto_status,
            "server_time": data.get("serverTime", ""),
            "after_hours": data.get("afterHours", False),
            "early_hours": data.get("earlyHours", False),
            "raw_response": data  # Keep full response for debugging
        }
        
        # Store in multi-tier cache
        _set_cache(cache_key, result)
        logger.info(f"Market status: FX={fx_status}, Crypto={crypto_status}, Time={result['server_time']}")
        
        return result
        
    except (MarketStatusError, RateLimitError) as e:
        logger.error(f"Failed to fetch market status: {e}")
        
        # Return cached data if available, even if expired
        if cached:
            logger.warning(f"Using stale market status cache (age: {(now - cached[1]).total_seconds():.0f}s)")
            return cached[0]
        
        # No cache available - this is critical
        raise


def get_forex_market_window(refresh_holidays: bool = False) -> MarketWindow:
    """Return forex market window with metadata health state."""
    now = datetime.now(timezone.utc)
    holidays, cached_at = _get_holiday_cache(refresh_if_missing=refresh_holidays)
    window = compute_market_window(now, holidays, cached_at, HOLIDAYS_CACHE_TTL)
    return window


def _get_holiday_cache(refresh_if_missing: bool = False) -> Tuple[Optional[List[Dict]], Optional[datetime]]:
    """Return holiday list and cached timestamp, optionally fetching when missing."""
    now = datetime.now(timezone.utc)
    cached = _get_cache("holidays")
    if cached:
        holidays, cached_time = cached
        return holidays, _normalize_cached_time(cached_time)

    if refresh_if_missing:
        try:
            holidays = fetch_upcoming_holidays(days_ahead=3)
            return holidays, now
        except Exception as e:
            logger.warning(f"Holiday refresh failed: {e}")

    return None, None


def is_forex_market_open() -> Tuple[bool, str]:
    """
    Primary function: Check if forex market is open for trading
    
    Implements 2-stage check:
    1. Check for market holidays (cached daily)
    2. Check real-time trading hours (cached 5 minutes)
    
    Returns:
        Tuple of (is_open: bool, reason: str)
        
    Examples:
        (False, "Market Holiday: Christmas") - Holiday closure
        (False, "Market Closed: Weekend") - Weekend closure
        (True, "Market Open") - Trading allowed
        
    Usage:
        is_open, reason = is_forex_market_open()
        if is_open:
            # Safe to call Twelve Data API
            fetch_candles()
        else:
            # Skip API calls, save rate limit
            logger.info(f"Skipping data fetch: {reason}")
    """
    try:
        window = get_forex_market_window(refresh_holidays=True)
        reason = window.reason
        if window.health == MetadataHealth.OFFLINE:
            reason = f"{reason} (Metadata Offline - holiday data missing)"
        elif window.health == MetadataHealth.DEGRADED:
            reason = f"{reason} (Holiday metadata stale)"
        return window.is_open, reason
    except Exception as e:
        logger.error(f"Market status check failed: {e}")
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return False, "Market Closed: Weekend (fallback)"
        return True, "Market Status Unknown (fallback assuming open)"


def refresh_holiday_cache():
    """
    Force refresh of holiday cache
    Call this at the start of each trading day
    """
    _clear_cache("holidays")
    logger.info("Holiday cache cleared - will refresh on next check")
    fetch_upcoming_holidays(days_ahead=3)


def get_cache_stats() -> Dict:
    """
    Get cache statistics for monitoring/debugging
    
    Returns:
        Dictionary with cache status information
    """
    now = datetime.now()
    
    stats = {
        "redis_available": _redis_available,
        "file_cache_exists": CACHE_FILE.exists(),
        "circuit_breaker_open": _check_circuit_breaker(),
    }
    
    # Check Redis cache
    if _redis_available and _redis_client:
        try:
            holidays_cached = _redis_client.exists("market:holidays")
            status_cached = _redis_client.exists("market:status")
            
            stats["holidays_cached_redis"] = bool(holidays_cached)
            stats["market_status_cached_redis"] = bool(status_cached)
            
            if holidays_cached:
                ttl = _redis_client.ttl("market:holidays")
                stats["holidays_ttl_remaining"] = ttl
            
            if status_cached:
                ttl = _redis_client.ttl("market:status")
                stats["market_status_ttl_remaining"] = ttl
        except Exception as e:
            logger.debug(f"Error getting Redis cache stats: {e}")
    
    # Check file cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f:
                file_cache = json.load(f)
                stats["file_cache_keys"] = list(file_cache.keys())
        except:
            pass
    
    # Circuit breaker stats
    if _circuit_breaker_open_until:
        stats["circuit_breaker_resets_at"] = _circuit_breaker_open_until.isoformat()
    
    return stats


# ============================================================================
# MODULE INITIALIZATION - Controlled Startup
# ============================================================================

# Prevent multiple workers from hitting API simultaneously
_initialization_complete = False
_init_lock = Lock()


def initialize_market_status(force: bool = False):
    """
    Initialize market status module with cache warmup
    
    This should be called ONCE at application startup, not on every worker spawn.
    Uses lock to prevent multiple workers from initializing simultaneously.
    
    Args:
        force: If True, force re-initialization even if already done
    """
    global _initialization_complete
    
    with _init_lock:
        if _initialization_complete and not force:
            logger.debug("Market status already initialized, skipping")
            return
        
        logger.info("="*60)
        logger.info("Initializing market status module...")
        logger.info("="*60)
        
        # Initialize cache backends
        _init_file_cache()
        _init_redis()
        
        # Check if we have valid cached data
        cached_holidays = _get_cache("holidays")
        cached_status = _get_cache("status")
        
        if cached_holidays and cached_status:
            holiday_age = (datetime.now() - cached_holidays[1]).total_seconds()
            status_age = (datetime.now() - cached_status[1]).total_seconds()
            
            if holiday_age < HOLIDAYS_CACHE_TTL and status_age < MARKET_STATUS_CACHE_TTL:
                logger.info(f"✓ Using cached data (holidays: {holiday_age:.0f}s old, status: {status_age:.0f}s old)")
                _initialization_complete = True
                return
        
        # Need fresh data - but be careful about API limits
        try:
            if not _check_circuit_breaker():
                # Safe to make API calls
                logger.info("Warming up cache with API data...")
                
                # Fetch holidays (less frequent, more important)
                try:
                    fetch_upcoming_holidays(days_ahead=3)
                except Exception as e:
                    logger.warning(f"Could not fetch holidays: {e}")
                
                # Fetch market status
                try:
                    is_open, reason = is_forex_market_open()
                    logger.info(f"Initial market check: {reason}")
                except Exception as e:
                    logger.warning(f"Could not fetch market status: {e}")
            else:
                logger.warning("Circuit breaker open - using cached data only")
        
        except Exception as e:
            logger.error(f"Error during initialization: {e}")
        
        _initialization_complete = True
        logger.info("="*60)
        logger.info("Market status module initialization complete")
        logger.info("="*60)


# DO NOT auto-initialize on import anymore!
# Let the application explicitly call initialize_market_status() once
if __name__ == "__main__":
    # Only auto-init when running this module directly (for testing)
    initialize_market_status()
