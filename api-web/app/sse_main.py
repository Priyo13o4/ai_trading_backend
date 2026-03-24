"""
Minimal FastAPI application for the dedicated SSE service (api-sse container).

This app mounts ONLY:
  - CORS middleware (same allowed origins as api-web)
  - Security headers middleware
  - Session auth dependency on all /api/stream/* routes
  - The SSE router

It does NOT include:
  - REST API routes (signals, news, strategies, etc.)
  - Background janitors (strategy expiry, session pruning)
  - Gunicorn multi-process logic — this runs pure Uvicorn

This means api-sse startup is instant and its entire event loop
is dedicated to holding SSE connections open.
"""

import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .authn.session_store import SESSION_REDIS
from .authn.deps import require_session
from .redis_cache import CACHE_REDIS
from .redis_pool import RedisPool
from .sse import router as sse_router, _sse_auth, startup_sse_resources, shutdown_sse_resources

logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── CORS ─────────────────────────────────────────────────────────────────────────

def _parse_cors_origins() -> list[str]:
    raw = (os.getenv("ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "https://pipfactor.com",
            "https://www.pipfactor.com",
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
    raw = (os.getenv("ALLOWED_ORIGIN_REGEX") or "").strip()
    return raw or r"^https://([a-zA-Z0-9_-]+\.)*(pipfactor\.com|pages\.dev)$"


# ── App ───────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="PipFactor SSE Service",
    description="Dedicated async Server-Sent Events service.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    if is_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# ── Wire auth into all SSE routes ─────────────────────────────────────────────────
# Override the _sse_auth sentinel with the real require_session dependency.
# All /api/stream/* endpoints declare `_ctx=Depends(_sse_auth)` so this
# single override enforces auth across every SSE stream.

app.dependency_overrides[_sse_auth] = require_session


# ── Mount SSE router ──────────────────────────────────────────────────────────────

app.include_router(sse_router)


# ── Health ────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    checks = {}

    app_redis_started_at = time.perf_counter()
    try:
        await CACHE_REDIS.ping()
        checks["redis_app"] = {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - app_redis_started_at) * 1000, 2),
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

    all_healthy = all(check.get("status") == "healthy" for check in checks.values())
    payload = {
        "status": "healthy" if all_healthy else "degraded",
        "service": "api-sse",
        "checks": checks,
    }
    if all_healthy:
        return payload
    return JSONResponse(status_code=503, content=payload)


@app.on_event("startup")
async def startup():
    logger.info("=" * 60)
    logger.info("api-sse SSE service starting (PID %s)", os.getpid())
    logger.info("=" * 60)
    await startup_sse_resources()


@app.on_event("shutdown")
async def shutdown():
    try:
        await shutdown_sse_resources()
    except Exception as exc:
        logger.error("api-sse SSE resource shutdown failed: %s", exc, exc_info=True)

    try:
        await RedisPool.close_all()
    except Exception as exc:
        logger.error("api-sse Redis pool shutdown failed: %s", exc, exc_info=True)

    try:
        await CACHE_REDIS.aclose()
    except Exception as exc:
        logger.error("api-sse cache redis shutdown failed: %s", exc, exc_info=True)

    try:
        await SESSION_REDIS.aclose()
    except Exception as exc:
        logger.error("api-sse session redis shutdown failed: %s", exc, exc_info=True)
