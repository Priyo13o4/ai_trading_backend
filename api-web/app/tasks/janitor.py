import asyncio
import logging
import os

from app.db import AsyncSessionLocal, expire_elapsed_strategies_batch
from app.cache import invalidate_strategy_cache_domain
from app.authn.session_store import prune_stale_session_indexes_scan

logger = logging.getLogger(__name__)

def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except Exception:
            value = default
    if value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value

STRATEGY_EXPIRY_JANITOR_BATCH_SIZE = _env_int("STRATEGY_EXPIRY_JANITOR_BATCH_SIZE", 1000, minimum=1, maximum=5000)
STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK = _env_int("STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK", 10, minimum=1, maximum=50)
STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS = _env_int("STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS", 30, minimum=1, maximum=3600)

SESSION_INDEX_PRUNE_INTERVAL_SECONDS = _env_int("SESSION_INDEX_PRUNE_INTERVAL_SECONDS", 300, minimum=5, maximum=86400)
SESSION_INDEX_PRUNE_USER_SCAN_COUNT = _env_int("SESSION_INDEX_PRUNE_USER_SCAN_COUNT", 100, minimum=1, maximum=2000)
SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE = _env_int("SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE", 500, minimum=1, maximum=5000)

async def _run_strategy_expiry_janitor_tick() -> int:
    expired_ids: set[int] = set()

    async with AsyncSessionLocal() as db:
        for _ in range(STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK):
            rows = await expire_elapsed_strategies_batch(
                db,
                STRATEGY_EXPIRY_JANITOR_BATCH_SIZE,
            )
            if not rows:
                break

            for row in rows:
                strategy_id = row.get("strategy_id")
                if strategy_id is not None:
                    expired_ids.add(int(strategy_id))

            if len(rows) < STRATEGY_EXPIRY_JANITOR_BATCH_SIZE:
                break

    if expired_ids:
        invalidated = await asyncio.to_thread(invalidate_strategy_cache_domain, expired_ids)
        logger.info(
            "[JANITOR] Expired %s strategies and invalidated cache detail=%s list=%s",
            len(expired_ids),
            invalidated.get("deleted_detail", 0),
            invalidated.get("deleted_list", 0),
        )

    return len(expired_ids)


async def strategy_expiry_janitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("[JANITOR] Strategy expiry janitor started")
    while not stop_event.is_set():
        try:
            await _run_strategy_expiry_janitor_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[JANITOR] Strategy janitor tick failed: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue

    logger.info("[JANITOR] Strategy expiry janitor stopped")


async def session_index_prune_janitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("[JANITOR] Session index prune janitor started")
    cursor = 0

    while not stop_event.is_set():
        try:
            cursor, stats = await prune_stale_session_indexes_scan(
                cursor=cursor,
                user_scan_count=SESSION_INDEX_PRUNE_USER_SCAN_COUNT,
                sid_probe_batch_size=SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE,
            )

            if stats.get("stale_removed", 0) > 0 or stats.get("errors", 0) > 0:
                logger.info(
                    "[JANITOR] Session index prune scanned=%s users_pruned=%s stale_removed=%s errors=%s cursor=%s",
                    stats.get("users_scanned", 0),
                    stats.get("users_pruned", 0),
                    stats.get("stale_removed", 0),
                    stats.get("errors", 0),
                    cursor,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[JANITOR] Session index prune tick failed: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=SESSION_INDEX_PRUNE_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue

    logger.info("[JANITOR] Session index prune janitor stopped")
