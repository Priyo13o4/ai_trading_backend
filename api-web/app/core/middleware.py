import os
import time
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

from app.main import (
    AUTH_CSRF_EXEMPT_PATHS,
    TRUST_PROXY_HEADERS,
    _request_id_from_request,
    _request_latency_ms_from_request,
)
from app.authn.csrf import enforce_csrf
from app.authn.session_store import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

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


async def csrf_middleware(request: Request, call_next):
    # Enforce CSRF for cookie-authenticated state-changing requests.
    try:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            request_path = request.url.path
            if request_path in AUTH_CSRF_EXEMPT_PATHS or request_path.startswith("/api/webhooks/"):
                pass
            else:
                if request.cookies.get(SESSION_COOKIE_NAME):
                    enforce_csrf(request, CSRF_COOKIE_NAME)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)

    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https:; script-src 'self' 'unsafe-inline' https:; connect-src 'self' https: wss:",
    )

    forwarded_proto = ""
    if TRUST_PROXY_HEADERS:
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    if is_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    return response
