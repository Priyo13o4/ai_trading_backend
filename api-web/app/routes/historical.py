"""
Historical Data API Routes
===========================
Smart query endpoints with database + cache layer
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
import psycopg
from psycopg.rows import dict_row
import os
import json
import asyncio
import logging
import uuid

from ..cache import redis_client
from trading_common.timeframes import (
    normalize_timeframe,
    is_broker_timeframe,
    is_derived_cagg_timeframe,
    cagg_relation_for_timeframe,
    timeframe_minutes,
    assert_timeframe_policy,
    TimeframePolicyError,
)

router = APIRouter(prefix="/api/historical", tags=["historical"])

logger = logging.getLogger(__name__)

# Configuration - Build DATABASE_URL from components (matches scripts/calculate_recent_indicators.py pattern)
_db_host = os.getenv("POSTGRES_HOST", "postgres")
_db_port = os.getenv("POSTGRES_PORT", "5432")
_db_name = os.getenv("POSTGRES_DB") or os.getenv("TRADING_BOT_DB", "ai_trading_bot_data")
_db_user = os.getenv("POSTGRES_USER", "postgres")
_db_password = os.getenv("POSTGRES_PASSWORD", "")

CACHE_TTL = 300  # 5 minutes

DATABASE_URL = os.getenv("DATABASE_URL") or f"postgresql://{_db_user}:{_db_password}@{_db_host}:{_db_port}/{_db_name}"


def _floor_utc_bucket(dt: datetime, timeframe_minutes: int) -> datetime:
    """Floor a UTC datetime to the bucket start.

    Must match the MT5 ingest forming-bucket alignment so historical can append the
    current *forming* candle for higher TFs.
    """
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)

    if timeframe_minutes >= 43200:  # MN1
        return dt.replace(day=1, hour=0, minute=0)

    if timeframe_minutes >= 10080:  # W1
        # Forex trading week alignment: Sunday 22:00 UTC.
        # Python weekday(): Mon=0 ... Sun=6
        days_since_sunday = (dt.weekday() + 1) % 7
        sunday = dt - timedelta(days=days_since_sunday)
        bucket = sunday.replace(hour=22, minute=0)
        if dt < bucket:
            bucket -= timedelta(days=7)
        return bucket

    if timeframe_minutes >= 1440:  # D1
        return dt.replace(hour=0, minute=0)

    total_minutes = dt.hour * 60 + dt.minute
    period_minute = (total_minutes // timeframe_minutes) * timeframe_minutes
    return dt.replace(hour=period_minute // 60, minute=period_minute % 60)


def _forming_state_key(symbol: str, timeframe: str, bucket_start: datetime) -> str:
    return f"forming:bucket:{str(symbol).upper()}:{str(timeframe).upper()}:{int(bucket_start.timestamp())}"


def _use_timescale_caggs() -> bool:
    return (os.getenv("USE_TIMESCALE_CAGGS") or "").strip().lower() in {"1", "true", "yes", "y"}


def get_db_connection():
    """Get PostgreSQL connection"""
    return psycopg.connect(DATABASE_URL)


def _cagg_refresh_lock_key(*, relation: str, start: datetime, end: datetime) -> str:
    # Lock granularity is per (relation, exact range) for correctness.
    # This prevents refresh storms when multiple clients request the same missing window.
    return f"cagg_refresh_lock:{relation}:{int(start.timestamp())}:{int(end.timestamp())}"


def _release_redis_lock_best_effort(key: str, token: str) -> None:
    # Atomic compare-and-del.
    try:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        redis_client.eval(script, 1, key, token)
    except Exception:
        return


@router.get("/{symbol}/{timeframe}")
async def get_historical_data(
    symbol: str,
    timeframe: str,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    before: Optional[int] = Query(None, description="Fetch data before this Unix timestamp (for lazy loading)"),
    limit: int = Query(1000, ge=1, le=10000, description="Max bars to return"),
    include_indicators: bool = Query(True, description="Include technical indicators"),
    include_forming: bool = Query(True, description="Append current forming candle for higher timeframes (best-effort)")
):
    """
    Get historical OHLCV data with optional indicators
    
    Examples:
    - /api/historical/XAUUSD/H1?limit=100
    - /api/historical/EURUSD/D1?start_date=2023-01-01&end_date=2024-01-01
    - /api/historical/GBPUSD/M5?limit=500&include_indicators=false
    - /api/historical/XAUUSD/H1?before=1640995200&limit=1000  (lazy loading)
    """
    
    # Generate cache key (request-shaped, but normalized for systematic invalidation)
    sym = str(symbol or "").upper()
    tf = normalize_timeframe(timeframe)
    cache_key = f"historical:{sym}:{tf}:{start_date}:{end_date}:{before}:{limit}:{include_indicators}:{include_forming}"
    
    # Try Redis cache first
    try:
        cached = redis_client.get(cache_key)
        if cached:
            logger.info(f"[API] Cache HIT for historical {sym} {tf} (limit={limit})")
            return json.loads(cached)
        else:
            logger.info(f"[API] Cache MISS for historical {sym} {tf} (limit={limit}), querying database")
    except Exception as e:
        # Redis is required globally; however cache lookups must not break API responses.
        logger.warning(f"[API] Cache lookup failed for historical {sym} {tf}: {e}")
        cached = None
    
    # Query database
    try:
        conn = get_db_connection()
        
        with conn.cursor(row_factory=dict_row) as cur:
            # Build query
            query = """
                SELECT 
                    c.time as datetime,
                    c.open, c.high, c.low, c.close, c.volume
            """
            
            if include_indicators:
                query += """,
                    ti.ema_9, ti.ema_21, ti.ema_50, ti.ema_100, ti.ema_200,
                    ti.rsi, ti.macd_main, ti.macd_signal, ti.macd_histogram,
                    ti.atr, ti.bb_upper, ti.bb_middle, ti.bb_lower,
                    ti.adx, ti.dmp, ti.dmn, ti.obv_slope
                """
            
            tf = normalize_timeframe(timeframe)
            # LOCKED ARCHITECTURE:
            # - Only derived TFs (M5..H4) may use CAGGs
            # - D1/W1/MN1 must NEVER be queried from CAGGs
            use_caggs = _use_timescale_caggs() and is_derived_cagg_timeframe(tf)

            # No raw fallback for derived TFs.
            if is_derived_cagg_timeframe(tf) and not use_caggs:
                raise HTTPException(
                    status_code=503,
                    detail="Derived timeframe history requires Timescale CAGGs (USE_TIMESCALE_CAGGS=true).",
                )

            # Fail-fast invariants (prevents silent regressions).
            try:
                if use_caggs:
                    assert_timeframe_policy(tf, "cagg")
                elif is_broker_timeframe(tf):
                    assert_timeframe_policy(tf, "broker_raw")
            except TimeframePolicyError as e:
                raise HTTPException(status_code=500, detail=str(e))
            rel: Optional[str] = None
            if use_caggs:
                rel = cagg_relation_for_timeframe(tf)
                query += f"\n                FROM {rel} c\n            "
            else:
                query += """
                    FROM candlesticks c
                """
            
            if include_indicators:
                if use_caggs:
                    query += """
                        LEFT JOIN technical_indicators ti
                        ON c.symbol = ti.symbol
                        AND ti.timeframe = %s
                        AND c.time = ti.time
                    """
                else:
                    query += """
                        LEFT JOIN technical_indicators ti 
                        ON c.symbol = ti.symbol 
                        AND c.timeframe = ti.timeframe 
                        AND c.time = ti.time
                    """
            
            if use_caggs:
                query += """
                    WHERE c.symbol = %s
                """
                params = []
                if include_indicators:
                    params.append(tf)
                params.append(sym)
            else:
                query += """
                    WHERE c.symbol = %s AND c.timeframe = %s
                """
                params = [sym, tf]
            
            if start_date:
                query += " AND c.time >= %s"
                params.append(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc))
            
            if end_date:
                query += " AND c.time <= %s"
                params.append(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc))
            
            # Support 'before' parameter for lazy loading (fetch data before timestamp)
            if before:
                before_datetime = datetime.fromtimestamp(before, tz=timezone.utc)
                query += " AND c.time < %s"
                params.append(before_datetime)
            
            # Always order by time DESC (newest first) for consistent frontend handling
            query += " ORDER BY c.time DESC LIMIT %s"
            params.append(limit)
            
            cur.execute(query, params)
            rows = cur.fetchall()

            # "Scroll forever" semantics for derived TFs:
            # If CAGG query returns empty but raw M1 exists for the requested window,
            # refresh the CAGG on-demand for that time range and retry.
            raw_has_data_for_window = False
            if use_caggs and not rows and rel is not None:
                try:
                    tf_minutes = timeframe_minutes(tf)
                    refresh_end: Optional[datetime] = None
                    refresh_start: Optional[datetime] = None

                    if end_date:
                        refresh_end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
                    elif before:
                        refresh_end = datetime.fromtimestamp(before, tz=timezone.utc)

                    if start_date:
                        refresh_start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
                    elif refresh_end is not None and tf_minutes is not None:
                        refresh_start = refresh_end - timedelta(minutes=int(tf_minutes) * int(limit))

                    if refresh_start is not None and refresh_end is not None and refresh_start < refresh_end:
                        cur.execute(
                            """
                            SELECT 1
                            FROM candlesticks
                            WHERE symbol=%s AND timeframe='M1'
                              AND time >= %s AND time <= %s
                            LIMIT 1
                            """,
                            (sym, refresh_start, refresh_end),
                        )
                        raw_has_data_for_window = cur.fetchone() is not None

                        if raw_has_data_for_window:
                            lock_ttl = int(os.getenv("CAGG_REFRESH_LOCK_TTL_SECONDS", "90"))
                            lock_key = _cagg_refresh_lock_key(relation=rel, start=refresh_start, end=refresh_end)
                            token = uuid.uuid4().hex
                            acquired = False
                            started = asyncio.get_event_loop().time()

                            try:
                                acquired = bool(redis_client.set(lock_key, token, nx=True, ex=lock_ttl))
                            except Exception:
                                acquired = False

                            if acquired:
                                logger.info(
                                    "cagg_refresh_triggered",
                                    extra={
                                        "event": "cagg_refresh_triggered",
                                        "symbol": str(symbol).upper(),
                                        "timeframe": tf,
                                        "relation": rel,
                                        "range_start": refresh_start.isoformat(),
                                        "range_end": refresh_end.isoformat(),
                                    },
                                )

                                try:
                                    for attempt in range(3):
                                        cur.execute(
                                            "CALL refresh_continuous_aggregate(%s::regclass, %s, %s)",
                                            (rel, refresh_start, refresh_end),
                                        )
                                        conn.commit()

                                        cur.execute(query, params)
                                        rows = cur.fetchall()
                                        if rows:
                                            break

                                        await asyncio.sleep(0.25 * (attempt + 1))
                                finally:
                                    _release_redis_lock_best_effort(lock_key, token)

                                duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
                                if rows:
                                    logger.info(
                                        "cagg_refresh_completed",
                                        extra={
                                            "event": "cagg_refresh_completed",
                                            "symbol": str(symbol).upper(),
                                            "timeframe": tf,
                                            "relation": rel,
                                            "range_start": refresh_start.isoformat(),
                                            "range_end": refresh_end.isoformat(),
                                            "duration_ms": duration_ms,
                                        },
                                    )
                                else:
                                    logger.warning(
                                        "cagg_refresh_timeout",
                                        extra={
                                            "event": "cagg_refresh_timeout",
                                            "symbol": str(symbol).upper(),
                                            "timeframe": tf,
                                            "relation": rel,
                                            "range_start": refresh_start.isoformat(),
                                            "range_end": refresh_end.isoformat(),
                                            "duration_ms": duration_ms,
                                        },
                                    )
                            else:
                                # Another request is refreshing this same window.
                                logger.info(
                                    "cagg_refresh_in_progress",
                                    extra={
                                        "event": "cagg_refresh_in_progress",
                                        "symbol": str(symbol).upper(),
                                        "timeframe": tf,
                                        "relation": rel,
                                        "range_start": refresh_start.isoformat(),
                                        "range_end": refresh_end.isoformat(),
                                    },
                                )
                                # Wait briefly for the other refresher to finish.
                                for _ in range(6):
                                    await asyncio.sleep(0.25)
                                    cur.execute(query, params)
                                    rows = cur.fetchall()
                                    if rows:
                                        break
                except Exception:
                    # Do not fail the request if refresh fails; downstream logic will raise 404 if still empty.
                    pass

            # Best-effort append the current forming candle (for UI freshness) for derived TFs.
            # This uses Redis state maintained by mt5_ingest and does NOT write to DB.
            forming_row: Optional[dict] = None
            if include_forming and use_caggs:
                try:
                    minutes = timeframe_minutes(tf)
                    if minutes is not None:
                        # Use latest M1 timestamp as the anchor (not wall-clock), so closed markets don't jump buckets.
                        cur.execute(
                            """
                            SELECT MAX(time) AS latest
                            FROM candlesticks
                            WHERE symbol=%s AND timeframe='M1'
                            """,
                            (sym,),
                        )
                        latest = cur.fetchone()
                        latest_dt = (latest or {}).get("latest")
                        if latest_dt:
                            bucket_start = _floor_utc_bucket(latest_dt, int(minutes))
                            key = _forming_state_key(symbol, tf, bucket_start)
                            state = redis_client.hgetall(key) or {}
                            if state and state.get("open") and state.get("high") and state.get("low") and state.get("close"):
                                forming_row = {
                                    "datetime": bucket_start,
                                    "open": float(state["open"]),
                                    "high": float(state["high"]),
                                    "low": float(state["low"]),
                                    "close": float(state["close"]),
                                    "volume": float(state.get("volume") or 0),
                                    "_is_forming": True,
                                }
                except Exception:
                    forming_row = None
        
        conn.close()
        
        if not rows:
            # For derived TFs, never "silently empty" if raw M1 exists; surface a retryable error.
            if use_caggs and raw_has_data_for_window:
                raise HTTPException(
                    status_code=503,
                    detail="Derived timeframe data is being materialized (Timescale CAGG refresh). Please retry.",
                )
            # For broker timeframes, an empty DB usually means the MT5 history for this TF isn't populated yet.
            if is_broker_timeframe(tf):
                raise HTTPException(
                    status_code=503,
                    detail="Broker timeframe history is not available yet. Ensure MT5 bridge is connected and retry.",
                )
            raise HTTPException(status_code=404, detail="No data found")
        
        # Convert to response format - data is always DESC from DB (newest first)
        data = []
        for row in rows:
            item = {
                "time": row['datetime'].isoformat(),  # Frontend expects 'time' key
                "datetime": row['datetime'].isoformat(),  # Keep for backward compatibility
                "open": float(row['open']),
                "high": float(row['high']),
                "low": float(row['low']),
                "close": float(row['close']),
                "volume": float(row['volume'])
            }
            
            if include_indicators:
                item['indicators'] = {
                    "ema_9": float(row['ema_9']) if row.get('ema_9') is not None else None,
                    "ema_21": float(row['ema_21']) if row.get('ema_21') is not None else None,
                    "ema_50": float(row['ema_50']) if row.get('ema_50') is not None else None,
                    "ema_100": float(row['ema_100']) if row.get('ema_100') is not None else None,
                    "ema_200": float(row['ema_200']) if row.get('ema_200') is not None else None,
                    "rsi": float(row['rsi']) if row.get('rsi') is not None else None,
                    "macd": {
                        "main": float(row['macd_main']) if row.get('macd_main') is not None else None,
                        "signal": float(row['macd_signal']) if row.get('macd_signal') is not None else None,
                        "histogram": float(row['macd_histogram']) if row.get('macd_histogram') is not None else None
                    },
                    "atr": float(row['atr']) if row.get('atr') is not None else None,
                    "bollinger_bands": {
                        "upper": float(row['bb_upper']) if row.get('bb_upper') is not None else None,
                        "middle": float(row['bb_middle']) if row.get('bb_middle') is not None else None,
                        "lower": float(row['bb_lower']) if row.get('bb_lower') is not None else None
                    },
                    "adx": float(row['adx']) if row.get('adx') is not None else None
                }
            
            data.append(item)

        if forming_row is not None:
            forming_item = {
                "time": forming_row["datetime"].isoformat(),
                "datetime": forming_row["datetime"].isoformat(),
                "open": float(forming_row["open"]),
                "high": float(forming_row["high"]),
                "low": float(forming_row["low"]),
                "close": float(forming_row["close"]),
                "volume": float(forming_row.get("volume") or 0),
                "is_forming": True,
            }

            # If DB already returned the current bucket (possible with different CAGG policies),
            # overwrite it so the frontend always sees the freshest values + is_forming flag.
            if data and data[0].get("datetime") == forming_item["datetime"]:
                data[0].update(forming_item)
            else:
                data.insert(0, forming_item)
        
        response = {
            "symbol": sym,
            "timeframe": tf,
            "bars": len(data),
            "candles": data,  # Frontend expects 'candles' key
            "data": data,     # Keep for backward compatibility
            "metadata": {
                "start": data[0]['datetime'],
                "end": data[-1]['datetime']
            }
        }
        
        # Cache response
        try:
            redis_client.setex(cache_key, CACHE_TTL, json.dumps(response))
            logger.info(f"[API] Cached {len(data)} candles for {sym} {tf} with TTL={CACHE_TTL}s")
        except Exception as e:
            logger.warning(f"[API] Cache set failed for {sym} {tf}: {e}")
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/coverage/{symbol}")
async def get_data_coverage(symbol: str):
    """
    Get data coverage summary for a symbol across all timeframes
    
    Returns: completeness %, date ranges, total bars per timeframe
    """
    try:
        conn = get_db_connection()
        
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT * FROM data_coverage_summary
                WHERE symbol = %s
                ORDER BY 
                    CASE timeframe
                        WHEN 'M5' THEN 1
                        WHEN 'M15' THEN 2
                        WHEN 'H1' THEN 3
                        WHEN 'H4' THEN 4
                        WHEN 'D1' THEN 5
                    END
            """, (symbol,))
            
            rows = cur.fetchall()
        
        conn.close()
        
        if not rows:
            raise HTTPException(status_code=404, detail=f"No data found for {symbol}")
        
        return {
            "symbol": symbol,
            "timeframes": [dict(row) for row in rows]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/gaps/{symbol}/{timeframe}")
async def get_data_gaps(
    symbol: str,
    timeframe: str,
    gap_threshold_hours: int = Query(24, ge=1, description="Gap threshold in hours")
):
    """
    Find gaps in historical data
    
    Returns: List of gaps with start/end times and duration
    """
    try:
        conn = get_db_connection()
        
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM find_data_gaps(%s, %s, %s)",
                (symbol, timeframe, gap_threshold_hours)
            )
            
            gaps = cur.fetchall()
        
        conn.close()
        
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "gap_threshold_hours": gap_threshold_hours,
            "gaps_found": len(gaps),
            "gaps": [dict(gap) for gap in gaps]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@router.get("/freshness")
async def get_data_freshness():
    """
    Get freshness status for all symbols/timeframes
    
    Returns: Latest timestamp and age in minutes
    """
    try:
        conn = get_db_connection()
        
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM data_freshness ORDER BY symbol, timeframe")
            rows = cur.fetchall()
        
        conn.close()
        
        return {
            "data": [dict(row) for row in rows]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
