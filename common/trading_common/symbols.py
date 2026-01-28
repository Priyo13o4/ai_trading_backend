"""Active symbol discovery (DB as source of truth, Redis as shared cache).

Design:
- DB is authoritative: symbols come from distinct `candlesticks.symbol`.
- Redis provides a shared, short-lived cache to avoid repeated DB lookups.
- ENV can override (for controlled deployments / debugging).

This module offers both sync and async entrypoints for:
- FastAPI async services (mt5_ingest, routes)
- Standalone scripts (indicators)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Iterable, Optional

import psycopg
import redis

logger = logging.getLogger(__name__)


SYMBOLS_ACTIVE_KEY = os.getenv("SYMBOLS_ACTIVE_REDIS_KEY", "symbols:active")
SYMBOLS_ACTIVE_TTL_SECONDS = int(os.getenv("SYMBOLS_ACTIVE_TTL_SECONDS", "60"))


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    """Normalize and deduplicate symbol list."""
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        if not s:
            continue
        sym = str(s).strip().upper()
        if not sym:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _env_override_symbols() -> list[str]:
    """Check for environment variable symbol overrides."""
    allow = (os.getenv("SYMBOLS_ALLOW_ENV_OVERRIDE") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    if not allow:
        return []
    # Precedence order: explicit overrides for different subsystems.
    for name in ("ACTIVE_SYMBOLS", "AGG_SYMBOLS", "MT5_SUBSCRIBE_SYMBOLS"):
        raw = (os.getenv(name) or "").strip()
        if raw:
            return _normalize_symbols(raw.split(","))
    return []


def _discover_symbols_from_db_sync(postgres_dsn: str) -> list[str]:
    """Fetch symbols from database."""
    try:
        with psycopg.connect(postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT symbol FROM candlesticks WHERE symbol IS NOT NULL AND symbol <> '' ORDER BY symbol"
                )
                return _normalize_symbols([r[0] for r in cur.fetchall() if r and r[0]])
    except Exception as e:
        logger.warning("[symbols] DB discovery failed; err=%s", e)
        return []


def _read_symbols_from_redis_sync(redis_client: redis.Redis) -> list[str]:
    """Read symbols from Redis cache."""
    try:
        t = redis_client.type(SYMBOLS_ACTIVE_KEY)
        if t == "set":
            members = redis_client.smembers(SYMBOLS_ACTIVE_KEY) or set()
            return _normalize_symbols(members)
        if t == "string":
            raw = redis_client.get(SYMBOLS_ACTIVE_KEY)
            if not raw:
                return []
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return _normalize_symbols(data)
            except Exception:
                return []
        return []
    except Exception:
        return []


def _write_symbols_to_redis_sync(redis_client: redis.Redis, symbols: list[str], ttl_seconds: int) -> None:
    """Write symbols to Redis cache."""
    try:
        pipe = redis_client.pipeline()
        pipe.delete(SYMBOLS_ACTIVE_KEY)
        if symbols:
            pipe.sadd(SYMBOLS_ACTIVE_KEY, *symbols)
        pipe.expire(SYMBOLS_ACTIVE_KEY, int(ttl_seconds))
        pipe.execute()
    except Exception as e:
        logger.debug("[symbols] Redis refresh failed (non-fatal): %s", e)


def refresh_active_symbols_sync(
    redis_client: redis.Redis,
    postgres_dsn: str,
    ttl_seconds: Optional[int] = None,
) -> list[str]:
    """Refresh Redis symbols cache from DB and return the normalized list."""
    ttl = int(ttl_seconds or SYMBOLS_ACTIVE_TTL_SECONDS)
    syms = _discover_symbols_from_db_sync(postgres_dsn)
    if syms:
        _write_symbols_to_redis_sync(redis_client, syms, ttl_seconds=ttl)
    return syms


def get_active_symbols_sync(
    redis_client: redis.Redis,
    postgres_dsn: str,
    ttl_seconds: Optional[int] = None,
    fallback: Optional[list[str]] = None,
) -> list[str]:
    """
    Get active symbols.

    Precedence:
    1) ENV override
    2) Redis cache (symbols:active)
    3) DB discovery + refresh Redis
    4) fallback
    """
    override = _env_override_symbols()
    if override:
        return override

    cached = _read_symbols_from_redis_sync(redis_client)
    if cached:
        return cached

    syms = refresh_active_symbols_sync(
        redis_client=redis_client,
        postgres_dsn=postgres_dsn,
        ttl_seconds=ttl_seconds,
    )
    if syms:
        return syms

    return _normalize_symbols(fallback or [])


async def get_active_symbols(
    redis_client: redis.Redis,
    postgres_dsn: str,
    ttl_seconds: Optional[int] = None,
    fallback: Optional[list[str]] = None,
) -> list[str]:
    """Async version of get_active_symbols_sync."""
    return await asyncio.to_thread(
        get_active_symbols_sync,
        redis_client=redis_client,
        postgres_dsn=postgres_dsn,
        ttl_seconds=ttl_seconds,
        fallback=fallback,
    )


async def refresh_active_symbols(
    redis_client: redis.Redis,
    postgres_dsn: str,
    ttl_seconds: Optional[int] = None,
) -> list[str]:
    """Async version of refresh_active_symbols_sync."""
    return await asyncio.to_thread(
        refresh_active_symbols_sync,
        redis_client=redis_client,
        postgres_dsn=postgres_dsn,
        ttl_seconds=ttl_seconds,
    )
