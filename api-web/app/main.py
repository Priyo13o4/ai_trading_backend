import json, asyncio, logging, os, hmac, uuid, fcntl
from datetime import datetime, timezone
from typing import Optional, Any
import psycopg
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Query
from fastapi_limiter import FastAPILimiter
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from .singleflight import singleflight_cache

from .auth import REDIS, log_redis_connection_health
from .authn.deps import require_session
from .authn.authz import require_permission
from .authn.csrf import enforce_csrf
from .cors_env import cors_origin_regex_from_env, parse_cors_origins_from_env
from .authn.routes import router as auth_router
from .payments.routes import payments_router
from .payments.webhook_handler import webhook_router
from .routes.referrals import referrals_router
from .authn.session_store import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_REDIS,
    prune_stale_session_indexes_scan,
)
from .db import (
    POSTGRES_DSN,
    AsyncSessionLocal,
    get_db,
    supabase_db,
    get_supabase_client,
    shutdown_db_executor,
    get_latest_signal_from_db, 
    get_old_signal_from_db,
    get_news_preview_from_db,
    get_latest_regime_from_db,
    get_regime_for_pair,
    get_regime_market_data_from_db,
    get_latest_news_from_db, 
    get_upcoming_news_from_db,
    get_news_count,
    get_active_strategies,
    get_strategies_all_from_db,
    get_strategy_by_id_from_db,
    expire_elapsed_strategies_batch,
    get_latest_weekly_macro_playbook_from_db,
    get_economic_event_analysis_from_db,
    get_missing_core_tables,
    get_pair_performance,
    insert_trade_outcome,
    update_trade_outcome
)
from .redis_pool import RedisPool
from .utils import json_dumps
from trading_common.market_data import (
    TIMEFRAME_MAP,
    SYMBOL_INFO
)
from trading_common.symbols import get_active_symbols

# Import historical routes
from .routes.historical import router as historical_router
# Import cache and SSE
from .cache import (
    NewsCache,
    StrategyCache,
    publish_news_snapshot,
    publish_strategy_update,
    publish_strategies_snapshot,
    invalidate_strategy_cache_domain,
)
from .payments.tasks import (
    deferred_cancellation_janitor_loop,
    plisio_renewal_invoice_janitor_loop,
    webhook_events_worker_loop,
)
from .notifications.error_alerts import notify_runtime_error_event
from .observability.debug import debug_log

