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
from datetime import datetime, timedelta, timezone
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

# Incremental processing defaults (override with CLI args or env vars)
DEFAULT_SAFETY_BACKFILL_BARS = int(os.getenv("INDICATOR_SAFETY_BACKFILL_BARS", "2"))
DEFAULT_MAX_NEW_BARS_PER_CYCLE = int(os.getenv("INDICATOR_MAX_NEW_BARS_PER_CYCLE", "8"))
DEFAULT_LOOKBACK_BARS = int(os.getenv("INDICATOR_LOOKBACK_BARS", "300"))
DEFAULT_FORCE_OVERLAP_RECOMPUTE_MINUTES = int(
    os.getenv("INDICATOR_FORCE_OVERLAP_RECOMPUTE_MINUTES", "60")
)

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

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
    "MN1": 2592000,
}


def _safe_positive_int(value: int, *, fallback: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except Exception:
        return fallback
    return parsed if parsed >= minimum else fallback


def _safe_non_negative_int(value: int, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return fallback
    return parsed if parsed >= 0 else fallback


def _timeframe_delta(timeframe: str) -> timedelta:
    seconds = TIMEFRAME_SECONDS.get((timeframe or "").strip().upper(), 300)
    return timedelta(seconds=seconds)


def _ensure_indicator_watermarks_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS indicator_update_watermarks (
                symbol VARCHAR(20) NOT NULL,
                timeframe VARCHAR(10) NOT NULL,
                watermark_time TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (symbol, timeframe)
            )
            """
        )


def _acquire_watermark_pair_lock(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
) -> None:
    """Serialize watermark reads/writes per (symbol, timeframe) within a transaction."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))
            """,
            (symbol, timeframe),
        )


def _get_watermark_for_update(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT watermark_time, updated_at
            FROM indicator_update_watermarks
            WHERE symbol = %s AND timeframe = %s
            FOR UPDATE
            """,
            (symbol, timeframe),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]


def _upsert_watermark(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    watermark_time: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indicator_update_watermarks (symbol, timeframe, watermark_time)
            VALUES (%s, %s, %s)
            ON CONFLICT (symbol, timeframe)
            DO UPDATE SET
                watermark_time = GREATEST(
                    indicator_update_watermarks.watermark_time,
                    EXCLUDED.watermark_time
                ),
                updated_at = NOW()
            """,
            (symbol, timeframe, watermark_time),
        )


def _fetch_candle_times_since(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    lower_bound: datetime,
    upper_bound: datetime,
    max_rows: int,
) -> List[datetime]:
    relation = _ohlcv_relation_for_timeframe(timeframe)
    with conn.cursor() as cur:
        if relation == "candlesticks":
            cur.execute(
                """
                SELECT time
                FROM candlesticks
                WHERE symbol = %s
                  AND timeframe = %s
                  AND time > %s
                  AND time <= %s
                ORDER BY time ASC
                LIMIT %s
                """,
                (symbol, timeframe, lower_bound, upper_bound, max_rows),
            )
        else:
            cur.execute(
                f"""
                SELECT time
                FROM {relation}
                WHERE symbol = %s
                  AND time > %s
                  AND time <= %s
                ORDER BY time ASC
                LIMIT %s
                """,
                (symbol, lower_bound, upper_bound, max_rows),
            )
        return [row[0] for row in cur.fetchall()]


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
    limit: int = 1000,
    at_or_before: datetime | None = None,
) -> pd.DataFrame:
    """Fetch recent N bars from database.
    
    CRITICAL: For CAGGs, excludes incomplete current bucket using end_offset.
    This prevents indicator calculation on partial/unmaterialized candles.
    """
    relation = _ohlcv_relation_for_timeframe(timeframe)
    with conn.cursor(row_factory=dict_row) as cur:
        if relation == "candlesticks":
            if at_or_before is None:
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
                cur.execute("""
                    SELECT 
                        time as datetime,
                        open, high, low, close, volume
                    FROM candlesticks
                    WHERE symbol = %s
                      AND timeframe = %s
                      AND time <= %s
                    ORDER BY time DESC
                    LIMIT %s
                """, (symbol, timeframe, at_or_before, limit))
        else:
            # CAGG query: exclude incomplete current bucket using end_offset
            # This matches the policy settings in continuous_aggregates.sql
            end_offset_seconds = CAGG_END_OFFSETS.get(timeframe.upper(), 60)
            if at_or_before is None:
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
            else:
                cur.execute(f"""
                    SELECT
                        time as datetime,
                        open, high, low, close, volume
                    FROM {relation}
                    WHERE symbol = %s
                      AND time <= LEAST(%s, NOW() - INTERVAL '{end_offset_seconds} seconds')
                    ORDER BY time DESC
                    LIMIT %s
                """, (symbol, at_or_before, limit))
        
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


def store_latest_indicators(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    at_time: datetime,
    indicators: Dict,
    commit: bool = True,
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
    if commit:
        conn.commit()
    return inserted


def calculate_and_store_indicators(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    *,
    verbose: bool = False,
    safety_backfill_bars: int = DEFAULT_SAFETY_BACKFILL_BARS,
    max_new_bars_per_cycle: int = DEFAULT_MAX_NEW_BARS_PER_CYCLE,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    force_overlap_recompute_minutes: int = DEFAULT_FORCE_OVERLAP_RECOMPUTE_MINUTES,
) -> Dict:
    """Calculate indicators for one symbol/timeframe and store"""
    if verbose:
        print(f"\n📊 {symbol} @ {timeframe}")
    safety_backfill_bars = _safe_positive_int(safety_backfill_bars, fallback=2)
    max_new_bars_per_cycle = _safe_positive_int(max_new_bars_per_cycle, fallback=8)
    lookback_bars = _safe_positive_int(lookback_bars, fallback=300)
    force_overlap_recompute_minutes = _safe_non_negative_int(
        force_overlap_recompute_minutes,
        fallback=60,
    )
    
    latest_candle_time = _get_latest_candle_time(conn, symbol=symbol, timeframe=timeframe)
    if latest_candle_time is None:
        if verbose:
            print("   ⚠️  No candles found")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "skipped_no_data",
            "bars_processed": 0,
            "bars_stored": 0,
            "success": False,
        }

    try:
        with conn.transaction():
            _acquire_watermark_pair_lock(
                conn,
                symbol=symbol,
                timeframe=timeframe,
            )

            watermark_time, watermark_updated_at = _get_watermark_for_update(
                conn,
                symbol=symbol,
                timeframe=timeframe,
            )

            if watermark_time is None:
                seeded_indicator_time = _get_latest_indicator_time(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    at_or_before=latest_candle_time,
                )
                if seeded_indicator_time and _indicator_row_has_values(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    at_time=seeded_indicator_time,
                ):
                    watermark_time = seeded_indicator_time

            timeframe_step = _timeframe_delta(timeframe)

            if watermark_time is None:
                # Bootstrap: process only a bounded recent window.
                lower_bound = latest_candle_time - (timeframe_step * max_new_bars_per_cycle)
                candidate_times = _fetch_candle_times_since(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    lower_bound=lower_bound,
                    upper_bound=latest_candle_time,
                    max_rows=max_new_bars_per_cycle,
                )
            else:
                # First find truly new closed candles beyond watermark.
                new_candle_times = _fetch_candle_times_since(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    lower_bound=watermark_time,
                    upper_bound=latest_candle_time,
                    max_rows=max_new_bars_per_cycle,
                )

                if not new_candle_times:
                    should_force_overlap = False
                    if force_overlap_recompute_minutes > 0 and watermark_updated_at is not None:
                        should_force_overlap = (
                            datetime.now(timezone.utc) - watermark_updated_at
                        ) >= timedelta(minutes=force_overlap_recompute_minutes)

                    if not should_force_overlap:
                        if verbose:
                            print(f"   ⏭️  Up-to-date (watermark={watermark_time}, latest={latest_candle_time})")
                        return {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "status": "up_to_date",
                            "bars_processed": 0,
                            "bars_stored": 0,
                            "success": True,
                        }

                    overlap_lower_bound = watermark_time - (timeframe_step * safety_backfill_bars)
                    candidate_times = _fetch_candle_times_since(
                        conn,
                        symbol=symbol,
                        timeframe=timeframe,
                        lower_bound=overlap_lower_bound,
                        upper_bound=watermark_time,
                        max_rows=safety_backfill_bars,
                    )
                    if verbose:
                        print(
                            "   ♻️  Forced overlap refresh on stable watermark "
                            f"(age>={force_overlap_recompute_minutes}m, candidates={len(candidate_times)})"
                        )
                else:
                    # Apply tiny overlap only when there is at least one new candle.
                    overlap_lower_bound = watermark_time - (timeframe_step * safety_backfill_bars)
                    candidate_times = _fetch_candle_times_since(
                        conn,
                        symbol=symbol,
                        timeframe=timeframe,
                        lower_bound=overlap_lower_bound,
                        upper_bound=new_candle_times[-1],
                        max_rows=max_new_bars_per_cycle + safety_backfill_bars,
                    )

            if not candidate_times:
                if verbose:
                    print(f"   ⏭️  Up-to-date (watermark={watermark_time}, latest={latest_candle_time})")
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "status": "up_to_date",
                    "bars_processed": 0,
                    "bars_stored": 0,
                    "success": True,
                }

            stored_count = 0
            processed_count = 0
            last_success_time = None

            for candle_time in candidate_times:
                df = fetch_recent_candlesticks(
                    conn,
                    symbol,
                    timeframe,
                    limit=lookback_bars,
                    at_or_before=candle_time,
                )
                if df.empty:
                    continue
                processed_count += 1

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
                    volatility_lookback=VOLATILITY_LOOKBACK,
                )

                if not indicators:
                    continue

                inserted = store_latest_indicators(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    at_time=candle_time,
                    indicators=indicators,
                    commit=False,
                )
                stored_count += 1 if inserted else 0
                last_success_time = candle_time

            if last_success_time is None:
                if verbose:
                    print("   ⚠️  Insufficient history for indicator calculation in this cycle")
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "status": "skipped_insufficient_data",
                    "bars_processed": processed_count,
                    "bars_stored": 0,
                    "success": False,
                }

            _upsert_watermark(
                conn,
                symbol=symbol,
                timeframe=timeframe,
                watermark_time=last_success_time,
            )

            if verbose:
                print(
                    f"   ✅ Stored {stored_count} snapshots, watermark -> {last_success_time} "
                    f"(latest={latest_candle_time}, candidates={len(candidate_times)})"
                )

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "stored",
                "bars_processed": processed_count,
                "bars_stored": stored_count,
                "success": True,
            }

    except Exception as e:
        if verbose:
            print(f"   ❌ Error: {e}")
            import traceback
            traceback.print_exc()
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "error",
            "bars_processed": 0,
            "bars_stored": 0,
            "success": False,
            "error": str(e),
        }


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
    parser.add_argument(
        "--safety-backfill-bars",
        type=int,
        default=DEFAULT_SAFETY_BACKFILL_BARS,
        help="Small overlap window to recompute recent closed bars (default from INDICATOR_SAFETY_BACKFILL_BARS)",
    )
    parser.add_argument(
        "--max-new-bars-per-cycle",
        type=int,
        default=DEFAULT_MAX_NEW_BARS_PER_CYCLE,
        help="Maximum new bars processed per symbol/timeframe in one run (default from INDICATOR_MAX_NEW_BARS_PER_CYCLE)",
    )
    parser.add_argument(
        "--lookback-bars",
        type=int,
        default=DEFAULT_LOOKBACK_BARS,
        help="Bars loaded per snapshot computation (default from INDICATOR_LOOKBACK_BARS)",
    )
    parser.add_argument(
        "--force-overlap-recompute-minutes",
        type=int,
        default=DEFAULT_FORCE_OVERLAP_RECOMPUTE_MINUTES,
        help=(
            "When no new closed candle exists, force a tiny overlap recompute at this interval in minutes "
            "(default from INDICATOR_FORCE_OVERLAP_RECOMPUTE_MINUTES; 0 disables)"
        ),
    )
    args = parser.parse_args()

    if not args.verbose:
        # Keep stdout clean in batch runs; the indicator engine logs warnings for short histories.
        logging.getLogger("app.indicators.technical").setLevel(logging.ERROR)
    
    print("="*70)
    print("📈 Post-Backfill Indicator Calculation (v2.0 - DST-Safe)")
    print("="*70)
    print(
        f"Incremental mode: backfill_bars={_safe_positive_int(args.safety_backfill_bars, fallback=2)} "
        f"max_new_per_cycle={_safe_positive_int(args.max_new_bars_per_cycle, fallback=8)} "
        f"lookback_bars={_safe_positive_int(args.lookback_bars, fallback=300)} "
        f"force_overlap_recompute_minutes={_safe_non_negative_int(args.force_overlap_recompute_minutes, fallback=60)}"
    )
    print("="*70)
    
    try:
        conn = psycopg.connect(DATABASE_URL)
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return 1
    
    def _parse_csv(value: str) -> List[str]:
        return [s.strip().upper() for s in (value or "").split(",") if s.strip()]

    try:
        _ensure_indicator_watermarks_table(conn)
        conn.commit()

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
                result = calculate_and_store_indicators(
                    conn,
                    symbol,
                    timeframe,
                    verbose=args.verbose,
                    safety_backfill_bars=args.safety_backfill_bars,
                    max_new_bars_per_cycle=args.max_new_bars_per_cycle,
                    lookback_bars=args.lookback_bars,
                    force_overlap_recompute_minutes=args.force_overlap_recompute_minutes,
                )
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
            preview_count = 5
            for r in errors[:preview_count]:
                print(f"   - {r['symbol']} @ {r['timeframe']}")
            if len(errors) > preview_count:
                print(f"   - ... and {len(errors) - preview_count} more")

        total_bars = sum(r.get("bars_stored", 0) for r in stored)
        print(f"📊 Total indicator rows stored: {total_bars:,}")
        print("="*70)

        return 0 if not errors else 1
        
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
