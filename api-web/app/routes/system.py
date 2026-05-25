import os
import time
import asyncio
import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import POSTGRES_DSN, get_supabase_client, supabase_db
from app.auth import REDIS
from app.authn.session_store import SESSION_REDIS
from trading_common.symbols import get_active_symbols
from trading_common.market_data import SYMBOL_INFO

router = APIRouter()

@router.get("/api/symbols")
async def get_symbols():
    """
    Get list of active trading symbols with metadata.
    Frontend should call this on startup to dynamically populate symbol lists.
    """
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


async def _postgres_health_check() -> dict:
    started_at = time.perf_counter()

    def _probe():
        with psycopg.connect(POSTGRES_DSN, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

    try:
        await asyncio.to_thread(_probe)
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)[:160]}


async def _supabase_health_check() -> dict:
    project_url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip()
    service_key = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    if not project_url or not service_key:
        return {"status": "skipped", "reason": "not_configured"}

    started_at = time.perf_counter()
    try:
        supabase = get_supabase_client()
        await supabase_db(
            lambda: supabase.table("subscription_plans").select("id").limit(1).execute(),
            timeout=2.0,
        )
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)[:160]}


@router.get("/api/health")
async def health():
    checks = {}

    redis_started_at = time.perf_counter()
    try:
        await REDIS.ping()
        checks["redis_app"] = {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - redis_started_at) * 1000, 2),
        }
    except Exception as exc:
        checks["redis_app"] = {"status": "unhealthy", "error": str(exc)[:160]}

    session_started_at = time.perf_counter()
    try:
        await SESSION_REDIS.ping()
        checks["redis_session"] = {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - session_started_at) * 1000, 2),
        }
    except Exception as exc:
        checks["redis_session"] = {"status": "unhealthy", "error": str(exc)[:160]}

    checks["postgres"] = await _postgres_health_check()
    checks["supabase"] = await _supabase_health_check()

    required_checks = {
        name: check
        for name, check in checks.items()
        if check.get("status") != "skipped"
    }
    all_healthy = all(check.get("status") == "healthy" for check in required_checks.values())
    payload = {
        "status": "healthy" if all_healthy else "degraded",
        "version": "2.0.0",
        "checks": checks,
    }

    if all_healthy:
        return payload
    return JSONResponse(status_code=503, content=payload)
