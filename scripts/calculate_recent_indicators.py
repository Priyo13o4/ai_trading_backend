#!/usr/bin/env python3
"""
Post-Backfill Indicator Calculation Script
===========================================
Purpose: Calculate indicators for recent 1000 bars after OHLCV backfill
Strategy: Fetch from database, calculate, store back

Usage:
    python calculate_recent_indicators.py
    python calculate_recent_indicators.py --symbol XAUUSD --timeframe H1
"""

import os
import sys
import argparse
from datetime import datetime
from typing import Dict, List
import pandas as pd
from tqdm import tqdm
import psycopg
from psycopg.rows import dict_row
import pandas_ta as ta

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
FALLBACK_TIMEFRAMES = ["D1", "H4", "H1", "M15", "M5"]

# How many recent bars to keep indicators for
RECENT_BARS_LIMIT = 1000

# Indicator parameters (from market_data.py)
EMA_PERIODS = [9, 21, 50, 100, 200]
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD = 14
BB_PERIOD, BB_DEVIATION = 20, 2.0
ROC_PERIOD = 10
ADX_PERIOD = 14
OBV_SLOPE_PERIOD = 14
MOMENTUM_EMA = 9
VOLATILITY_LOOKBACK = 100


def calculate_all_indicators(df, **kwargs):
    """Calculate all technical indicators using pandas_ta"""
    if df.empty or len(df) < 200:
        return {}
    
    # EMAs
    for period in kwargs.get('ema_periods', [9, 21, 50, 100, 200]):
        df[f'EMA_{period}'] = ta.ema(df['close'], length=period)
    
    # EMA momentum slope (9-period EMA slope)
    if 'EMA_9' in df.columns:
        df['ema_momentum_slope'] = df['EMA_9'].diff(periods=kwargs.get('momentum_ema', 9))
    
    # RSI
    rsi_result = ta.rsi(df['close'], length=kwargs.get('rsi_period', 14))
    df['rsi'] = rsi_result
    
    # MACD
    macd = ta.macd(
        df['close'],
        fast=kwargs.get('macd_fast', 12),
        slow=kwargs.get('macd_slow', 26),
        signal=kwargs.get('macd_signal', 9)
    )
    if macd is not None and not macd.empty:
        df['macd_main'] = macd.iloc[:, 0]
        df['macd_histogram'] = macd.iloc[:, 1]
        df['macd_signal'] = macd.iloc[:, 2]
    
    # ATR
    atr = ta.atr(df['high'], df['low'], df['close'], length=kwargs.get('atr_period', 14))
    df['atr'] = atr
    
    # ATR percentile
    if 'atr' in df.columns:
        lookback = kwargs.get('volatility_lookback', 100)
        df['atr_percentile'] = df['atr'].rolling(window=lookback).apply(
            lambda x: (x.iloc[-1] >= x).sum() / len(x) * 100 if len(x) > 0 else 50
        )
    
    # Bollinger Bands
    bb = ta.bbands(
        df['close'],
        length=kwargs.get('bb_period', 20),
        std=kwargs.get('bb_deviation', 2.0)
    )
    if bb is not None and not bb.empty:
        df['bb_lower'] = bb.iloc[:, 0]
        df['bb_middle'] = bb.iloc[:, 1]
        df['bb_upper'] = bb.iloc[:, 2]
        
        # BB squeeze ratio
        df['bb_width'] = df['bb_upper'] - df['bb_lower']
        df['bb_squeeze_ratio'] = df['bb_width'] / df['bb_middle']
        
        # BB width percentile
        lookback = kwargs.get('volatility_lookback', 100)
        df['bb_width_percentile'] = df['bb_width'].rolling(window=lookback).apply(
            lambda x: (x.iloc[-1] >= x).sum() / len(x) * 100 if len(x) > 0 else 50
        )
    
    # ROC
    df['roc_percent'] = ta.roc(df['close'], length=kwargs.get('roc_period', 10))
    
    # ADX
    adx = ta.adx(
        df['high'],
        df['low'],
        df['close'],
        length=kwargs.get('adx_period', 14)
    )
    if adx is not None and not adx.empty:
        df['adx'] = adx.iloc[:, 0]
        df['dmp'] = adx.iloc[:, 1]
        df['dmn'] = adx.iloc[:, 2]
    
    # OBV slope
    obv = ta.obv(df['close'], df['volume'])
    if obv is not None:
        df['obv'] = obv
        df['obv_slope'] = df['obv'].diff(periods=kwargs.get('obv_slope_period', 14))
    
    return df.to_dict('list')


