#!/usr/bin/env python3
"""
Post-Backfill Indicator Calculation Script (v2.0 - DST-Safe)
=============================================================
Purpose: Calculate indicators for recent 1000 bars after OHLCV backfill
Strategy: Fetch from database, calculate using technical.py, store back

**CRITICAL FIXES (v2.0):**
1. Uses technical.calculate_all_indicators() - no duplication
2. Checks for D1/W1/MN1 existence before processing (broker-provided)
3. Excludes incomplete CAGG buckets using end_offset

**IMPORTANT:**
- D1/W1/MN1 candles are sourced from broker (DST-aware)
- These timeframes are NOT aggregated; read directly from candlesticks table
- M5-H4 use TimescaleDB continuous aggregates if enabled
- Incomplete current buckets are excluded to prevent partial indicator calculation

Usage:
    python calculate_recent_indicators.py
    python calculate_recent_indicators.py --symbol XAUUSD --timeframe H1
    python calculate_recent_indicators.py --check-htf   # Pre-flight check for D1/W1/MN1
"""

import os
import sys
import argparse
from datetime import datetime
from typing import Dict, List
import logging
import pandas as pd
from tqdm import tqdm
import psycopg
from psycopg.rows import dict_row

# Allow importing app modules when running from repo/container.
scripts_dir = os.path.abspath(os.path.dirname(__file__))
container_root = os.path.abspath(os.path.join(scripts_dir, '..'))
container_app_dir = os.path.join(container_root, 'app')
repo_root = os.path.abspath(os.path.join(scripts_dir, '..', '..'))
api_web_root = os.path.join(repo_root, 'api-web')
api_web_app_dir = os.path.join(api_web_root, 'app')

if os.path.isdir(container_app_dir):
    sys.path.insert(0, container_root)
elif os.path.isdir(api_web_app_dir):
    sys.path.insert(0, api_web_root)
else:
    sys.path.insert(0, scripts_dir)

# Import unified indicator calculation from technical.py (single source of truth)
from app.indicators.technical import calculate_all_indicators

# Configuration
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "",
)

if not DATABASE_URL:
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("TRADING_BOT_DB") or os.getenv("POSTGRES_DB", "ai_trading_bot_data")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not user or password is None or password == "":
        raise RuntimeError("Set DATABASE_URL or POSTGRES_USER/POSTGRES_PASSWORD")
    DATABASE_URL = f"postgresql://{user}:{password}@{host}:{port}/{db}"

# Legacy fallbacks (used only if DB discovery fails)
FALLBACK_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
FALLBACK_TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4"]  # Fixed-duration only (no D1/W1/MN1 without broker)

# How many recent bars to keep indicators for
RECENT_BARS_LIMIT = 1000

# Indicator parameters (passed to technical.calculate_all_indicators)
EMA_PERIODS = [9, 21, 50, 100, 200]
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD = 14
BB_PERIOD, BB_DEVIATION = 20, 2.0
ROC_PERIOD = 10
ADX_PERIOD = 14
OBV_SLOPE_PERIOD = 14
MOMENTUM_EMA = 21  # Fixed: EMA momentum uses 21, not 9
VOLATILITY_LOOKBACK = 100

# CAGG end_offset mapping (exclude incomplete current buckets)
# Matches continuous_aggregates.sql policy settings
# Prevents indicator calculation on partial/unmaterialized candles
CAGG_END_OFFSETS = {
    'M5': 60,      # 1 minute offset
    'M15': 120,    # 2 minutes offset
    'M30': 300,    # 5 minutes offset
    'H1': 600,     # 10 minutes offset
    'H4': 1800,    # 30 minutes offset
}


def _use_timescale_caggs() -> bool:
    return (os.getenv("USE_TIMESCALE_CAGGS") or "").strip().lower() in {"1", "true", "yes", "y"}


