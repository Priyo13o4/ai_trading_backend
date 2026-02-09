import json, asyncio, logging, os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from .auth import auth_context, REDIS, log_redis_connection_health
from .authn.authz import require_permission
from .authn.csrf import enforce_csrf
from .authn.routes import router as auth_router
from .authn.session_store import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from .db import (
    get_latest_signal_from_db, 
    get_old_signal_from_db, 
    get_latest_regime_from_db,
    get_regime_for_pair,
    get_regime_market_data_from_db,
    get_latest_news_from_db, 
    get_upcoming_news_from_db,
    get_news_count,
    get_active_strategies,
    get_pair_performance,
    insert_trade_outcome,
    update_trade_outcome
)
from .utils import json_dumps
from trading_common.market_data import (
    TIMEFRAME_MAP,
    SYMBOL_INFO
)
from trading_common.symbols import get_active_symbols

# Import historical routes
from .routes.historical import router as historical_router

# Import cache and SSE
from .cache import CandleCache, NewsCache, StrategyCache, check_redis_connection
from .sse import router as sse_router

# Configure logging with UTC
import time
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Trading Bot API",
    description="FastAPI backend with Redis caching, auth gating, and MT5 integration",
    version="2.0.0"
)


def _parse_cors_origins() -> list[str]:
    raw = (os.getenv("ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "https://pipfactor.com",
        ]

    try:
        if raw.startswith("["):
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
    except Exception:
        pass

    return [o.strip() for o in raw.split(",") if o.strip()]


def _cors_origin_regex() -> str:
    return os.getenv("ALLOWED_ORIGIN_REGEX", r"https://.*\.pipfactor\.com")


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    # Enforce CSRF for cookie-authenticated state-changing requests.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.url.path.startswith("/auth/"):
            # /auth/exchange is pre-session; /auth/validate is non-mutating but POST.
            pass
        elif request.url.path.startswith("/webhooks/"):
            pass
        else:
            if request.cookies.get(SESSION_COOKIE_NAME):
                enforce_csrf(request, CSRF_COOKIE_NAME)

    return await call_next(request)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================================
# STARTUP INITIALIZATION
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """
    Application startup event
    """
    import os
    
    # Use file lock to log only from first worker
    lock_file = "/tmp/fastapi_startup.lock"
    is_first = not os.path.exists(lock_file)
    
    if is_first:
        open(lock_file, 'w').close()
        logger.info("="*80)
        logger.info("FASTAPI APPLICATION STARTUP (Worker PID: %s)", os.getpid())
        logger.info("="*80)
    
    # Log Redis connectivity (only from first worker)
    if is_first:
        try:
            await log_redis_connection_health()
        except Exception as err:
            logger.error("Redis health check failed: %s", err)
    
    if is_first:
        logger.info("="*80)
        logger.info("STARTUP COMPLETE")
        logger.info("="*80)


# Include routers
app.include_router(historical_router)
app.include_router(sse_router)  # Server-Sent Events for real-time updates
app.include_router(auth_router)

# ============================================================================
# SYMBOLS ENDPOINT (Dynamic symbol list for frontend)
# ============================================================================

@app.get("/api/symbols")
async def get_symbols():
    """
    Get list of active trading symbols with metadata.
    Frontend should call this on startup to dynamically populate symbol lists.
    """
    # Import POSTGRES_DSN from db module
    from .db import POSTGRES_DSN
    
    # DB/Redis is the source of truth for symbols.
    symbols = await get_active_symbols(redis_client=REDIS, postgres_dsn=POSTGRES_DSN, fallback=[])

    # Build metadata for all symbols (with defaults for unknown symbols)
    metadata = {}
    for symbol in symbols:
        if symbol in SYMBOL_INFO:
            metadata[symbol] = SYMBOL_INFO[symbol]
        else:
            # Default metadata for symbols without explicit info
            metadata[symbol] = {
                "name": symbol,
                "type": "unknown",
                "precision": 5
            }
    
    return {
        "symbols": symbols,
        "metadata": metadata,
        "count": len(symbols)
    }

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/api/health")
async def health(): 
    logger.info("[API] Health check requested")
    return {"status": "ok", "version": "2.0.0"}

# ============================================================================
# STRATEGY ENDPOINTS (AI-Generated Trading Recommendations)
# ============================================================================

@app.get("/api/signals/{pair}")
async def get_signal(pair: str, request: Request, response: Response, ctx=Depends(auth_context)):
    """Get latest active strategy for a trading pair (requires auth)"""
    logger.info(f"[API] GET /api/signals/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        require_permission(ctx, "signals")
        
        key = f"latest:signal:{pair.upper()}"
        
        # Try Redis cache first
        cached = await REDIS.get(key)
        if cached: 
            logger.info(f"[API] Cache HIT for signal: {pair}")
            return JSONResponse(content=json.loads(cached))

        # Cache miss - fetch from database
        logger.info(f"[API] Cache MISS for signal: {pair}, querying database")
        row = await asyncio.to_thread(get_latest_signal_from_db, pair)
        
        if not row: 
            logger.warning(f"[API] No active strategy found for pair: {pair}")
            raise HTTPException(404, f"No active strategy found for {pair}")
        
        # Cache the result
        ttl = int(row.get("expiry_minutes") or 30) * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached signal for {pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/signals/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/preview/{pair}")
async def get_signal_preview(pair: str, request: Request):
    """Get old strategy preview for main page (no auth required)"""
    logger.info(f"[API] GET /api/preview/{pair} - Public access")
    
    try:
        # Only XAUUSD previews for now
        if pair.upper() != "XAUUSD":
            logger.warning(f"[API] Preview requested for unsupported pair: {pair}")
            raise HTTPException(404, "Preview only available for XAUUSD")
        
        key = f"preview:signal:{pair.upper()}"
        
        # Try Redis cache
        cached = await REDIS.get(key)
        if cached: 
            logger.info(f"[API] Cache HIT for preview: {pair}")
            return JSONResponse(content=json.loads(cached))

        # Cache miss - get old signal
        logger.info(f"[API] Cache MISS for preview: {pair}, querying database")
        row = await asyncio.to_thread(get_old_signal_from_db, pair)
        
        if not row: 
            logger.warning(f"[API] No preview strategy found for pair: {pair}")
            raise HTTPException(404, "No preview available")
        
        # Cache preview for 1 hour (it's old data)
        ttl = 60 * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached preview for {pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/preview/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/strategies")
async def get_all_active_strategies(pair: str = None, ctx=Depends(auth_context)):
    """Get all active strategies, optionally filtered by pair"""
    logger.info(f"[API] GET /api/strategies?pair={pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        strategies = await asyncio.to_thread(get_active_strategies, pair)
        logger.info(f"[API] Found {len(strategies)} active strategies")
        serialized = json_dumps({"strategies": strategies})
        return JSONResponse(content=json.loads(serialized))
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

# ============================================================================
# REGIME ANALYSIS ENDPOINTS
# ============================================================================

@app.get("/api/regime")
async def get_regime(request: Request, response: Response, ctx=Depends(auth_context)):
    """Get latest regime analysis for all trading pairs"""
    logger.info(f"[API] GET /api/regime - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        key = "latest:regime"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for regime data")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss
        logger.info("[API] Cache MISS for regime, querying database")
        rows = await asyncio.to_thread(get_latest_regime_from_db)
        
        if not rows:
            logger.warning("[API] No regime data found in database")
            raise HTTPException(404, "No regime data found")
        
        # Cache for 15 minutes
        ttl = 15 * 60
        serialized = json_dumps(rows)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached regime data for {len(rows)} pairs with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/regime/market-data")
async def get_regime_market_data_markdown(request: Request):
    """
    Get comprehensive market data for regime analysis (n8n workflow endpoint)
    Returns JSON with markdown split by symbol for LLM processing
    Requires X-API-Key header for authentication
    """
    # API Key authentication for n8n workflow
    api_key = request.headers.get("X-API-Key")
    expected_key = os.getenv("N8N_MARKET_DATA_KEY")
    
    if not expected_key:
        logger.error("[API] N8N_MARKET_DATA_KEY not configured in environment")
        raise HTTPException(500, "API key authentication not configured")
    
    if not api_key or api_key != expected_key:
        logger.warning(f"[API] Invalid API key attempt for /api/regime/market-data from {request.client.host}")
        raise HTTPException(401, "Invalid or missing API key")
    
    logger.info("[API] GET /api/regime/market-data - n8n workflow request (authenticated)")
    
    try:
        key = "regime:market-data:markdown"
        
        # Try cache (5 min TTL for fresh data)
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for regime market data (markdown)")
            import json
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss - fetch from database
        logger.info("[API] Cache MISS for regime market data, querying database")
        data = await asyncio.to_thread(get_regime_market_data_from_db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            raise HTTPException(404, "No market data available")
        
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
        
        # Cache for 5 minutes
        ttl = 5 * 60
        import json
        await REDIS.setex(key, ttl, json.dumps(response_data))
        logger.info(f"[API] Cached regime market data for {len(market_data_formatted)} symbols with TTL={ttl}s")
        
        return JSONResponse(content=response_data)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/regime/market-data/json")
async def get_regime_market_data_json(request: Request):
    """
    Get comprehensive market data for regime analysis (JSON format)
    Returns MT5-compatible JSON format with indicators, structure, and recent bars
    Requires X-API-Key header for authentication
    """
    # API Key authentication for n8n workflow
    api_key = request.headers.get("X-API-Key")
    expected_key = os.getenv("N8N_MARKET_DATA_KEY")
    
    if not expected_key:
        logger.error("[API] N8N_MARKET_DATA_KEY not configured in environment")
        raise HTTPException(500, "API key authentication not configured")
    
    if not api_key or api_key != expected_key:
        logger.warning(f"[API] Invalid API key attempt for /api/regime/market-data/json from {request.client.host}")
        raise HTTPException(401, "Invalid or missing API key")
    
    logger.info("[API] GET /api/regime/market-data/json - authenticated request")
    
    try:
        key = "regime:market-data:json"
        
        # Try cache (5 min TTL for fresh data)
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for JSON market data")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss - fetch from database
        logger.info("[API] Cache MISS for JSON market data, querying database")
        data = await asyncio.to_thread(get_regime_market_data_from_db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            raise HTTPException(404, "No market data available")
        
        # Cache for 5 minutes
        ttl = 5 * 60
        serialized = json_dumps(data)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached JSON market data for {len(data.get('market_data', {}))} symbols with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data/json: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/regime/{pair}")
async def get_regime_by_pair(pair: str, ctx=Depends(auth_context)):
    """Get latest regime analysis for a specific pair"""
    logger.info(f"[API] GET /api/regime/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        key = f"regime:{pair.upper()}"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for regime: {pair}")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss
        logger.info(f"[API] Cache MISS for regime: {pair}, querying database")
        row = await asyncio.to_thread(get_regime_for_pair, pair)
        
        if not row:
            logger.warning(f"[API] No regime data found for pair: {pair}")
            raise HTTPException(404, f"No regime data for {pair}")
        
        # Cache for 15 minutes
        ttl = 15 * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached regime for {pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

# ============================================================================
# NEWS ENDPOINTS
# ============================================================================

@app.get("/api/news/current")
async def get_current_news(
    request: Request, 
    response: Response, 
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(auth_context)
):
    """Get current/recent high-impact forex news with pagination"""
    logger.info(f"[API] GET /api/news/current - User: {ctx.get('user_id', 'anonymous')}, limit={limit}, offset={offset}")
    
    try:
        # Different cache key for each page
        key = f"latest:news:current:{limit}:{offset}"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for current news (offset={offset})")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss
        logger.info(f"[API] Cache MISS for current news, querying database (offset={offset})")
        rows = await asyncio.to_thread(get_latest_news_from_db, limit, offset)
        total = await asyncio.to_thread(get_news_count)
        
        if not rows:
            logger.info("[API] No current news found in database")
            return JSONResponse(content={"news": [], "total": total, "limit": limit, "offset": offset})
        
        # Cache for 5 minutes
        ttl = 5 * 60
        result = {"news": rows, "total": total, "limit": limit, "offset": offset}
        serialized = json_dumps(result)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached {len(rows)} current news items with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/current: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/news/upcoming")
async def get_upcoming_news(request: Request, response: Response, ctx=Depends(auth_context)):
    """Get upcoming high-impact forex events"""
    logger.info(f"[API] GET /api/news/upcoming - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        key = "latest:news:upcoming"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for upcoming news")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss
        logger.info("[API] Cache MISS for upcoming news, querying database")
        rows = await asyncio.to_thread(get_upcoming_news_from_db)
        
        if not rows:
            logger.info("[API] No upcoming news found in database")
            return JSONResponse(content={"news": []})
        
        # Cache for 5 minutes
        ttl = 5 * 60
        serialized = json_dumps(rows)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached {len(rows)} upcoming news items with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/upcoming: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/news/markers/{symbol}")
async def get_news_markers(symbol: str, hours: int = None, min_importance: int = 3):
    """Get news markers for chart annotations
    
    Args:
        symbol: Trading pair (e.g., XAUUSD)
        hours: Time range in hours (default: None = all time)
        min_importance: Minimum importance score (1-5, default 3)
    
    Returns:
        List of news events with timestamps for chart markers
    """
    import psycopg
    from psycopg.rows import dict_row
    from datetime import datetime, timedelta, timezone
    from .cache import NewsMarkersCache
    
    symbol = symbol.upper()
    
    # If hours not specified, use large default (1 year)
    if hours is None:
        hours = 8760  # 365 days
    
    logger.info(f"GET /api/news/markers/{symbol}?hours={hours}&min_importance={min_importance}")
    
    # Try cache first (cache key includes importance filter)
    cache_key = f"{symbol}_{hours}h_imp{min_importance}"
    cached_markers = NewsMarkersCache.get(symbol, hours)
    if cached_markers:
        # Filter by importance on cache hit
        filtered = [m for m in cached_markers if m.get('importance', 0) >= min_importance]
        logger.info(f"Cache HIT: news markers for {symbol} ({len(filtered)}/{len(cached_markers)} after importance filter)")
        return {"markers": filtered}
    
    logger.info(f"Cache MISS: news markers for {symbol}, querying database")
    
    try:
        DATABASE_URL = os.getenv("DATABASE_URL")
        if not DATABASE_URL:
            pg_host = os.getenv("POSTGRES_HOST", "localhost")
            pg_port = os.getenv("POSTGRES_PORT", "5432")
            pg_db = os.getenv("POSTGRES_DB", "ai_trading_bot_data")
            pg_user = os.getenv("POSTGRES_USER", "postgres")
            pg_password = os.getenv("POSTGRES_PASSWORD", "")
            DATABASE_URL = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"
        
        # Calculate time range
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=hours)
        
        # Map symbol to instruments (handle different naming conventions)
        symbol_map = {
            'XAUUSD': ['XAU/USD', 'GOLD', 'XAUUSD'],
            'EURUSD': ['EUR/USD', 'EURUSD', 'EUR'],
            'GBPUSD': ['GBP/USD', 'GBPUSD', 'GBP'],
            'USDJPY': ['USD/JPY', 'USDJPY', 'JPY'],
            'USDCAD': ['USD/CAD', 'USDCAD', 'CAD'],
            'AUDUSD': ['AUD/USD', 'AUDUSD', 'AUD']
        }
        
        instruments = symbol_map.get(symbol, [symbol])
        
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Query news relevant to this symbol within time range
                query = """
                    SELECT 
                        email_id,
                        headline,
                        email_received_at as time,
                        importance_score,
                        sentiment_score,
                        market_impact_prediction,
                        volatility_expectation,
                        forex_instruments,
                        breaking_news,
                        central_bank_related,
                        news_category
                    FROM email_news_analysis
                    WHERE forex_relevant = true
                        AND email_received_at >= %s
                        AND email_received_at <= %s
                        AND importance_score >= %s
                        AND (
                            primary_instrument = ANY(%s)
                            OR forex_instruments && %s
                        )
                    ORDER BY email_received_at DESC
                    LIMIT 500
                """
                
                cur.execute(query, (start_time, now, min_importance, instruments, instruments))
                news_items = cur.fetchall()
        
        # Format for chart markers
        markers = []
        for item in news_items:
            # Determine marker color based on sentiment and impact
            color = '#64748b'  # neutral grey
            if item['market_impact_prediction'] == 'bullish':
                color = '#22c55e'  # green
            elif item['market_impact_prediction'] == 'bearish':
                color = '#ef4444'  # red
            elif item['breaking_news'] or item['importance_score'] >= 5:
                color = '#f59e0b'  # orange for breaking/high importance
            
            # Marker shape based on type
            shape = 'circle'
            if item['central_bank_related']:
                shape = 'arrowDown'
            elif item['breaking_news']:
                shape = 'arrowUp'
            
            markers.append({
                'time': item['time'].isoformat() if item['time'] else None,
                'id': item['email_id'],
                'headline': item['headline'][:100],  # Truncate for marker
                'full_headline': item['headline'],
                'importance': item['importance_score'],
                'sentiment': float(item['sentiment_score']) if item['sentiment_score'] else 0,
                'impact': item['market_impact_prediction'],
                'volatility': item['volatility_expectation'],
                'instruments': item['forex_instruments'],
                'breaking': item['breaking_news'],
                'category': item['news_category'],
                'color': color,
                'shape': shape
            })
        
        # Cache the results
        NewsMarkersCache.set(symbol, markers, hours)
        
        logger.info(f"Returning {len(markers)} news markers for {symbol}")
        return {"markers": markers}
        
    except Exception as e:
        logger.error(f"Error fetching news markers for {symbol}: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to fetch news markers: {str(e)}")

# ============================================================================
# PERFORMANCE ANALYTICS
# ============================================================================

@app.get("/api/performance/{pair}")
async def get_performance(pair: str, ctx=Depends(auth_context)):
    """Get performance metrics for a trading pair"""
    logger.info(f"[API] GET /api/performance/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        metrics = await asyncio.to_thread(get_pair_performance, pair)
        if not metrics:
            logger.warning(f"[API] No performance data found for pair: {pair}")
            return JSONResponse(content={"message": f"No trade history for {pair}"})
        
        logger.info(f"[API] Performance for {pair}: {metrics.get('total_trades')} trades")
        serialized = json_dumps(metrics)
        return JSONResponse(content=json.loads(serialized))
    except Exception as e:
        logger.error(f"[API ERROR] /api/performance/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

# ============================================================================
# MT5 TRADE TRACKING ENDPOINTS (Future)
# ============================================================================

@app.post("/api/trades/outcome")
async def record_trade_outcome(request: Request):
    """Record MT5 trade execution (called by EA when opening position)"""
    logger.info("[API] POST /api/trades/outcome")
    
    try:
        trade_data = await request.json()
        logger.info(f"[API] Recording trade outcome for ticket: {trade_data.get('ticket')}")
        
        result = await asyncio.to_thread(insert_trade_outcome, trade_data)
        logger.info(f"[API] Trade recorded with signal_id: {result['signal_id']}")
        
        return JSONResponse(content={"signal_id": result['signal_id'], "status": "recorded"})
    except Exception as e:
        logger.error(f"[API ERROR] /api/trades/outcome: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.put("/api/trades/{ticket}/close")
async def close_trade(ticket: int, request: Request):
    """Update trade when closed in MT5 (records P/L, exit price, outcome)"""
    logger.info(f"[API] PUT /api/trades/{ticket}/close")
    
    try:
        outcome_data = await request.json()
        logger.info(f"[API] Closing trade {ticket} with P/L: {outcome_data.get('pnl')}")
        
        result = await asyncio.to_thread(update_trade_outcome, ticket, outcome_data)
        
        if not result:
            logger.warning(f"[API] No signal found with ticket: {ticket}")
            raise HTTPException(404, f"No signal found with ticket {ticket}")
        
        logger.info(f"[API] Trade {ticket} closed successfully")
        return JSONResponse(content={"signal_id": result['signal_id'], "status": "updated"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/trades/{ticket}/close: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")
