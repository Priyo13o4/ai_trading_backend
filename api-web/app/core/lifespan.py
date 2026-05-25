from contextlib import asynccontextmanager
import logging
import os
from fastapi import FastAPI

from app.db import AsyncSessionLocal, get_missing_core_tables, shutdown_db_executor
from app.auth import log_redis_connection_health
from app.authn.session_store import SESSION_REDIS
from app.redis_pool import RedisPool

from app.tasks.leader import try_acquire_janitor_leader_lock, release_janitor_leader_lock
from app.tasks.manager import TaskManager
from app.tasks.janitor import strategy_expiry_janitor_loop, session_index_prune_janitor_loop
from app.tasks.janitor import STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS, STRATEGY_EXPIRY_JANITOR_BATCH_SIZE, STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK, SESSION_INDEX_PRUNE_INTERVAL_SECONDS, SESSION_INDEX_PRUNE_USER_SCAN_COUNT, SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE

from app.payments.tasks import deferred_cancellation_janitor_loop, plisio_renewal_invoice_janitor_loop, webhook_events_worker_loop

logger = logging.getLogger(__name__)

# Need to redefine _env_int since SESSION_INDEX_PRUNE_ENABLED isn't in janitor (it's in main.py)
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    lock_file = "/tmp/fastapi_startup.lock"
    is_first = not os.path.exists(lock_file)
    
    if is_first:
        open(lock_file, 'w').close()
        logger.info("="*80)
        logger.info("FASTAPI APPLICATION STARTUP (Worker PID: %s)", os.getpid())
        logger.info("="*80)
    
    # Log Redis connectivity (only from first worker)
    if is_first:
        try:
            await log_redis_connection_health()
        except Exception as err:
            logger.error("Redis health check failed: %s", err)

    if is_first:
        try:
            async with AsyncSessionLocal() as _startup_db:
                missing_tables = await get_missing_core_tables(_startup_db)
            if missing_tables:
                logger.warning(
                    "[BOOTSTRAP WARNING] Missing core API tables in public schema: %s. "
                    "Mounted init SQL currently does not create all strategy/news tables on fresh bootstrap; "
                    "apply app schema migrations before serving production traffic.",
                    ", ".join(missing_tables),
                )
        except Exception as err:
            logger.error("Core table bootstrap check failed: %s", err)
    
    janitor_leader = try_acquire_janitor_leader_lock()

    SESSION_INDEX_PRUNE_ENABLED = bool(
        _env_int("SESSION_INDEX_PRUNE_ENABLED", 1, minimum=0, maximum=1)
    )

    if is_first:
        logger.info("="*80)
        logger.info("STARTUP COMPLETE")
        # _authdbg isn't here but we can skip logging CORS config or import it. It will remain in main.py.
        logger.info(
            "[JANITOR] Config interval=%ss batch_size=%s max_batches_per_tick=%s",
            STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS,
            STRATEGY_EXPIRY_JANITOR_BATCH_SIZE,
            STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK,
        )
        logger.info(
            "[JANITOR] Session-index prune enabled=%s interval=%ss scan_count=%s sid_probe_batch=%s",
            SESSION_INDEX_PRUNE_ENABLED,
            SESSION_INDEX_PRUNE_INTERVAL_SECONDS,
            SESSION_INDEX_PRUNE_USER_SCAN_COUNT,
            SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE,
        )
        logger.info("="*80)

    if janitor_leader:
        TaskManager.start_tasks(
            strategy_loop_func=strategy_expiry_janitor_loop,
            session_prune_func=session_index_prune_janitor_loop,
            deferred_cancellation_func=deferred_cancellation_janitor_loop,
            plisio_renewal_func=plisio_renewal_invoice_janitor_loop,
            webhook_worker_func=webhook_events_worker_loop,
            session_index_prune_enabled=SESSION_INDEX_PRUNE_ENABLED
        )
    else:
        logger.info("[JANITOR] Skipping janitor task startup on non-leader worker pid=%s", os.getpid())

    yield

    # SHUTDOWN
    await TaskManager.stop_tasks()
    release_janitor_leader_lock()

    try:
        await RedisPool.close_all()
    except Exception as exc:
        logger.error("[SHUTDOWN] Redis pool shutdown failed: %s", exc, exc_info=True)

    try:
        await SESSION_REDIS.aclose()
    except Exception as exc:
        logger.error("[SHUTDOWN] Session redis shutdown failed: %s", exc, exc_info=True)

    try:
        shutdown_db_executor()
    except Exception as exc:
        logger.error("[SHUTDOWN] DB executor shutdown failed: %s", exc, exc_info=True)