def _ohlcv_relation_for_timeframe(timeframe: str) -> str:
    """Return a safe SQL relation name for the requested timeframe.

    - M1 always reads from the base hypertable `candlesticks`.
    - D1/W1/MN1 read from `candlesticks` (broker-provided, not aggregated).
    - M5-H4 can read from Timescale continuous aggregates when enabled.
    """
    tf = (timeframe or "").strip().upper()
    
    # M1 and broker-provided HTF always use base table
    if tf in ("M1", "D1", "W1", "MN1") or not _use_timescale_caggs():
        return "candlesticks"

    # Only fixed-duration timeframes use CAGGs
    mapping = {
        "M5": "candlesticks_m5",
        "M15": "candlesticks_m15",
        "M30": "candlesticks_m30",
        "H1": "candlesticks_h1",
        "H4": "candlesticks_h4",
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return mapping[tf]


def _check_broker_htf_exists(conn: psycopg.Connection) -> List[str]:
    """Check which broker-provided HTF timeframes have data.
    
    Returns list of available HTF timeframes (D1, W1, MN1).
    Empty list if no broker data exists yet.
    """
    with conn.cursor() as cur:
        # NOTE: Postgres requires ORDER BY expressions to appear in the select list
        # when using DISTINCT. Using GROUP BY avoids that restriction.
        cur.execute("""
            SELECT timeframe
            FROM candlesticks
            WHERE timeframe IN ('D1', 'W1', 'MN1')
            GROUP BY timeframe
            ORDER BY
                CASE timeframe
                    WHEN 'D1' THEN 1
                    WHEN 'W1' THEN 2
                    WHEN 'MN1' THEN 3
                    ELSE 999
                END
        """)
        return [row[0] for row in cur.fetchall()]


def fetch_recent_candlesticks(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    limit: int = 1000
) -> pd.DataFrame:
    """Fetch recent N bars from database.
    
    CRITICAL: For CAGGs, excludes incomplete current bucket using end_offset.
    This prevents indicator calculation on partial/unmaterialized candles.
    """
    relation = _ohlcv_relation_for_timeframe(timeframe)
    with conn.cursor(row_factory=dict_row) as cur:
        if relation == "candlesticks":
            cur.execute("""
                SELECT 
                    time as datetime,
                    open, high, low, close, volume
                FROM candlesticks
                WHERE symbol = %s AND timeframe = %s
                ORDER BY time DESC
                LIMIT %s
            """, (symbol, timeframe, limit))
        else:
            # CAGG query: exclude incomplete current bucket using end_offset
            # This matches the policy settings in continuous_aggregates.sql
            end_offset_seconds = CAGG_END_OFFSETS.get(timeframe.upper(), 60)
            cur.execute(f"""
                SELECT
                    time as datetime,
                    open, high, low, close, volume
                FROM {relation}
                WHERE symbol = %s
                  AND time <= NOW() - INTERVAL '{end_offset_seconds} seconds'
                ORDER BY time DESC
                LIMIT %s
            """, (symbol, limit))
        
        rows = cur.fetchall()
    
    if not rows:
        return pd.DataFrame()
    
    df = pd.DataFrame(rows)
    df = df.sort_values('datetime').reset_index(drop=True)

    # Normalize DB numeric types (psycopg may return Decimal for NUMERIC aggregates).
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    
    return df


def _discover_symbols(conn: psycopg.Connection) -> List[str]:
    # Prefer shared Redis-backed symbol cache if available.
    try:
        import redis
        from trading_common.symbols import get_active_symbols_sync

        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD"),
            db=int(os.getenv("REDIS_DB", "0")),
            decode_responses=True,
        )
        syms = get_active_symbols_sync(redis_client=r, postgres_dsn=DATABASE_URL, fallback=list(FALLBACK_SYMBOLS))
        if syms:
            return syms
    except Exception:
        pass

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM candlesticks ORDER BY symbol")
        return [row[0] for row in cur.fetchall()]


