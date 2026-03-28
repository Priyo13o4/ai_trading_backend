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
import redis
import json

router = APIRouter(prefix="/api/historical", tags=["historical"])

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:your_password@localhost:5432/trading_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
CACHE_TTL = 300  # 5 minutes


def get_db_connection():
    """Get PostgreSQL connection"""
    return psycopg.connect(DATABASE_URL)


def get_redis_client():
    """Get Redis client"""
    try:
        return redis.from_url(REDIS_URL, decode_responses=True)
    except:
        return None


@router.get("/{symbol}/{timeframe}")
async def get_historical_data(
    symbol: str,
    timeframe: str,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    before: Optional[int] = Query(None, description="Fetch data before this Unix timestamp (for lazy loading)"),
    limit: int = Query(1000, ge=1, le=5000, description="Max bars to return"),
    include_indicators: bool = Query(True, description="Include technical indicators")
):
    """
    Get historical OHLCV data with optional indicators
    
    Examples:
    - /api/historical/XAUUSD/H1?limit=100
    - /api/historical/EURUSD/D1?start_date=2023-01-01&end_date=2024-01-01
    - /api/historical/GBPUSD/M5?limit=500&include_indicators=false
    - /api/historical/XAUUSD/H1?before=1640995200&limit=1000  (lazy loading)
    """
    
    # Generate cache key
    cache_key = f"historical:{symbol}:{timeframe}:{start_date}:{end_date}:{before}:{limit}:{include_indicators}"
    
    # Try Redis cache first
    redis_client = get_redis_client()
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except:
            pass
    
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
            
            query += """
                FROM candlesticks c
            """
            
            if include_indicators:
                query += """
                    LEFT JOIN technical_indicators ti 
                    ON c.symbol = ti.symbol 
                    AND c.timeframe = ti.timeframe 
                    AND c.time = ti.time
                """
            
            query += """
                WHERE c.symbol = %s AND c.timeframe = %s
            """
            
            params = [symbol, timeframe]
            
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
        
        conn.close()
        
        if not rows:
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
                    "ema_9": float(row['ema_9']) if row.get('ema_9') else None,
                    "ema_21": float(row['ema_21']) if row.get('ema_21') else None,
                    "ema_50": float(row['ema_50']) if row.get('ema_50') else None,
                    "ema_100": float(row['ema_100']) if row.get('ema_100') else None,
                    "ema_200": float(row['ema_200']) if row.get('ema_200') else None,
                    "rsi": float(row['rsi']) if row.get('rsi') else None,
                    "macd": {
                        "main": float(row['macd_main']) if row.get('macd_main') else None,
                        "signal": float(row['macd_signal']) if row.get('macd_signal') else None,
                        "histogram": float(row['macd_histogram']) if row.get('macd_histogram') else None
                    },
                    "atr": float(row['atr']) if row.get('atr') else None,
                    "bollinger_bands": {
                        "upper": float(row['bb_upper']) if row.get('bb_upper') else None,
                        "middle": float(row['bb_middle']) if row.get('bb_middle') else None,
                        "lower": float(row['bb_lower']) if row.get('bb_lower') else None
                    },
                    "adx": float(row['adx']) if row.get('adx') else None
                }
            
            data.append(item)
        
        response = {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": len(data),
            "candles": data,  # Frontend expects 'candles' key
            "data": data,     # Keep for backward compatibility
            "metadata": {
                "start": data[0]['datetime'],
                "end": data[-1]['datetime']
            }
        }
        
        # Cache response
        if redis_client:
            try:
                redis_client.setex(cache_key, CACHE_TTL, json.dumps(response))
            except:
                pass
        
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
