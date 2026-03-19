"""FastAPI dependency wrappers for session-based authentication.

These are the canonical, hardened auth dependencies for all routes in main.py.
Self-contained — does NOT import from routes.py to avoid circular imports.

Usage:
    from .authn.deps import require_session, optional_session

    @app.get("/my-protected-route")
    async def handler(ctx = Depends(require_session)):
        user_id = ctx["user_id"]
        plan    = ctx["plan"]
        perms   = ctx["permissions"]
"""

import hashlib
import hmac
import ipaddress
import logging
import os
from typing import Any

from fastapi import HTTPException, Request

from .session_store import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    delete_session,
    get_session,
    refresh_session_activity,
)

logger = logging.getLogger(__name__)

TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}


def _request_client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded = (request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _ip_prefix(ip_value: str) -> str:
    ip_raw = (ip_value or "").strip()
    if not ip_raw:
        return ""
    try:
        parsed = ipaddress.ip_address(ip_raw)
    except ValueError:
        return ""
    if isinstance(parsed, ipaddress.IPv4Address):
        parts = ip_raw.split(".")
        return ".".join(parts[:3]) + ".*" if len(parts) == 4 else ""
    return ":".join(parsed.exploded.split(":")[:4]) + ":*"


def _ua_hash(user_agent: str) -> str:
    normalized = (user_agent or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _device_matches(session: dict[str, Any], request: Request) -> bool:
    expected_ua = str(session.get("ua_hash") or "")
    expected_ip = str(session.get("ip_prefix") or "")
    if not expected_ua and not expected_ip:
        return True
    ua_ok = (not expected_ua) or hmac.compare_digest(
        _ua_hash(request.headers.get("user-agent") or ""), expected_ua
    )
    ip_ok = (not expected_ip) or hmac.compare_digest(
        _ip_prefix(_request_client_ip(request)), expected_ip
    )
    return ua_ok and ip_ok


async def require_session(request: Request) -> dict[str, Any]:
    """Require an active, device-bound session. Raises HTTP 401 if absent/invalid."""
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = await get_session(sid)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # 🛡️ CSRF PROTECTION: Enforce for all mutating methods (POST, PATCH, DELETE, etc.)
    # We use the 'Double Submit Cookie' pattern. The frontend must send the value
    # from the 'csrf_token' cookie in the 'X-CSRF-Token' header.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
        header_csrf = (
            request.headers.get("X-CSRF-Token")
            or request.headers.get("x-csrf-token")
            or ""
        )
        if not cookie_csrf or not header_csrf or not hmac.compare_digest(cookie_csrf, header_csrf):
            logger.warning(
                "auth.csrf_failure user_id=%s method=%s",
                session.get("user_id") or "unknown",
                request.method,
            )
            raise HTTPException(
                status_code=403, detail="CSRF validation failed. Please refresh the page."
            )

    if not _device_matches(session, request):
        await delete_session(sid)
        logger.warning(
            "auth.session.device_mismatch user_id=%s sid=%s",
            session.get("user_id") or "",
            sid,
        )
        raise HTTPException(status_code=401, detail="Session invalidated")

    refreshed = await refresh_session_activity(sid, session)
    if not refreshed:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return refreshed


async def optional_session(request: Request) -> dict[str, Any] | None:
    """Best-effort session check. Returns None when not authenticated. Never raises."""
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return None

    session = await get_session(sid)
    if not session:
        return None

    if not _device_matches(session, request):
        return None

    return await refresh_session_activity(sid, session)
