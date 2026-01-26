import json, asyncio, logging, os, subprocess, threading
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
from .market_data import (
    TIMEFRAME_MAP,
    SYMBOL_INFO
)
from .symbols import get_active_symbols

# Import market status checker
from .market_status import (
    is_forex_market_open,
    refresh_holiday_cache,
    get_cache_stats as get_market_cache_stats,
    initialize_market_status  # NEW: explicit initialization
)

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

# MT5 Configuration (from bridge_server.py)
WINE_EXECUTABLE = "/Volumes/My Drive/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
WINEPREFIX_PATH = "/Users/priyodip/Library/Application Support/net.metaquotes.wine.metatrader5"
PYTHON_EXE_WINE_PATH = "C:\\python86\\python.exe"
COLLECTOR_SCRIPT_PATH = "C:\\Program Files\\MetaTrader 5\\MQL5\\Scripts\\send_json_to_n8nV4.py"
SIGNAL_OUTPUT_PATH = "/Users/priyodip/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/priyodip/mt5_output/strategy_signals.json"

app = FastAPI(
    title="AI Trading Bot API",
    description="FastAPI backend with Redis caching, auth gating, and MT5 integration",
    version="2.0.0"
)


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
    allow_origins=[
        "http://localhost:3000", 
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://pipfactor.com",
    ],  
    allow_origin_regex=r"https://.*\.pipfactor\.com",
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
    Initialize market status module ONCE (prevents workers hitting API)
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
    
    # Initialize market status with persistent cache
    try:
        initialize_market_status()
    except Exception as e:
        if is_first:
            logger.error(f"Market status initialization error (non-fatal): {e}")
    
    if is_first:
        logger.info("="*80)
        logger.info("STARTUP COMPLETE")
        logger.info("="*80)

    # Start MT5 ingest TCP server (port 9001) inside the API process.
    # Under gunicorn multi-worker, only one worker will be able to bind the port.
    try:
        if os.getenv("MT5_INGEST_ENABLE", "false").strip().lower() in {"1", "true", "yes", "y", "on"}:
            from .mt5_ingest import mt5_ingest_server
            from .mt5_symbol_notify import start_symbol_notify_listener

            async def _start_mt5_ingest_once() -> None:
                try:
                    await mt5_ingest_server.start()
                except OSError as e:
                    # Another worker likely owns the port.
                    logger.info("[MT5] Ingest start skipped (likely already bound): %s", e)

            asyncio.create_task(_start_mt5_ingest_once())

            # Start DB-triggered symbol hot-add listener.
            asyncio.create_task(start_symbol_notify_listener())
    except Exception as e:
        # Non-fatal: API can still serve HTTP even if MT5 ingest is down.
        logger.error("[MT5] Failed to start ingest server: %s", e)


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
    # DB/Redis is the source of truth for symbols.
    symbols = await get_active_symbols(redis_client=REDIS, fallback=[])

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
async def get_regime_market_data():
    """
    Get comprehensive market data for regime analysis (n8n workflow endpoint)
    Returns MT5-compatible format with indicators, structure, and recent bars
    """
    logger.info("[API] GET /api/regime/market-data - n8n workflow request")
    
    try:
        key = "regime:market-data"
        
        # Try cache (5 min TTL for fresh data)
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for market data")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss - fetch from database
        logger.info("[API] Cache MISS for market data, querying database")
        data = await asyncio.to_thread(get_regime_market_data_from_db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            raise HTTPException(404, "No market data available")
        
        # Cache for 5 minutes
        ttl = 5 * 60
        serialized = json_dumps(data)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached market data for {len(data.get('market_data', {}))} symbols with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data: {str(e)}", exc_info=True)
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

# ============================================================================
# MT5 INTEGRATION ENDPOINTS (from bridge_server.py)
# ============================================================================

def run_collector_script():
    """Run MT5 data collector script via Wine"""
    logger.info("[MT5] Running data collector script")
    command = [
        WINE_EXECUTABLE,
        PYTHON_EXE_WINE_PATH,
        COLLECTOR_SCRIPT_PATH
    ]
    env = os.environ.copy()
    env['WINEPREFIX'] = WINEPREFIX_PATH
    env['WINEDEBUG'] = '-all'
    
    try:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate()
        logger.info(f"[MT5] Collector STDOUT: {stdout}")
        if stderr:
            logger.error(f"[MT5] Collector STDERR: {stderr}")
    except Exception as e:
        logger.error(f"[MT5] Collector error: {str(e)}", exc_info=True)

@app.post("/trigger")
async def trigger_collector():
    """
    Trigger MT5 data collection (from bridge_server.py)
    Runs regime classifier data collection in background
    """
    logger.info(f"[API] POST /trigger - Launching MT5 collector at {datetime.now()}")
    
    try:
        # Run collector in background thread
        threading.Thread(target=run_collector_script, daemon=True).start()
        logger.info("[API] MT5 collector triggered successfully")
        return JSONResponse(content={"status": "triggered", "timestamp": datetime.now().isoformat()})
    except Exception as e:
        logger.error(f"[API ERROR] /trigger: {str(e)}", exc_info=True)
        raise HTTPException(500, "Failed to trigger collector")

@app.post("/signal")
async def receive_signal_from_n8n(request: Request):
    """
    Receive signals from n8n Strategy Selector (from bridge_server.py)
    Saves signals to JSON file for MT5 EA to consume
    """
    logger.info(f"[API] POST /signal - Receiving signals from n8n at {datetime.now()}")
    
    try:
        signal_data = await request.json()
        
        if not signal_data or not isinstance(signal_data, list):
            logger.warning("[API] Invalid signal data received (not a list)")
            raise HTTPException(400, "Invalid or empty signal data")

        logger.info(f"[API] Received {len(signal_data)} signals from n8n")

        # Save to file for MT5 EA
        try:
            with open(SIGNAL_OUTPUT_PATH, "w") as f:
                json.dump(signal_data, f, indent=2)
            logger.info(f"[API] Saved {len(signal_data)} signals to {SIGNAL_OUTPUT_PATH}")
        except Exception as file_error:
            logger.error(f"[API ERROR] Failed to save signals to file: {str(file_error)}")
            raise HTTPException(500, "Failed to save signals to file")

        return JSONResponse(content={
            "status": "saved", 
            "signals": len(signal_data),
            "timestamp": datetime.now().isoformat()
        })
    
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /signal: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

