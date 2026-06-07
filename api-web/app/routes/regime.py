from datetime import datetime
from typing import Optional
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_signals_context
from app.db import AsyncSessionLocal, get_db, get_latest_regime_from_db, get_regime_market_data_from_db, get_regime_for_pair
from app.singleflight import singleflight_cache
from app.auth import REDIS
from app.utils import json_dumps
from app.core.dependencies import _require_internal_api_key

logger = logging.getLogger(__name__)

router = APIRouter()

def _regime_all_key(request: Request, response: Response, ctx: dict = None) -> str:
    return "regime:all"

@router.get("/api/regime")
@singleflight_cache(key_builder=_regime_all_key, ttl=900)
async def get_regime(request: Request, response: Response, ctx: dict = Depends(require_signals_context)):
    """Get latest regime analysis for all trading pairs"""
    logger.info(f"[API] GET /api/regime - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        logger.info("[API] Cache MISS/Bypass for regime, querying database")
        async with AsyncSessionLocal() as db:
            rows = await get_latest_regime_from_db(db)
        
        if not rows:
            logger.warning("[API] No regime data found in database")
            return {"_cache_status": "NOT_FOUND"}
        
        return rows
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _regime_market_data_key(request: Request, db: AsyncSession = None) -> str:
    return "regime:market_data"

@router.get("/api/regime/market-data")
@singleflight_cache(key_builder=_regime_market_data_key, ttl=300)
async def get_regime_market_data_markdown(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Get comprehensive market data for regime analysis (n8n workflow endpoint)
    Returns JSON with markdown split by symbol for LLM processing
    Requires X-API-Key header for authentication
    """
    # API Key authentication for n8n workflow
    try:
        _require_internal_api_key(request, "N8N_MARKET_DATA_KEY")
    except HTTPException:
        logger.warning(f"[API] Invalid API key attempt for /api/regime/market-data from {request.client.host}")
        raise
    
    logger.info("[API] GET /api/regime/market-data - n8n workflow request (authenticated)")
    
    try:
        logger.info("[API] Cache MISS/Bypass for regime market data, querying database")
        data = await get_regime_market_data_from_db(db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            return {"_cache_status": "NOT_FOUND"}
        
        # Convert to markdown format split by symbol
        market_data_raw = data.get("market_data", {})
        analysis_timestamp = data.get("analysis_timestamp", datetime.now().isoformat())
        collection_info = data.get("collection_info", {})
        
        logger.info(f"[API] Converting {len(market_data_raw)} symbols to markdown format")
        
        def format_symbol_markdown(symbol: str, data: dict, timestamp: str) -> str:
            """Format a single symbol's data as markdown optimized for AI analysis"""
            md = f"# {symbol} Technical Analysis Report\n\n"
            md += f"**📅 Analysis Timestamp:** {timestamp}\n\n"
            md += "="*80 + "\n\n"
            
            # Sort timeframes by importance: D1, W1, H4, H1, M15, M5
            timeframe_order = ["D1", "W1", "H4", "H1", "M15", "M5"]
            sorted_tfs = sorted(data.keys(), key=lambda x: timeframe_order.index(x) if x in timeframe_order else 999)
            
            for timeframe in sorted_tfs:
                metrics = data[timeframe]
                
                md += f"## 📊 {timeframe} Timeframe\n\n"
                
                # Price Summary Box
                current_price = metrics.get('current_price', 'N/A')
                md += f"### 💰 Price: {current_price}\n\n"
                
                # Technical Indicators
                if "technical_indicators" in metrics:
                    ind = metrics["technical_indicators"]
                    
                    # Trend Analysis Section
                    md += "### 📈 Trend Analysis\n\n"
                    rsi = ind.get('rsi', 'N/A')
                    adx = ind.get('adx', 'N/A')
                    dmp = ind.get('dmp', 'N/A')
                    dmn = ind.get('dmn', 'N/A')
                    
                    # Trend signal interpretation
                    if rsi != 'N/A' and rsi is not None:
                        rsi_signal = "Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"
                        md += f"- **RSI(14)**: {rsi} ({rsi_signal})\n"
                    else:
                        md += f"- **RSI(14)**: {rsi}\n"
                    
                    if adx != 'N/A' and adx is not None:
                        trend_strength = "Strong" if adx > 25 else "Weak"
                        md += f"- **ADX(14)**: {adx} ({trend_strength} Trend)\n"
                    else:
                        md += f"- **ADX(14)**: {adx}\n"
                    
                    md += f"- **+DI**: {dmp}\n"
                    md += f"- **-DI**: {dmn}\n\n"
                    
                    # Momentum Section
                    md += "### ⚡ Momentum Indicators\n\n"
                    md += f"- **MACD Line**: {ind.get('macd_main', 'N/A')}\n"
                    md += f"- **MACD Signal**: {ind.get('macd_signal', 'N/A')}\n"
                    md += f"- **MACD Histogram**: {ind.get('macd_histogram', 'N/A')}\n"
                    md += f"- **ROC %**: {ind.get('roc_percent', 'N/A')}\n"
                    md += f"- **EMA Momentum Slope**: {ind.get('ema_momentum_slope', 'N/A')}\n"
                    md += f"- **OBV Slope**: {ind.get('obv_slope', 'N/A')}\n\n"
                    
                    # Volatility Section
                    md += "### 🌊 Volatility Metrics\n\n"
                    atr = ind.get('atr', 'N/A')
                    atr_pct = ind.get('atr_percentile', 'N/A')
                    if atr_pct != 'N/A' and atr_pct is not None:
                        vol_level = "High" if atr_pct > 75 else "Low" if atr_pct < 25 else "Normal"
                        md += f"- **ATR(14)**: {atr} (Percentile: {atr_pct}% - {vol_level})\n\n"
                    else:
                        md += f"- **ATR(14)**: {atr}\n\n"
                    
                    # EMAs Section
                    if "emas" in ind:
                        emas = ind["emas"]
                        md += "### 📊 Exponential Moving Averages\n\n"
                        for period in [9, 21, 50, 100, 200]:
                            ema_val = emas.get(f'EMA_{period}', 'N/A')
                            if ema_val != 'N/A' and ema_val is not None:
                                md += f"- **EMA-{period}**: {ema_val}\n"
                        md += "\n"
                    
                    # Bollinger Bands Section
                    bb_upper = ind.get('bb_upper')
                    bb_middle = ind.get('bb_middle')
                    bb_lower = ind.get('bb_lower')
                    bb_squeeze = ind.get('bb_squeeze_ratio')
                    bb_width_pct = ind.get('bb_width_percentile')
                    
                    if bb_upper or bb_middle or bb_lower:
                        md += "### 📉 Bollinger Bands\n\n"
                        md += f"- **Upper Band**: {bb_upper if bb_upper else 'N/A'}\n"
                        md += f"- **Middle Band (SMA-20)**: {bb_middle if bb_middle else 'N/A'}\n"
                        md += f"- **Lower Band**: {bb_lower if bb_lower else 'N/A'}\n"
                        md += f"- **Squeeze Ratio**: {bb_squeeze if bb_squeeze else 'N/A'}\n"
                        
                        if bb_width_pct != 'N/A' and bb_width_pct is not None:
                            squeeze_level = "Tight Squeeze" if bb_width_pct < 25 else "Wide Expansion" if bb_width_pct > 75 else "Normal"
                            md += f"- **Width Percentile**: {bb_width_pct}% ({squeeze_level})\n\n"
                        else:
                            md += f"- **Width Percentile**: {bb_width_pct}\n\n"
                
                # Market Structure Section
                if "market_structure" in metrics:
                    struct = metrics["market_structure"]
                    md += "### 🏗️ Market Structure (50-bar Range)\n\n"
                    md += f"- **Recent High**: {struct.get('recent_high', 'N/A')}\n"
                    md += f"- **Recent Low**: {struct.get('recent_low', 'N/A')}\n"
                    range_pct = struct.get('range_percent', 'N/A')
                    if range_pct != 'N/A' and range_pct is not None:
                        volatility = "High Volatility" if range_pct > 10 else "Low Volatility" if range_pct < 3 else "Moderate"
                        md += f"- **Range**: {range_pct}% ({volatility})\n\n"
                    else:
                        md += f"- **Range**: {range_pct}%\n\n"
                
                # Recent Price Action Table
                if "recent_bars_detail" in metrics and isinstance(metrics["recent_bars_detail"], list):
                    bars = metrics["recent_bars_detail"][:5]  # Last 5 bars
                    md += f"### 🕐 Recent Price Action (Last {len(bars)} Candles)\n\n"
                    md += "| Time | Open | High | Low | Close | Volume | Type |\n"
                    md += "|:-----|-----:|-----:|----:|------:|-------:|:----:|\n"
                    for bar in bars:
                        candle_type = bar.get('candle_type', 'N/A')
                        emoji = "🟢" if candle_type == "Bullish" else "🔴" if candle_type == "Bearish" else "⚪"
                        md += f"| {bar.get('time', 'N/A')} | {bar.get('open', 'N/A')} | {bar.get('high', 'N/A')} | {bar.get('low', 'N/A')} | {bar.get('close', 'N/A')} | {bar.get('volume', 'N/A')} | {emoji} {candle_type} |\n"
                    md += "\n"
                
                md += "---\n\n"
            
            return md.strip()
        
        # Generate markdown for each symbol
        market_data_formatted = {}
        null_indicators = []
        
        for symbol, symbol_data in market_data_raw.items():
            # Check for null indicators
            for tf, metrics in symbol_data.items():
                if "technical_indicators" in metrics:
                    ind = metrics["technical_indicators"]
                    null_fields = [k for k, v in ind.items() if v is None and k != "emas"]
                    if ind.get("emas"):
                        null_emas = [k for k, v in ind["emas"].items() if v is None]
                        if null_emas:
                            null_fields.append(f"emas.{','.join(null_emas)}")
                    if null_fields:
                        null_indicators.append(f"{symbol}/{tf}: {', '.join(null_fields)}")
            
            market_data_formatted[symbol] = format_symbol_markdown(symbol, symbol_data, analysis_timestamp)
        
        if null_indicators:
            logger.warning(f"[API] Found null indicators: {null_indicators[:5]}...")  # Log first 5
        
        # Build response
        response_data = {
            "analysis_timestamp": analysis_timestamp,
            "collection_info": {
                **collection_info,
                "format": "markdown",
                "symbols": list(market_data_formatted.keys()),
                "timeframes": ["D1", "W1", "H4", "H1", "M15", "M5"]
            },
            "market_data": market_data_formatted
        }
        
        return response_data
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _regime_market_data_json_key(request: Request, symbol: Optional[str] = None, symbols: Optional[str] = None, db: AsyncSession = None) -> str:
    return f"regime:market_data_json:{symbol}:{symbols}"

@router.get("/api/regime/market-data/json")
@singleflight_cache(key_builder=_regime_market_data_json_key, ttl=300)
async def get_regime_market_data_json(
    request: Request,
    symbol: Optional[str] = Query(None, description="Single symbol filter, e.g. XAUUSD"),
    symbols: Optional[str] = Query(None, description="Comma-separated symbols filter, e.g. XAUUSD,EURUSD"),
    db: AsyncSession = Depends(get_db)
):
    """
    Get comprehensive market data for regime analysis (JSON format)
    Returns MT5-compatible JSON format with indicators, structure, and recent bars
    Optional query filters:
    - symbol=XAUUSD
    - symbols=XAUUSD,EURUSD
    Requires X-API-Key header for authentication
    """
    # API Key authentication for n8n workflow
    try:
        _require_internal_api_key(request, "N8N_MARKET_DATA_KEY")
    except HTTPException:
        logger.warning(f"[API] Invalid API key attempt for /api/regime/market-data/json from {request.client.host}")
        raise
    
    requested_symbols = []
    if symbol:
        requested_symbols.append(str(symbol).strip().upper())
    if symbols:
        requested_symbols.extend(
            [s.strip().upper() for s in str(symbols).split(",") if s and s.strip()]
        )
    requested_symbols = sorted(set([s for s in requested_symbols if s]))

    def _apply_symbol_filter(full_payload: dict) -> dict:
        if not requested_symbols:
            return full_payload

        market_data = full_payload.get("market_data", {})
        filtered_market_data = {
            sym: market_data[sym]
            for sym in requested_symbols
            if sym in market_data
        }

        if not filtered_market_data:
            logger.warning(f"[API] No market data found for requested symbols: {requested_symbols}")
            raise HTTPException(404, f"No market data found for requested symbols: {', '.join(requested_symbols)}")

        collection_info = full_payload.get("collection_info", {})
        return {
            **full_payload,
            "collection_info": {
                **collection_info,
                "requested_symbols": requested_symbols,
                "symbols": list(filtered_market_data.keys()),
                "symbols_count": len(filtered_market_data),
            },
            "market_data": filtered_market_data,
        }

    logger.info(
        f"[API] GET /api/regime/market-data/json - authenticated request"
        f" (symbols={requested_symbols if requested_symbols else 'ALL'})"
    )
    
    try:
        logger.info("[API] Cache MISS/Bypass for JSON market data, querying database")
        data = await get_regime_market_data_from_db(db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            return {"_cache_status": "NOT_FOUND"}
        
        return _apply_symbol_filter(data)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data/json: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _regime_by_pair_key(pair: str, ctx: dict = None) -> str:
    return f"regime:pair:{pair}"

@router.get("/api/regime/{pair}")
@singleflight_cache(key_builder=_regime_by_pair_key, ttl=900)
async def get_regime_by_pair(pair: str, ctx=Depends(require_signals_context)):
    """Get latest regime analysis for a specific pair"""
    logger.info(f"[API] GET /api/regime/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        logger.info(f"[API] Cache MISS/Bypass for regime: {pair}, querying database")
        async with AsyncSessionLocal() as db:
            row = await get_regime_for_pair(db, pair)
        
        if not row:
            logger.warning(f"[API] No regime data found for pair: {pair}")
            return {"_cache_status": "NOT_FOUND"}
        
        return row
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")
