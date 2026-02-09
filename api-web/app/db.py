import os
import psycopg
import logging
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_db_name = os.getenv('TRADING_BOT_DB') or os.getenv('POSTGRES_DB')
POSTGRES_DSN = f"host={os.getenv('POSTGRES_HOST')} port={os.getenv('POSTGRES_PORT')} dbname={_db_name} user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')}"


def _use_timescale_caggs() -> bool:
    return (os.getenv("USE_TIMESCALE_CAGGS") or "").strip().lower() in {"1", "true", "yes", "y"}


def _ohlcv_relation_for_timeframe(timeframe: str) -> str:
    # LOCKED ARCHITECTURE:
    # - Broker provides: M1, D1, W1, MN1  (always queried from candlesticks)
    # - CAGGs provide: M5, M15, M30, H1, H4 (optional, gated by USE_TIMESCALE_CAGGS)
    from trading_common.timeframes import (
        normalize_timeframe,
        is_derived_cagg_timeframe,
        cagg_relation_for_timeframe,
        assert_timeframe_policy,
        TimeframePolicyError,
    )

    tf = normalize_timeframe(timeframe)

    # Broker-provided TFs always come from the base candlesticks table.
    if tf in {"M1", "D1", "W1", "MN1"}:
        assert_timeframe_policy(tf, "broker_raw")
        return "candlesticks"

    # Derived TFs must come from Timescale CAGGs (no raw fallback).
    if is_derived_cagg_timeframe(tf):
        assert_timeframe_policy(tf, "cagg")
        if not _use_timescale_caggs():
            raise TimeframePolicyError(
                f"Derived timeframe {tf} requires Timescale CAGGs (USE_TIMESCALE_CAGGS=true)"
            )
        return cagg_relation_for_timeframe(tf)

    raise ValueError(f"Unsupported timeframe: {tf}")

# ============================================================================
# STRATEGY & SIGNAL QUERIES (NEW SCHEMA v2.0)
# ============================================================================

def get_latest_signal_from_db(pair: str):
    """
    Get latest active strategy for a trading pair
    Returns AI-generated trading recommendation from n8n Strategy Selector
    """
    logger.info(f"[DB] Fetching latest strategy for pair: {pair}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT 
                    strategy_id,
                    strategy_name,
                                        symbol as pair,
                    direction,
                    confidence,
                    take_profit,
                    stop_loss,
                    risk_reward_ratio,
                    expiry_minutes,
                    expiry_time,
                    timestamp as created_at,
                    detailed_analysis,
                    entry_signal,
                    status
                  FROM strategies
                                    WHERE symbol = %s
                  AND status = 'active'
                  AND expiry_time > NOW()
                  ORDER BY confidence DESC, timestamp DESC
                  LIMIT 1
                """, (pair.upper(),))
                result = cur.fetchone()
                if result:
                    logger.info(f"[DB] Found strategy for {pair}: {result.get('strategy_name')} ({result.get('confidence')})")
                else:
                    logger.warning(f"[DB] No active strategy found for {pair}")
                return result
    except Exception as e:
        logger.error(f"[DB ERROR] get_latest_signal_from_db({pair}): {str(e)}", exc_info=True)
        raise

def get_old_signal_from_db(pair: str):
    """
    Get the 2nd most recent strategy for preview purposes
    Used on main page to show sample signals without giving real-time data
    """
    logger.info(f"[DB] Fetching preview strategy for pair: {pair}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT 
                    strategy_id,
                    strategy_name,
                                        symbol as pair,
                    direction,
                    confidence,
                    take_profit,
                    stop_loss,
                    risk_reward_ratio,
                    expiry_minutes,
                    timestamp as created_at,
                    detailed_analysis,
                    entry_signal,
                    status
                  FROM strategies
                                    WHERE symbol = %s
                  ORDER BY timestamp DESC
                  LIMIT 1 OFFSET 1
                """, (pair.upper(),))
                result = cur.fetchone()
                if result:
                    logger.info(f"[DB] Found preview strategy for {pair}")
                else:
                    logger.warning(f"[DB] No preview strategy found for {pair}")
                return result
    except Exception as e:
        logger.error(f"[DB ERROR] get_old_signal_from_db({pair}): {str(e)}", exc_info=True)
        raise

# ============================================================================
# REGIME ANALYSIS QUERIES
# ============================================================================