# Configure logging with UTC
import time
logging.Formatter.converter = time.gmtime
LOG_LEVEL_NAME = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logger.info("logging.configured level=%s", LOG_LEVEL_NAME)
TRUST_PROXY_HEADERS = (os.getenv("TRUST_PROXY_HEADERS") or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
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


STRATEGY_CACHE_MAX_TTL_SECONDS = 300
STRATEGY_CACHE_MIN_TTL_SECONDS = 1
STRATEGY_EXPIRY_JANITOR_BATCH_SIZE = _env_int(
    "STRATEGY_EXPIRY_JANITOR_BATCH_SIZE",
    1000,
    minimum=1,
    maximum=5000,
)
STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK = _env_int(
    "STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK",
    10,
    minimum=1,
    maximum=50,
)
STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS = _env_int(
    "STRATEGY_EXPIRY_JANITOR_INTERVAL_SECONDS",
    30,
    minimum=1,
    maximum=3600,
)
SESSION_INDEX_PRUNE_ENABLED = bool(
    _env_int("SESSION_INDEX_PRUNE_ENABLED", 1, minimum=0, maximum=1)
)
SESSION_INDEX_PRUNE_INTERVAL_SECONDS = _env_int(
    "SESSION_INDEX_PRUNE_INTERVAL_SECONDS",
    300,
    minimum=5,
    maximum=86400,
)
SESSION_INDEX_PRUNE_USER_SCAN_COUNT = _env_int(
    "SESSION_INDEX_PRUNE_USER_SCAN_COUNT",
    100,
    minimum=1,
    maximum=2000,
)
SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE = _env_int(
    "SESSION_INDEX_PRUNE_SID_PROBE_BATCH_SIZE",
    500,
    minimum=1,
    maximum=5000,
)

_strategy_expiry_janitor_task: Optional[asyncio.Task] = None
_strategy_expiry_janitor_stop: Optional[asyncio.Event] = None
_session_index_prune_janitor_task: Optional[asyncio.Task] = None
_session_index_prune_janitor_stop: Optional[asyncio.Event] = None
_deferred_cancellation_janitor_task: Optional[asyncio.Task] = None
_deferred_cancellation_janitor_stop: Optional[asyncio.Event] = None
_plisio_renewal_janitor_task: Optional[asyncio.Task] = None
_plisio_renewal_janitor_stop: Optional[asyncio.Event] = None
_webhook_events_worker_task: Optional[asyncio.Task] = None
_webhook_events_worker_stop: Optional[asyncio.Event] = None
_janitor_leader_lock_handle = None
_janitor_is_leader = False
_events_singleflight_local_locks: dict[str, asyncio.Lock] = {}
_events_singleflight_local_locks_guard = asyncio.Lock()
PREVIEW_SUPPORTED_PAIRS = {"XAUUSD", "BTCUSD", "EURUSD"}

app = FastAPI(
    title="AI Trading Bot API",
    description="FastAPI backend with Redis caching, auth gating, and MT5 integration",
    version="2.0.0"
)




AUTH_CSRF_EXEMPT_PATHS = {
    "/auth/exchange",
    "/auth/logout",
    "/auth/logout-all",
    "/auth/invalidate",
    "/api/webhooks/razorpay",
    "/api/webhooks/plisio",
}


def _cors_allow_headers() -> list[str]:
    required_headers = {
        "Content-Type",
        "X-CSRF-Token",
        "X-Request-ID",
        "X-Correlation-ID",
        "Authorization",
        "X-Requested-With",
        "X-API-Key",
    }

    raw = (os.getenv("ALLOWED_CORS_HEADERS") or "").strip()
    if raw:
        extras = {h.strip() for h in raw.split(",") if h.strip()}
        return sorted(required_headers | extras)

    # Keep this list tight to known frontend/internal usage.
    return sorted(required_headers)


def _authdbg(message: str, *args) -> None:
    debug_log(logger, "cors", message, *args)


def _request_id_from_request(request: Request) -> str:
    from_state = (getattr(request.state, "request_id", "") or "").strip()
    if from_state:
        return from_state

    return (
        (request.headers.get("x-request-id") or "").strip()
        or (request.headers.get("x-correlation-id") or "").strip()
        or uuid.uuid4().hex[:12]
    )


def _request_user_id_from_request(request: Request) -> Optional[str]:
    state_user_id = (getattr(request.state, "user_id", "") or "").strip()
    if state_user_id:
        return state_user_id
    return None


def _request_latency_ms_from_request(request: Request) -> Optional[float]:
    raw_latency = getattr(request.state, "latency_ms", None)
    if raw_latency is not None:
        try:
            return max(0.0, round(float(raw_latency), 2))
        except Exception:
            pass

    start = getattr(request.state, "request_started_monotonic", None)
    if start is None:
        return None

    try:
        return max(0.0, round((time.perf_counter() - float(start)) * 1000.0, 2))
    except Exception:
        return None


from .core.middleware import request_context_middleware, csrf_middleware, security_headers_middleware
app.middleware("http")(security_headers_middleware)
app.middleware("http")(csrf_middleware)
app.middleware("http")(request_context_middleware)

def _runtime_error_context_from_request(request: Request) -> dict[str, Any]:
    client_ip = request.client.host if request.client else ""
    context = {
        "request_id": _request_id_from_request(request),
        "query": request.url.query,
        "client_ip": client_ip,
        "user_agent": (request.headers.get("user-agent") or "")[:200],
        "referer": (request.headers.get("referer") or "")[:200],
    }
    latency_ms = _request_latency_ms_from_request(request)
    if latency_ms is not None:
        context["latency_ms"] = latency_ms
    return context


async def _emit_runtime_error_alert(
    *,
    request: Request,
    error_id: str,
    request_id: str,
    status_code: int,
    message_safe: str,
    message_internal: str,
    extra_context: Optional[dict[str, Any]] = None,
) -> None:
    context = _runtime_error_context_from_request(request)
    if extra_context:
        context.update(extra_context)

    latency_ms = _request_latency_ms_from_request(request)

    try:
        await notify_runtime_error_event(
            error_id=error_id,
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            status_code=status_code,
            user_id=_request_user_id_from_request(request),
            message_safe=message_safe,
            message_internal=message_internal,
            latency_ms=latency_ms,
            context=context,
        )
    except Exception as alert_exc:
        logger.error(
            "Runtime error alert dispatch failed error_id=%s request_id=%s reason=%s",
            error_id,
            request_id,
            alert_exc,
        )


def _normalize_optional_query_value(value: Optional[str], *, lowercase: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None

    return normalized.lower() if lowercase else normalized


def _http_exception_message(detail: Any, *, fallback: str) -> str:
    if isinstance(detail, str):
        text = detail.strip()
        if text:
            return text[:220]

    if isinstance(detail, dict):
        candidate = detail.get("message")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:220]

    return fallback


def _cache_key_token(value) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _singleflight_lock_key(cache_key: str) -> str:
    return f"lock:{cache_key}"


async def _redis_get_best_effort(key: str) -> Optional[str]:
    try:
        return await REDIS.get(key)
    except Exception as exc:
        logger.warning("[API] Redis GET failed for key=%s: %s", key, exc)
        return None


async def _redis_setex_best_effort(key: str, ttl_seconds: int, value: str) -> bool:
    try:
        await REDIS.setex(key, ttl_seconds, value)
        return True
    except Exception as exc:
        logger.warning("[API] Redis SETEX failed for key=%s: %s", key, exc)
        return False


async def _redis_exists_best_effort(key: str) -> Optional[bool]:
    try:
        return bool(await REDIS.exists(key))
    except Exception as exc:
        logger.warning("[API] Redis EXISTS failed for key=%s: %s", key, exc)
        return None


async def _acquire_redis_lock(lock_key: str, lock_ttl_seconds: int) -> tuple[bool, str]:
    token = uuid.uuid4().hex
    try:
        acquired = bool(await REDIS.set(lock_key, token, nx=True, ex=lock_ttl_seconds))
        return acquired, token
    except Exception as exc:
        logger.warning("[API] Redis lock acquire failed for key=%s: %s", lock_key, exc)
        return False, token


async def _release_redis_lock_best_effort(lock_key: str, token: str) -> None:
    # Atomic compare-and-del so one request cannot release another request's lock.
    try:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        await REDIS.eval(script, 1, lock_key, token)
    except Exception as exc:
        logger.warning("[API] Redis lock release failed for key=%s: %s", lock_key, exc)
        return


async def _get_events_local_singleflight_lock(cache_key: str) -> asyncio.Lock:
    async with _events_singleflight_local_locks_guard:
        lock = _events_singleflight_local_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            _events_singleflight_local_locks[cache_key] = lock
        return lock


async def _cleanup_events_local_singleflight_lock(cache_key: str, lock: asyncio.Lock) -> None:
    async with _events_singleflight_local_locks_guard:
        current = _events_singleflight_local_locks.get(cache_key)
        if current is lock and not lock.locked():
            _events_singleflight_local_locks.pop(cache_key, None)




def _seconds_to_expiry(expiry_value) -> Optional[int]:
    if not expiry_value:
        return None

    try:
        if isinstance(expiry_value, datetime):
            expiry_dt = expiry_value
        elif isinstance(expiry_value, str):
            parsed = expiry_value.strip().replace("Z", "+00:00")
            expiry_dt = datetime.fromisoformat(parsed)
        else:
            return None

        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        return int((expiry_dt - now_utc).total_seconds())
    except Exception:
        return None


def _strategy_cache_ttl(rows: list[dict]) -> int:
    seconds_candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        seconds = _seconds_to_expiry(row.get("expiry_time"))
        if seconds is not None:
            seconds_candidates.append(seconds)

    if not seconds_candidates:
        return STRATEGY_CACHE_MAX_TTL_SECONDS

    seconds_to_earliest_expiry = min(seconds_candidates)
    return min(
        STRATEGY_CACHE_MAX_TTL_SECONDS,
        max(STRATEGY_CACHE_MIN_TTL_SECONDS, seconds_to_earliest_expiry),
    )


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


async def _strategy_expiry_janitor_loop(stop_event: asyncio.Event) -> None:
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


async def _session_index_prune_janitor_loop(stop_event: asyncio.Event) -> None:
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


def _try_acquire_janitor_leader_lock() -> bool:
    global _janitor_leader_lock_handle, _janitor_is_leader

    if _janitor_is_leader:
        return True

    lock_path = (os.getenv("JANITOR_LEADER_LOCK_PATH") or "/tmp/fastapi_janitor_leader.lock").strip() or "/tmp/fastapi_janitor_leader.lock"
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
        _janitor_leader_lock_handle = lock_handle
        _janitor_is_leader = True
        logger.info("[JANITOR] Acquired leader lock path=%s pid=%s", lock_path, os.getpid())
        return True
    except BlockingIOError:
        lock_handle.close()
        _janitor_is_leader = False
        logger.info("[JANITOR] Leader lock held by another worker path=%s", lock_path)
        return False
    except Exception:
        lock_handle.close()
        _janitor_is_leader = False
        logger.exception("[JANITOR] Failed to acquire leader lock path=%s", lock_path)
        return False


def _release_janitor_leader_lock() -> None:
    global _janitor_leader_lock_handle, _janitor_is_leader

    handle = _janitor_leader_lock_handle
    _janitor_leader_lock_handle = None
    _janitor_is_leader = False
    if handle is None:
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.exception("[JANITOR] Failed to unlock leader lock")
    finally:
        try:
            handle.close()
        except Exception:
            logger.exception("[JANITOR] Failed to close leader lock handle")









# Add CORS middleware
CORS_ALLOW_ORIGINS = parse_cors_origins_from_env()
CORS_ALLOW_ORIGIN_REGEX = cors_origin_regex_from_env()
CORS_ALLOW_HEADERS = _cors_allow_headers()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=CORS_ALLOW_HEADERS,
)


