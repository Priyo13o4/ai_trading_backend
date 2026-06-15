import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from trading_common.models import Strategy

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

def _signal_latest_key(pair: str, *args, **kwargs):
    return f"latest:signal:{pair.upper()}"

def _signal_latest_ttl(data):
    if hasattr(data, 'body'):
        import json
        payload = json.loads(data.body.decode('utf-8'))
        return int(payload.get("expiry_minutes", 30)) * 60
    elif isinstance(data, dict):
        return int(data.get("expiry_minutes", 30)) * 60
    return 30 * 60

@router.get("/api/signals/{pair}")
@singleflight_cache(key_builder=_signal_latest_key, ttl_func=_signal_latest_ttl)
async def get_signal_latest(
    pair: str,
    ctx=Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    """Get latest active strategy for a trading pair (requires auth)"""
    logger.info(f"[API] GET /api/signals/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        require_permission(ctx, "signals")
        
        # Cache miss - fetch from database
        logger.info(f"[API] Cache MISS/Bypass for signal: {pair}, querying database")
        row = await get_latest_signal_from_db(db, pair)
        
        if not row: 
            logger.warning(f"[API] No active strategy found for pair: {pair}")
            return {"_cache_status": "NOT_FOUND"}
        
        return row
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/signals/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

def _signal_preview_key(pair: str, *args, **kwargs):
    return f"preview:signal:v2:{pair.upper().strip()}"

@router.get("/api/preview/{pair}")
@singleflight_cache(key_builder=_signal_preview_key, ttl=3600)
async def get_signal_preview(pair: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Get old strategy preview for main page (no auth required)"""
    logger.info(f"[API] GET /api/preview/{pair} - Public access")
    
    try:
        normalized_pair = pair.upper().strip()
        if normalized_pair not in PREVIEW_SUPPORTED_PAIRS:
            logger.warning(f"[API] Preview requested for unsupported pair: {pair}")
            supported = ", ".join(sorted(PREVIEW_SUPPORTED_PAIRS))
            raise HTTPException(404, f"Preview only available for: {supported}")
        
        response.headers["Cache-Control"] = "public, max-age=300"
        
        # Cache miss - get old signal
        logger.info(f"[API] Cache MISS/Bypass for preview: {normalized_pair}, querying database")
        row = await get_old_signal_from_db(db, normalized_pair)
        
        if not row: 
            logger.warning(f"[API] No preview strategy found for pair: {normalized_pair}")
            return {"_cache_status": "NOT_FOUND"}
        
        return row
    
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

def _strategies_legacy_key(pair: Optional[str] = None, status: Optional[str] = None, include_historical: bool = False, limit: int = 20, offset: int = 0, *args, **kwargs):
    def _t(v):
        return str(v).lower().strip() if v else "none"
    return f"latest:strategies:legacy:v3:{_t(pair)}:{_t(status)}:{include_historical}:{limit}:{offset}"

@router.get("/api/strategies")
@singleflight_cache(key_builder=_strategies_legacy_key, ttl=3600)
async def get_strategies(
    request: Request,
    response: Response,
    pair: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    include_historical: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_signals_context),
    db: AsyncSession = Depends(get_db),
):
    """Get strategies (legacy frontend endpoint, returns active by default)."""
    logger.info(
        f"[API] GET /api/strategies - User: {ctx.get('user_id', 'anonymous')}, pair={pair}, status={status}"
    )

    try:
        if status == "all" or include_historical:
            rows, total = await get_strategies_all_from_db(
                db,
                symbol=pair,
                status=None if status == "all" else status,
                limit=limit,
                offset=offset,
            )
            return {"strategies": rows, "total": total}

        rows = await get_active_strategies(db, pair)
        return {"strategies": rows, "total": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


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
    strategy_id = payload.get("strategy_id")
    
    try:
        async with AsyncSessionLocal() as db:
            strategies = await get_active_strategies(db, pair)
        StrategyCache.set(strategies, pair or "all")
        
        from app.cache import invalidate_strategy_cache_domain
        if strategy_id:
            invalidate_strategy_cache_domain([int(strategy_id)])
        else:
            invalidate_strategy_cache_domain([])
            
    except Exception as exc:
        logger.warning("Failed to refresh strategies cache after publish: %s", exc)

    return {"status": "ok"}

def _strategy_by_id_key(strategy_id: int, *args, **kwargs):
    return f"latest:strategy:id:{strategy_id}"

def _strategy_by_id_ttl(data):
    if hasattr(data, 'body'):
        import json
        payload = json.loads(data.body.decode('utf-8'))
        return _strategy_cache_ttl([payload])
    elif isinstance(data, dict):
        return _strategy_cache_ttl([data])
    return 300

@router.get("/api/strategies/{strategy_id}")
@singleflight_cache(key_builder=_strategy_by_id_key, ttl_func=_strategy_by_id_ttl)
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

        logger.info(f"[API] Cache MISS/Bypass for strategy id={strategy_id}, querying database")
        row = await get_strategy_by_id_from_db(db, strategy_id)
        if not row:
            return {"_cache_status": "NOT_FOUND"}

        return row
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies/{strategy_id}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


class StrategyPushRequest(BaseModel):
    strategy_id: int


@router.post("/api/n8n/strategies/push")
async def n8n_push_strategy(request: Request, payload: StrategyPushRequest, db: AsyncSession = Depends(get_db)):
    """Webhook to trigger the push of an existing strategy to MT5."""
    # Authenticate the webhook request
    _require_internal_api_key(
        request,
        "N8N_MARKET_DATA_KEY"  # We can use the same key or a custom one
    )
    
    try:
        # Fetch the strategy from the database
        result = await db.execute(select(Strategy).where(Strategy.strategy_id == payload.strategy_id))
        strategy = result.scalar_one_or_none()
        
        if not strategy:
            raise HTTPException(status_code=404, detail=f"Strategy {payload.strategy_id} not found in database")
            
        logger.info(f"[API] Received strategy push request for ID: {strategy.strategy_id} ({strategy.strategy_name})")
        
        # Build a payload with ONLY the fields MT5 needs, omitting heavy text like detailed_analysis
        mt5_payload = {
            "strategy_id": strategy.strategy_id,
            "strategy_name": strategy.strategy_name,
            "symbol": strategy.symbol,
            "direction": strategy.direction,
            "take_profit": float(strategy.take_profit) if strategy.take_profit else 0,
            "stop_loss": float(strategy.stop_loss) if strategy.stop_loss else 0,
            "entry_signal": strategy.entry_signal,
            "confidence": strategy.confidence,
            "expiry_minutes": strategy.expiry_minutes if strategy.expiry_minutes else 240,
            "risk_reward_ratio": float(strategy.risk_reward_ratio) if strategy.risk_reward_ratio else 0,
            "timestamp": strategy.timestamp.isoformat() if strategy.timestamp else None,
            "expiry_time": strategy.expiry_time.isoformat() if strategy.expiry_time else None,
            "execution_allowed": bool(strategy.execution_allowed) if strategy.execution_allowed is not None else True,
            "trade_recommended": bool(strategy.trade_recommended) if strategy.trade_recommended is not None else True,
            "risk_level": strategy.risk_level,
            "trade_mode": strategy.trade_mode,
            "pre_entry_rule": strategy.pre_entry_rule,
            "post_entry_rule": strategy.post_entry_rule,
        }
        
        # Publish to Redis Pub/Sub for api-worker to pick up
        await REDIS.publish("mt5:strategy_push", json.dumps(mt5_payload))
        logger.info(f"[API] Broadcast strategy {strategy.strategy_id} to Redis pubsub")
        
        return {"status": "success", "strategy_id": strategy.strategy_id, "message": "Pushed to MT5 bridge"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing strategy push: {e}", exc_info=True)
        raise HTTPException(500, "Internal server error")
