import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_signals_context
from app.authn.deps import require_session
from app.authn.authz import require_permission
from app.db import AsyncSessionLocal, get_db, get_latest_signal_from_db, get_old_signal_from_db, get_strategies_all_from_db, get_strategy_by_id_from_db, get_active_strategies
from app.singleflight import singleflight_cache
from app.auth import REDIS
from app.utils import json_dumps
from app.utils import _normalize_optional_query_value, _strategy_cache_ttl, PREVIEW_SUPPORTED_PAIRS
from app.core.dependencies import _require_internal_api_key
from app.cache import publish_strategy_update, StrategyCache

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/api/signals/{pair}")
async def get_signal_latest(
    pair: str,
    ctx=Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Get latest active strategy for a trading pair (requires auth)"""
    logger.info(f"[API] GET /api/signals/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        require_permission(ctx, "signals")
        
        key = f"latest:signal:{pair.upper()}"
        
        # Try Redis cache first
        cached = await REDIS.get(key)
        if cached: 
            payload = json.loads(cached)
            if isinstance(payload, dict) and payload.get("_cache_status") == "NOT_FOUND":
                logger.info(f"[API] Cache HIT (negative) for signal: {pair}")
                raise HTTPException(404, f"No active strategy found for {pair}")
            logger.info(f"[API] Cache HIT for signal: {pair}")
            return JSONResponse(content=payload)

        # Cache miss - fetch from database
        logger.info(f"[API] Cache MISS for signal: {pair}, querying database")
        row = await get_latest_signal_from_db(db, pair)
        
        if not row: 
            logger.warning(f"[API] No active strategy found for pair: {pair}")
            await REDIS.setex(key, 60, json_dumps({"_cache_status": "NOT_FOUND"}))
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

@router.get("/api/preview/{pair}")
async def get_signal_preview(pair: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Get old strategy preview for main page (no auth required)"""
    logger.info(f"[API] GET /api/preview/{pair} - Public access")
    
    try:
        normalized_pair = pair.upper().strip()
        if normalized_pair not in PREVIEW_SUPPORTED_PAIRS:
            logger.warning(f"[API] Preview requested for unsupported pair: {pair}")
            supported = ", ".join(sorted(PREVIEW_SUPPORTED_PAIRS))
            raise HTTPException(404, f"Preview only available for: {supported}")
        
        key = f"preview:signal:v2:{normalized_pair}"
        response_headers = {"Cache-Control": "public, max-age=300"}
        
        # Try Redis cache
        cached = await REDIS.get(key)
        if cached: 
            payload = json.loads(cached)
            if isinstance(payload, dict) and payload.get("_cache_status") == "NOT_FOUND":
                logger.info(f"[API] Cache HIT (negative) for preview: {normalized_pair}")
                raise HTTPException(404, "No preview available")
            logger.info(f"[API] Cache HIT for preview: {normalized_pair}")
            return JSONResponse(content=payload, headers=response_headers)

        # Cache miss - get old signal
        logger.info(f"[API] Cache MISS for preview: {normalized_pair}, querying database")
        row = await get_old_signal_from_db(db, normalized_pair)
        
        if not row: 
            logger.warning(f"[API] No preview strategy found for pair: {normalized_pair}")
            await REDIS.setex(key, 60, json_dumps({"_cache_status": "NOT_FOUND"}))
            raise HTTPException(404, "No preview available")
        
        # Cache preview for 1 hour (it's old data)
        ttl = 60 * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached preview for {normalized_pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized), headers=response_headers)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/preview/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _strategies_all_key(*args, **kwargs):
    symbol = kwargs.get('symbol') or (args[2] if len(args) > 2 else None)
    direction = kwargs.get('direction') or (args[3] if len(args) > 3 else None)
    status = kwargs.get('status') or (args[4] if len(args) > 4 else None)
    search = kwargs.get('search') or (args[5] if len(args) > 5 else None)
    limit = kwargs.get('limit', 20)
    offset = kwargs.get('offset', 0)
    
    def _t(v):
        return str(v).lower().strip() if v else "none"
        
    return f"strategies:all:v3:{_t(symbol)}:{_t(direction)}:{_t(status)}:{_t(search)}:{limit}:{offset}"

@router.get("/api/strategies/all")
@singleflight_cache(key_builder=_strategies_all_key, ttl=3600)
async def get_strategies_all(
    request: Request,
    response: Response,
    symbol: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_signals_context),
    db: AsyncSession = Depends(get_db),
):
    """Get strategies with optional filters + pagination (requires signals permission)."""
    logger.info(
        "[API] GET /api/strategies/all - User: %s, symbol=%s, direction=%s, status=%s, search=%s, limit=%s, offset=%s",
        ctx.get("user_id", "anonymous"),
        symbol,
        direction,
        status,
        search,
        limit,
        offset,
    )

    try:
        normalized_symbol = _normalize_optional_query_value(symbol)
        cache_symbol = normalized_symbol.lower() if normalized_symbol else None

        normalized_direction = _normalize_optional_query_value(direction, lowercase=True)
        if normalized_direction and normalized_direction not in {"buy", "sell"}:
            raise HTTPException(422, "direction must be one of: buy, sell")

        normalized_status = _normalize_optional_query_value(status, lowercase=True)
        normalized_search = _normalize_optional_query_value(search)
        cache_search = normalized_search.lower() if normalized_search else None

        logger.info("[API] Fetching /api/strategies/all from database")
        rows, total = await get_strategies_all_from_db(
            db,
            normalized_symbol,
            normalized_direction,
            normalized_status,
            normalized_search,
            limit,
            offset,
        )

        if not rows:
            return {"strategies": [], "total": total, "limit": limit, "offset": offset, "_cache_status": "NOT_FOUND"}

        return {
            "strategies": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies/all: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@router.post("/api/strategies/publish")
async def publish_strategy_update_endpoint(request: Request):
    """Publish a strategy update from external automation (n8n)."""
    _require_internal_api_key(request, "N8N_STRATEGY_PUBLISH_KEY", "N8N_MARKET_DATA_KEY")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    if isinstance(payload, dict) and "strategy" in payload:
        payload = payload.get("strategy")

    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected strategy object payload")

    publish_strategy_update(payload)

    pair = payload.get("trading_pair") or payload.get("symbol") or payload.get("pair")
    try:
        async with AsyncSessionLocal() as db:
            strategies = await get_active_strategies(db, pair)
        StrategyCache.set(strategies, pair or "all")
    except Exception as exc:
        logger.warning("Failed to refresh strategies cache after publish: %s", exc)

    return {"status": "ok"}

@router.get("/api/strategies/{strategy_id}")
async def get_strategy_by_id(
    strategy_id: int,
    request: Request,
    response: Response,
    ctx=Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Get a single strategy by ID (requires signals permission)."""
    logger.info(f"[API] GET /api/strategies/{strategy_id} - User: {ctx.get('user_id', 'anonymous')}")

    try:
        require_permission(ctx, "signals")

        key = f"latest:strategy:id:{strategy_id}"
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for strategy id={strategy_id}")
            return JSONResponse(content=json.loads(cached))

        logger.info(f"[API] Cache MISS for strategy id={strategy_id}, querying database")
        row = await get_strategy_by_id_from_db(db, strategy_id)
        if not row:
            await REDIS.setex(key, 60, json_dumps({"_cache_status": "NOT_FOUND"}))
            raise HTTPException(404, f"Strategy {strategy_id} not found")

        ttl = _strategy_cache_ttl([row])
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        return JSONResponse(content=json.loads(serialized))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies/{strategy_id}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")
