import logging

from fastapi import HTTPException, Request, Response

from .redis_cache import CACHE_REDIS as REDIS
from .authn.session_store import SESSION_COOKIE_NAME, get_session

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


async def log_redis_connection_health():
    """Ping Redis and log auth/cache connectivity."""
    try:
        pong = await REDIS.ping()
        logger.info(
            "Redis cache reachable | pong=%s",
            pong,
        )
    except Exception as err:
        logger.error(
            "Redis cache unreachable: %s",
            err,
        )


async def auth_context(request: Request, response: Response):
    """Cookie + Redis session auth. No bearer tokens; no Supabase calls."""
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session = await get_session(sid)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return {
        "user_id": session.get("user_id"),
        "plan": session.get("plan"),
        "permissions": session.get("permissions", []),
    }


async def optional_auth_context(request: Request, response: Response):
    """Best-effort cookie + Redis session auth.

    Returns an anonymous context if not authenticated.
    """
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return {"user_id": None, "plan": "free", "permissions": []}

    session = await get_session(sid)
    if not session:
        return {"user_id": None, "plan": "free", "permissions": []}

    return {
        "user_id": session.get("user_id"),
        "plan": session.get("plan"),
        "permissions": session.get("permissions", []),
    }