def get_latest_regime_from_db():
    """
    Get latest regime analysis for all trading pairs
    Returns current market regime classifications
    """
    logger.info("[DB] Fetching latest regime data for all pairs")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT DISTINCT ON (trading_pair)
                    regime_id,
                    trading_pair as symbol,
                    regime_type,
                    regime_summary as text,
                    confidence_score as confidence,
                    analysis_timestamp as timestamp,
                    batch_id,
                    created_at
                  FROM regime_data
                  ORDER BY trading_pair, analysis_timestamp DESC
                """)
                results = cur.fetchall()
                logger.info(f"[DB] Found regime data for {len(results)} pairs")
                return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_latest_regime_from_db: {str(e)}", exc_info=True)
        raise

def get_regime_for_pair(pair: str):
    """Get latest regime for a specific trading pair"""
    logger.info(f"[DB] Fetching regime for pair: {pair}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT 
                    regime_id,
                    trading_pair as symbol,
                    regime_type,
                    regime_summary as text,
                    confidence_score as confidence,
                    analysis_timestamp as timestamp,
                    market_data,
                    collection_info,
                    batch_id,
                    created_at
                  FROM regime_data
                  WHERE trading_pair = %s
                  ORDER BY analysis_timestamp DESC
                  LIMIT 1
                """, (pair.upper(),))
                result = cur.fetchone()
                if result:
                    logger.info(f"[DB] Found regime for {pair}: {result.get('regime_type')}")
                else:
                    logger.warning(f"[DB] No regime data found for {pair}")
                return result
    except Exception as e:
        logger.error(f"[DB ERROR] get_regime_for_pair({pair}): {str(e)}", exc_info=True)
        raise

def get_regime_market_data_from_db():
    """
    Get comprehensive market data for all symbols/timeframes for regime analysis
    Returns MT5-compatible format with indicators, structure, and recent bars
    """
    from datetime import datetime, timezone
    logger.info("[DB] Fetching comprehensive market data for regime analysis")
    
    try:
        market_data = {}
        successful_symbols = 0
        failed_symbols = []
        
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            # Discover symbols/timeframes from DB so adding new pairs doesn't require code changes
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT symbol FROM candlesticks ORDER BY symbol")
                symbols = [row["symbol"] for row in cur.fetchall()]

            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT timeframe FROM technical_indicators ORDER BY timeframe")
                timeframes = [row["timeframe"] for row in cur.fetchall()]

            if not timeframes:
                # Fallback if indicators table is empty.
                if _use_timescale_caggs():
                    timeframes = ["M5", "M15", "H1", "H4", "D1", "W1"]
                else:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT DISTINCT timeframe FROM candlesticks WHERE timeframe <> 'M1' ORDER BY timeframe"
                        )
                        timeframes = [row["timeframe"] for row in cur.fetchall()]
            
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
                        with conn.cursor() as cur:
                            cur.execute("""
                                SELECT 
                                    time, ema_9, ema_21, ema_50, ema_100, ema_200,
                                    rsi, macd_main, macd_signal, macd_histogram,
                                    atr, atr_percentile, bb_upper, bb_middle, bb_lower,
                                    bb_squeeze_ratio, bb_width_percentile,
                                    roc_percent, ema_momentum_slope,
                                    adx, dmp, dmn, obv_slope
                                FROM technical_indicators
                                WHERE symbol = %s AND timeframe = %s
                                ORDER BY time DESC
                                LIMIT 1
                            """, (symbol, timeframe))
                            indicators = cur.fetchone()
                        
                        if not indicators:
                            logger.warning(f"[DB] No indicators for {symbol}/{timeframe}")
                            continue
                        
                        # Get recent bars for market structure analysis (USE TIMESCALE CAGGs)
                        rel = _ohlcv_relation_for_timeframe(timeframe)
                        with conn.cursor() as cur:
                            if rel == "candlesticks":
                                # M1 or non-CAGG mode
                                cur.execute("""
                                    SELECT time, open, high, low, close, volume
                                    FROM candlesticks
                                    WHERE symbol = %s AND timeframe = %s
                                    ORDER BY time DESC
                                    LIMIT 100
                                """, (symbol, timeframe))
                            else:
                                # Use TimescaleDB continuous aggregate for higher timeframes
                                cur.execute(f"""
                                    SELECT time, open, high, low, close, volume
                                    FROM {rel}
                                    WHERE symbol = %s
                                    ORDER BY time DESC
                                    LIMIT 100
                                """, (symbol,))
                            candles = cur.fetchall()
                        
                        if not candles:
                            logger.warning(f"[DB] No candles for {symbol}/{timeframe}")
                            continue
                        
                        # Convert to list for easier processing
                        candle_list = list(candles)
                        current_candle = candle_list[0]
                        
                        # Calculate market structure from recent 50 bars
                        recent_50 = candle_list[:50]
                        recent_high = max(c['high'] for c in recent_50)
                        recent_low = min(c['low'] for c in recent_50)
                        range_percent = round(((recent_high - recent_low) / recent_low) * 100, 2) if recent_low > 0 else 0.0
                        
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
                                "is_market_open": True  # Will be determined by n8n workflow
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
                                "swing_analysis": {
                                    "total_swing_highs": 0,
                                    "total_swing_lows": 0,
                                    "higher_highs": 0,
                                    "lower_highs": 0,
                                    "higher_lows": 0,
                                    "lower_lows": 0
                                },
                                "price_level_analysis": {
                                    "pivot_points": pivot_data,
                                    "volume_profile": {}
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
            "analysis_timestamp": now.isoformat() + 'Z',
            "collection_info": {
                "analysis_bars": {tf: 250 for tf in timeframes},
                "recent_bars_detail": 10,
                "current_session": session,
                "successful_symbols": successful_symbols,
                "total_symbols": len(symbols),
                "failed_symbols": failed_symbols,
                "data_source": "stored_indicators"
            },
            "market_data": market_data
        }
        
        logger.info(f"[DB] Successfully fetched market data for {successful_symbols}/{len(symbols)} symbols")
        return result
        
    except Exception as e:
        logger.error(f"[DB ERROR] get_regime_market_data_from_db: {str(e)}", exc_info=True)
        return None

