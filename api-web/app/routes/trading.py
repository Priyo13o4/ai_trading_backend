import json
import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.singleflight import singleflight_cache
from app.authn.deps import require_session
from app.db import get_db, get_pair_performance, insert_trade_outcome, update_trade_outcome
from app.utils import json_dumps
from app.core.dependencies import _require_internal_api_key

logger = logging.getLogger(__name__)

router = APIRouter()

def performance_key_builder(func, *args, **kwargs):
    pair = kwargs.get('pair')
    if not pair and args:
        pair = args[0]
    return f"performance:{pair}"

@router.get("/api/performance/{pair}")
@singleflight_cache(ttl=300, key_builder=performance_key_builder)
async def get_performance(pair: str, ctx=Depends(require_session), db: AsyncSession = Depends(get_db)):
    """Get performance metrics for a trading pair"""
    logger.info(f"[API] GET /api/performance/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        metrics = await get_pair_performance(db, pair)
        if not metrics:
            logger.warning(f"[API] No performance data found for pair: {pair}")
            return JSONResponse(content={"message": f"No trade history for {pair}"})
        
        logger.info(f"[API] Performance for {pair}: {metrics.get('total_trades')} trades")
        serialized = json_dumps(metrics)
        return JSONResponse(content=json.loads(serialized))
    except Exception as e:
        logger.error(f"[API ERROR] /api/performance/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@router.post("/api/trades/outcome")
async def record_trade_outcome(request: Request, db: AsyncSession = Depends(get_db)):
    """Record MT5 trade execution (called by EA when opening position)"""
    auth_source = _require_internal_api_key(
        request,
        "MT5_TRADE_WEBHOOK_KEY",
        "N8N_MARKET_DATA_KEY",
    )
    logger.info("[API] POST /api/trades/outcome - Internal auth=verified")
    
    try:
        trade_data = await request.json()
        logger.info(f"[API] Recording trade outcome for ticket: {trade_data.get('ticket')}")
        
        result = await insert_trade_outcome(db, trade_data)
        logger.info(f"[API] Trade recorded with signal_id: {result['signal_id']}")
        
        return JSONResponse(content={"signal_id": result['signal_id'], "status": "recorded"})
    except Exception as e:
        logger.error(f"[API ERROR] /api/trades/outcome: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@router.put("/api/trades/{ticket}/close")
async def close_trade(ticket: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Update trade when closed in MT5 (records P/L, exit price, outcome)"""
    auth_source = _require_internal_api_key(
        request,
        "MT5_TRADE_WEBHOOK_KEY",
        "N8N_MARKET_DATA_KEY",
    )
    logger.info("[API] PUT /api/trades/%s/close - Internal auth=verified", ticket)
    
    try:
        outcome_data = await request.json()
        logger.info(f"[API] Closing trade {ticket} with P/L: {outcome_data.get('pnl')}")
        
        result = await update_trade_outcome(db, ticket, outcome_data)
        
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
