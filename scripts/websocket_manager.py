#!/usr/bin/env python3
"""
Twelve Data WebSocket manager for tick ingestion.
- Subscribes to EUR/USD and XAU/USD price streams.
- Buckets ticks into M1 candles and stores finalized candles to Postgres.
- Skips invalid timestamps using trading_calendar.validate_timestamp.
- Sends heartbeat every 10 seconds to keep connection alive
"""
import json
import os
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

import psycopg
import websocket
import redis

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.trading_calendar import validate_timestamp
from app.market_status import initialize_market_status

# Configure logging with UTC
import time as time_module
logging.Formatter.converter = time_module.gmtime
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG to see heartbeat messages
    format="%(asctime)s UTC | %(levelname)-5s | websocket_manager | %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("websocket_manager")


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
API_KEYS = [k for k in [os.getenv("TWELVE_DATA_API_KEY"), os.getenv("TWELVE_DATA_API_KEY_2")] if k]
WS_ENDPOINT = "wss://ws.twelvedata.com/v1/quotes/price"
WS_SYMBOLS = ["EUR/USD", "XAU/USD"]
MIN_SLEEP_ON_ERROR = 5

# Redis setup
try:
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD"),
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True
    )
    redis_client.ping()
    logger.info("Redis connected for WebSocket pubsub")
except Exception as e:
    logger.warning(f"Redis not available: {e}")
    redis_client = None

HOLIDAYS_CACHE_TTL = 345600  # 96 hours

class Bucket:
    def __init__(self, minute_start: datetime, price: float):
        self.minute_start = minute_start
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.volume = 0
        self.tick_count = 1

    def update(self, price: float):
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1


def format_symbol(symbol: str) -> str:
    if symbol.startswith("XAU"):
        return "XAU/USD"
    if len(symbol) == 6:
        return f"{symbol[:3]}/{symbol[3:]}"
    return symbol


def get_api_key() -> str:
    if not API_KEYS:
        raise RuntimeError("No Twelve Data API keys configured")
    return API_KEYS[0]


