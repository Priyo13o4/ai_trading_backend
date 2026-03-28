#!/usr/bin/env python3
"""
Aggregate M1 candles into higher timeframes (M5/M15/H1/H4/D1).
- Reads new M1 candles since last aggregated time per symbol/timeframe.
- Uses same aggregation logic as historical importer (anchor on first candle timestamp).
- Idempotent upserts into candlesticks table.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict
import os
import sys

import psycopg

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Configure logging with UTC
import time
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC | %(levelname)-5s | candle_aggregator | %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("candle_aggregator")


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

FALLBACK_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]

# All target timeframes including M30, W1, MN1
TARGET_TIMEFRAMES = {
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,    # 7 * 24 * 60
    "MN1": 43200,   # 30 * 24 * 60 (approximate)
}


def discover_symbols(conn: psycopg.Connection) -> List[str]:
    """Discover symbols dynamically.

    Precedence:
    1) `AGG_SYMBOLS` env (comma-separated)
    2) `MT5_SUBSCRIBE_SYMBOLS` env (comma-separated)
    3) distinct symbols already present in `candlesticks`
    4) fallback static list
    """
    env = (os.getenv("AGG_SYMBOLS") or os.getenv("MT5_SUBSCRIBE_SYMBOLS") or "").strip()
    if env:
        syms = [s.strip().upper() for s in env.split(",") if s.strip()]
        return syms or list(FALLBACK_SYMBOLS)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT symbol FROM candlesticks WHERE symbol IS NOT NULL AND symbol <> '' ORDER BY symbol"
            )
            syms = [str(r[0]).strip().upper() for r in cur.fetchall() if r and r[0]]
        return syms or list(FALLBACK_SYMBOLS)
    except Exception as e:
        logger.warning(f"Symbol discovery failed; using fallback. err={e}")
        return list(FALLBACK_SYMBOLS)


def _floor_to_timeframe(dt: datetime, timeframe_minutes: int) -> datetime:
    """Floor a timestamp to the aligned timeframe boundary (UTC).
    
    Special handling for:
    - W1: Align to Monday 00:00 UTC
    - MN1: Align to 1st of month 00:00 UTC
    """
    dt = dt.replace(second=0, microsecond=0)
    
    # Monthly: Align to 1st of month
    if timeframe_minutes >= 43200:  # MN1
        return dt.replace(day=1, hour=0, minute=0)
    
    # Weekly: Align to Monday 00:00 UTC
    if timeframe_minutes >= 10080:  # W1
        days_since_monday = dt.weekday()
        monday = dt - timedelta(days=days_since_monday)
        return monday.replace(hour=0, minute=0)
    
    # Daily and below
    if timeframe_minutes >= 1440:
        return dt.replace(hour=0, minute=0)
    
    total_minutes = dt.hour * 60 + dt.minute
    period_minute = (total_minutes // timeframe_minutes) * timeframe_minutes
    return dt.replace(hour=period_minute // 60, minute=period_minute % 60)


def aggregate_candles(candles: List[Dict], timeframe_minutes: int):
    """Aggregate M1 candles into higher timeframes with ALIGNED timestamps.

    Only emit fully closed buckets (boundary strictly before the current open bucket).
    """
    if not candles:
        return

    now_utc = datetime.now(timezone.utc)
    cutoff_boundary = _floor_to_timeframe(now_utc, timeframe_minutes)

    current_period = []
    period_boundary = None

    for candle in candles:
        candle_boundary = _floor_to_timeframe(candle['time'], timeframe_minutes)

        if period_boundary is None:
            period_boundary = candle_boundary

        if candle_boundary != period_boundary:
            if current_period:
                # Emit only if the bucket is closed (boundary < cutoff)
                if period_boundary < cutoff_boundary:
                    yield aggregate_period(current_period, period_boundary)
                else:
                    logger.debug(
                        "Skipping open bucket at %s (cutoff %s)",
                        period_boundary.isoformat(),
                        cutoff_boundary.isoformat()
                    )
            current_period = [candle]
            period_boundary = candle_boundary
        else:
            current_period.append(candle)

    if current_period:
        if period_boundary < cutoff_boundary:
            yield aggregate_period(current_period, period_boundary)
        else:
            logger.debug(
                "Skipping final open bucket at %s (cutoff %s)",
                period_boundary.isoformat(),
                cutoff_boundary.isoformat()
            )


def aggregate_period(period: List[Dict], boundary_time: datetime):
    """Aggregate period using ALIGNED boundary timestamp (e.g., 17:05, 17:10, not 17:06)"""
    return {
        "time": boundary_time,  # Use aligned boundary, NOT first candle time
        "open": period[0]['open'],
        "high": max(c['high'] for c in period),
        "low": min(c['low'] for c in period),
        "close": period[-1]['close'],
        "volume": sum(c.get('volume', 0) for c in period),
    }


def get_latest_time(conn, symbol: str, timeframe: str):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT MAX(time) FROM candlesticks WHERE symbol=%s AND timeframe=%s""",
            (symbol, timeframe),
        )
        res = cur.fetchone()[0]
    return res


def fetch_m1_since(conn, symbol: str, since_time: datetime):
    with conn.cursor() as cur:
        if since_time:
            # Incremental: fetch only new M1 candles since last aggregation
            cur.execute(
                """
                SELECT time, open, high, low, close, volume
                FROM candlesticks
                WHERE symbol=%s AND timeframe='M1' AND time > %s
                ORDER BY time ASC
                """,
                (symbol, since_time),
            )
        else:
            # Initial aggregation: fetch ALL M1 candles (no limit)
            cur.execute(
                """
                SELECT time, open, high, low, close, volume
                FROM candlesticks
                WHERE symbol=%s AND timeframe='M1'
                ORDER BY time ASC
                """,
                (symbol,),
            )
        rows = cur.fetchall()
    return [
        {
            "time": row[0],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5] or 0),
        }
        for row in rows
    ]


def store_aggregated(conn, symbol: str, timeframe: str, candles: List[Dict]):
    if not candles:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for candle in candles:
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
                    symbol,
                    timeframe,
                    candle['time'],
                    candle['open'],
                    candle['high'],
                    candle['low'],
                    candle['close'],
                    candle.get('volume', 0),
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


def run():
    conn = psycopg.connect(DATABASE_URL)
    total_inserted = 0

    symbols = discover_symbols(conn)
    logger.info(f"Discovered symbols: {symbols}")

    for symbol in symbols:
        for timeframe, minutes in TARGET_TIMEFRAMES.items():
            latest = get_latest_time(conn, symbol, timeframe)
            m1_since = latest if latest else None
            m1_candles = fetch_m1_since(conn, symbol, m1_since)
            if not m1_candles:
                logger.info(f"{symbol:8} {timeframe:4} | No M1 source candles")
                continue

            aggregated = list(aggregate_candles(m1_candles, minutes))
            if not aggregated:
                logger.info(f"{symbol:8} {timeframe:4} | Nothing to aggregate")
                continue

            inserted = store_aggregated(conn, symbol, timeframe, aggregated)
            total_inserted += inserted
            logger.info(f"{symbol:8} {timeframe:4} | Aggregated {len(aggregated)} -> inserted {inserted}")

    conn.close()
    logger.info(f"Aggregation complete. Total inserted/updated: {total_inserted}")


if __name__ == "__main__":
    run()