def _discover_timeframes(conn: psycopg.Connection, include_m1: bool = False) -> List[str]:
    """Discover available timeframes, checking for broker-provided HTF existence.
    
    CRITICAL: Only includes D1/W1/MN1 if broker data actually exists.
    This prevents attempting to calculate indicators for missing timeframes.
    """
    if _use_timescale_caggs():
        # In CAGG mode the base `candlesticks` table contains:
        # - M1 (from ticks/EA)
        # - D1/W1/MN1 (from broker only, if connected)
        # CAGGs exist for: M5, M15, M30, H1, H4
        
        # Always include CAGG timeframes (aggregated from M1)
        tfs = ["M5", "M15", "M30", "H1", "H4"]
        
        # Check if broker-provided HTF data exists before including
        broker_tfs = _check_broker_htf_exists(conn)
        if broker_tfs:
            print(f"✅ Broker HTF found: {broker_tfs}")
            tfs.extend(broker_tfs)
        else:
            print("⚠️  No broker HTF data (D1/W1/MN1) found - skipping these timeframes")
            print("   Connect MT5 EA to backfill broker data, then re-run this script")
        
        if include_m1:
            tfs.insert(0, "M1")
        return tfs

    with conn.cursor() as cur:
        if include_m1:
            cur.execute("SELECT DISTINCT timeframe FROM candlesticks ORDER BY timeframe")
        else:
            cur.execute("SELECT DISTINCT timeframe FROM candlesticks WHERE timeframe != 'M1' ORDER BY timeframe")
        return [row[0] for row in cur.fetchall()]


def _get_latest_candle_time(conn: psycopg.Connection, *, symbol: str, timeframe: str):
    relation = _ohlcv_relation_for_timeframe(timeframe)
    with conn.cursor() as cur:
        if relation == "candlesticks":
            cur.execute(
                "SELECT MAX(time) FROM candlesticks WHERE symbol = %s AND timeframe = %s",
                (symbol, timeframe),
            )
        else:
            # CAGG: exclude incomplete bucket
            end_offset_seconds = CAGG_END_OFFSETS.get(timeframe.upper(), 60)
            cur.execute(
                f"SELECT MAX(time) FROM {relation} WHERE symbol = %s AND time <= NOW() - INTERVAL '{end_offset_seconds} seconds'",
                (symbol,),
            )
        return cur.fetchone()[0]


def _get_latest_indicator_time(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    at_or_before: datetime | None = None,
):
    with conn.cursor() as cur:
        if at_or_before is None:
            cur.execute(
                "SELECT MAX(time) FROM technical_indicators WHERE symbol = %s AND timeframe = %s",
                (symbol, timeframe),
            )
        else:
            cur.execute(
                """
                SELECT MAX(time)
                FROM technical_indicators
                WHERE symbol = %s AND timeframe = %s AND time <= %s
                """,
                (symbol, timeframe, at_or_before),
            )
        return cur.fetchone()[0]


def _indicator_row_has_values(
        conn: psycopg.Connection,
        *,
        symbol: str,
        timeframe: str,
        at_time: datetime,
) -> bool:
        """Return True if the indicator row at `at_time` has any populated values.

        We have seen placeholder rows where only (symbol,timeframe,time) existed
        but all indicator columns were NULL. Those must be treated as missing.
        """
        with conn.cursor() as cur:
                cur.execute(
                        """
                        SELECT 1
                        FROM technical_indicators
                        WHERE symbol = %s
                            AND timeframe = %s
                            AND time = %s
                            AND (
                                ema_9 IS NOT NULL
                                OR ema_21 IS NOT NULL
                                OR ema_50 IS NOT NULL
                                OR ema_100 IS NOT NULL
                                OR ema_200 IS NOT NULL
                                OR rsi IS NOT NULL
                                OR macd_main IS NOT NULL
                                OR atr IS NOT NULL
                                OR adx IS NOT NULL
                            )
                        LIMIT 1
                        """,
                        (symbol, timeframe, at_time),
                )
                return cur.fetchone() is not None


