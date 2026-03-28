#!/usr/bin/env python3
"""
Fill Data Gaps - Only fetch missing/new data from Twelve Data
==============================================================
Checks database for latest candle and fetches from there forward
Publishes updates to Redis for real-time SSE streaming
Includes market status check to avoid wasting API calls during closed markets
"""

import os
import sys
from datetime import datetime, timedelta, timezone
import psycopg
import requests
import redis
import json
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logger = logging.getLogger("fill_data_gaps")

# Add project root to path to import app.* modules when running inside container (/app)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.market_status import get_forex_market_window, initialize_market_status
from app.trading_calendar import MetadataHealth, TimestampValidation, validate_timestamp, split_into_trading_windows
from app.rate_limiter import get_api_key_with_limit, record_api_call, get_rate_limit_status


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

# Redis connection for cache and pub/sub
try:
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD"),
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True
    )
    redis_client.ping()
    REDIS_AVAILABLE = True
    logger.info("Redis connected")
except Exception as e:
    logger.warning(f"Redis not available: {e}")
    REDIS_AVAILABLE = False

# ALL SYMBOLS - No more WebSocket, pure REST API
# Limited to 5 symbols to stay under API quota (1600 calls/day, updates every 5 min)
# Removed: USDCAD, USDCHF (less popular pairs)
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]

TIMEFRAMES = ["M1"]  # Only fetch M1, higher TFs aggregated by candle_aggregator
TIMEFRAME_MAP = {
    "M1": "1min"
}

