import logging
import asyncio
from datetime import datetime, timezone
import pandas as pd
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_common.models import RegimeData
from trading_common.indicators.technical import calculate_all_indicators
from .connection import _use_timescale_caggs
from .helpers import _ohlcv_relation_for_timeframe, _compute_swing_analysis

logger = logging.getLogger(__name__)

# Process-wide lock to prevent NumPy/TA-Lib segfaults when multiple threads
# attempt to calculate indicators concurrently during a cache miss/stampede.
_numpy_calc_lock = asyncio.Lock()


async def get_latest_regime_from_db(session: AsyncSession):
    """
    Get latest regime analysis for all trading pairs
    Returns current market regime classifications
    """
    logger.info("[DB] Fetching latest regime data for all pairs")
    try:
        stmt = (
            select(RegimeData)
            .distinct(RegimeData.trading_pair)
            .order_by(RegimeData.trading_pair, desc(RegimeData.analysis_timestamp))
        )
        result = await session.execute(stmt)
        results = [row.to_dict() for row in result.scalars().all()]
        logger.info(f"[DB] Found regime data for {len(results)} pairs")
        return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_latest_regime_from_db: {str(e)}", exc_info=True)
        raise


async def get_regime_for_pair(session: AsyncSession, pair: str):
    """Get latest regime for a specific trading pair"""
    logger.info(f"[DB] Fetching regime for pair: {pair}")
    try:
        stmt = (
            select(RegimeData)
            .where(RegimeData.trading_pair == pair)
            .order_by(desc(RegimeData.analysis_timestamp))
            .limit(1)
        )
        result = await session.execute(stmt)
        regime = result.scalar_one_or_none()
        if regime:
            return regime.to_dict()
        logger.warning(f"[DB] No regime found for {pair}")
        return None
    except Exception as e:
        logger.error(f"[DB ERROR] get_regime_for_pair: {str(e)}", exc_info=True)
        raise