def store_candle(conn: psycopg.Connection, symbol: str, candle: Dict, holidays, holidays_cached_at):
    validation = validate_timestamp(candle["time"], holidays, holidays_cached_at, HOLIDAYS_CACHE_TTL)
    if not validation.is_valid:
        logger.warning(f"Rejecting {symbol} M1 @ {candle['time']} | {validation.reason}")
        return False

    # Normalize symbol format: "EUR/USD" -> "EURUSD" to match historical data format
    normalized_symbol = symbol.replace("/", "")

    try:
        with conn.cursor() as cur:
            logger.debug(f"Inserting candle: symbol={normalized_symbol} (from {symbol}), time={candle['time']}, open={candle['open']}, close={candle['close']}")
            cur.execute(
                """
                INSERT INTO candlesticks (symbol, timeframe, time, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, timeframe, time) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                """,
                (
                    normalized_symbol,
                    "M1",
                    candle["time"],
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"],
                    candle["volume"]
                ),
            )
            logger.debug(f"INSERT executed successfully, rowcount: {cur.rowcount}")
            # With autocommit=True, no need to call conn.commit() - it's automatic!
        
        # VERIFY the insert worked
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM candlesticks WHERE symbol = %s AND timeframe = 'M1' AND time = %s",
                (normalized_symbol, candle["time"])
            )
            count = cur.fetchone()[0]
            if count == 0:
                logger.error(f"❌ VERIFICATION FAILED: Candle not found in DB after commit! {normalized_symbol} @ {candle['time']}")
            else:
                logger.info(f"✅ Stored M1 candle for {normalized_symbol} @ {candle['time']:%Y-%m-%d %H:%M} (verified)")
                # NOTE: No SSE/Redis publish - frontend uses regular polling like other symbols
    except Exception as e:
        logger.error(f"❌ ERROR storing candle for {symbol}: {type(e).__name__}: {e}")
        logger.error(f"   Connection status: {conn.info.status if hasattr(conn, 'info') else 'unknown'}")
        # Try to reconnect
        try:
            conn.close()
        except:
            pass
        raise ConnectionError(f"Database error: {type(e).__name__}: {e}")
    
    # Publish to Redis for SSE frontend
    if redis_client:
        try:
            message = json.dumps({
                'type': 'candle_update',
                'symbol': symbol,
                'timeframe': 'M1',
                'candle': {
                    'time': candle['time'].isoformat(),
                    'open': candle['open'],
                    'high': candle['high'],
                    'low': candle['low'],
                    'close': candle['close'],
                    'volume': candle['volume']
                },
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            redis_client.publish('updates:candles', message)
            logger.debug(f"Published {symbol} M1 candle to Redis")
        except Exception as e:
            logger.warning(f"Failed to publish to Redis: {e}")
    
    return True


def finalize_bucket(conn, symbol: str, bucket: Bucket, holidays, holidays_cached_at):
    candle = {
        "time": bucket.minute_start,
        "open": bucket.open,
        "high": bucket.high,
        "low": bucket.low,
        "close": bucket.close,
        "volume": bucket.tick_count,
    }
    try:
        store_candle(conn, symbol, candle, holidays, holidays_cached_at)
    except ConnectionError as e:
        logger.error(f"Connection lost during candle storage: {e}")
        # Connection will be recreated on next WebSocket reconnect
        return False
    return True


def run_websocket():
    if not API_KEYS:
        logger.error("No Twelve Data API keys configured")
        return
    initialize_market_status()
    
    # CRITICAL: Set autocommit=True so each INSERT is immediately visible to other connections
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    logger.info(f"Connected to database with autocommit=True. DB: {conn.info.dbname}, Host: {conn.info.host}")

    # Load holiday cache once
    holidays = None
    holidays_cached_at = None
    try:
        from app.market_status import _get_cache
        cached = _get_cache("holidays")
        if cached:
            holidays, holidays_cached_at = cached
    except Exception as exc:
        logger.warning(f"Could not load holiday cache: {exc}")

    buckets: Dict[str, Bucket] = {}
    tick_count = {'count': 0, 'last_log': time.time()}

    def on_message(ws, message):
        tick_count['count'] += 1
        # Log heartbeat every 100 ticks
        if tick_count['count'] % 100 == 0:
            elapsed = time.time() - tick_count['last_log']
            logger.info(f"💓 Received {tick_count['count']} ticks ({100/elapsed:.1f} ticks/sec)")
            tick_count['last_log'] = time.time()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Failed to decode WebSocket message")
            return

        if payload.get("event") == "subscribe-status":
            logger.info(f"Subscribed: {payload}")
            return
        
        # Log price events immediately
        if payload.get("event") == "price":
            logger.info(f"📊 Price event: {payload.get('symbol')} = ${payload.get('price')}")

        symbol = payload.get("symbol")
        price = payload.get("price")
        if symbol is None or price is None:
            logger.debug(f"Ignoring message without symbol/price: {payload}")
            return

        try:
            price = float(price)
        except Exception:
            return

        ts_ms = payload.get("timestamp") or payload.get("ts")
        if ts_ms:
            try:
                # TwelveData sends Unix timestamp in SECONDS, not milliseconds
                ts = datetime.fromtimestamp(float(ts_ms), tz=timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        minute_start = ts.replace(second=0, microsecond=0)
        bucket = buckets.get(symbol)
        if bucket and bucket.minute_start == minute_start:
            bucket.update(price)
        else:
            if bucket:
                finalize_bucket(conn, symbol, bucket, holidays, holidays_cached_at)
            buckets[symbol] = Bucket(minute_start, price)

    def on_error(ws, error):
        # TwelveData closes connection when no ticks - this is normal
        error_str = str(error)
        if "Connection to remote host was lost" in error_str:
            logger.info(f"WebSocket idle disconnect (normal for low-activity periods)")
        else:
            logger.error(f"WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        logger.info(f"WebSocket closed: {close_status_code} {close_msg if close_msg else 'idle timeout'}")

    def on_open(ws):
        logger.info("WebSocket connected, subscribing...")
        subscribe_msg = {
            "action": "subscribe",
            "params": {
                "symbols": ",".join(WS_SYMBOLS)
            }
        }
        ws.send(json.dumps(subscribe_msg))
        
        # Start heartbeat thread (TwelveData recommends every 10 seconds)
        def send_heartbeat():
            while ws.keep_running:
                time.sleep(10)
                if ws.keep_running:
                    try:
                        ws.send(json.dumps({"action": "heartbeat"}))
                        logger.debug("💓 Heartbeat sent")
                    except Exception as e:
                        logger.warning(f"Heartbeat failed: {e}")
                        break
        
        heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
        heartbeat_thread.start()
        logger.info("Heartbeat thread started (10s interval)")

    api_key = get_api_key()
    ws_url = f"{WS_ENDPOINT}?apikey={api_key}"

    while True:
        try:
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error(f"WebSocket loop error: {exc}")
        time.sleep(MIN_SLEEP_ON_ERROR)

    conn.close()


if __name__ == "__main__":
    run_websocket()