# ============================================================================
# NEWS ANALYSIS QUERIES (UNCHANGED - Using existing email_news_analysis)
# ============================================================================

def get_latest_news_from_db(limit: int = 50, offset: int = 0):
    """
    Get current/recent forex news from email_news_analysis table
    Supports pagination with limit/offset
    """
    logger.info(f"[DB] Fetching current news (limit={limit}, offset={offset})")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT 
                    email_id as id,
                    headline as title,
                    COALESCE(ai_analysis_summary, original_email_content) as text,
                    email_received_at as timestamp,
                    importance_score,
                    sentiment_score,
                    forex_instruments,
                    forexfactory_category,
                    market_impact_prediction,
                    volatility_expectation,
                    forexfactory_urls[1] as forexfactory_url,
                    human_takeaway,
                    attention_score,
                    news_state,
                    market_pressure,
                    attention_window,
                    confidence_label,
                    expected_followups
                  FROM email_news_analysis
                  WHERE forex_relevant = true
                  AND importance_score >= 2
                  ORDER BY email_received_at DESC
                  LIMIT %s OFFSET %s
                """, (limit, offset))
                results = cur.fetchall()
                logger.info(f"[DB] Found {len(results)} current news items")
                return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_latest_news_from_db: {str(e)}")
        raise

def get_news_count():
    """Get total count of forex-relevant news for pagination"""
    logger.info("[DB] Counting news items")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT COUNT(*) as total
                  FROM email_news_analysis
                  WHERE forex_relevant = true
                  AND importance_score >= 2
                """)
                result = cur.fetchone()
                return result['total'] if result else 0
    except Exception as e:
        logger.error(f"[DB ERROR] get_news_count: {str(e)}")
        raise