async def get_regime_market_data_from_db(db: AsyncSession):
    """
    Get comprehensive market data for all symbols/timeframes for regime analysis
    Returns MT5-compatible format with indicators, structure, and recent bars
    """
    logger.info("[DB] Fetching comprehensive market data for regime analysis")
    
    try:
        market_data = {}
        successful_symbols = 0
        failed_symbols = []

        # Discover symbols/timeframes from DB so adding new pairs doesn't require code changes
        symbols_res = await db.execute(text("SELECT DISTINCT symbol FROM candlesticks WHERE time >= NOW() - INTERVAL '7 days' ORDER BY symbol"))
        symbols = [row._mapping["symbol"] for row in symbols_res]

        timeframes_res = await db.execute(text("SELECT DISTINCT timeframe FROM technical_indicators WHERE time >= NOW() - INTERVAL '7 days' ORDER BY timeframe"))
        timeframes = [row._mapping["timeframe"] for row in timeframes_res]

        if not timeframes:
            # Fallback if indicators table is empty.
            if _use_timescale_caggs():
                timeframes = ["M5", "M15", "H1", "H4", "D1", "W1"]
            else:
                tf_res = await db.execute(
                    text("SELECT DISTINCT timeframe FROM candlesticks WHERE timeframe <> 'M1' AND time >= NOW() - INTERVAL '7 days' ORDER BY timeframe")
                )
                timeframes = [row._mapping["timeframe"] for row in tf_res]

        # Filter to include M5, M15, H1, H4, D1, W1 (exclude MN1 for regime analysis)
        timeframes = [tf for tf in timeframes if tf in ["M5", "M15", "H1", "H4", "D1", "W1"]]

        if not symbols or not timeframes:
            logger.warning("[DB] No symbols/timeframes available for regime market data")
            return None

        for symbol in symbols:
            symbol_data = {}
            symbol_success = False
            
            for timeframe in timeframes:
                try:
                    # Get latest indicators
                    ind_res = await db.execute(
                        text("""
                            SELECT 
                                time, ema_9, ema_21, ema_50, ema_100, ema_200,
                                rsi, macd_main, macd_signal, macd_histogram,
                                atr, atr_percentile, bb_upper, bb_middle, bb_lower,
                                bb_squeeze_ratio, bb_width_percentile,
                                roc_percent, ema_momentum_slope,
                                adx, dmp, dmn, obv_slope
                            FROM technical_indicators
                            WHERE symbol = :symbol AND timeframe = :timeframe
                            ORDER BY time DESC
                            LIMIT 1
                        """),
                        {"symbol": symbol, "timeframe": timeframe}
                    )
                    ind_row = ind_res.fetchone()
                    indicators = dict(ind_row._mapping) if ind_row else {}
                    
                    if not indicators:
                        logger.warning(f"[DB] No indicators for {symbol}/{timeframe}")
                        indicators = {}
                    
                    # Get recent bars for market structure analysis (USE TIMESCALE CAGGs)
                    rel = _ohlcv_relation_for_timeframe(timeframe)
                    if rel == "candlesticks":
                        # M1 or non-CAGG mode
                        candles_res = await db.execute(
                            text("""
                                SELECT time, open, high, low, close, volume
                                FROM candlesticks
                                WHERE symbol = :symbol AND timeframe = :timeframe
                                ORDER BY time DESC
                                LIMIT 300
                            """),
                            {"symbol": symbol, "timeframe": timeframe}
                        )
                    else:
                        # Use TimescaleDB continuous aggregate for higher timeframes
                        candles_res = await db.execute(
                            text(f"""
                                SELECT time, open, high, low, close, volume
                                FROM {rel}
                                WHERE symbol = :symbol
                                ORDER BY time DESC
                                LIMIT 300
                            """),
                            {"symbol": symbol}
                        )
                    candles = [dict(r._mapping) for r in candles_res.fetchall()]
                    
                    if not candles:
                        logger.warning(f"[DB] No candles for {symbol}/{timeframe}")
                        continue
                    
                    # Convert to list for easier processing
                    candle_list = list(candles)
                    current_candle = candle_list[0]

                    # CHECK FOR STALE INDICATORS: If latest candle is newer than stored indicators, recalculate on-the-fly
                    is_on_the_fly = False
                    indicators_time = indicators.get('time')
                    
                    if not indicators_time or current_candle['time'] > indicators_time:
                        try:
                            def _run_calc(clist):
                                # Prepare DataFrame for calculation (need at least 250 bars for lookbacks)
                                df = pd.DataFrame(clist).sort_values('time').reset_index(drop=True)
                                # Mapping columns to what calculate_all_indicators expects
                                df = df.rename(columns={'time': 'datetime'})
                                for col in ["open", "high", "low", "close", "volume"]:
                                    df[col] = pd.to_numeric(df[col])
                                
                                # Calculate all indicators
                                return calculate_all_indicators(
                                    df=df,
                                    ema_periods=[9, 21, 50, 100, 200],
                                    rsi_period=14,
                                    macd_fast=12,
                                    macd_slow=26,
                                    macd_signal=9,
                                    atr_period=14,
                                    bb_period=20,
                                    bb_deviation=2.0,
                                    roc_period=10,
                                    adx_period=14,
                                    obv_slope_period=14,
                                    momentum_ema=21,
                                    volatility_lookback=100
                                )
                            
                            # Run synchronously on main thread to avoid NumPy/Gunicorn threading segfaults
                            async with _numpy_calc_lock:
                                calc_res = _run_calc(candle_list)
                            
                            if calc_res:
                                indicators['ema_9'] = calc_res.get('emas', {}).get('EMA_9')
                                indicators['ema_21'] = calc_res.get('emas', {}).get('EMA_21')
                                indicators['ema_50'] = calc_res.get('emas', {}).get('EMA_50')
                                indicators['ema_100'] = calc_res.get('emas', {}).get('EMA_100')
                                indicators['ema_200'] = calc_res.get('emas', {}).get('EMA_200')
                                indicators['rsi'] = calc_res.get('rsi')
                                indicators['macd_main'] = calc_res.get('macd_main')
                                indicators['macd_signal'] = calc_res.get('macd_signal')
                                indicators['macd_histogram'] = calc_res.get('macd_histogram')
                                indicators['atr'] = calc_res.get('atr')
                                indicators['atr_percentile'] = calc_res.get('atr_percentile')
                                indicators['bb_upper'] = calc_res.get('bb_upper')
                                indicators['bb_middle'] = calc_res.get('bb_middle')
                                indicators['bb_lower'] = calc_res.get('bb_lower')
                                indicators['bb_squeeze_ratio'] = calc_res.get('bb_squeeze_ratio')
                                indicators['bb_width_percentile'] = calc_res.get('bb_width_percentile')
                                indicators['roc_percent'] = calc_res.get('roc_percent')
                                indicators['ema_momentum_slope'] = calc_res.get('ema_momentum_slope')
                                indicators['adx'] = calc_res.get('adx')
                                indicators['dmp'] = calc_res.get('dmp')
                                indicators['dmn'] = calc_res.get('dmn')
                                indicators['obv_slope'] = calc_res.get('obv_slope')
                                is_on_the_fly = True
                        except Exception as calc_err:
                            logger.warning(f"[DB] On-the-fly calculation failed for {symbol}/{timeframe}: {calc_err}")

                    if not indicators and not is_on_the_fly:
                        logger.warning(f"[DB] No indicators available for {symbol}/{timeframe} after attempt")
                        continue
                    
                    # Calculate market structure from recent 50 bars
                    recent_50 = candle_list[:50]
                    recent_high = max(c['high'] for c in recent_50)
                    recent_low = min(c['low'] for c in recent_50)
                    range_percent = round(((recent_high - recent_low) / recent_low) * 100, 2) if recent_low > 0 else 0.0
                    swing_analysis = _compute_swing_analysis(candle_list, lookback=50, flank=2)
                    
                    # Calculate pivot points from previous bar
                    pivot_data = {}
                    if len(candle_list) > 1:
                        prev = candle_list[1]
                        # Classic pivots
                        P = (prev['high'] + prev['low'] + prev['close']) / 3
                        R1, S1 = (2 * P) - prev['low'], (2 * P) - prev['high']
                        R2, S2 = P + (prev['high'] - prev['low']), P - (prev['high'] - prev['low'])
                        R3, S3 = P + 2 * (prev['high'] - prev['low']), P - 2 * (prev['high'] - prev['low'])
                        pivot_data['classic'] = {
                            "R3": round(R3, 4), "R2": round(R2, 4), "R1": round(R1, 4),
                            "P": round(P, 4),
                            "S1": round(S1, 4), "S2": round(S2, 4), "S3": round(S3, 4)
                        }
                        # Woodie pivots
                        P_w = (prev['high'] + prev['low'] + 2 * prev['close']) / 4
                        R1_w, S1_w = (2 * P_w) - prev['low'], (2 * P_w) - prev['high']
                        R2_w, S2_w = P_w + (prev['high'] - prev['low']), P_w - (prev['high'] - prev['low'])
                        pivot_data['woodie'] = {
                            "R2": round(R2_w, 4), "R1": round(R1_w, 4), "P": round(P_w, 4),
                            "S1": round(S1_w, 4), "S2": round(S2_w, 4)
                        }
                        # Camarilla pivots
                        Range = prev['high'] - prev['low']
                        pivot_data['camarilla'] = {
                            "H4": round(prev['close'] + Range * 1.1 / 2, 4),
                            "H3": round(prev['close'] + Range * 1.1 / 4, 4),
                            "H2": round(prev['close'] + Range * 1.1 / 6, 4),
                            "H1": round(prev['close'] + Range * 1.1 / 12, 4),
                            "L1": round(prev['close'] - Range * 1.1 / 12, 4),
                            "L2": round(prev['close'] - Range * 1.1 / 6, 4),
                            "L3": round(prev['close'] - Range * 1.1 / 4, 4),
                            "L4": round(prev['close'] - Range * 1.1 / 2, 4)
                        }
                    
                    # Format recent bars detail (last 10)
                    recent_bars = []
                    for candle in candle_list[:10]:
                        total_range = candle['high'] - candle['low']
                        body = abs(candle['close'] - candle['open'])
                        recent_bars.append({
                            "time": candle['time'].strftime('%Y-%m-%d %H:%M:%S'),
                            "open": round(float(candle['open']), 5),
                            "high": round(float(candle['high']), 5),
                            "low": round(float(candle['low']), 5),
                            "close": round(float(candle['close']), 5),
                            "volume": int(candle['volume']),
                            "body_size_percent": round((body / total_range) * 100, 2) if total_range > 0 else 0,
                            "candle_type": "Bullish" if candle['close'] > candle['open'] else "Bearish" if candle['close'] < candle['open'] else "Neutral"
                        })
                    
                    # Construct timeframe data
                    symbol_data[timeframe] = {
                        "current_price": round(float(current_candle['close']), 5),
                        "current_volume": int(current_candle['volume']),
                        "data_quality": {
                            "total_bars": len(candle_list),
                            "data_gaps": 0,
                            "last_update": current_candle['time'].strftime('%Y-%m-%d %H:%M:%S'),
                            "data_freshness_minutes": (datetime.now(timezone.utc) - current_candle['time']).total_seconds() / 60,
                            "is_market_open": True
                        },
                        "technical_indicators": {
                            "emas": {
                                "EMA_9": round(float(indicators['ema_9']), 5) if indicators['ema_9'] is not None else None,
                                "EMA_21": round(float(indicators['ema_21']), 5) if indicators['ema_21'] is not None else None,
                                "EMA_50": round(float(indicators['ema_50']), 5) if indicators['ema_50'] is not None else None,
                                "EMA_100": round(float(indicators['ema_100']), 5) if indicators['ema_100'] is not None else None,
                                "EMA_200": round(float(indicators['ema_200']), 5) if indicators['ema_200'] is not None else None
                            },
                            "rsi": round(float(indicators['rsi']), 2) if indicators['rsi'] is not None else None,
                            "macd_main": round(float(indicators['macd_main']), 5) if indicators['macd_main'] is not None else None,
                            "macd_signal": round(float(indicators['macd_signal']), 5) if indicators['macd_signal'] is not None else None,
                            "macd_histogram": round(float(indicators['macd_histogram']), 5) if indicators['macd_histogram'] is not None else None,
                            "atr": round(float(indicators['atr']), 5) if indicators['atr'] is not None else None,
                            "atr_percentile": round(float(indicators['atr_percentile']), 1) if indicators['atr_percentile'] is not None else None,
                            "bb_upper": round(float(indicators['bb_upper']), 5) if indicators['bb_upper'] is not None else None,
                            "bb_middle": round(float(indicators['bb_middle']), 5) if indicators['bb_middle'] is not None else None,
                            "bb_lower": round(float(indicators['bb_lower']), 5) if indicators['bb_lower'] is not None else None,
                            "bb_squeeze_ratio": round(float(indicators['bb_squeeze_ratio']), 5) if indicators['bb_squeeze_ratio'] is not None else None,
                            "bb_width_percentile": round(float(indicators['bb_width_percentile']), 1) if indicators['bb_width_percentile'] is not None else None,
                            "roc_percent": round(float(indicators['roc_percent']), 5) if indicators['roc_percent'] is not None else None,
                            "ema_momentum_slope": round(float(indicators['ema_momentum_slope']), 5) if indicators['ema_momentum_slope'] is not None else None,
                            "adx": round(float(indicators['adx']), 2) if indicators['adx'] is not None else None,
                            "dmp": round(float(indicators['dmp']), 2) if indicators['dmp'] is not None else None,
                            "dmn": round(float(indicators['dmn']), 2) if indicators['dmn'] is not None else None,
                            "obv_slope": round(float(indicators['obv_slope']), 5) if indicators['obv_slope'] is not None else None
                        },
                        "market_structure": {
                            "recent_high": round(recent_high, 5),
                            "recent_low": round(recent_low, 5),
                            "range_percent": range_percent,
                            "swing_analysis": swing_analysis,
                            "price_level_analysis": {
                                "pivot_points": pivot_data
                            }
                        },
                        "recent_bars_detail": recent_bars
                    }
                    
                    symbol_success = True
                    
                except Exception as tf_error:
                    logger.error(f"[DB ERROR] Failed to fetch {symbol}/{timeframe}: {str(tf_error)}")
                    continue
            
            if symbol_success:
                market_data[symbol] = symbol_data
                successful_symbols += 1
            else:
                failed_symbols.append(symbol)
    
        if not market_data:
            logger.error("[DB] No market data could be fetched")
            return None
        
        # Determine current session
        now = datetime.now(timezone.utc)
        hour = now.hour
        if (hour >= 23) or (hour < 8):
            session = "Asian Session"
        elif (hour >= 8) and (hour < 13):
            session = "London Session"
        elif (hour >= 13) and (hour < 17):
            session = "London / New York Overlap"
        elif (hour >= 17) and (hour < 22):
            session = "New York Session"
        else:
            session = "Session Gap"
        
        result = {
            "analysis_timestamp": now.isoformat(),
            "collection_info": {
                "analysis_bars": {tf: 250 for tf in timeframes},
                "recent_bars_detail": 10,
                "current_session": session,
                "successful_symbols": successful_symbols,
                "total_symbols": len(symbols),
                "failed_symbols": failed_symbols,
                "data_source": "hybrid_on_the_fly"
            },
            "market_data": market_data
        }
        
        logger.info(f"[DB] Successfully fetched market data for {successful_symbols}/{len(symbols)} symbols")
        return result
        
    except Exception as e:
        logger.error(f"[DB ERROR] get_regime_market_data_from_db: {str(e)}", exc_info=True)
        return None
