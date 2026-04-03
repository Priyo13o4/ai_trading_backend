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

import logging
import os
import time
import asyncio
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .authn.session_store import SESSION_REDIS
from .authn.deps import require_session
from .cors_env import cors_origin_regex_from_env, parse_cors_origins_from_env
from .notifications.error_alerts import notify_runtime_error_event
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
TRUST_PROXY_HEADERS = (os.getenv("TRUST_PROXY_HEADERS") or "").strip().lower() in {"1", "true", "yes", "on"}


# ── CORS ─────────────────────────────────────────────────────────────────────────


# ── App ───────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="PipFactor SSE Service",
    description="Dedicated async Server-Sent Events service.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins_from_env(),
    allow_origin_regex=cors_origin_regex_from_env(),
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
    forwarded_proto = ""
    if TRUST_PROXY_HEADERS:
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


def _request_id_from_request(request: Request) -> str:
    from_state = (getattr(request.state, "request_id", "") or "").strip()
    if from_state:
        return from_state

    return (
        (request.headers.get("x-request-id") or "").strip()
        or (request.headers.get("x-correlation-id") or "").strip()
        or uuid.uuid4().hex[:12]
    )


def _request_latency_ms_from_request(request: Request) -> Optional[float]:
    raw_latency = getattr(request.state, "latency_ms", None)
    if raw_latency is not None:
        try:
            return max(0.0, round(float(raw_latency), 2))
        except Exception:
            pass

    start = getattr(request.state, "request_started_monotonic", None)
    if start is None:
        return None

    try:
        return max(0.0, round((time.perf_counter() - float(start)) * 1000.0, 2))
    except Exception:
        return None


def _runtime_error_context_from_request(request: Request) -> dict[str, Any]:
    client_ip = request.client.host if request.client else ""
    context = {
        "request_id": _request_id_from_request(request),
        "query": request.url.query,
        "client_ip": client_ip,
        "user_agent": (request.headers.get("user-agent") or "")[:200],
        "referer": (request.headers.get("referer") or "")[:200],
    }
    latency_ms = _request_latency_ms_from_request(request)
    if latency_ms is not None:
        context["latency_ms"] = latency_ms
    return context


async def _emit_runtime_error_alert(
    *,
    request: Request,
    error_id: str,
    request_id: str,
    status_code: int,
    message_safe: str,
    message_internal: str,
    extra_context: Optional[dict[str, Any]] = None,
) -> None:
    context = _runtime_error_context_from_request(request)
    if extra_context:
        context.update(extra_context)

    latency_ms = _request_latency_ms_from_request(request)

    try:
        await notify_runtime_error_event(
            error_id=error_id,
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            status_code=status_code,
            user_id=None,
            message_safe=message_safe,
            message_internal=message_internal,
            latency_ms=latency_ms,
            context=context,
        )
    except Exception as alert_exc:
        logger.error(
            "api-sse runtime error alert dispatch failed error_id=%s request_id=%s reason=%s",
            error_id,
            request_id,
            alert_exc,
        )


def _http_exception_message(detail: Any, *, fallback: str) -> str:
    if isinstance(detail, str):
        text = detail.strip()
        if text:
            return text[:220]

    if isinstance(detail, dict):
        candidate = detail.get("message")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:220]

    return fallback


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = _request_id_from_request(request)
    request.state.request_id = request_id
    request.state.request_started_monotonic = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        request.state.latency_ms = _request_latency_ms_from_request(request)
        raise

    request.state.latency_ms = _request_latency_ms_from_request(request)
    response.headers.setdefault("X-Request-ID", request_id)
    if request.state.latency_ms is not None:
        response.headers.setdefault("X-Response-Time-Ms", f"{request.state.latency_ms:.2f}")
    return response


@app.exception_handler(HTTPException)
async def global_http_exception_handler(request: Request, exc: HTTPException):
    status_code = int(getattr(exc, "status_code", 500) or 500)
    request_id = _request_id_from_request(request)
    if status_code < 500:
        message = _http_exception_message(exc.detail, fallback="Request could not be completed")
        return JSONResponse(
            status_code=status_code,
            content={
                "message": message,
                "detail": exc.detail,
            },
            headers=getattr(exc, "headers", None),
        )

    error_id = f"runtime-{uuid.uuid4().hex[:20]}"
    detail_text = str(getattr(exc, "detail", ""))[:800]
    message = "Internal server error"

    logger.warning(
        "api-sse runtime.http_exception.expected error_id=%s request_id=%s method=%s path=%s status=%s detail=%s",
        error_id,
        request_id,
        request.method,
        request.url.path,
        status_code,
        detail_text,
    )

    return JSONResponse(
        status_code=status_code,
        content={
            "message": message,
            "detail": message,
            "error_id": error_id,
            "request_id": request_id,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def global_unhandled_exception_handler(request: Request, exc: Exception):
    request_id = _request_id_from_request(request)
    error_id = f"runtime-{uuid.uuid4().hex[:20]}"
    message_internal = f"{exc.__class__.__name__}: {str(exc)[:800]}"
    message = "Internal server error"

    logger.error(
        "api-sse runtime.unhandled_exception error_id=%s request_id=%s method=%s path=%s",
        error_id,
        request_id,
        request.method,
        request.url.path,
        exc_info=True,
    )

    asyncio.create_task(
        _emit_runtime_error_alert(
            request=request,
            error_id=error_id,
            request_id=request_id,
            status_code=500,
            message_safe="Internal server error",
            message_internal=message_internal,
            extra_context={"exception_type": exc.__class__.__name__, "service_component": "api-sse"},
        )
    )

    return JSONResponse(
        status_code=500,
        content={
            "message": message,
            "detail": message,
            "error_id": error_id,
            "request_id": request_id,
        },
    )


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