MIN_SLEEP = 3.8  # 16 calls/min across two keys
HOLIDAYS_CACHE_TTL = 345600  # 96 hours - match market_status.py


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def wait_for_next_5min_candle():
    """Wait until the next 5-minute candle boundary opens"""
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    current_second = now.second
    
    # Calculate next 5-minute boundary (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)
    next_boundary_minute = ((current_minute // 5) + 1) * 5
    
    if next_boundary_minute >= 60:
        # Next hour
        target_time = now.replace(hour=(now.hour + 1) % 24, minute=0, second=0, microsecond=0)
        if now.hour == 23:
            target_time = target_time + timedelta(days=1)
    else:
        target_time = now.replace(minute=next_boundary_minute, second=0, microsecond=0)
    
    wait_seconds = (target_time - now).total_seconds()
    
    if wait_seconds > 0:
        logger.info(f"Waiting {wait_seconds:.0f}s for next 5-min candle at {target_time.strftime('%H:%M:%S')} UTC")
        time.sleep(wait_seconds)
        logger.info(f"5-min candle boundary reached, proceeding...")


def format_symbol(symbol: str) -> str:
    """Convert XAUUSD to XAU/USD format"""
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

def get_latest_candle_time(conn, symbol: str, timeframe: str):
    """Get the most recent candle timestamp from database"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(time) 
            FROM candlesticks 
            WHERE symbol = %s AND timeframe = %s
        """, (symbol, timeframe))
        result = cur.fetchone()
        return result[0] if result[0] else None

def fetch_gap_data(symbol: str, timeframe: str, start_time: datetime = None):
    """Fetch data from Twelve Data starting from a specific time"""
    formatted_symbol = format_symbol(symbol)
    api_key = get_api_key()
    
    params = {
        'symbol': formatted_symbol,
        'interval': TIMEFRAME_MAP[timeframe],
        'outputsize': 5000,  # Max available
        'apikey': api_key,
        'timezone': 'UTC',
        'format': 'JSON',
    }
    
    # If start_time provided, add it as filter
    if start_time:
        params['start_date'] = start_time.strftime('%Y-%m-%d %H:%M:%S')
    
    logger.info(f"Fetching {symbol}/{timeframe} from Twelve Data API")
    response = requests.get('https://api.twelvedata.com/time_series', params=params, timeout=30)
    
    # Record the API call for rate limiting
    rate_status = record_api_call(api_key)
    logger.debug(f"Rate limit: key {rate_status['key']} usage {rate_status['usage_after']}/{rate_status['limit']}")
    
    if response.status_code != 200:
        logger.error(f"API error: HTTP {response.status_code}")
        return None
    
    data = response.json()
    
    if 'values' not in data:
        if 'message' in data:
            logger.error(f"API message: {data['message']}")
        else:
            logger.error("No data returned")
        return None
    
    return data['values']

def insert_candles(conn, symbol: str, timeframe: str, values: list, holidays: list = None, holidays_cached_at: datetime = None):
    """Insert candles into database with conflict handling and Redis updates
    
    CRITICAL: All timestamps are validated before INSERT
    No candle may be stored without explicit timestamp validation
    """
    if not values:
        return 0
    
    inserted = 0
    rejected = 0
    new_candles = []
    
    with conn.cursor() as cur:
        for row in values:
            try:
                # Parse datetime - handle both formats (with and without time)
                dt_str = row['datetime']
                try:
                    dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                except ValueError:
                    dt = datetime.strptime(dt_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                
                # ============================================================
                # TIER-2 VALIDATION: Authoritative timestamp validation
                # ============================================================
                validation = validate_timestamp(
                    dt,
                    holidays,
                    holidays_cached_at,
                    HOLIDAYS_CACHE_TTL
                )
                
                if not validation.is_valid:
                    rejected += 1
                    logger.warning(
                        f"REJECTED {symbol}/{timeframe} @ {dt} | "
                        f"Reason: {validation.reason} | "
                        f"Confidence: {validation.confidence_level} | "
                        f"Scope: {validation.validation_scope}"
                    )
                    continue
                
                # Log validation for transparency
                if rejected == 0 and inserted == 0:  # First candle
                    logger.debug(
                        f"Validation scope: {validation.validation_scope} | "
                        f"Confidence: {validation.confidence_level}"
                    )
                
                cur.execute("""
                    INSERT INTO candlesticks 
                    (symbol, timeframe, time, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, timeframe, time) DO NOTHING
                """, (
                    symbol,
                    timeframe,
                        dt,
                    float(row['open']),
                    float(row['high']),
                    float(row['low']),
                    float(row['close']),
                    int(row.get('volume', 0))
                ))
                
                if cur.rowcount > 0:
                    inserted += 1
                    # Store candle data for Redis publishing
                    new_candles.append({
                        'time': dt.isoformat(),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'volume': int(row.get('volume', 0))
                    })
            except Exception as e:
                logger.error(f"Error inserting row: {e}")
                continue
        
        conn.commit()
    
    if rejected > 0:
        logger.info(f"Rejected {rejected} invalid timestamps (out of {len(values)} total)")
    
    # Update Redis cache and publish to SSE subscribers
    if REDIS_AVAILABLE and inserted > 0:
        try:
            # Invalidate cache for this symbol/timeframe
            cache_key = f"candles:{symbol}:{timeframe}"
            redis_client.delete(cache_key)
            
            # Publish updates to SSE subscribers (send last candle only)
            if new_candles:
                latest_candle = new_candles[-1]
                message = json.dumps({
                    'type': 'candle_update',
                    'symbol': symbol,
                    'timeframe': timeframe,
                    'candle': latest_candle,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                redis_client.publish('updates:candles', message)
        except Exception as e:
            logger.warning(f"Redis update failed: {e}")
    
    return inserted

def fill_gaps(conn, symbols_override=None):
    """Fill data gaps for all symbols and timeframes
    
    FIX 3 IMPLEMENTATION: Window decomposition
    - Splits historical ranges into valid trading windows
    - Fetches only valid periods
    - Every timestamp validated before INSERT
    
    Args:
        conn: Database connection
        symbols_override: Optional list of symbols to process (overrides SYMBOLS)
    """
    priority_symbols = symbols_override  # Rename for clarity
    
    # ============================================================================
    # GET HOLIDAY METADATA (needed for timestamp validation)
    # ============================================================================
    logger.info("Loading holiday metadata for timestamp validation")
    
    holidays = None
    holidays_cached_at = None
    
    try:
        from app.market_status import _get_cache
        cached = _get_cache("holidays")
        if cached:
            holidays, holidays_cached_at = cached
            logger.info(f"Loaded {len(holidays) if holidays else 0} holidays from cache")
    except Exception as e:
        logger.warning(f"Could not load holiday metadata: {e}")
        logger.warning("Will proceed with weekend-only validation (OFFLINE mode)")
    
    # ============================================================================
    # DATA FETCHING with window decomposition
    # ============================================================================
    
    total_inserted = 0
    total_rejected = 0
    total_api_calls = 0
    
    logger.info("Filling data gaps from Twelve Data")

    # Use override symbols if provided, otherwise use default SYMBOLS list
    symbols_to_process = symbols_override if symbols_override is not None else SYMBOLS
    
    for symbol in symbols_to_process:
        logger.info(f"Symbol {symbol} start")
        
        for timeframe in TIMEFRAMES:
            logger.info(f"Timeframe {timeframe}")
            
            # Get latest candle in database
            latest_time = get_latest_candle_time(conn, symbol, timeframe)
            
            if latest_time:
                # Calculate time since last candle
                now = datetime.now(timezone.utc)
                hours_ago = (now - latest_time).total_seconds() / 3600
                minutes_ago = (now - latest_time).total_seconds() / 60
                logger.debug(f"Latest candle: {latest_time} ({hours_ago:.1f}h ago)")
                
                # Define threshold based on timeframe (fetch if data is older than 2x the interval)
                thresholds = {
                    'M1': 5,    # 5 minutes (safety net for WS disconnect)
                    'M5': 10,   # 10 minutes (2x 5min interval)
                    'M15': 30,  # 30 minutes (2x 15min interval)
                    'H1': 120,  # 2 hours (2x 1h interval)
                    'H4': 480,  # 8 hours (2x 4h interval)
                    'D1': 1440  # 24 hours (1 day)
                }
                
                threshold_minutes = thresholds.get(timeframe, 60)
                
                # If data is recent (within threshold), skip
                if minutes_ago < threshold_minutes:
                    logger.info(f"Data up to date ({minutes_ago:.0f}m ago, threshold: {threshold_minutes}m)")
                    continue
                
                # ============================================================
                # WINDOW DECOMPOSITION: Split range into valid trading windows
                # ============================================================
                now = datetime.now(timezone.utc)
                logger.info(f"Splitting range {latest_time} → {now} into valid trading windows")
                
                windows = split_into_trading_windows(
                    latest_time,
                    now,
                    holidays,
                    holidays_cached_at,
                    HOLIDAYS_CACHE_TTL
                )
                
                if not windows:
                    logger.info("No valid trading windows in range (all closed periods)")
                    continue
                
                logger.info(f"Found {len(windows)} valid trading window(s)")
                
                # Fetch each window separately
                all_values = []
                for window_idx, (window_start, window_end) in enumerate(windows, 1):
                    logger.info(f"Window {window_idx}/{len(windows)}: {window_start} → {window_end}")
                    window_values = fetch_gap_data(symbol, timeframe, window_start)
                    total_api_calls += 1
                    
                    if window_values:
                        all_values.extend(window_values)
                        logger.debug(f"Fetched {len(window_values)} candles from window {window_idx}")
                    
                    # Rate limiting between windows
                    if window_idx < len(windows):
                        time.sleep(MIN_SLEEP)
                
                values = all_values
            else:
                # No data exists, fetch all available (still use single fetch for initial load)
                logger.info("No data in DB, fetching all available")
                values = fetch_gap_data(symbol, timeframe)
                total_api_calls += 1
            
            if values:
                # Insert new candles with timestamp validation
                inserted = insert_candles(conn, symbol, timeframe, values, holidays, holidays_cached_at)
                total_inserted += inserted
                logger.info(f"Inserted {inserted} new candles (fetched {len(values)} total)")
            else:
                logger.info("No data fetched for this symbol/timeframe")
            
            # Rate limiting between symbols/timeframes
            logger.debug(f"Sleeping {MIN_SLEEP}s (rate limit)")
            time.sleep(MIN_SLEEP)

    # After all symbols and timeframes are processed
    if priority_symbols:
        priority_completed = True
        logger.info(f"✓ PRIORITY SYMBOLS {priority_symbols} COMPLETED")
    
    logger.info("Gap filling complete")
    logger.info(f"API calls: {total_api_calls}")
    logger.info(f"New candles inserted: {total_inserted}")
    
    return priority_completed if priority_symbols else None

def main(skip_wait=False):
    """Main entry point for gap filler
    
    Args:
        skip_wait: If True, skip waiting for 5-min boundary (for manual runs)
    """
    # Log rate limit status at startup
    status = get_rate_limit_status()
    logger.info(f"Rate limit status: {status['total_usage']}/{status['total_limit']} calls used this minute")
    
    # Wait for next 5-minute candle boundary (unless skipped)
    if not skip_wait:
        wait_for_next_5min_candle()
    
    # Initialize market status module (loads cache)
    logger.info("Gap filler initialization")
    initialize_market_status()
    
    logger.info("Connecting to database")
    conn = psycopg.connect(DATABASE_URL)
    
    try:
        # Fill gaps for ALL symbols (no more WebSocket, pure REST)
        logger.info("")
        logger.info("="*80)
        logger.info(f"FILLING GAPS FOR ALL SYMBOLS: {SYMBOLS}")
        logger.info("="*80)
        fill_gaps(conn)
        
        # Show updated summary
        logger.info("")
        logger.info("Database summary")
        logger.info("-"*100)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, timeframe, COUNT(*) as bars,
                       MIN(time) as earliest, MAX(time) as latest
                FROM candlesticks
                GROUP BY symbol, timeframe
                ORDER BY symbol, timeframe
            """)
            
            logger.info(f"{'Symbol':<10} {'Timeframe':<10} {'Bars':>12} {'Earliest':<20} {'Latest':<20}")
            logger.info("-"*100)
            for row in cur.fetchall():
                logger.info(f"{row[0]:<10} {row[1]:<10} {row[2]:>12,} {str(row[3]):<20} {str(row[4]):<20}")
        logger.info("-"*100)
        
        # Final rate limit status
        final_status = get_rate_limit_status()
        logger.info(f"Final rate limit status: {final_status['total_usage']}/{final_status['total_limit']} calls")
    
    finally:
        conn.close()


if __name__ == "__main__":
    main()
