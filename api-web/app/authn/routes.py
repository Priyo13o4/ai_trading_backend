import logging
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from .csrf import generate_csrf_token
from .rate_limit_auth import rate_limit
from .session_store import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    delete_all_sessions_for_user,
    delete_session,
    get_cached_perms,
    get_session,
    invalidate_perms,
    set_cached_perms,
)
from .supabase_rpc import rpc_get_active_subscription
from .token_verify import verify_supabase_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")


def _should_use_secure_cookie(request: Request) -> bool:
    if not COOKIE_SECURE:
        return False

    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host in {"localhost", "127.0.0.1"}:
        return False

    # Prefer reverse-proxy hint; default to https for non-local hosts.
    xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if xf_proto:
        return xf_proto == "https"

    return True


def _set_cookie(request: Request, response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=(name == SESSION_COOKIE_NAME),
        secure=_should_use_secure_cookie(request),
        samesite=COOKIE_SAMESITE,
        path="/",
    )


@router.post("/exchange")
async def auth_exchange(request: Request, response: Response) -> dict[str, Any]:
    await rate_limit(request, "auth_exchange", limit_per_minute=20)

    body = await request.json()
    token = (body.get("access_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="access_token is required")

    claims = await verify_supabase_access_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    supabase_exp = int(claims.get("exp") or 0)
    now = int(time.time())
    if supabase_exp <= now:
        raise HTTPException(status_code=401, detail="Token expired")

    perms_cached = await get_cached_perms(user_id)
    if perms_cached:
        plan = perms_cached.get("plan") or "free"
        permissions = perms_cached.get("permissions") or []
    else:
        sub = await rpc_get_active_subscription(user_id)
        if sub and sub.get("is_current") and (sub.get("status") in ("active", "trial")):
            plan = sub.get("plan_name") or "unknown"
            permissions = ["dashboard", "signals"]
        else:
            plan = (sub.get("plan_name") if sub else None) or "free"
            permissions = ["dashboard"]

        await set_cached_perms(
            user_id,
            {
                "allowed": True,
                "plan": plan,
                "permissions": permissions,
                "updated_at": int(time.time()),
            },
        )

    created = await create_session(
        user_id=user_id,
        supabase_exp=supabase_exp,
        plan=plan,
        permissions=permissions,
    )

    csrf_token = generate_csrf_token()
    _set_cookie(request, response, SESSION_COOKIE_NAME, created["sid"], max_age=created["ttl"])
    _set_cookie(request, response, CSRF_COOKIE_NAME, csrf_token, max_age=created["ttl"])

    return {
        "ok": True,
        "user_id": user_id,
        "plan": plan,
        "permissions": permissions,
        "csrf_token": csrf_token,
        "expires_in": created["ttl"],
    }


@router.post("/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        await delete_session(sid)

    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/logout-all")
async def auth_logout_all(request: Request, response: Response) -> dict[str, Any]:
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = await get_session(sid)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    deleted = await delete_all_sessions_for_user(session["user_id"])
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"ok": True, "deleted": deleted}


@router.post("/validate")
async def auth_validate(request: Request) -> dict[str, Any]:
    await rate_limit(request, "auth_validate", limit_per_minute=120)

    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return {"allowed": False}

    session = await get_session(sid)
    if not session:
        return {"allowed": False}

    return {
        "allowed": True,
        "user_id": session.get("user_id"),
        "plan": session.get("plan"),
        "permissions": session.get("permissions", []),
    }


@router.post("/invalidate")
async def auth_invalidate_user(request: Request) -> dict[str, Any]:
    """Internal webhook target: invalidate a user's perms + all sessions."""
    await rate_limit(request, "auth_invalidate", limit_per_minute=60)

    secret = os.getenv("AUTH_INVALIDATION_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    provided = request.headers.get("x-webhook-secret")
    if provided != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = await request.json()
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    await invalidate_perms(user_id)
    deleted = await delete_all_sessions_for_user(user_id)
    logger.info("auth.invalidated user=%s sessions=%s", user_id, deleted)

    return {"ok": True, "deleted": deleted}