def fetch_recent_candlesticks(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    limit: int = 1000
) -> pd.DataFrame:
    """Fetch recent N bars from database"""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT 
                time as datetime,
                open, high, low, close, volume
            FROM candlesticks
            WHERE symbol = %s AND timeframe = %s
            ORDER BY time DESC
            LIMIT %s
        """, (symbol, timeframe, limit))
        
        rows = cur.fetchall()
    
    if not rows:
        return pd.DataFrame()
    
    df = pd.DataFrame(rows)
    df = df.sort_values('datetime').reset_index(drop=True)
    
    return df


def _discover_symbols(conn: psycopg.Connection) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM candlesticks ORDER BY symbol")
        return [row[0] for row in cur.fetchall()]


def _discover_timeframes(conn: psycopg.Connection, include_m1: bool = False) -> List[str]:
    with conn.cursor() as cur:
        if include_m1:
            cur.execute("SELECT DISTINCT timeframe FROM candlesticks ORDER BY timeframe")
        else:
            cur.execute(
                "SELECT DISTINCT timeframe FROM candlesticks WHERE timeframe <> 'M1' ORDER BY timeframe"
            )
        return [row[0] for row in cur.fetchall()]


def _get_latest_time(conn: psycopg.Connection, table: str, symbol: str, timeframe: str):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MAX(time) FROM {table} WHERE symbol = %s AND timeframe = %s",
            (symbol, timeframe),
        )
        return cur.fetchone()[0]


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
                # Momentum
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
                # Directional
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


def calculate_and_store_indicators(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str
) -> Dict:
    """Calculate indicators for one symbol/timeframe and store"""
    print(f"\n📊 {symbol} @ {timeframe}")
    
    latest_candle_time = _get_latest_time(conn, "candlesticks", symbol, timeframe)
    latest_indicator_time = _get_latest_time(conn, "technical_indicators", symbol, timeframe)

    if latest_candle_time and latest_indicator_time and latest_indicator_time >= latest_candle_time:
        print(f"   ⏭️  Up-to-date (candles={latest_candle_time}, indicators={latest_indicator_time})")
        return {"symbol": symbol, "timeframe": timeframe, "bars_processed": 0, "bars_stored": 0, "success": True}

    # Fetch data
    df = fetch_recent_candlesticks(conn, symbol, timeframe, limit=RECENT_BARS_LIMIT)
    
    if df.empty:
        print(f"   ⚠️  No data found in database")
        return {"symbol": symbol, "timeframe": timeframe, "bars_processed": 0, "success": False}
    
    print(f"   Fetched {len(df)} bars from database")
    
    try:
        # Calculate indicators - this modifies df in place and returns dict
        calculate_all_indicators(
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
        
        # df now has all indicator columns, use it directly
        indicators_df = df.copy()
        
        # Store in database
        inserted = store_indicators_batch(conn, symbol, timeframe, indicators_df)
        
        print(f"   ✅ Stored {inserted} indicator rows")
        
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars_processed": len(df),
            "bars_stored": inserted,
            "success": True
        }
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return {"symbol": symbol, "timeframe": timeframe, "bars_processed": 0, "success": False}


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
    args = parser.parse_args()
    
    print("="*70)
    print("📈 Post-Backfill Indicator Calculation")
    print("="*70)
    print(f"Target: Recent {RECENT_BARS_LIMIT} bars per symbol/timeframe")
    print("="*70)
    
    try:
        conn = psycopg.connect(DATABASE_URL)
        print("✅ Connected to PostgreSQL\n")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return
    
    def _parse_csv(value: str) -> List[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    try:
        if args.symbol:
            symbols_to_process = [args.symbol]
        elif args.symbols:
            symbols_to_process = _parse_csv(args.symbols)
        else:
            symbols_to_process = _discover_symbols(conn) or FALLBACK_SYMBOLS

        if args.timeframe:
            timeframes_to_process = [args.timeframe]
        elif args.timeframes:
            timeframes_to_process = _parse_csv(args.timeframes)
        else:
            timeframes_to_process = _discover_timeframes(conn, include_m1=args.include_m1) or FALLBACK_TIMEFRAMES
        
        results = []
        
        for symbol in symbols_to_process:
            for timeframe in timeframes_to_process:
                result = calculate_and_store_indicators(conn, symbol, timeframe)
                results.append(result)
        
        # Summary
        successful = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]
        total_bars = sum(r['bars_processed'] for r in successful)
        
        print("\n" + "="*70)
        print("📊 SUMMARY")
        print("="*70)
        print(f"Successful: {len(successful)}/{len(results)}")
        print(f"Failed: {len(failed)}")
        print(f"Total bars processed: {total_bars:,}")
        print("="*70)
        
        if failed:
            print("\n⚠️  Failed symbol/timeframes:")
            for r in failed:
                print(f"   - {r['symbol']} @ {r['timeframe']}")
        
    finally:
        conn.close()
        print("\n👋 Disconnected from database")


if __name__ == "__main__":
    main()
