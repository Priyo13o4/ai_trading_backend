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
import logging
import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request

from .session_store import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    delete_session,
    get_session,
    persist_session,
    refresh_session_activity,
)
from .supabase_rpc import rpc_get_active_subscription

logger = logging.getLogger(__name__)

TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}
SESSION_BINDING_MODE = (os.getenv("SESSION_BINDING_MODE") or "ua_only").strip().lower()
AUTHDBG_ENABLED = os.getenv("AUTHDBG_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
SESSION_ENTITLEMENT_SYNC_SECONDS = max(
    30,
    int(os.getenv("SESSION_ENTITLEMENT_SYNC_SECONDS", "120") or "120"),
)


def _rid(request: Request) -> str:
    return (
        (request.headers.get("x-request-id") or "").strip()
        or (request.headers.get("x-correlation-id") or "").strip()
        or uuid.uuid4().hex[:12]
    )


def _sid_hash(sid: str | None) -> str:
    if not sid:
        return "none"
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:10]


def _authdbg(message: str, *args: Any) -> None:
    if AUTHDBG_ENABLED:
        logger.info("AUTHDBG " + message, *args)


def _ua_hash(user_agent: str) -> str:
    normalized = (user_agent or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _device_matches(session: dict[str, Any], request: Request) -> bool:
    if SESSION_BINDING_MODE in {"off", "none", "disabled"}:
        return True

    expected_ua = str(session.get("ua_hash") or "")
    if not expected_ua:
        return True
    ua_ok = (not expected_ua) or hmac.compare_digest(
        _ua_hash(request.headers.get("user-agent") or ""), expected_ua
    )
    return ua_ok


def _derive_plan_and_permissions_from_subscription(sub: dict[str, Any] | None) -> tuple[str, list[str]]:
    if sub and sub.get("is_current") and (sub.get("status") in ("active", "trial")):
        return (sub.get("plan_name") or "free", ["dashboard", "signals"])
    return ("free", ["dashboard"])


def _should_resync_entitlements(session: dict[str, Any]) -> bool:
    now = int(time.time())
    last_synced_at = int(session.get("entitlements_synced_at") or 0)
    return (now - last_synced_at) >= SESSION_ENTITLEMENT_SYNC_SECONDS


async def require_session(request: Request) -> dict[str, Any]:
    """Require an active, device-bound session. Raises HTTP 401 if absent/invalid."""
    rid = _rid(request)
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    _authdbg(
        "event=session.require.start rid=%s path=%s method=%s sid_present=%s",
        rid,
        request.url.path,
        request.method,
        int(bool(sid)),
    )
    if not sid:
        _authdbg(
            "event=session.require.denied rid=%s reason=missing_sid path=%s method=%s",
            rid,
            request.url.path,
            request.method,
        )
        logger.debug("auth.require_session.missing_sid path=%s method=%s", request.url.path, request.method)
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = await get_session(sid)
    if not session:
        _authdbg(
            "event=session.require.denied rid=%s reason=sid_not_found sid=%s path=%s",
            rid,
            _sid_hash(sid),
            request.url.path,
        )
        logger.debug("auth.require_session.sid_not_found sid=%s path=%s", sid, request.url.path)
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
            _authdbg(
                "event=session.csrf rid=%s method=%s passed=0 cookie_present=%s header_present=%s",
                rid,
                request.method,
                int(bool(cookie_csrf)),
                int(bool(header_csrf)),
            )
            logger.warning(
                "auth.csrf_failure user_id=%s method=%s",
                session.get("user_id") or "unknown",
                request.method,
            )
            raise HTTPException(
                status_code=403, detail="CSRF validation failed. Please refresh the page."
            )
        _authdbg(
            "event=session.csrf rid=%s method=%s passed=1 cookie_present=%s header_present=%s",
            rid,
            request.method,
            int(bool(cookie_csrf)),
            int(bool(header_csrf)),
        )

    if not _device_matches(session, request):
        _authdbg(
            "event=session.bind rid=%s mode=%s passed=0 expected_ua=%s sid=%s",
            rid,
            SESSION_BINDING_MODE,
            int(bool(session.get("ua_hash"))),
            _sid_hash(sid),
        )
        await delete_session(sid)
        logger.warning(
            "auth.session.device_mismatch user_id=%s sid=%s mode=%s ua_present=%s",
            session.get("user_id") or "",
            sid,
            SESSION_BINDING_MODE,
            int(bool(session.get("ua_hash"))),
        )
        raise HTTPException(status_code=401, detail="Session invalidated")

    _authdbg(
        "event=session.bind rid=%s mode=%s passed=1 expected_ua=%s sid=%s",
        rid,
        SESSION_BINDING_MODE,
        int(bool(session.get("ua_hash"))),
        _sid_hash(sid),
    )

    refreshed = await refresh_session_activity(sid, session)
    if not refreshed:
        _authdbg(
            "event=session.refresh rid=%s result=failed sid=%s path=%s",
            rid,
            _sid_hash(sid),
            request.url.path,
        )
        logger.debug("auth.require_session.refresh_failed sid=%s path=%s", sid, request.url.path)
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Periodically re-sync entitlements from Supabase so long-lived sessions
    # naturally pick up trial/subscription expiry without requiring re-login.
    if _should_resync_entitlements(refreshed) and refreshed.get("user_id"):
        try:
            sub = await rpc_get_active_subscription(str(refreshed.get("user_id")))
            next_plan, next_permissions = _derive_plan_and_permissions_from_subscription(sub)
            has_drift = (
                str(refreshed.get("plan") or "free") != str(next_plan)
                or list(refreshed.get("permissions") or []) != list(next_permissions)
            )

            refreshed["entitlements_synced_at"] = int(time.time())
            if has_drift:
                refreshed["plan"] = next_plan
                refreshed["permissions"] = next_permissions

            persisted = await persist_session(sid, refreshed)
            if persisted:
                refreshed = persisted
        except Exception:
            logger.warning(
                "auth.session.entitlement_resync_failed sid=%s user_id=%s",
                sid,
                refreshed.get("user_id") or "",
                exc_info=True,
            )

    _authdbg(
        "event=session.refresh rid=%s result=ok sid=%s ttl_hint=%s user_tail=%s",
        rid,
        _sid_hash(sid),
        int(refreshed.get("exp") or 0),
        str(refreshed.get("user_id") or "")[-6:],
    )

    return refreshed