def store_indicators_batch(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    indicators_df: pd.DataFrame
) -> int:
    """Store calculated indicators in database"""
    if indicators_df.empty:
        return 0
    
    inserted = 0
    
    with conn.cursor() as cur:
        data_tuples = [
            (
                symbol,
                timeframe,
                row['datetime'],
                # EMAs
                float(row.get('EMA_9', 0)) if pd.notna(row.get('EMA_9')) else None,
                float(row.get('EMA_21', 0)) if pd.notna(row.get('EMA_21')) else None,
                float(row.get('EMA_50', 0)) if pd.notna(row.get('EMA_50')) else None,
                float(row.get('EMA_100', 0)) if pd.notna(row.get('EMA_100')) else None,
                float(row.get('EMA_200', 0)) if pd.notna(row.get('EMA_200')) else None,
                float(row.get('ema_momentum_slope', 0)) if pd.notna(row.get('ema_momentum_slope')) else None,
                # RSI & MACD
                float(row.get('rsi', 0)) if pd.notna(row.get('rsi')) else None,
                float(row.get('macd_main', 0)) if pd.notna(row.get('macd_main')) else None,
                float(row.get('macd_signal', 0)) if pd.notna(row.get('macd_signal')) else None,
                float(row.get('macd_histogram', 0)) if pd.notna(row.get('macd_histogram')) else None,
                float(row.get('roc_percent', 0)) if pd.notna(row.get('roc_percent')) else None,
                # Volatility
                float(row.get('atr', 0)) if pd.notna(row.get('atr')) else None,
                float(row.get('atr_percentile', 0)) if pd.notna(row.get('atr_percentile')) else None,
                float(row.get('bb_upper', 0)) if pd.notna(row.get('bb_upper')) else None,
                float(row.get('bb_middle', 0)) if pd.notna(row.get('bb_middle')) else None,
                float(row.get('bb_lower', 0)) if pd.notna(row.get('bb_lower')) else None,
                float(row.get('bb_squeeze_ratio', 0)) if pd.notna(row.get('bb_squeeze_ratio')) else None,
                float(row.get('bb_width_percentile', 0)) if pd.notna(row.get('bb_width_percentile')) else None,
                # Trend
                float(row.get('adx', 0)) if pd.notna(row.get('adx')) else None,
                float(row.get('dmp', 0)) if pd.notna(row.get('dmp')) else None,
                float(row.get('dmn', 0)) if pd.notna(row.get('dmn')) else None,
                # Volume
                float(row.get('obv_slope', 0)) if pd.notna(row.get('obv_slope')) else None
            )
            for _, row in indicators_df.iterrows()
        ]
        
        cur.executemany("""
            INSERT INTO technical_indicators (
                symbol, timeframe, time,
                ema_9, ema_21, ema_50, ema_100, ema_200, ema_momentum_slope,
                rsi, macd_main, macd_signal, macd_histogram, roc_percent,
                atr, atr_percentile, bb_upper, bb_middle, bb_lower, 
                bb_squeeze_ratio, bb_width_percentile,
                adx, dmp, dmn, obv_slope
            )
            VALUES (
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (symbol, timeframe, time) 
            DO UPDATE SET
                ema_9 = EXCLUDED.ema_9,
                ema_21 = EXCLUDED.ema_21,
                ema_50 = EXCLUDED.ema_50,
                ema_100 = EXCLUDED.ema_100,
                ema_200 = EXCLUDED.ema_200,
                ema_momentum_slope = EXCLUDED.ema_momentum_slope,
                rsi = EXCLUDED.rsi,
                macd_main = EXCLUDED.macd_main,
                macd_signal = EXCLUDED.macd_signal,
                macd_histogram = EXCLUDED.macd_histogram,
                roc_percent = EXCLUDED.roc_percent,
                atr = EXCLUDED.atr,
                atr_percentile = EXCLUDED.atr_percentile,
                bb_upper = EXCLUDED.bb_upper,
                bb_middle = EXCLUDED.bb_middle,
                bb_lower = EXCLUDED.bb_lower,
                bb_squeeze_ratio = EXCLUDED.bb_squeeze_ratio,
                bb_width_percentile = EXCLUDED.bb_width_percentile,
                adx = EXCLUDED.adx,
                dmp = EXCLUDED.dmp,
                dmn = EXCLUDED.dmn,
                obv_slope = EXCLUDED.obv_slope,
                updated_at = NOW()
        """, data_tuples)
        
        inserted = cur.rowcount
    
    conn.commit()
    return inserted


