import json
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.db import (
    AsyncSessionLocal, 
    get_db, 
    get_latest_news_from_db, 
    get_news_count, 
    get_news_by_id_from_db, 
    get_upcoming_news_from_db, 
    get_latest_weekly_macro_playbook_from_db, 
    get_economic_event_analysis_from_db,
    get_news_preview_from_db,
    get_strategies_all_from_db,
    get_latest_regime_from_db
)
from app.singleflight import singleflight_cache
from app.auth import REDIS
from app.authn.deps import require_session
from app.core.dependencies import require_signals_context
from app.utils import json_dumps
from app.core.dependencies import _require_internal_api_key
from app.cache import (
    NewsCache, 
    NewsMarkersCache,
    StrategyCache,
    publish_news_snapshot,
    publish_playbook_update,
    publish_event_analysis_update,
    invalidate_strategy_cache_domain,
    publish_strategies_snapshot,
    publish_regime_update
)

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/webhooks/n8n/news-updated")
async def handle_n8n_news_update(request: Request):
    """
    Webhook for n8n to call when new news/events are ingested.
    Clears related caches and triggers an SSE update.
    Requires X-API-Key header.
    """
    try:
        _require_internal_api_key(request, "N8N_MARKET_DATA_KEY")
    except HTTPException:
        logger.warning(f"[API] Invalid API key attempt for webhook from {request.client.host}")
        raise
        
    try:
        # Check for optional JSON body to determine which news type was updated
        news_type = "all"
        try:
            body = await request.json()
            news_type = body.get("type", "all").lower()
        except Exception:
            # Fallback to "all" if body is missing or invalid
            pass

        cursor = 0
        deleted_count = 0
        
        # Determine pattern based on type
        pattern = "latest:news:*"
        if news_type == "current": pattern = "latest:news:current:*"
        elif news_type == "playbook": pattern = "latest:news:playbook*"
        elif news_type == "events" or news_type == "event": pattern = "latest:news:events:*"

        # Clear Redis caches
        while True:
            cursor, keys = await REDIS.scan(cursor, match=pattern, count=100)
            if keys:
                await REDIS.delete(*keys)
                deleted_count += len(keys)
            if cursor == 0:
                break
                
        # Always clear the legacy global key
        await REDIS.delete("news_cache:all")
        
        updates_sent = []

        # Broadcast specific snapshots based on the updated type
        if news_type in ["current", "all"]:
            async with AsyncSessionLocal() as db:
                latest_news = await get_latest_news_from_db(db, 20, 0)
            if latest_news:
                NewsCache.set(latest_news, "all")
                publish_news_snapshot(latest_news)
                updates_sent.append("current_news")

        if news_type in ["playbook", "all"]:
            async with AsyncSessionLocal() as db:
                playbook = await get_latest_weekly_macro_playbook_from_db(db)
            if playbook:
                publish_playbook_update(playbook)
                updates_sent.append("weekly_playbook")

        if news_type in ["events", "event", "all"]:
            async with AsyncSessionLocal() as db:
                event_payload = await get_economic_event_analysis_from_db(db, 20, 0)
            events = event_payload.get("events") if isinstance(event_payload, dict) else None
            if events:
                publish_event_analysis_update(events)
                updates_sent.append("event_analysis")

        if news_type in ["strategies", "strategy", "all"]:
            invalidate_strategy_cache_domain([]) 
            
            async with AsyncSessionLocal() as db:
                latest_strategies, _ = await get_strategies_all_from_db(db, None)
            
            if latest_strategies:
                StrategyCache.set(latest_strategies, "all")
                publish_strategies_snapshot(latest_strategies)
                updates_sent.append("strategies_snapshot")

        if news_type in ["regime", "all"]:
            # Only clear the frontend-facing caches to preserve internal n8n pipeline caches
            await REDIS.delete("latest:regime")
            deleted_count += 1
            
            async with AsyncSessionLocal() as db:
                latest_regimes = await get_latest_regime_from_db(db)
            if latest_regimes:
                # Targeted deletion of pair-specific frontend caches (avoid wildcard regime:*)
                pair_keys = [f"regime:{r['trading_pair'].upper()}" for r in latest_regimes]
                if pair_keys:
                    await REDIS.delete(*pair_keys)
                    deleted_count += len(pair_keys)
                    
                for regime in latest_regimes:
                    publish_regime_update(regime)
                updates_sent.append("regime_updates")
            
        logger.info(f"[API] Webhook triggered ({news_type}): Cleared {deleted_count} caches, pushed: {', '.join(updates_sent)}")
        return {
            "status": "success", 
            "news_type": news_type,
            "cleared_count": deleted_count,
            "updates_pushed": updates_sent
        }
    except Exception as e:
        logger.error(f"[API ERROR] Webhook failed: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


def _current_news_key(*args, **kwargs):
    limit = kwargs.get('limit', 20)
    offset = kwargs.get('offset', 0)
    return f"current_news:v2:limit{limit}:offset{offset}"

@router.get("/api/news/current")
@singleflight_cache(key_builder=_current_news_key, ttl=300)
async def get_current_news(
    request: Request, 
    response: Response, 
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_session),
    db: AsyncSession = Depends(get_db)
):
    """Get current/recent high-impact forex news with pagination"""
    logger.info(f"[API] GET /api/news/current - User: {ctx.get('user_id', 'anonymous')}, limit={limit}, offset={offset}")
    
    try:
        logger.info(f"[API] Fetching current news from database (offset={offset})")
        rows = await get_latest_news_from_db(db, limit, offset)
        total = await get_news_count(db)
        
        if not rows:
            logger.info("[API] No current news found in database")
            return {"news": [], "total": total, "limit": limit, "offset": offset, "_cache_status": "NOT_FOUND"}
        
        result = {"news": rows, "total": total, "limit": limit, "offset": offset}
        
        if offset == 0:
            NewsCache.set(rows, "all")
            publish_news_snapshot(rows)
        
        return result
    
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/current: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _news_by_id_key(*args, **kwargs):
    item_id = kwargs.get('item_id')
    return f"singleflight:news_by_id:{item_id}"

