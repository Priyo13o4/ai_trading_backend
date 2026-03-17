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


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r. Falling back to secure default=%s.",
            name,
            raw,
            default,
        )
        return default

    if value < minimum:
        logger.warning(
            "Invalid %s=%r (must be >= %s). Falling back to secure default=%s.",
            name,
            raw,
            minimum,
            default,
        )
        return default

    return value


SERVER_SESSION_MAX_TTL = _env_int("SERVER_SESSION_MAX_TTL", 86400, minimum=1)  # 24h cap
SERVER_SESSION_REMEMBER_MAX_TTL = _env_int("SERVER_SESSION_REMEMBER_MAX_TTL", 30 * 24 * 3600, minimum=1)  # 30d cap
SERVER_SESSION_MAX_PER_USER = _env_int("SERVER_SESSION_MAX_PER_USER", 5, minimum=1)
PERMS_CACHE_TTL_SECONDS = _env_int("PERMS_CACHE_TTL_SECONDS", 900, minimum=1)  # 15m

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


def _user_sessions_index_key(user_id: str) -> str:
    return f"user_sessions_idx:{user_id}"


def _perms_key(user_id: str) -> str:
    return f"user:perms:{user_id}"


def _session_cap_ttl_seconds(remember_me: bool) -> int:
    return SERVER_SESSION_REMEMBER_MAX_TTL if remember_me else SERVER_SESSION_MAX_TTL


def _compute_ttl_seconds(*, now: int, supabase_exp: int, remember_me: bool) -> int:
    supabase_remaining = int(supabase_exp) - now
    selected_cap = _session_cap_ttl_seconds(bool(remember_me))
    return max(1, min(supabase_remaining, selected_cap))


def _extract_sid_from_session_key(key: str) -> Optional[str]:
    prefix = "session:"
    if not key.startswith(prefix):
        return None
    sid = key[len(prefix) :]
    return sid or None


def _session_is_for_user(raw: str, user_id: str, now: int) -> bool:
    try:
        data = json.loads(raw)
    except Exception:
        return False

    if str(data.get("user_id") or "") != str(user_id):
        return False

    exp = int(data.get("exp") or 0)
    return not exp or exp >= now


async def _scan_active_session_ids_for_user(user_id: str) -> set[str]:
    """Fallback scan to avoid missed invalidation when session indexes drift or expire."""
    now = int(time.time())
    cursor = 0
    found: set[str] = set()

    while True:
        cursor, keys = await SESSION_REDIS.scan(cursor=cursor, match="session:*", count=500)
        if keys:
            values = await SESSION_REDIS.mget(keys)
            for key, raw in zip(keys, values):
                if not raw:
                    continue
                if not _session_is_for_user(raw, user_id, now):
                    continue

                sid = _extract_sid_from_session_key(key)
                if sid:
                    found.add(sid)

        if cursor == 0:
            break

    return found


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


async def _evict_user_overflow_sessions(user_id: str) -> int:
    try:
        total = int(await SESSION_REDIS.zcard(_user_sessions_index_key(user_id)))
    except Exception:
        logger.warning(
            "auth.session.evict event=evict user_id=%s reason=index-read-failed",
            user_id,
            exc_info=True,
        )
        return 0

    overflow = total - SERVER_SESSION_MAX_PER_USER
    if overflow <= 0:
        return 0

    evicted = 0
    try:
        oldest_sids = await SESSION_REDIS.zrange(_user_sessions_index_key(user_id), 0, overflow - 1)
    except Exception:
        logger.warning(
            "auth.session.evict event=evict user_id=%s reason=index-range-failed overflow=%s",
            user_id,
            overflow,
            exc_info=True,
        )
        return 0

    for sid in oldest_sids:
        await SESSION_REDIS.delete(_session_key(sid))
        pipe = SESSION_REDIS.pipeline()
        pipe.srem(_user_sessions_key(user_id), sid)
        pipe.zrem(_user_sessions_index_key(user_id), sid)
        await pipe.execute()
        evicted += 1
        logger.info("auth.session.evict event=evict user_id=%s sid=%s reason=cap", user_id, sid)

    return evicted