def store_latest_indicators(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    at_time: datetime,
    indicators: Dict,
) -> int:
    """Upsert the latest computed indicator snapshot for a symbol/timeframe."""
    emas = (indicators or {}).get("emas") or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO technical_indicators (
                symbol, timeframe, time,
                ema_9, ema_21, ema_50, ema_100, ema_200, ema_momentum_slope,
                rsi, macd_main, macd_signal, macd_histogram, roc_percent,
                atr, atr_percentile, bb_upper, bb_middle, bb_lower,
                bb_squeeze_ratio, bb_width_percentile,
                adx, dmp, dmn, obv_slope
            )
            VALUES (
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (symbol, timeframe, time)
            DO UPDATE SET
                ema_9 = EXCLUDED.ema_9,
                ema_21 = EXCLUDED.ema_21,
                ema_50 = EXCLUDED.ema_50,
                ema_100 = EXCLUDED.ema_100,
                ema_200 = EXCLUDED.ema_200,
                ema_momentum_slope = EXCLUDED.ema_momentum_slope,
                rsi = EXCLUDED.rsi,
                macd_main = EXCLUDED.macd_main,
                macd_signal = EXCLUDED.macd_signal,
                macd_histogram = EXCLUDED.macd_histogram,
                roc_percent = EXCLUDED.roc_percent,
                atr = EXCLUDED.atr,
                atr_percentile = EXCLUDED.atr_percentile,
                bb_upper = EXCLUDED.bb_upper,
                bb_middle = EXCLUDED.bb_middle,
                bb_lower = EXCLUDED.bb_lower,
                bb_squeeze_ratio = EXCLUDED.bb_squeeze_ratio,
                bb_width_percentile = EXCLUDED.bb_width_percentile,
                adx = EXCLUDED.adx,
                dmp = EXCLUDED.dmp,
                dmn = EXCLUDED.dmn,
                obv_slope = EXCLUDED.obv_slope,
                updated_at = NOW()
            """,
            (
                symbol,
                timeframe,
                at_time,
                emas.get("EMA_9"),
                emas.get("EMA_21"),
                emas.get("EMA_50"),
                emas.get("EMA_100"),
                emas.get("EMA_200"),
                (indicators or {}).get("ema_momentum_slope"),
                (indicators or {}).get("rsi"),
                (indicators or {}).get("macd_main"),
                (indicators or {}).get("macd_signal"),
                (indicators or {}).get("macd_histogram"),
                (indicators or {}).get("roc_percent"),
                (indicators or {}).get("atr"),
                (indicators or {}).get("atr_percentile"),
                (indicators or {}).get("bb_upper"),
                (indicators or {}).get("bb_middle"),
                (indicators or {}).get("bb_lower"),
                (indicators or {}).get("bb_squeeze_ratio"),
                (indicators or {}).get("bb_width_percentile"),
                (indicators or {}).get("adx"),
                (indicators or {}).get("dmp"),
                (indicators or {}).get("dmn"),
                (indicators or {}).get("obv_slope"),
            ),
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def calculate_and_store_indicators(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    *,
    verbose: bool = False,
) -> Dict:
    """Calculate indicators for one symbol/timeframe and store"""
    if verbose:
        print(f"\n📊 {symbol} @ {timeframe}")
    
    latest_candle_time = _get_latest_candle_time(conn, symbol=symbol, timeframe=timeframe)
    latest_indicator_time = _get_latest_indicator_time(
        conn,
        symbol=symbol,
        timeframe=timeframe,
        at_or_before=latest_candle_time,
    )

    has_values = False
    if latest_indicator_time:
        has_values = _indicator_row_has_values(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            at_time=latest_indicator_time,
        )

    if latest_candle_time and latest_indicator_time and has_values:
        # In CAGG mode, candle timestamps are the bucket start; indicators must match exactly.
        if _use_timescale_caggs() and timeframe.upper() not in ("M1", "D1", "W1", "MN1"):
            if latest_indicator_time == latest_candle_time:
                if verbose:
                    print(f"   ⏭️  Up-to-date (candles={latest_candle_time}, indicators={latest_indicator_time})")
                return {"symbol": symbol, "timeframe": timeframe, "status": "up_to_date", "bars_processed": 0, "bars_stored": 0, "success": True}
        else:
            if latest_indicator_time >= latest_candle_time:
                if verbose:
                    print(f"   ⏭️  Up-to-date (candles={latest_candle_time}, indicators={latest_indicator_time})")
                return {"symbol": symbol, "timeframe": timeframe, "status": "up_to_date", "bars_processed": 0, "bars_stored": 0, "success": True}

    if latest_candle_time and latest_indicator_time and not has_values and verbose:
        print(f"   ⚠️  Indicators row exists but is empty at {latest_indicator_time}; recalculating")

    # Fetch data
    df = fetch_recent_candlesticks(conn, symbol, timeframe, limit=RECENT_BARS_LIMIT)
    
    if df.empty:
        if verbose:
            print(f"   ⚠️  No data found in database")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "skipped_no_data",
            "bars_processed": 0,
            "bars_stored": 0,
            "success": False,
        }

    if verbose:
        print(f"   Fetched {len(df)} bars from database")
    
    try:
        # Calculate ALL indicators using technical.py (single source of truth)
        # NOTE: calculate_all_indicators returns a snapshot dict (it does not mutate df)
        indicators = calculate_all_indicators(
            df=df,
            ema_periods=EMA_PERIODS,
            rsi_period=RSI_PERIOD,
            macd_fast=MACD_FAST,
            macd_slow=MACD_SLOW,
            macd_signal=MACD_SIGNAL,
            atr_period=ATR_PERIOD,
            bb_period=BB_PERIOD,
            bb_deviation=BB_DEVIATION,
            roc_period=ROC_PERIOD,
            adx_period=ADX_PERIOD,
            obv_slope_period=OBV_SLOPE_PERIOD,
            momentum_ema=MOMENTUM_EMA,
            volatility_lookback=VOLATILITY_LOOKBACK
        )

        if not indicators:
            if verbose:
                print("   ⚠️  Insufficient data for indicator calculation")
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "skipped_insufficient_data",
                "bars_processed": len(df),
                "bars_stored": 0,
                "success": False,
            }

        at_time = df["datetime"].iloc[-1]
        inserted = store_latest_indicators(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            at_time=at_time,
            indicators=indicators,
        )

        if verbose:
            print(f"   ✅ Stored latest indicator snapshot at {at_time}")
        else:
            print(f"✅ {symbol} {timeframe}: stored snapshot at {at_time}")
        
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "stored",
            "bars_processed": len(df),
            "bars_stored": 1 if inserted else 0,
            "success": True
        }
        
    except Exception as e:
        if verbose:
            print(f"   ❌ Error: {e}")
            import traceback
            traceback.print_exc()
        else:
            print(f"❌ {symbol} {timeframe}: {e}")
        return {"symbol": symbol, "timeframe": timeframe, "status": "error", "bars_processed": 0, "bars_stored": 0, "success": False}


def main():
    parser = argparse.ArgumentParser(description="Calculate indicators for recent data")
    parser.add_argument("--symbol", help="Single symbol (e.g., XAUUSD)")
    parser.add_argument("--timeframe", help="Single timeframe (e.g., H1)")
    parser.add_argument(
        "--symbols",
        help="Comma-separated symbols (overrides DB discovery), e.g. XAUUSD,EURUSD",
    )
    parser.add_argument(
        "--timeframes",
        help="Comma-separated timeframes (overrides DB discovery), e.g. M5,M15,H1",
    )
    parser.add_argument(
        "--include-m1",
        action="store_true",
        help="Include M1 timeframe when auto-discovering timeframes from DB",
    )
    parser.add_argument(
        "--check-htf",
        action="store_true",
        help="Pre-flight check: verify broker HTF (D1/W1/MN1) data exists, then exit",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose per-symbol/timeframe logging",
    )
    args = parser.parse_args()

    if not args.verbose:
        # Keep stdout clean in batch runs; the indicator engine logs warnings for short histories.
        logging.getLogger("app.indicators.technical").setLevel(logging.ERROR)
    
    print("="*70)
    print("📈 Post-Backfill Indicator Calculation (v2.0 - DST-Safe)")
    print("="*70)
    print(f"Target: Recent {RECENT_BARS_LIMIT} bars per symbol/timeframe")
    print("="*70)
    
    try:
        conn = psycopg.connect(DATABASE_URL)
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return 1
    
    def _parse_csv(value: str) -> List[str]:
        return [s.strip().upper() for s in (value or "").split(",") if s.strip()]

    try:
        # Pre-flight check mode
        if args.check_htf:
            print("\n🔍 Checking broker HTF data availability...")
            broker_tfs = _check_broker_htf_exists(conn)
            if broker_tfs:
                print(f"✅ Broker HTF found: {broker_tfs}")
                print("\nCounts per symbol:")
                with conn.cursor() as cur:
                    for tf in broker_tfs:
                        cur.execute(
                            "SELECT symbol, COUNT(*) FROM candlesticks WHERE timeframe = %s GROUP BY symbol ORDER BY symbol",
                            (tf,)
                        )
                        print(f"\n  {tf}:")
                        for symbol, count in cur.fetchall():
                            print(f"    {symbol}: {count:,} candles")
                return 0
            else:
                print("❌ No broker HTF data (D1/W1/MN1) found")
                print("\n⚠️  REQUIRED: Connect MT5 EA to backfill D1/W1/MN1 before running indicators")
                print("   See: docs/DST_FIX_MIGRATION.md")
                return 1
        
        # Normal indicator calculation mode
        if args.symbol and args.timeframe:
            symbols = [args.symbol.upper()]
            timeframes = [args.timeframe.upper()]
        else:
            if args.symbols:
                symbols = _parse_csv(args.symbols)
            else:
                symbols = _discover_symbols(conn)
                if not symbols:
                    symbols = FALLBACK_SYMBOLS
                    print(f"⚠️  Using fallback symbols: {symbols}")

            if args.timeframes:
                timeframes = _parse_csv(args.timeframes)
            else:
                timeframes = _discover_timeframes(conn, include_m1=args.include_m1)
                if not timeframes:
                    timeframes = FALLBACK_TIMEFRAMES
                    print(f"⚠️  Using fallback timeframes: {timeframes}")
        
        print(f"\n📋 Processing:")
        print(f"   Symbols: {', '.join(symbols)}")
        print(f"   Timeframes: {', '.join(timeframes)}")
        
        results = []
        for symbol in symbols:
            for timeframe in timeframes:
                result = calculate_and_store_indicators(conn, symbol, timeframe, verbose=args.verbose)
                results.append(result)
        
        # Summary
        print("\n" + "="*70)
        print("SUMMARY")
        print("="*70)
        
        stored = [r for r in results if r.get("status") == "stored"]
        up_to_date = [r for r in results if r.get("status") == "up_to_date"]
        skipped_insufficient = [r for r in results if r.get("status") == "skipped_insufficient_data"]
        skipped_no_data = [r for r in results if r.get("status") == "skipped_no_data"]
        errors = [r for r in results if r.get("status") == "error"]

        print(f"✅ Stored: {len(stored)}")
        print(f"⏭️  Up-to-date: {len(up_to_date)}")
        if skipped_insufficient:
            tfs = sorted({r['timeframe'] for r in skipped_insufficient})
            print(f"⚠️  Skipped (insufficient history): {len(skipped_insufficient)} (tfs={tfs})")
        if skipped_no_data:
            tfs = sorted({r['timeframe'] for r in skipped_no_data})
            print(f"⚠️  Skipped (no data): {len(skipped_no_data)} (tfs={tfs})")
        if errors:
            print(f"❌ Errors: {len(errors)}")
            for r in errors:
                print(f"   - {r['symbol']} @ {r['timeframe']}")

        total_bars = sum(r.get("bars_stored", 0) for r in stored)
        print(f"📊 Total indicator rows stored: {total_bars:,}")
        print("="*70)

        return 0 if not errors else 1
        
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
