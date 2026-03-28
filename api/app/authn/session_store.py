import json
import logging
import os
import time
import uuid
from typing import Any, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "session")
CSRF_COOKIE_NAME = os.getenv("CSRF_COOKIE_NAME", "csrf_token")

SERVER_SESSION_MAX_TTL = int(os.getenv("SERVER_SESSION_MAX_TTL", "86400"))  # 24h cap
PERMS_CACHE_TTL_SECONDS = int(os.getenv("PERMS_CACHE_TTL_SECONDS", "900"))  # 15m

SESSION_REDIS_URL = os.getenv("SESSION_REDIS_URL")
if not SESSION_REDIS_URL:
    _host = os.getenv("SESSION_REDIS_HOST")
    _port = os.getenv("SESSION_REDIS_PORT")
    _db = os.getenv("SESSION_REDIS_DB", "0")
    _pwd = os.getenv("SESSION_REDIS_PASSWORD")
    if _host and _port and _pwd is not None:
        SESSION_REDIS_URL = f"redis://:{_pwd}@{_host}:{_port}/{_db}"

if not SESSION_REDIS_URL:
    raise RuntimeError("SESSION_REDIS_URL (or SESSION_REDIS_HOST/PORT/PASSWORD) is required")

SESSION_REDIS = aioredis.from_url(SESSION_REDIS_URL, decode_responses=True)


def _session_key(sid: str) -> str:
    return f"session:{sid}"


def _user_sessions_key(user_id: str) -> str:
    return f"user_sessions:{user_id}"


def _perms_key(user_id: str) -> str:
    return f"user:perms:{user_id}"


async def get_session(sid: str) -> Optional[dict[str, Any]]:
    raw = await SESSION_REDIS.get(_session_key(sid))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None

    now = int(time.time())
    exp = int(data.get("exp") or 0)
    if exp and exp < now:
        return None
    return data


async def create_session(*, user_id: str, supabase_exp: int, plan: str, permissions: list[str]) -> dict[str, Any]:
    now = int(time.time())
    ttl = max(1, min(int(supabase_exp) - now, SERVER_SESSION_MAX_TTL))
    exp = now + ttl

    sid = str(uuid.uuid4())
    session = {
        "ver": 1,
        "user_id": user_id,
        "plan": plan,
        "permissions": permissions,
        "iat": now,
        "exp": exp,
        "supabase_exp": int(supabase_exp),
    }

    key = _session_key(sid)
    pipe = SESSION_REDIS.pipeline()
    pipe.setex(key, ttl, json.dumps(session))
    pipe.sadd(_user_sessions_key(user_id), sid)
    pipe.expire(_user_sessions_key(user_id), ttl)
    await pipe.execute()

    logger.info("auth.session.created user=%s ttl=%s", user_id, ttl)
    return {"sid": sid, "ttl": ttl, "session": session}


async def delete_session(sid: str) -> None:
    data = await get_session(sid)
    if data and data.get("user_id"):
        await SESSION_REDIS.srem(_user_sessions_key(data["user_id"]), sid)
    await SESSION_REDIS.delete(_session_key(sid))


async def delete_all_sessions_for_user(user_id: str) -> int:
    sids = await SESSION_REDIS.smembers(_user_sessions_key(user_id))
    if not sids:
        await SESSION_REDIS.delete(_user_sessions_key(user_id))
        return 0

    pipe = SESSION_REDIS.pipeline()
    for sid in sids:
        pipe.delete(_session_key(sid))
    pipe.delete(_user_sessions_key(user_id))
    await pipe.execute()
    return len(sids)


async def get_cached_perms(user_id: str) -> Optional[dict[str, Any]]:
    raw = await SESSION_REDIS.get(_perms_key(user_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def set_cached_perms(user_id: str, data: dict[str, Any], ttl_seconds: int | None = None) -> None:
    ttl = int(ttl_seconds or PERMS_CACHE_TTL_SECONDS)
    await SESSION_REDIS.setex(_perms_key(user_id), ttl, json.dumps(data))


async def invalidate_perms(user_id: str) -> None:
    await SESSION_REDIS.delete(_perms_key(user_id))