@router.get("/api/news/{item_id:int}")
@singleflight_cache(key_builder=_news_by_id_key, ttl=3600)
async def get_news_by_id(item_id: int, request: Request, response: Response, ctx=Depends(require_session), db: AsyncSession = Depends(get_db)):
    """Fetch a specific news record securely with TTL caching"""
    logger.info(f"[API] GET /api/news/{item_id} - User: {ctx.get('user_id', 'anonymous')}")
    try:
        key = f"news:item:{item_id}"
        cached = await REDIS.get(key)
        if cached:
            payload = json.loads(cached)
            if isinstance(payload, dict) and payload.get("_cache_status") == "NOT_FOUND":
                logger.info(f"[API] Cache HIT (negative) for news ID {item_id}")
                raise HTTPException(404, "News item not found")
            logger.info(f"[API] Cache HIT for news ID {item_id}")
            return JSONResponse(content=payload)
            
        logger.info(f"[API] Cache MISS for news ID {item_id}")
        row = await get_news_by_id_from_db(db, item_id)
        
        if not row:
            await REDIS.setex(key, 60, json_dumps({"_cache_status": "NOT_FOUND"}))
            raise HTTPException(404, "News item not found")
            
        ttl = 60 * 60 # 60 minutes
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        
        return JSONResponse(content=json.loads(serialized))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/{item_id}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _news_upcoming_key(*args, **kwargs):
    return "latest:news:upcoming"

@router.get("/api/news/upcoming")
@singleflight_cache(key_builder=_news_upcoming_key, ttl=300)
async def get_upcoming_news(request: Request, response: Response, ctx=Depends(require_session), db: AsyncSession = Depends(get_db)):
    """Get upcoming high-impact forex events"""
    logger.info(f"[API] GET /api/news/upcoming - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        logger.info("[API] Fetching upcoming news from database")
        rows = await get_upcoming_news_from_db(db)
        normalized_rows = rows if isinstance(rows, list) else []
        
        if not normalized_rows:
            logger.info("[API] No upcoming news found in database")
            return {"_cache_status": "NOT_FOUND"}
            
        return normalized_rows
    
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/upcoming: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _news_playbook_key(*args, **kwargs):
    return "latest:news:playbook"

@router.get("/api/news/playbook")
@singleflight_cache(key_builder=_news_playbook_key, ttl=300)
async def get_news_playbook(request: Request, response: Response, ctx=Depends(require_session), db: AsyncSession = Depends(get_db)):
    """Get the latest weekly macro playbook (authenticated session required)."""
    logger.info(f"[API] GET /api/news/playbook - User: {ctx.get('user_id', 'anonymous')}")

    try:
        logger.info("[API] Fetching /api/news/playbook from database")
        row = await get_latest_weekly_macro_playbook_from_db(db)
        if not row:
            return {"_cache_status": "NOT_FOUND"}
            
        return {"playbook": [row]}
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/playbook: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _news_events_key(*args, **kwargs):
    upcoming_only = kwargs.get('upcoming_only', False)
    limit = kwargs.get('limit', 20)
    offset = kwargs.get('offset', 0)
    return f"latest:news:events:{int(upcoming_only)}:{limit}:{offset}"

@router.get("/api/news/events")
@singleflight_cache(key_builder=_news_events_key, ttl=300)
async def get_news_events(
    request: Request,
    response: Response,
    upcoming_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_session),
    db: AsyncSession = Depends(get_db)
):
    """Get economic event analysis rows with optional upcoming-only filter."""
    logger.info(
        "[API] GET /api/news/events - User: %s, upcoming_only=%s, limit=%s, offset=%s",
        ctx.get("user_id", "anonymous"),
        upcoming_only,
        limit,
        offset,
    )

    try:
        logger.info("[API] Fetching /api/news/events from database")
        row_data = await get_economic_event_analysis_from_db(
            db,
            limit,
            offset,
            upcoming_only,
        )
        rows, total = row_data["events"], row_data["total"]
        
        if not rows:
            return {"events": [], "total": total, "limit": limit, "offset": offset, "upcoming_only": upcoming_only, "_cache_status": "NOT_FOUND"}

        return {
            "events": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "upcoming_only": upcoming_only,
        }
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/events: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _news_markers_key(*args, **kwargs):
    symbol = kwargs.get('symbol') or (args[0] if args else None)
    hours = kwargs.get('hours') or 8760
    min_imp = kwargs.get('min_importance', 3)
    limit = kwargs.get('limit', 500)
    before = kwargs.get('before')
    
    if before is None and limit == 500:
        return f"news_markers:v2:{symbol}:{hours}h:imp{min_imp}"
    return None