def get_upcoming_news_from_db():
    """
    Get upcoming high-impact news events
    Returns breaking news and high importance items
    """
    logger.info("[DB] Fetching upcoming news")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT 
                    email_id as id,
                    headline as title,
                    COALESCE(ai_analysis_summary, original_email_content) as text,
                    email_received_at as timestamp,
                    importance_score,
                    sentiment_score,
                    market_impact_prediction,
                    impact_timeframe,
                    volatility_expectation,
                    forex_instruments,
                    breaking_news,
                    forexfactory_urls[1] as forexfactory_url
                  FROM email_news_analysis
                  WHERE forex_relevant = true
                  AND (importance_score >= 4 OR breaking_news = true)
                  ORDER BY importance_score DESC, email_received_at DESC
                  LIMIT 25
                """)
                results = cur.fetchall()
                logger.info(f"[DB] Found {len(results)} upcoming news items")
                return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_upcoming_news_from_db: {str(e)}")
        raise

# ============================================================================
# TRADE OUTCOME TRACKING (For future MT5 integration)
# ============================================================================

def insert_trade_outcome(trade_data: dict):
    """
    Insert MT5 trade execution outcome into signals table
    Called when EA opens a position
    """
    logger.info(f"[DB] Inserting trade outcome for ticket: {trade_data.get('ticket')}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  INSERT INTO signals (
                    strategy_id,
                    mt5_ticket,
                    mt5_magic_number,
                    trading_pair,
                    direction,
                    entry_price,
                    take_profit,
                    stop_loss,
                    lot_size,
                    entry_time,
                    status,
                    market_conditions_at_entry
                  ) VALUES (
                    %(strategy_id)s,
                    %(ticket)s,
                    %(magic_number)s,
                    %(pair)s,
                    %(direction)s,
                    %(entry_price)s,
                    %(tp)s,
                    %(sl)s,
                    %(lot_size)s,
                    %(entry_time)s,
                    'open',
                    %(market_conditions)s::JSONB
                  )
                  RETURNING signal_id
                """, trade_data)
                result = cur.fetchone()
                conn.commit()
                logger.info(f"[DB] Trade inserted with signal_id: {result['signal_id']}")
                return result
    except Exception as e:
        logger.error(f"[DB ERROR] insert_trade_outcome: {str(e)}")
        raise

def update_trade_outcome(ticket: int, outcome_data: dict):
    """
    Update trade when it closes in MT5
    Records final P/L, exit price, whether TP/SL was hit
    """
    logger.info(f"[DB] Updating trade outcome for ticket: {ticket}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  UPDATE signals SET
                    exit_price = %(exit_price)s,
                    exit_time = %(exit_time)s,
                    status = %(status)s,
                    pnl = %(pnl)s,
                    pnl_pips = %(pnl_pips)s,
                    hit_tp = %(hit_tp)s,
                    hit_sl = %(hit_sl)s,
                    commission = %(commission)s,
                    swap = %(swap)s,
                    execution_notes = %(notes)s,
                    updated_at = NOW()
                  WHERE mt5_ticket = %(ticket)s
                  RETURNING signal_id
                """, {**outcome_data, 'ticket': ticket})
                result = cur.fetchone()
                conn.commit()
                if result:
                    logger.info(f"[DB] Trade outcome updated for signal_id: {result['signal_id']}")
                else:
                    logger.warning(f"[DB] No signal found with ticket: {ticket}")
                return result
    except Exception as e:
        logger.error(f"[DB ERROR] update_trade_outcome({ticket}): {str(e)}")
        raise

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_active_strategies(pair: str = None):
    """
    Get all active strategies, optionally filtered by pair
    Uses helper function from database
    """
    logger.info(f"[DB] Fetching active strategies{f' for {pair}' if pair else ''}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                if pair:
                    cur.execute("SELECT * FROM get_active_strategies(%s)", (pair.upper(),))
                else:
                    cur.execute("""
                      SELECT * FROM strategies
                      WHERE status = 'active'
                      AND expiry_time > NOW()
                      ORDER BY confidence DESC
                    """)
                results = cur.fetchall()
                logger.info(f"[DB] Found {len(results)} active strategies")
                return results
    except Exception as e:
        logger.error(f"[DB ERROR] get_active_strategies: {str(e)}")
        raise

def get_pair_performance(pair: str):
    """
    Get performance metrics for a trading pair
    Uses helper function from database
    """
    logger.info(f"[DB] Fetching performance metrics for {pair}")
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM get_pair_performance(%s)", (pair.upper(),))
                result = cur.fetchone()
                if result:
                    logger.info(f"[DB] Performance for {pair}: {result.get('total_trades')} trades, {result.get('win_rate')}% win rate")
                return result
    except Exception as e:
        logger.error(f"[DB ERROR] get_pair_performance({pair}): {str(e)}")
        raise

# ============================================================================
# LEGACY FUNCTIONS (For backward compatibility - will be removed)
# ============================================================================

def update_signal_preview(trading_pair: str, signal_data: dict):
    """
    Legacy function - kept for backward compatibility
    In v2.0, preview uses get_old_signal_from_db directly
    """
    logger.warning(f"[DB] update_signal_preview called - this is a legacy function")
    # No-op in v2.0 - previews are fetched directly from strategies table
    pass