@app.middleware("http")
async def cors_debug_middleware(request: Request, call_next):
    rid = (request.headers.get("x-request-id") or "").strip() or uuid.uuid4().hex[:12]
    origin = request.headers.get("origin") or ""
    is_preflight = int(request.method == "OPTIONS" and bool(request.headers.get("access-control-request-method")))
    _authdbg(
        "event=cors.request rid=%s method=%s path=%s origin=%s preflight=%s",
        rid,
        request.method,
        request.url.path,
        origin,
        is_preflight,
    )

    response = await call_next(request)
    _authdbg(
        "event=cors.response rid=%s status=%s acao=%s acc=%s",
        rid,
        response.status_code,
        response.headers.get("access-control-allow-origin") or "",
        response.headers.get("access-control-allow-credentials") or "",
    )
    return response


@app.exception_handler(HTTPException)
async def global_http_exception_handler(request: Request, exc: HTTPException):
    status_code = int(getattr(exc, "status_code", 500) or 500)
    request_id = _request_id_from_request(request)

    if status_code < 500:
        message = _http_exception_message(exc.detail, fallback="Request could not be completed")
        return JSONResponse(
            status_code=status_code,
            content={
                "message": message,
                "detail": exc.detail,
            },
            headers=getattr(exc, "headers", None),
        )

    error_id = f"runtime-{uuid.uuid4().hex[:20]}"
    detail_text = str(getattr(exc, "detail", ""))[:800]
    message = "Internal server error"

    logger.warning(
        "runtime.http_exception.expected error_id=%s request_id=%s method=%s path=%s status=%s detail=%s",
        error_id,
        request_id,
        request.method,
        request.url.path,
        status_code,
        detail_text,
    )

    return JSONResponse(
        status_code=status_code,
        content={
            "message": message,
            "detail": message,
            "error_id": error_id,
            "request_id": request_id,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def global_unhandled_exception_handler(request: Request, exc: Exception):
    request_id = _request_id_from_request(request)
    error_id = f"runtime-{uuid.uuid4().hex[:20]}"
    message_internal = f"{exc.__class__.__name__}: {str(exc)[:800]}"
    message = "Internal server error"

    logger.error(
        "runtime.unhandled_exception error_id=%s request_id=%s method=%s path=%s",
        error_id,
        request_id,
        request.method,
        request.url.path,
        exc_info=True,
    )

    asyncio.create_task(
        _emit_runtime_error_alert(
            request=request,
            error_id=error_id,
            request_id=request_id,
            status_code=500,
            message_safe="Internal server error",
            message_internal=message_internal,
            extra_context={"exception_type": exc.__class__.__name__},
        )
    )

    return JSONResponse(
        status_code=500,
        content={
            "message": message,
            "detail": message,
            "error_id": error_id,
            "request_id": request_id,
        },
    )


# ============================================================================
# STARTUP INITIALIZATION
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """
    Application startup event
    """
    import os
    
    # Use file lock to log only from first worker
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
    
    global _strategy_expiry_janitor_stop, _strategy_expiry_janitor_task
    global _session_index_prune_janitor_stop, _session_index_prune_janitor_task
    global _deferred_cancellation_janitor_stop, _deferred_cancellation_janitor_task
    global _plisio_renewal_janitor_stop, _plisio_renewal_janitor_task
    global _webhook_events_worker_stop, _webhook_events_worker_task

    janitor_leader = _try_acquire_janitor_leader_lock()

    if is_first:
        logger.info("="*80)
        logger.info("STARTUP COMPLETE")
        _authdbg(
            "event=cors.config allow_origins_count=%s has_origin_regex=%s allow_credentials=1 allow_headers=%s",
            len(CORS_ALLOW_ORIGINS),
            int(bool(CORS_ALLOW_ORIGIN_REGEX)),
            ",".join(CORS_ALLOW_HEADERS),
        )
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
        if _strategy_expiry_janitor_task is None:
            _strategy_expiry_janitor_stop = asyncio.Event()
            _strategy_expiry_janitor_task = asyncio.create_task(
                _strategy_expiry_janitor_loop(_strategy_expiry_janitor_stop)
            )

        if SESSION_INDEX_PRUNE_ENABLED and _session_index_prune_janitor_task is None:
            _session_index_prune_janitor_stop = asyncio.Event()
            _session_index_prune_janitor_task = asyncio.create_task(
                _session_index_prune_janitor_loop(_session_index_prune_janitor_stop)
            )

        if _deferred_cancellation_janitor_task is None:
            _deferred_cancellation_janitor_stop = asyncio.Event()
            _deferred_cancellation_janitor_task = asyncio.create_task(
                deferred_cancellation_janitor_loop(_deferred_cancellation_janitor_stop)
            )

        if _plisio_renewal_janitor_task is None:
            _plisio_renewal_janitor_stop = asyncio.Event()
            _plisio_renewal_janitor_task = asyncio.create_task(
                plisio_renewal_invoice_janitor_loop(_plisio_renewal_janitor_stop)
            )

        if _webhook_events_worker_task is None:
            _webhook_events_worker_stop = asyncio.Event()
            _webhook_events_worker_task = asyncio.create_task(
                webhook_events_worker_loop(_webhook_events_worker_stop)
            )
    else:
        logger.info("[JANITOR] Skipping janitor task startup on non-leader worker pid=%s", os.getpid())

@app.on_event("shutdown")
async def shutdown_event():
    global _strategy_expiry_janitor_stop, _strategy_expiry_janitor_task
    global _session_index_prune_janitor_stop, _session_index_prune_janitor_task
    global _deferred_cancellation_janitor_stop, _deferred_cancellation_janitor_task
    global _plisio_renewal_janitor_stop, _plisio_renewal_janitor_task
    global _webhook_events_worker_stop, _webhook_events_worker_task

    if _strategy_expiry_janitor_task is None:
        pass

    if _strategy_expiry_janitor_stop is not None:
        _strategy_expiry_janitor_stop.set()

    if _strategy_expiry_janitor_task is not None:
        try:
            await asyncio.wait_for(_strategy_expiry_janitor_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[JANITOR] Timed out waiting for strategy janitor to stop, cancelling task")
            _strategy_expiry_janitor_task.cancel()
            try:
                await _strategy_expiry_janitor_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            logger.error("[JANITOR] Strategy janitor shutdown failed: %s", exc, exc_info=True)
        finally:
            _strategy_expiry_janitor_task = None
            _strategy_expiry_janitor_stop = None

    if _session_index_prune_janitor_stop is not None:
        _session_index_prune_janitor_stop.set()

    if _session_index_prune_janitor_task is not None:
        try:
            await asyncio.wait_for(_session_index_prune_janitor_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[JANITOR] Timed out waiting for session index janitor to stop, cancelling task")
            _session_index_prune_janitor_task.cancel()
            try:
                await _session_index_prune_janitor_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            logger.error("[JANITOR] Session index janitor shutdown failed: %s", exc, exc_info=True)
        finally:
            _session_index_prune_janitor_task = None
            _session_index_prune_janitor_stop = None

    if _deferred_cancellation_janitor_stop is not None:
        _deferred_cancellation_janitor_stop.set()

    if _deferred_cancellation_janitor_task is not None:
        try:
            await asyncio.wait_for(_deferred_cancellation_janitor_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[JANITOR] Timed out waiting for deferred cancellation janitor to stop, cancelling task")
            _deferred_cancellation_janitor_task.cancel()
            try:
                await _deferred_cancellation_janitor_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            logger.error("[JANITOR] Deferred cancellation janitor shutdown failed: %s", exc, exc_info=True)
        finally:
            _deferred_cancellation_janitor_task = None
            _deferred_cancellation_janitor_stop = None

    if _plisio_renewal_janitor_stop is not None:
        _plisio_renewal_janitor_stop.set()

    if _plisio_renewal_janitor_task is not None:
        try:
            await asyncio.wait_for(_plisio_renewal_janitor_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[JANITOR] Timed out waiting for Plisio renewal janitor to stop, cancelling task")
            _plisio_renewal_janitor_task.cancel()
            try:
                await _plisio_renewal_janitor_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            logger.error("[JANITOR] Plisio renewal janitor shutdown failed: %s", exc, exc_info=True)
        finally:
            _plisio_renewal_janitor_task = None
            _plisio_renewal_janitor_stop = None

    if _webhook_events_worker_stop is not None:
        _webhook_events_worker_stop.set()

    if _webhook_events_worker_task is not None:
        try:
            await asyncio.wait_for(_webhook_events_worker_task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[WEBHOOK WORKER] Timed out waiting for worker to stop, cancelling task")
            _webhook_events_worker_task.cancel()
            try:
                await _webhook_events_worker_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            logger.error("[WEBHOOK WORKER] Shutdown failed: %s", exc, exc_info=True)
        finally:
            _webhook_events_worker_task = None
            _webhook_events_worker_stop = None

    _release_janitor_leader_lock()

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


# Include routers
from .routes.system import router as system_router
from .routes.trading import router as trading_router
from .routes.regime import router as regime_router
from .routes.strategies import router as strategies_router
from .routes.news import router as news_router

app.include_router(system_router)
app.include_router(trading_router)
app.include_router(regime_router)
app.include_router(strategies_router)
app.include_router(news_router)
from .core.dependencies import require_signals_context
app.include_router(historical_router, dependencies=[Depends(require_signals_context)])
app.include_router(auth_router)
app.include_router(payments_router)
app.include_router(webhook_router)
app.include_router(referrals_router)

# ============================================================================