@router.get("/api/news/markers/{symbol}")
@singleflight_cache(key_builder=_news_markers_key, ttl=300)
async def get_news_markers(
    symbol: str,
    hours: int = None,
    min_importance: int = 3,
    before: Optional[str] = Query(default=None, description="ISO UTC timestamp cursor for older pages"),
    limit: int = Query(default=500, ge=50, le=1000),
    ctx=Depends(require_signals_context),
    db: AsyncSession = Depends(get_db),
):
    """Get news markers for chart annotations
    
    Args:
        symbol: Trading pair (e.g., XAUUSD)
        hours: Time range in hours (default: None = all time)
        min_importance: Minimum importance score (1-5, default 3)
        before: Optional cursor. Returns rows strictly older than this timestamp.
        limit: Maximum rows returned.
    
    Returns:
        List of news events with timestamps for chart markers
    """
    symbol = symbol.upper()
    
    # If hours not specified, use large default (1 year)
    if hours is None:
        hours = 8760  # 365 days
    
    cursor_before: Optional[datetime] = None
    if before:
        try:
            normalized_before = before.strip()
            if normalized_before.endswith("Z"):
                normalized_before = f"{normalized_before[:-1]}+00:00"
            parsed_before = datetime.fromisoformat(normalized_before)
            if parsed_before.tzinfo is None:
                parsed_before = parsed_before.replace(tzinfo=timezone.utc)
            cursor_before = parsed_before.astimezone(timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'before' cursor. Use ISO UTC timestamp.")

    logger.info(
        "GET /api/news/markers/%s?hours=%s&min_importance=%s&before=%s&limit=%s - User: %s",
        symbol,
        hours,
        min_importance,
        before,
        limit,
        ctx.get("user_id", "anonymous"),
    )
    
    logger.info(f"Cache MISS/Bypass: news markers for {symbol}, querying database")
    
    try:
        # Calculate time range
        range_end = cursor_before or datetime.now(timezone.utc)
        start_time = range_end - timedelta(hours=hours)

        # Map symbol to instruments (handle different naming conventions)
        symbol_map = {
            'XAUUSD': ['XAU/USD', 'GOLD', 'XAUUSD'],
            'EURUSD': ['EUR/USD', 'EURUSD', 'EUR'],
            'GBPUSD': ['GBP/USD', 'GBPUSD', 'GBP'],
            'USDJPY': ['USD/JPY', 'USDJPY', 'JPY'],
            'USDCAD': ['USD/CAD', 'USDCAD', 'CAD'],
            'AUDUSD': ['AUD/USD', 'AUDUSD', 'AUD'],
        }

        instruments = symbol_map.get(symbol, [symbol])

        res = await db.execute(
            text("""
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
                    AND email_received_at >= :start_time
                    AND email_received_at < :range_end
                    AND importance_score >= :min_importance
                    AND (
                        primary_instrument = ANY(:instruments)
                        OR COALESCE(forex_instruments, '[]'::jsonb) ?| :instruments
                    )
                ORDER BY email_received_at DESC
                LIMIT :fetch_limit
            """),
            {
                "start_time": start_time,
                "range_end": range_end,
                "min_importance": min_importance,
                "instruments": instruments,
                "fetch_limit": limit + 1,
            },
        )
        news_items = [dict(r) for r in res.mappings().fetchall()]

        has_more = len(news_items) > limit
        if has_more:
            news_items = news_items[:limit]
        
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
        
        logger.info(f"Returning {len(markers)} news markers for {symbol}")
        return {
            "markers": markers,
            "has_more": has_more,
            "cursor_before": markers[-1]['time'] if markers else None,
        }
        
    except Exception as e:
        logger.error(f"Error fetching news markers for {symbol}: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to fetch news markers: {str(e)}")

def _news_preview_key(*args, **kwargs):
    return "preview:news:latest"

@router.get("/api/news/preview")
@singleflight_cache(key_builder=_news_preview_key, ttl=1800)
async def get_news_preview(request: Request, db: AsyncSession = Depends(get_db)):
    """Get latest high-impact news item for landing page (no auth required)."""
    logger.info("[API] GET /api/news/preview - Public access")

    try:
        logger.info("[API] Cache MISS/Bypass for news preview, querying database")
        row = await get_news_preview_from_db(db)

        if not row:
            logger.warning("[API] No high-impact news found for preview")
            return {"_cache_status": "NOT_FOUND"}

        return row

    except Exception as e:
        logger.error(f"[API ERROR] /api/news/preview: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")