async def create_session(
    *,
    user_id: str,
    supabase_exp: int,
    plan: str,
    permissions: list[str],
    remember_me: bool = False,
    ua_hash: str | None = None,
    ip_prefix: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    ttl = _compute_ttl_seconds(now=now, supabase_exp=int(supabase_exp), remember_me=bool(remember_me))
    exp = now + ttl

    sid = str(uuid.uuid4())
    session = {
        "ver": 1,
        "user_id": user_id,
        "plan": plan,
        "permissions": permissions,
        "iat": now,
        "last_activity": now,
        "exp": exp,
        "supabase_exp": int(supabase_exp),
        "remember_me": bool(remember_me),
        "ua_hash": ua_hash or "",
        "ip_prefix": ip_prefix or "",
    }

    key = _session_key(sid)
    pipe = SESSION_REDIS.pipeline()
    pipe.setex(key, ttl, json.dumps(session))
    # Keep legacy set writes for backward compatibility with existing keys.
    pipe.sadd(_user_sessions_key(user_id), sid)
    # Primary index: expiry-sorted set for robust membership + cleanup.
    pipe.zadd(_user_sessions_index_key(user_id), {sid: exp})
    await pipe.execute()
    evicted = await _evict_user_overflow_sessions(user_id)

    logger.info(
        "auth.session.created event=create user_id=%s sid=%s ttl=%s remember=%s evicted=%s",
        user_id,
        sid,
        ttl,
        int(bool(remember_me)),
        evicted,
    )
    return {"sid": sid, "ttl": ttl, "session": session}


async def delete_session(sid: str) -> None:
    data = await get_session(sid)
    if data and data.get("user_id"):
        user_id = data["user_id"]
        pipe = SESSION_REDIS.pipeline()
        pipe.srem(_user_sessions_key(user_id), sid)
        pipe.zrem(_user_sessions_index_key(user_id), sid)
        await pipe.execute()
    await SESSION_REDIS.delete(_session_key(sid))


async def refresh_session_activity(sid: str, session: dict[str, Any]) -> Optional[dict[str, Any]]:
    now = int(time.time())
    supabase_exp = int(session.get("supabase_exp") or 0)
    remember_me = bool(session.get("remember_me"))

    if supabase_exp <= now:
        await delete_session(sid)
        logger.info(
            "auth.session.refresh event=refresh user_id=%s sid=%s status=expired",
            session.get("user_id") or "",
            sid,
        )
        return None

    ttl = _compute_ttl_seconds(now=now, supabase_exp=supabase_exp, remember_me=remember_me)
    exp = now + ttl

    updated = dict(session)
    updated["last_activity"] = now
    updated["exp"] = exp

    pipe = SESSION_REDIS.pipeline()
    pipe.setex(_session_key(sid), ttl, json.dumps(updated))
    if updated.get("user_id"):
        pipe.zadd(_user_sessions_index_key(updated["user_id"]), {sid: exp})
    await pipe.execute()

    logger.info(
        "auth.session.refresh event=refresh user_id=%s sid=%s ttl=%s remember=%s",
        updated.get("user_id") or "",
        sid,
        ttl,
        int(remember_me),
    )
    return updated


async def delete_all_sessions_for_user(user_id: str) -> int:
    now = int(time.time())
    try:
        legacy_sids = await SESSION_REDIS.smembers(_user_sessions_key(user_id))
    except Exception:
        logger.warning("auth.session.invalidate legacy-index-read-failed user=%s", user_id, exc_info=True)
        legacy_sids = set()

    try:
        indexed_sids = await SESSION_REDIS.zrangebyscore(_user_sessions_index_key(user_id), now, "+inf")
    except Exception:
        logger.warning("auth.session.invalidate sorted-index-read-failed user=%s", user_id, exc_info=True)
        indexed_sids = []

    sids = set(legacy_sids or set()) | set(indexed_sids or [])
    if not sids:
        # SCAN is a safety net for index drift/expiry, not the default path.
        sids |= await _scan_active_session_ids_for_user(user_id)

    if not sids:
        pipe = SESSION_REDIS.pipeline()
        pipe.delete(_user_sessions_key(user_id))
        pipe.delete(_user_sessions_index_key(user_id))
        pipe.zremrangebyscore(_user_sessions_index_key(user_id), "-inf", now)
        await pipe.execute()
        return 0

    pipe = SESSION_REDIS.pipeline()
    for sid in sids:
        pipe.delete(_session_key(sid))
    pipe.delete(_user_sessions_key(user_id))
    pipe.delete(_user_sessions_index_key(user_id))
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


async def put_replay_guard_once(key: str, ttl_seconds: int) -> bool:
    """Store key once with TTL; returns False when replay key already exists."""
    ttl = max(1, int(ttl_seconds))
    result = await SESSION_REDIS.set(key, "1", ex=ttl, nx=True)
    return bool(result)


def _chunked(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]


async def prune_stale_session_indexes_scan(
    *,
    cursor: int = 0,
    user_scan_count: int = 100,
    sid_probe_batch_size: int = 500,
) -> tuple[int, dict[str, int]]:
    """Prune stale sid references from user session index keys.

    This is safe to run periodically in production:
    - removes only sid members whose corresponding session:<sid> key no longer exists
    - keeps active session keys untouched
    - processes user index keys incrementally via SCAN cursor
    """
    safe_user_scan_count = max(1, int(user_scan_count or 100))
    safe_sid_probe_batch_size = max(1, int(sid_probe_batch_size or 500))

    try:
        next_cursor, user_keys = await SESSION_REDIS.scan(
            cursor=int(cursor or 0),
            match="user_sessions:*",
            count=safe_user_scan_count,
        )
    except Exception:
        logger.warning("auth.session.prune scan-failed", exc_info=True)
        return 0, {
            "users_scanned": 0,
            "stale_removed": 0,
            "users_pruned": 0,
            "errors": 1,
        }

    users_scanned = 0
    stale_removed = 0
    users_pruned = 0
    errors = 0

    for set_key in user_keys:
        users_scanned += 1
        try:
            user_id = str(set_key).split(":", 1)[1]
            zset_key = _user_sessions_index_key(user_id)

            legacy_sids = await SESSION_REDIS.smembers(set_key)
            indexed_sids = await SESSION_REDIS.zrange(zset_key, 0, -1)
            candidate_sids = sorted(set(legacy_sids or set()) | set(indexed_sids or []))

            if not candidate_sids:
                # Clean empty shells to keep keyspace tidy.
                await SESSION_REDIS.delete(set_key)
                await SESSION_REDIS.delete(zset_key)
                continue

            active_sids: set[str] = set()
            for sid_batch in _chunked(candidate_sids, safe_sid_probe_batch_size):
                session_keys = [_session_key(sid) for sid in sid_batch]
                values = await SESSION_REDIS.mget(session_keys)
                for sid, raw in zip(sid_batch, values):
                    if raw:
                        active_sids.add(sid)

            stale_sids = [sid for sid in candidate_sids if sid not in active_sids]
            if stale_sids:
                pipe = SESSION_REDIS.pipeline()
                pipe.srem(set_key, *stale_sids)
                pipe.zrem(zset_key, *stale_sids)
                await pipe.execute()
                stale_removed += len(stale_sids)
                users_pruned += 1

            # Remove empty index keys after pruning.
            remaining_set = await SESSION_REDIS.scard(set_key)
            remaining_zset = await SESSION_REDIS.zcard(zset_key)
            if remaining_set == 0 and remaining_zset == 0:
                await SESSION_REDIS.delete(set_key)
                await SESSION_REDIS.delete(zset_key)
        except Exception:
            errors += 1
            logger.warning("auth.session.prune user-failed key=%s", set_key, exc_info=True)

    return int(next_cursor), {
        "users_scanned": users_scanned,
        "stale_removed": stale_removed,
        "users_pruned": users_pruned,
        "errors": errors,
    }
