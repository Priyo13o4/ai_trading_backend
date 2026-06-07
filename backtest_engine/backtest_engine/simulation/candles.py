from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Dict
from trading_common.models import Candlestick
from trading_common.timeframes import cagg_relation_for_timeframe

RAW_BROKER_TIMEFRAMES = {"D1", "W1", "MN1"}

async def load_m1_candles(
    session: AsyncSession,
    symbol: str,
    start_time: datetime,
    end_time: datetime | None
) -> pd.DataFrame:
    """Load M1 candles for exact execution simulation."""
    # Add native warmup buffer (- 7 days) during the data load sequence
    warmup_start = start_time - timedelta(days=7)
    
    end_clause = "AND time <= :end" if end_time is not None else ""
    query = text(f"""
        SELECT time as ts_open, open, high, low, close, volume 
        FROM candlesticks 
        WHERE symbol = :symbol 
        AND timeframe = 'M1'
        AND time >= :start
        {end_clause}
        ORDER BY time ASC
    """)
    
    params = {
        "symbol": symbol,
        "start": warmup_start,
    }
    if end_time is not None:
        params["end"] = end_time
    result = await session.execute(query, params)
    
    rows = result.fetchall()
    if not rows:
        return pd.DataFrame()
        
    df = pd.DataFrame(rows, columns=['ts_open', 'open', 'high', 'low', 'close', 'volume'])
    df.set_index('ts_open', inplace=True)
    return df


async def load_raw_broker_candles(
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    start_time: datetime,
    end_time: datetime | None,
) -> pd.DataFrame:
    """Load broker-provided raw candles that are not backed by CAGGs."""
    timeframe = str(timeframe or "").upper()
    if timeframe not in RAW_BROKER_TIMEFRAMES:
        raise ValueError(f"Unsupported raw broker timeframe: {timeframe}")

    warmup_start = start_time - timedelta(days=7)

    end_clause = "AND time <= :end" if end_time is not None else ""
    query = text(f"""
        SELECT time as ts_open, open, high, low, close, volume
        FROM candlesticks
        WHERE symbol = :symbol
        AND timeframe = :timeframe
        AND time >= :start
        {end_clause}
        ORDER BY time ASC
    """)

    params = {
        "symbol": symbol,
        "timeframe": timeframe,
        "start": warmup_start,
    }
    if end_time is not None:
        params["end"] = end_time
    result = await session.execute(query, params)

    rows = result.fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=['ts_open', 'open', 'high', 'low', 'close', 'volume'])
    df.set_index('ts_open', inplace=True)
    return df

async def load_cagg_candles(
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    start_time: datetime,
    end_time: datetime | None
) -> pd.DataFrame:
    """
    Load derived timeframe candles (M5, M15, M30, H1, H4) from TimescaleDB CAGGs.
    """
    cagg_name = cagg_relation_for_timeframe(timeframe)
    # Add native warmup buffer (- 7 days) during the data load sequence
    warmup_start = start_time - timedelta(days=7)
    
    end_clause = "AND time <= :end" if end_time is not None else ""
    query = text(f"""
        SELECT time as ts_open, open, high, low, close, volume 
        FROM {cagg_name} 
        WHERE symbol = :symbol 
        AND time >= :start
        {end_clause}
        ORDER BY time ASC
    """)
    
    params = {
        "symbol": symbol,
        "start": warmup_start,
    }
    if end_time is not None:
        params["end"] = end_time
    result = await session.execute(query, params)
    
    rows = result.fetchall()
    if not rows:
        return pd.DataFrame()
        
    df = pd.DataFrame(rows, columns=['ts_open', 'open', 'high', 'low', 'close', 'volume'])
    df.set_index('ts_open', inplace=True)
    return df
