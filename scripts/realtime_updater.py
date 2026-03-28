#!/usr/bin/env python3
"""
Real-Time Data Updater
======================
Purpose: Fetch latest candles every 5 minutes and update database
Usage: python realtime_updater.py (run as cron/systemd service)

V2 IMPROVEMENTS:
- Uses centralized market_status module
- Detects and prevents storing flat/duplicate data during closed markets
- Only stores data when price actually changed
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
import psycopg
import logging
import time
import subprocess

# Add project root to path to import app.* modules when running inside container (/app)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Configure logging with UTC
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("realtime_updater")

# Import our centralized market status module
from app.market_status import get_forex_market_window, initialize_market_status
from app.trading_calendar import MetadataHealth, TimestampValidation, validate_timestamp
from app.rate_limiter import get_api_key_with_limit, record_api_call, get_rate_limit_status, API_KEYS

# Configuration


def _build_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("TRADING_BOT_DB") or os.getenv("POSTGRES_DB", "ai_trading_bot_data")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not user or password is None or password == "":
        raise RuntimeError("Set DATABASE_URL or POSTGRES_USER/POSTGRES_PASSWORD")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = _build_database_url()

# ALL SYMBOLS - No more WebSocket, pure REST API
# Limited to 5 symbols to stay under API quota (1600 calls/day, updates every 5 min)
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAMES = ["M1"]

# Twelve Data timeframe mapping
TF_MAP = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day"
}

MIN_SLEEP = 3.8  # Safety delay between API calls

HOLIDAYS_CACHE_TTL = 345600  # 96 hours - match market_status.py

def format_symbol(symbol: str) -> str:
    """XAUUSD -> XAU/USD, EURUSD -> EUR/USD"""
    if symbol.startswith('XAU'):
        return 'XAU/USD'
    elif len(symbol) == 6:
        return f"{symbol[:3]}/{symbol[3:]}"
    return symbol


def get_api_key() -> str:
    """Get API key with rate limiting - auto-switches when exhausted"""
    key = get_api_key_with_limit()
    if not key:
        raise RuntimeError("No API keys available (all exhausted and timeout)")
    return key


def is_flat_candle(candle: dict) -> bool:
    """
    Detect if a candle is "flat" (no real price movement)
    This indicates stale data during market close
    
    Args:
        candle: Dictionary with 'open', 'high', 'low', 'close'
    
    Returns:
        True if candle is flat/duplicate (should not be stored)
    
    NOTE: Only checks for EXACT duplicates (o==h==l==c)
    Low-volatility candles (e.g., 0.005-0.02% range) are VALID and should be stored
    """
    o = candle['open']
    h = candle['high']
    l = candle['low']
    c = candle['close']
    
    # Sanity check
    if o == 0:
        return True
    
    # Check if OHLC are all identical (exact duplicate - stale/frozen data)
    # This catches truly flat candles where market data feed is stuck
    if o == h == l == c:
        return True
    
    return False


def is_duplicate_of_previous(conn: psycopg.Connection, symbol: str, timeframe: str, candle: dict) -> bool:
    """
    Check if this candle is a duplicate of the previous one
    Returns True if the new candle has identical OHLC to the last stored candle
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT open, high, low, close 
            FROM candlesticks 
            WHERE symbol = %s AND timeframe = %s
            ORDER BY time DESC 
            LIMIT 1
        """, (symbol, timeframe))
        
        prev = cur.fetchone()
        if not prev:
            return False
        
        prev_o, prev_h, prev_l, prev_c = prev
        
        # Check if all OHLC values are identical
        if (abs(candle['open'] - prev_o) < 0.0001 and
            abs(candle['high'] - prev_h) < 0.0001 and
            abs(candle['low'] - prev_l) < 0.0001 and
            abs(candle['close'] - prev_c) < 0.0001):
            return True
    
    return False


def fetch_latest_candles(symbol: str, timeframe: str, count: int = 5):
    """Fetch latest N candles from Twelve Data REST (M1 only here)"""
    formatted_symbol = format_symbol(symbol)
    interval = TF_MAP.get(timeframe, "1min")
    api_key = get_api_key()
    
    params = {
        "symbol": formatted_symbol,
        "interval": interval,
        "outputsize": count,
        "apikey": api_key,
        "timezone": "UTC",
        "format": "JSON"
    }
    
    try:
        response = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=20)
        
        # Record the API call for rate limiting
        rate_status = record_api_call(api_key)
        logger.debug(f"Rate limit: key {rate_status['key']} usage {rate_status['usage_after']}/{rate_status['limit']}")
        
        if response.status_code != 200:
            logger.error(f"API HTTP error for {symbol}/{timeframe}: {response.status_code}")
            return []
        data = response.json()
        if "values" not in data:
            message = data.get("message", "no values returned")
            logger.error(f"API response error for {symbol}/{timeframe}: {message}")
            return []
        values = data["values"]
        candles = []
        for row in values:
            dt_str = row.get("datetime")
            if not dt_str:
                continue
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    dt = datetime.fromisoformat(dt_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                except Exception:
                    logger.warning(f"Could not parse datetime {dt_str} for {symbol}/{timeframe}")
                    continue
            candles.append({
                "datetime": dt,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0))
            })
        # Ensure chronological order oldest->newest
        candles.sort(key=lambda c: c["datetime"])
        return candles
    except Exception as e:
        logger.error(f"API error for {symbol}/{timeframe}: {e}")
        return []


def store_candles(conn: psycopg.Connection, symbol: str, timeframe: str, candles: list, holidays: list = None, holidays_cached_at: datetime = None):
    """
    Store candles in database (UPSERT)
    
    FIX 4: TWO-TIER VALIDATION
    Tier-1: Flat candle detection (optimization, not correctness)
    Tier-2: Authoritative timestamp validation (MANDATORY)
    """
    if not candles:
        return 0, 0
    
    stored_count = 0
    skipped_count = 0
    rejected_count = 0
    
    with conn.cursor() as cur:
        for candle in candles:
            # ============================================================
            # TIER-1: Optimization (flat candle detection)
            # ============================================================
            # Skip flat candles (exact duplicates where o==h==l==c)
            if is_flat_candle(candle):
                skipped_count += 1
                o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
                logger.info(f"TIER-1 skip: {symbol} flat candle at {candle['datetime']} | "
                          f"O={o:.5f} H={h:.5f} L={l:.5f} C={c:.5f} (exact duplicate/stale data)")
                continue
            
            # Skip duplicates of previous candle
            if is_duplicate_of_previous(conn, symbol, timeframe, candle):
                skipped_count += 1
                logger.info(f"TIER-1 skip: {symbol} duplicate candle at {candle['datetime']} (identical to previous)")
                continue
            
            # ============================================================
            # TIER-2: Authoritative timestamp validation (MANDATORY)
            # ============================================================
            validation = validate_timestamp(
                candle['datetime'],
                holidays,
                holidays_cached_at,
                HOLIDAYS_CACHE_TTL
            )
            
            if not validation.is_valid:
                rejected_count += 1
                logger.warning(
                    f"TIER-2 REJECTED {symbol}/{timeframe} @ {candle['datetime']} | "
                    f"Reason: {validation.reason} | "
                    f"Confidence: {validation.confidence_level} | "
                    f"Scope: {validation.validation_scope}"
                )
                continue
            
            # Log validation scope for first candle
            if stored_count == 0 and rejected_count == 0 and skipped_count == 0:
                logger.debug(
                    f"Validation scope: {validation.validation_scope} | "
                    f"Confidence: {validation.confidence_level}"
                )
            
            # Store valid candle
            cur.execute("""
                INSERT INTO candlesticks (symbol, timeframe, time, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, timeframe, time) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """, (symbol, timeframe, candle['datetime'], candle['open'], 
                  candle['high'], candle['low'], candle['close'], candle['volume']))
            
            stored_count += 1
    
    conn.commit()
    
    if rejected_count > 0:
        logger.warning(f"TIER-2 rejected {rejected_count} invalid timestamps")
    
    return stored_count, skipped_count


def calculate_and_store_indicators(conn: psycopg.Connection, symbol: str, timeframe: str):
    """Calculate indicators for recent 1000 bars and store"""
    # Kept as a callable hook; the scheduler runs the full indicator updater
    # after candle aggregation. This function intentionally shells out to the
    # shared script to avoid duplicate logic.
    script_path = os.path.join(os.path.dirname(__file__), "calculate_recent_indicators.py")
    subprocess.run(
        [sys.executable, script_path, "--symbol", symbol, "--timeframe", timeframe],
        check=False,
        text=True,
    )


def update_metadata(conn: psycopg.Connection, symbol: str, timeframe: str):
    """Update metadata completeness"""
    with conn.cursor() as cur:
        cur.execute("SELECT calculate_data_completeness(%s, %s)", (symbol, timeframe))
        completeness = cur.fetchone()[0]
    
    conn.commit()
    return completeness


def main():
    """Main entry point for realtime updater
    
    FIX 4: Implements two-tier validation
    - Tier-1: Local optimization (skip obvious closed hours)
    - Tier-2: Authoritative validation (happens at INSERT boundary)
    """
    logger.info(f"Real-Time Data Update @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not API_KEYS:
        logger.error("No Twelve Data API keys configured")
        return
    
    # Initialize market status module (loads cache)
    try:
        initialize_market_status()
    except Exception as e:
        logger.warning(f"Market status initialization warning: {e}")
    
    # ============================================================
    # TIER-1 OPTIMIZATION: Skip fetch if obviously closed
    # ============================================================
    # This is a RUNTIME optimization only, NOT correctness enforcement
    # Correctness is enforced by Tier-2 validation at INSERT boundary
    try:
        now = datetime.now(timezone.utc)
        day_of_week = now.weekday()
        hour = now.hour
        
        # Simple weekend check (Friday 22:00 UTC - Sunday 22:00 UTC)
        if day_of_week == 5 or day_of_week == 6:  # Sat or Sun before 22:00
            if not (day_of_week == 6 and hour >= 22):  # Unless Sun after 22:00
                logger.info(f"TIER-1: Weekend detected, skipping fetch (optimization)")
                logger.info(f"TIER-2 validation would reject these timestamps anyway")
                return
        
        # Daily rollover check (22:00-23:00 UTC Monday-Thursday)
        # During this hour, brokers perform settlement and data is unreliable
        if hour == 22 and day_of_week in (0, 1, 2, 3):  # Mon-Thu at 22:xx UTC
            logger.info(f"TIER-1: Daily rollover period (22:00-23:00 UTC), skipping fetch")
            logger.info(f"Market closed for settlement - no reliable data available")
            return
            
    except Exception as e:
        logger.warning(f"TIER-1 optimization failed: {e}, proceeding with fetch")
    
    # ============================================================
    # GET HOLIDAY METADATA (for Tier-2 validation)
    # ============================================================
    holidays = None
    holidays_cached_at = None
    
    try:
        from app.market_status import _get_cache
        cached = _get_cache("holidays")
        if cached:
            holidays, holidays_cached_at = cached
    except Exception as e:
        logger.warning(f"Could not load holiday metadata: {e}")
    
    try:
        conn = psycopg.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return
    
    try:
        total_stored = 0
        total_skipped = 0
        
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                candles = fetch_latest_candles(symbol, timeframe, count=5)
                
                if candles:
                    stored, skipped = store_candles(conn, symbol, timeframe, candles, holidays, holidays_cached_at)
                    total_stored += stored
                    total_skipped += skipped
                    
                    if stored > 0:
                        latest_time = candles[-1]['datetime'].strftime('%Y-%m-%d %H:%M')
                        logger.info(f"{symbol:8} {timeframe:4} | Stored {stored} bars | Latest: {latest_time}")
                    else:
                        logger.info(f"{symbol:8} {timeframe:4} | Skipped {skipped} flat/duplicate bars")
                    
                    if stored > 0:
                        update_metadata(conn, symbol, timeframe)
                else:
                    logger.warning(f"{symbol:8} {timeframe:4} | No data from API")

                # Respect rate limit across API keys
                time.sleep(MIN_SLEEP)
        
        logger.info(f"Total bars stored: {total_stored}")
        if total_skipped > 0:
            logger.info(f"Total bars skipped (flat/duplicate): {total_skipped}")
        
    except Exception as e:
        logger.exception(f"Error during realtime update: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
