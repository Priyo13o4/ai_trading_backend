import json, asyncio, logging, os, hmac, uuid, fcntl
from datetime import datetime, timezone
from typing import Optional, Any
import psycopg
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
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
    async_db,
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


def _require_internal_api_key(request: Request, *env_var_names: str) -> str:
    provided = (request.headers.get("X-API-Key") or "").strip()
    if not provided:
        raise HTTPException(401, "Missing X-API-Key")

    for env_name in env_var_names:
        expected = (os.getenv(env_name) or "").strip()
        if expected and hmac.compare_digest(provided, expected):
            return env_name

    raise HTTPException(401, "Unauthorized")


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

    for _ in range(STRATEGY_EXPIRY_JANITOR_MAX_BATCHES_PER_TICK):
        rows = await asyncio.to_thread(
            expire_elapsed_strategies_batch,
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


async def require_signals_context(ctx=Depends(require_session)):
    require_permission(ctx, "signals")
    return ctx


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = _request_id_from_request(request)
    request.state.request_id = request_id
    request.state.request_started_monotonic = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        request.state.latency_ms = _request_latency_ms_from_request(request)
        raise

    request.state.latency_ms = _request_latency_ms_from_request(request)
    response.headers.setdefault("X-Request-ID", request_id)
    if request.state.latency_ms is not None:
        response.headers.setdefault("X-Response-Time-Ms", f"{request.state.latency_ms:.2f}")
    return response


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    # Enforce CSRF for cookie-authenticated state-changing requests.
    try:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            request_path = request.url.path
            if request_path in AUTH_CSRF_EXEMPT_PATHS or request_path.startswith("/api/webhooks/"):
                pass
            else:
                if request.cookies.get(SESSION_COOKIE_NAME):
                    enforce_csrf(request, CSRF_COOKIE_NAME)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)

    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https:; script-src 'self' 'unsafe-inline' https:; connect-src 'self' https: wss:",
    )

    forwarded_proto = ""
    if TRUST_PROXY_HEADERS:
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    if is_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    return response

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
            missing_tables = await asyncio.to_thread(get_missing_core_tables)
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
app.include_router(historical_router, dependencies=[Depends(require_signals_context)])
app.include_router(auth_router)
app.include_router(payments_router)
app.include_router(webhook_router)
app.include_router(referrals_router)

# ============================================================================
# SYMBOLS ENDPOINT (Dynamic symbol list for frontend)
# ============================================================================

@app.get("/api/symbols")
async def get_symbols():
    """
    Get list of active trading symbols with metadata.
    Frontend should call this on startup to dynamically populate symbol lists.
    """
    # Import POSTGRES_DSN from db module
    from .db import POSTGRES_DSN
    
    # DB/Redis is the source of truth for symbols.
    symbols = await get_active_symbols(redis_client=REDIS, postgres_dsn=POSTGRES_DSN, fallback=[])

    # Build metadata for all symbols (with defaults for unknown symbols)
    metadata = {}
    for symbol in symbols:
        if symbol in SYMBOL_INFO:
            metadata[symbol] = SYMBOL_INFO[symbol]
        else:
            # Default metadata for symbols without explicit info
            metadata[symbol] = {
                "name": symbol,
                "type": "unknown",
                "precision": 5
            }
    
    return {
        "symbols": symbols,
        "metadata": metadata,
        "count": len(symbols)
    }

# ============================================================================
# HEALTH CHECK
# ============================================================================


async def _postgres_health_check() -> dict:
    started_at = time.perf_counter()

    def _probe():
        with psycopg.connect(POSTGRES_DSN, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

    try:
        await asyncio.to_thread(_probe)
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)[:160]}


async def _supabase_health_check() -> dict:
    project_url = (os.getenv("SUPABASE_PROJECT_URL") or "").strip()
    service_key = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    if not project_url or not service_key:
        return {"status": "skipped", "reason": "not_configured"}

    started_at = time.perf_counter()
    try:
        supabase = get_supabase_client()
        await async_db(
            lambda: supabase.table("subscription_plans").select("id").limit(1).execute(),
            timeout=2.0,
        )
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)[:160]}


@app.get("/api/health")
async def health():
    checks = {}

    redis_started_at = time.perf_counter()
    try:
        await REDIS.ping()
        checks["redis_app"] = {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - redis_started_at) * 1000, 2),
        }
    except Exception as exc:
        checks["redis_app"] = {"status": "unhealthy", "error": str(exc)[:160]}

    session_started_at = time.perf_counter()
    try:
        await SESSION_REDIS.ping()
        checks["redis_session"] = {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - session_started_at) * 1000, 2),
        }
    except Exception as exc:
        checks["redis_session"] = {"status": "unhealthy", "error": str(exc)[:160]}

    checks["postgres"] = await _postgres_health_check()
    checks["supabase"] = await _supabase_health_check()

    required_checks = {
        name: check
        for name, check in checks.items()
        if check.get("status") != "skipped"
    }
    all_healthy = all(check.get("status") == "healthy" for check in required_checks.values())
    payload = {
        "status": "healthy" if all_healthy else "degraded",
        "version": "2.0.0",
        "checks": checks,
    }

    if all_healthy:
        return payload
    return JSONResponse(status_code=503, content=payload)

# ============================================================================
# STRATEGY ENDPOINTS (AI-Generated Trading Recommendations)
# ============================================================================

@app.get("/api/signals/{pair}")
async def get_signal(pair: str, request: Request, response: Response, ctx=Depends(require_session)):
    """Get latest active strategy for a trading pair (requires auth)"""
    logger.info(f"[API] GET /api/signals/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        require_permission(ctx, "signals")
        
        key = f"latest:signal:{pair.upper()}"
        
        # Try Redis cache first
        cached = await REDIS.get(key)
        if cached: 
            logger.info(f"[API] Cache HIT for signal: {pair}")
            return JSONResponse(content=json.loads(cached))

        # Cache miss - fetch from database
        logger.info(f"[API] Cache MISS for signal: {pair}, querying database")
        row = await asyncio.to_thread(get_latest_signal_from_db, pair)
        
        if not row: 
            logger.warning(f"[API] No active strategy found for pair: {pair}")
            raise HTTPException(404, f"No active strategy found for {pair}")
        
        # Cache the result
        ttl = int(row.get("expiry_minutes") or 30) * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached signal for {pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/signals/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/preview/{pair}")
async def get_signal_preview(pair: str, request: Request):
    """Get old strategy preview for main page (no auth required)"""
    logger.info(f"[API] GET /api/preview/{pair} - Public access")
    
    try:
        normalized_pair = pair.upper().strip()
        if normalized_pair not in PREVIEW_SUPPORTED_PAIRS:
            logger.warning(f"[API] Preview requested for unsupported pair: {pair}")
            supported = ", ".join(sorted(PREVIEW_SUPPORTED_PAIRS))
            raise HTTPException(404, f"Preview only available for: {supported}")
        
        key = f"preview:signal:v2:{normalized_pair}"
        response_headers = {"Cache-Control": "public, max-age=300"}
        
        # Try Redis cache
        cached = await REDIS.get(key)
        if cached: 
            logger.info(f"[API] Cache HIT for preview: {normalized_pair}")
            return JSONResponse(content=json.loads(cached), headers=response_headers)

        # Cache miss - get old signal
        logger.info(f"[API] Cache MISS for preview: {normalized_pair}, querying database")
        row = await asyncio.to_thread(get_old_signal_from_db, normalized_pair)
        
        if not row: 
            logger.warning(f"[API] No preview strategy found for pair: {normalized_pair}")
            raise HTTPException(404, "No preview available")
        
        # Cache preview for 1 hour (it's old data)
        ttl = 60 * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached preview for {normalized_pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized), headers=response_headers)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/preview/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/news/preview")
async def get_news_preview(request: Request):
    """Get latest high-impact news item for landing page (no auth required)."""
    logger.info("[API] GET /api/news/preview - Public access")

    try:
        key = "preview:news:latest"

        # Try Redis cache (30 min TTL - news changes slowly enough)
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for news preview")
            return JSONResponse(content=json.loads(cached))

        logger.info("[API] Cache MISS for news preview, querying database")
        row = await asyncio.to_thread(get_news_preview_from_db)

        if not row:
            logger.warning("[API] No high-impact news found for preview")
            raise HTTPException(404, "No news preview available")

        ttl = 30 * 60  # 30 minutes
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info("[API] Cached news preview with TTL=%ss", ttl)

        return JSONResponse(content=json.loads(serialized))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/preview: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/strategies")
async def get_all_active_strategies(pair: str = None, ctx=Depends(require_signals_context)):
    """Get all active strategies, optionally filtered by pair"""
    logger.info(f"[API] GET /api/strategies?pair={pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        normalized_pair = _normalize_optional_query_value(pair, lowercase=True)
        cache_pair = normalized_pair or "all"
        key = f"latest:strategies:{cache_pair}"

        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for /api/strategies")
            return JSONResponse(content=json.loads(cached))

        logger.info("[API] Cache MISS for /api/strategies, querying database")
        strategies = await asyncio.to_thread(get_active_strategies, pair)
        logger.info(f"[API] Found {len(strategies)} active strategies")
        StrategyCache.set(strategies, pair or "all")
        publish_strategies_snapshot(strategies)
        serialized = json_dumps({"strategies": strategies})
        ttl = _strategy_cache_ttl(strategies)
        await REDIS.setex(key, ttl, serialized)
        return JSONResponse(content=json.loads(serialized))
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/strategies/all")
async def get_strategies_all(
    request: Request,
    response: Response,
    symbol: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_signals_context),
):
    """Get strategies with optional filters + pagination (requires signals permission)."""
    logger.info(
        "[API] GET /api/strategies/all - User: %s, symbol=%s, direction=%s, status=%s, search=%s, limit=%s, offset=%s",
        ctx.get("user_id", "anonymous"),
        symbol,
        direction,
        status,
        search,
        limit,
        offset,
    )

    try:
        normalized_symbol = _normalize_optional_query_value(symbol)
        cache_symbol = normalized_symbol.lower() if normalized_symbol else None

        normalized_direction = _normalize_optional_query_value(direction, lowercase=True)
        if normalized_direction and normalized_direction not in {"buy", "sell"}:
            raise HTTPException(422, "direction must be one of: buy, sell")

        normalized_status = _normalize_optional_query_value(status, lowercase=True)
        normalized_search = _normalize_optional_query_value(search)
        cache_search = normalized_search.lower() if normalized_search else None

        key = (
            f"latest:strategies:all:{_cache_key_token(cache_symbol)}:"
            f"{_cache_key_token(normalized_direction)}:"
            f"{_cache_key_token(normalized_status)}:"
            f"{_cache_key_token(cache_search)}:"
            f"{_cache_key_token(limit)}:{_cache_key_token(offset)}"
        )
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for /api/strategies/all")
            return JSONResponse(content=json.loads(cached))

        logger.info("[API] Cache MISS for /api/strategies/all, querying database")
        rows, total = await asyncio.to_thread(
            get_strategies_all_from_db,
            normalized_symbol,
            normalized_direction,
            normalized_status,
            normalized_search,
            limit,
            offset,
        )

        result = {
            "strategies": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
        ttl = _strategy_cache_ttl(rows)
        serialized = json_dumps(result)
        await REDIS.setex(key, ttl, serialized)
        return JSONResponse(content=json.loads(serialized))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies/all: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.post("/api/strategies/publish")
async def publish_strategy_update_endpoint(request: Request):
    """Publish a strategy update from external automation (n8n)."""
    api_key = request.headers.get("X-API-Key")
    expected_key = os.getenv("N8N_STRATEGY_PUBLISH_KEY") or os.getenv("N8N_MARKET_DATA_KEY")

    if not expected_key or api_key != expected_key:
        raise HTTPException(401, "Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    if isinstance(payload, dict) and "strategy" in payload:
        payload = payload.get("strategy")

    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected strategy object payload")

    publish_strategy_update(payload)

    pair = payload.get("trading_pair") or payload.get("symbol") or payload.get("pair")
    try:
        strategies = await asyncio.to_thread(get_active_strategies, pair)
        StrategyCache.set(strategies, pair or "all")
    except Exception as exc:
        logger.warning("Failed to refresh strategies cache after publish: %s", exc)

    return {"status": "ok"}


@app.get("/api/strategies/{strategy_id}")
async def get_strategy_by_id(
    strategy_id: int,
    request: Request,
    response: Response,
    ctx=Depends(require_session),
):
    """Get a single strategy by ID (requires signals permission)."""
    logger.info(f"[API] GET /api/strategies/{strategy_id} - User: {ctx.get('user_id', 'anonymous')}")

    try:
        require_permission(ctx, "signals")

        key = f"latest:strategy:id:{strategy_id}"
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for strategy id={strategy_id}")
            return JSONResponse(content=json.loads(cached))

        logger.info(f"[API] Cache MISS for strategy id={strategy_id}, querying database")
        row = await asyncio.to_thread(get_strategy_by_id_from_db, strategy_id)
        if not row:
            raise HTTPException(404, f"Strategy {strategy_id} not found")

        ttl = _strategy_cache_ttl([row])
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        return JSONResponse(content=json.loads(serialized))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/strategies/{strategy_id}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

# ============================================================================
# REGIME ANALYSIS ENDPOINTS
# ============================================================================

@app.get("/api/regime")
async def get_regime(request: Request, response: Response, ctx=Depends(require_session)):
    """Get latest regime analysis for all trading pairs"""
    logger.info(f"[API] GET /api/regime - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        key = "latest:regime"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for regime data")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss
        logger.info("[API] Cache MISS for regime, querying database")
        rows = await asyncio.to_thread(get_latest_regime_from_db)
        
        if not rows:
            logger.warning("[API] No regime data found in database")
            raise HTTPException(404, "No regime data found")
        
        # Cache for 15 minutes
        ttl = 15 * 60
        serialized = json_dumps(rows)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached regime data for {len(rows)} pairs with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/regime/market-data")
async def get_regime_market_data_markdown(request: Request):
    """
    Get comprehensive market data for regime analysis (n8n workflow endpoint)
    Returns JSON with markdown split by symbol for LLM processing
    Requires X-API-Key header for authentication
    """
    # API Key authentication for n8n workflow
    api_key = request.headers.get("X-API-Key")
    expected_key = os.getenv("N8N_MARKET_DATA_KEY")
    
    if not expected_key:
        logger.error("[API] N8N_MARKET_DATA_KEY not configured in environment")
        raise HTTPException(500, "API key authentication not configured")
    
    if not api_key or api_key != expected_key:
        logger.warning(f"[API] Invalid API key attempt for /api/regime/market-data from {request.client.host}")
        raise HTTPException(401, "Invalid or missing API key")
    
    logger.info("[API] GET /api/regime/market-data - n8n workflow request (authenticated)")
    
    try:
        key = "regime:market-data:markdown"
        
        # Try cache (5 min TTL for fresh data)
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for regime market data (markdown)")
            import json
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss - fetch from database
        logger.info("[API] Cache MISS for regime market data, querying database")
        data = await asyncio.to_thread(get_regime_market_data_from_db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            raise HTTPException(404, "No market data available")
        
        # Convert to markdown format split by symbol
        market_data_raw = data.get("market_data", {})
        analysis_timestamp = data.get("analysis_timestamp", datetime.now().isoformat())
        collection_info = data.get("collection_info", {})
        
        logger.info(f"[API] Converting {len(market_data_raw)} symbols to markdown format")
        
        def format_symbol_markdown(symbol: str, data: dict, timestamp: str) -> str:
            """Format a single symbol's data as markdown optimized for AI analysis"""
            md = f"# {symbol} Technical Analysis Report\n\n"
            md += f"**📅 Analysis Timestamp:** {timestamp}\n\n"
            md += "="*80 + "\n\n"
            
            # Sort timeframes by importance: D1, W1, H4, H1, M15, M5
            timeframe_order = ["D1", "W1", "H4", "H1", "M15", "M5"]
            sorted_tfs = sorted(data.keys(), key=lambda x: timeframe_order.index(x) if x in timeframe_order else 999)
            
            for timeframe in sorted_tfs:
                metrics = data[timeframe]
                
                md += f"## 📊 {timeframe} Timeframe\n\n"
                
                # Price Summary Box
                current_price = metrics.get('current_price', 'N/A')
                md += f"### 💰 Price: {current_price}\n\n"
                
                # Technical Indicators
                if "technical_indicators" in metrics:
                    ind = metrics["technical_indicators"]
                    
                    # Trend Analysis Section
                    md += "### 📈 Trend Analysis\n\n"
                    rsi = ind.get('rsi', 'N/A')
                    adx = ind.get('adx', 'N/A')
                    dmp = ind.get('dmp', 'N/A')
                    dmn = ind.get('dmn', 'N/A')
                    
                    # Trend signal interpretation
                    if rsi != 'N/A' and rsi is not None:
                        rsi_signal = "Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"
                        md += f"- **RSI(14)**: {rsi} ({rsi_signal})\n"
                    else:
                        md += f"- **RSI(14)**: {rsi}\n"
                    
                    if adx != 'N/A' and adx is not None:
                        trend_strength = "Strong" if adx > 25 else "Weak"
                        md += f"- **ADX(14)**: {adx} ({trend_strength} Trend)\n"
                    else:
                        md += f"- **ADX(14)**: {adx}\n"
                    
                    md += f"- **+DI**: {dmp}\n"
                    md += f"- **-DI**: {dmn}\n\n"
                    
                    # Momentum Section
                    md += "### ⚡ Momentum Indicators\n\n"
                    md += f"- **MACD Line**: {ind.get('macd_main', 'N/A')}\n"
                    md += f"- **MACD Signal**: {ind.get('macd_signal', 'N/A')}\n"
                    md += f"- **MACD Histogram**: {ind.get('macd_histogram', 'N/A')}\n"
                    md += f"- **ROC %**: {ind.get('roc_percent', 'N/A')}\n"
                    md += f"- **EMA Momentum Slope**: {ind.get('ema_momentum_slope', 'N/A')}\n"
                    md += f"- **OBV Slope**: {ind.get('obv_slope', 'N/A')}\n\n"
                    
                    # Volatility Section
                    md += "### 🌊 Volatility Metrics\n\n"
                    atr = ind.get('atr', 'N/A')
                    atr_pct = ind.get('atr_percentile', 'N/A')
                    if atr_pct != 'N/A' and atr_pct is not None:
                        vol_level = "High" if atr_pct > 75 else "Low" if atr_pct < 25 else "Normal"
                        md += f"- **ATR(14)**: {atr} (Percentile: {atr_pct}% - {vol_level})\n\n"
                    else:
                        md += f"- **ATR(14)**: {atr}\n\n"
                    
                    # EMAs Section
                    if "emas" in ind:
                        emas = ind["emas"]
                        md += "### 📊 Exponential Moving Averages\n\n"
                        for period in [9, 21, 50, 100, 200]:
                            ema_val = emas.get(f'EMA_{period}', 'N/A')
                            if ema_val != 'N/A' and ema_val is not None:
                                md += f"- **EMA-{period}**: {ema_val}\n"
                        md += "\n"
                    
                    # Bollinger Bands Section
                    bb_upper = ind.get('bb_upper')
                    bb_middle = ind.get('bb_middle')
                    bb_lower = ind.get('bb_lower')
                    bb_squeeze = ind.get('bb_squeeze_ratio')
                    bb_width_pct = ind.get('bb_width_percentile')
                    
                    if bb_upper or bb_middle or bb_lower:
                        md += "### 📉 Bollinger Bands\n\n"
                        md += f"- **Upper Band**: {bb_upper if bb_upper else 'N/A'}\n"
                        md += f"- **Middle Band (SMA-20)**: {bb_middle if bb_middle else 'N/A'}\n"
                        md += f"- **Lower Band**: {bb_lower if bb_lower else 'N/A'}\n"
                        md += f"- **Squeeze Ratio**: {bb_squeeze if bb_squeeze else 'N/A'}\n"
                        
                        if bb_width_pct != 'N/A' and bb_width_pct is not None:
                            squeeze_level = "Tight Squeeze" if bb_width_pct < 25 else "Wide Expansion" if bb_width_pct > 75 else "Normal"
                            md += f"- **Width Percentile**: {bb_width_pct}% ({squeeze_level})\n\n"
                        else:
                            md += f"- **Width Percentile**: {bb_width_pct}\n\n"
                
                # Market Structure Section
                if "market_structure" in metrics:
                    struct = metrics["market_structure"]
                    md += "### 🏗️ Market Structure (50-bar Range)\n\n"
                    md += f"- **Recent High**: {struct.get('recent_high', 'N/A')}\n"
                    md += f"- **Recent Low**: {struct.get('recent_low', 'N/A')}\n"
                    range_pct = struct.get('range_percent', 'N/A')
                    if range_pct != 'N/A' and range_pct is not None:
                        volatility = "High Volatility" if range_pct > 10 else "Low Volatility" if range_pct < 3 else "Moderate"
                        md += f"- **Range**: {range_pct}% ({volatility})\n\n"
                    else:
                        md += f"- **Range**: {range_pct}%\n\n"
                
                # Recent Price Action Table
                if "recent_bars_detail" in metrics and isinstance(metrics["recent_bars_detail"], list):
                    bars = metrics["recent_bars_detail"][:5]  # Last 5 bars
                    md += f"### 🕐 Recent Price Action (Last {len(bars)} Candles)\n\n"
                    md += "| Time | Open | High | Low | Close | Volume | Type |\n"
                    md += "|:-----|-----:|-----:|----:|------:|-------:|:----:|\n"
                    for bar in bars:
                        candle_type = bar.get('candle_type', 'N/A')
                        emoji = "🟢" if candle_type == "Bullish" else "🔴" if candle_type == "Bearish" else "⚪"
                        md += f"| {bar.get('time', 'N/A')} | {bar.get('open', 'N/A')} | {bar.get('high', 'N/A')} | {bar.get('low', 'N/A')} | {bar.get('close', 'N/A')} | {bar.get('volume', 'N/A')} | {emoji} {candle_type} |\n"
                    md += "\n"
                
                md += "---\n\n"
            
            return md.strip()
        
        # Generate markdown for each symbol
        market_data_formatted = {}
        null_indicators = []
        
        for symbol, symbol_data in market_data_raw.items():
            # Check for null indicators
            for tf, metrics in symbol_data.items():
                if "technical_indicators" in metrics:
                    ind = metrics["technical_indicators"]
                    null_fields = [k for k, v in ind.items() if v is None and k != "emas"]
                    if ind.get("emas"):
                        null_emas = [k for k, v in ind["emas"].items() if v is None]
                        if null_emas:
                            null_fields.append(f"emas.{','.join(null_emas)}")
                    if null_fields:
                        null_indicators.append(f"{symbol}/{tf}: {', '.join(null_fields)}")
            
            market_data_formatted[symbol] = format_symbol_markdown(symbol, symbol_data, analysis_timestamp)
        
        if null_indicators:
            logger.warning(f"[API] Found null indicators: {null_indicators[:5]}...")  # Log first 5
        
        # Build response
        response_data = {
            "analysis_timestamp": analysis_timestamp,
            "collection_info": {
                **collection_info,
                "format": "markdown",
                "symbols": list(market_data_formatted.keys()),
                "timeframes": ["D1", "W1", "H4", "H1", "M15", "M5"]
            },
            "market_data": market_data_formatted
        }
        
        # Cache for 5 minutes
        ttl = 5 * 60
        from app.utils import json_dumps
        await REDIS.setex(key, ttl, json_dumps(response_data))
        logger.info(f"[API] Cached regime market data for {len(market_data_formatted)} symbols with TTL={ttl}s")
        
        return JSONResponse(content=response_data)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/regime/market-data/json")
async def get_regime_market_data_json(
    request: Request,
    symbol: Optional[str] = Query(None, description="Single symbol filter, e.g. XAUUSD"),
    symbols: Optional[str] = Query(None, description="Comma-separated symbols filter, e.g. XAUUSD,EURUSD")
):
    """
    Get comprehensive market data for regime analysis (JSON format)
    Returns MT5-compatible JSON format with indicators, structure, and recent bars
    Optional query filters:
    - symbol=XAUUSD
    - symbols=XAUUSD,EURUSD
    Requires X-API-Key header for authentication
    """
    # API Key authentication for n8n workflow
    api_key = request.headers.get("X-API-Key")
    expected_key = os.getenv("N8N_MARKET_DATA_KEY")
    
    if not expected_key:
        logger.error("[API] N8N_MARKET_DATA_KEY not configured in environment")
        raise HTTPException(500, "API key authentication not configured")
    
    if not api_key or api_key != expected_key:
        logger.warning(f"[API] Invalid API key attempt for /api/regime/market-data/json from {request.client.host}")
        raise HTTPException(401, "Invalid or missing API key")
    
    requested_symbols = []
    if symbol:
        requested_symbols.append(str(symbol).strip().upper())
    if symbols:
        requested_symbols.extend(
            [s.strip().upper() for s in str(symbols).split(",") if s and s.strip()]
        )
    requested_symbols = sorted(set([s for s in requested_symbols if s]))

    def _apply_symbol_filter(full_payload: dict) -> dict:
        if not requested_symbols:
            return full_payload

        market_data = full_payload.get("market_data", {})
        filtered_market_data = {
            sym: market_data[sym]
            for sym in requested_symbols
            if sym in market_data
        }

        if not filtered_market_data:
            logger.warning(f"[API] No market data found for requested symbols: {requested_symbols}")
            raise HTTPException(404, f"No market data found for requested symbols: {', '.join(requested_symbols)}")

        collection_info = full_payload.get("collection_info", {})
        return {
            **full_payload,
            "collection_info": {
                **collection_info,
                "requested_symbols": requested_symbols,
                "symbols": list(filtered_market_data.keys()),
                "symbols_count": len(filtered_market_data),
            },
            "market_data": filtered_market_data,
        }

    logger.info(
        f"[API] GET /api/regime/market-data/json - authenticated request"
        f" (symbols={requested_symbols if requested_symbols else 'ALL'})"
    )
    
    try:
        key = "regime:market-data:json"
        
        # Try cache (5 min TTL for fresh data)
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for JSON market data")
            return JSONResponse(content=_apply_symbol_filter(json.loads(cached)))
        
        # Cache miss - fetch from database
        logger.info("[API] Cache MISS for JSON market data, querying database")
        data = await asyncio.to_thread(get_regime_market_data_from_db)
        
        if not data or not data.get("market_data"):
            logger.warning("[API] No market data available")
            raise HTTPException(404, "No market data available")
        
        # Cache for 5 minutes
        ttl = 5 * 60
        serialized = json_dumps(data)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached JSON market data for {len(data.get('market_data', {}))} symbols with TTL={ttl}s")
        
        return JSONResponse(content=_apply_symbol_filter(json.loads(serialized)))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/market-data/json: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/regime/{pair}")
async def get_regime_by_pair(pair: str, ctx=Depends(require_session)):
    """Get latest regime analysis for a specific pair"""
    logger.info(f"[API] GET /api/regime/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        key = f"regime:{pair.upper()}"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for regime: {pair}")
            return JSONResponse(content=json.loads(cached))
        
        # Cache miss
        logger.info(f"[API] Cache MISS for regime: {pair}, querying database")
        row = await asyncio.to_thread(get_regime_for_pair, pair)
        
        if not row:
            logger.warning(f"[API] No regime data found for pair: {pair}")
            raise HTTPException(404, f"No regime data for {pair}")
        
        # Cache for 15 minutes
        ttl = 15 * 60
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached regime for {pair} with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/regime/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

# ============================================================================
# NEWS ENDPOINTS
# ============================================================================

@app.get("/api/news/current")
async def get_current_news(
    request: Request, 
    response: Response, 
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_session)
):
    """Get current/recent high-impact forex news with pagination"""
    logger.info(f"[API] GET /api/news/current - User: {ctx.get('user_id', 'anonymous')}, limit={limit}, offset={offset}")
    
    try:
        # Different cache key for each page
        key = f"latest:news:current:{limit}:{offset}"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for current news (offset={offset})")
            cached_payload = json.loads(cached)
            if offset == 0 and isinstance(cached_payload, dict):
                rows = cached_payload.get("news")
                if isinstance(rows, list):
                    NewsCache.set(rows, "all")
                    publish_news_snapshot(rows)
            return JSONResponse(content=cached_payload)
        
        # Cache miss
        logger.info(f"[API] Cache MISS for current news, querying database (offset={offset})")
        rows = await asyncio.to_thread(get_latest_news_from_db, limit, offset)
        total = await asyncio.to_thread(get_news_count)
        
        if not rows:
            logger.info("[API] No current news found in database")
            return JSONResponse(content={"news": [], "total": total, "limit": limit, "offset": offset})
        
        # Cache for 5 minutes
        ttl = 5 * 60
        result = {"news": rows, "total": total, "limit": limit, "offset": offset}
        serialized = json_dumps(result)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached {len(rows)} current news items with TTL={ttl}s")
        if offset == 0:
            NewsCache.set(rows, "all", ttl=ttl)
            publish_news_snapshot(rows)
        
        return JSONResponse(content=json.loads(serialized))
    
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/current: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/news/{item_id:int}")
async def get_news_by_id(item_id: int, request: Request, response: Response, ctx=Depends(require_session)):
    """Fetch a specific news record securely with TTL caching"""
    logger.info(f"[API] GET /api/news/{item_id} - User: {ctx.get('user_id', 'anonymous')}")
    try:
        from app.db import get_news_by_id_from_db
        
        key = f"news:item:{item_id}"
        cached = await REDIS.get(key)
        if cached:
            logger.info(f"[API] Cache HIT for news ID {item_id}")
            return JSONResponse(content=json.loads(cached))
            
        logger.info(f"[API] Cache MISS for news ID {item_id}")
        row = await asyncio.to_thread(get_news_by_id_from_db, item_id)
        
        if not row:
            raise HTTPException(404, "News item not found")
            
        ttl = 60 * 60 # 60 minutes
        serialized = json_dumps(row)
        await REDIS.setex(key, ttl, serialized)
        
        return JSONResponse(content=json.loads(serialized))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/{item_id}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/news/upcoming")
async def get_upcoming_news(request: Request, response: Response, ctx=Depends(require_session)):
    """Get upcoming high-impact forex events"""
    logger.info(f"[API] GET /api/news/upcoming - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        key = "latest:news:upcoming"
        
        # Try cache
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for upcoming news")
            cached_payload = json.loads(cached)
            normalized_cached = cached_payload.get("news", []) if isinstance(cached_payload, dict) else cached_payload
            if not isinstance(normalized_cached, list):
                normalized_cached = []
            return JSONResponse(content=normalized_cached)
        
        # Cache miss
        logger.info("[API] Cache MISS for upcoming news, querying database")
        rows = await asyncio.to_thread(get_upcoming_news_from_db)
        normalized_rows = rows if isinstance(rows, list) else []
        
        if not normalized_rows:
            logger.info("[API] No upcoming news found in database")
            return JSONResponse(content=[])
        
        # Cache for 5 minutes
        ttl = 5 * 60
        serialized = json_dumps(normalized_rows)
        await REDIS.setex(key, ttl, serialized)
        logger.info(f"[API] Cached {len(normalized_rows)} upcoming news items with TTL={ttl}s")
        
        return JSONResponse(content=json.loads(serialized))
    
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/upcoming: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/news/playbook")
async def get_news_playbook(request: Request, response: Response, ctx=Depends(require_session)):
    """Get the latest weekly macro playbook (authenticated session required)."""
    logger.info(f"[API] GET /api/news/playbook - User: {ctx.get('user_id', 'anonymous')}")

    try:
        key = "latest:news:playbook"
        cached = await REDIS.get(key)
        if cached:
            logger.info("[API] Cache HIT for /api/news/playbook")
            return JSONResponse(content=json.loads(cached))

        logger.info("[API] Cache MISS for /api/news/playbook, querying database")
        row = await asyncio.to_thread(get_latest_weekly_macro_playbook_from_db)
        result = {"playbook": [row] if row else []}
        ttl = 5 * 60
        serialized = json_dumps(result)
        await REDIS.setex(key, ttl, serialized)
        return JSONResponse(content=json.loads(serialized))
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/playbook: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/news/events")
async def get_news_events(
    request: Request,
    response: Response,
    upcoming_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx=Depends(require_session),
):
    """Get economic event analysis rows with optional upcoming-only filter."""
    logger.info(
        "[API] GET /api/news/events - User: %s, upcoming_only=%s, limit=%s, offset=%s",
        ctx.get("user_id", "anonymous"),
        upcoming_only,
        limit,
        offset,
    )

    async def _query_events_payload() -> str:
        rows, total = await asyncio.to_thread(
            get_economic_event_analysis_from_db,
            limit,
            offset,
            upcoming_only,
        )
        result = {
            "events": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "upcoming_only": upcoming_only,
        }
        return json_dumps(result)

    try:
        key = f"latest:news:events:{int(upcoming_only)}:{limit}:{offset}"
        cached = await _redis_get_best_effort(key)
        if cached:
            logger.info("[API] Cache HIT for /api/news/events")
            return JSONResponse(content=json.loads(cached))

        lock_key = _singleflight_lock_key(key)
        lock_ttl_raw = (os.getenv("EVENTS_SINGLEFLIGHT_LOCK_TTL_SECONDS") or "15").strip()
        try:
            lock_ttl = max(2, int(lock_ttl_raw))
        except ValueError:
            logger.warning(
                "[API] Invalid EVENTS_SINGLEFLIGHT_LOCK_TTL_SECONDS=%s, defaulting to 15",
                lock_ttl_raw,
            )
            lock_ttl = 15

        wait_timeout_raw = (os.getenv("EVENTS_SINGLEFLIGHT_WAIT_TIMEOUT_SECONDS") or str(lock_ttl)).strip()
        try:
            wait_timeout_seconds = max(0.2, float(wait_timeout_raw))
        except ValueError:
            logger.warning(
                "[API] Invalid EVENTS_SINGLEFLIGHT_WAIT_TIMEOUT_SECONDS=%s, defaulting to %s",
                wait_timeout_raw,
                lock_ttl,
            )
            wait_timeout_seconds = float(lock_ttl)

        lock_acquired, lock_token = await _acquire_redis_lock(lock_key, lock_ttl)

        if not lock_acquired:
            logger.info("[API] Single-flight lock busy for /api/news/events, waiting for warm cache")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(0.2, wait_timeout_seconds)

            while True:
                warmed = await _redis_get_best_effort(key)
                if warmed:
                    logger.info("[API] Cache filled by peer for /api/news/events")
                    return JSONResponse(content=json.loads(warmed))

                lock_exists = await _redis_exists_best_effort(lock_key)
                if lock_exists is not True:
                    lock_acquired, lock_token = await _acquire_redis_lock(lock_key, lock_ttl)
                    if lock_acquired:
                        logger.info("[API] Acquired single-flight lock for /api/news/events after wait")
                        break

                remaining = deadline - loop.time()
                if remaining <= 0:
                    break

                await asyncio.sleep(min(0.2, remaining))

            if not lock_acquired:
                logger.warning(
                    "[API] Single-flight wait timed out for /api/news/events; using local single-flight fallback"
                )
                local_lock = await _get_events_local_singleflight_lock(key)
                try:
                    async with local_lock:
                        warmed_local = await _redis_get_best_effort(key)
                        if warmed_local:
                            logger.info("[API] Cache filled before local fallback DB call for /api/news/events")
                            return JSONResponse(content=json.loads(warmed_local))

                        serialized = await _query_events_payload()
                        ttl = 5 * 60
                        await _redis_setex_best_effort(key, ttl, serialized)
                        return JSONResponse(content=json.loads(serialized))
                finally:
                    await _cleanup_events_local_singleflight_lock(key, local_lock)

        try:
            logger.info("[API] Cache MISS for /api/news/events, querying database")
            serialized = await _query_events_payload()
            ttl = 5 * 60
            await _redis_setex_best_effort(key, ttl, serialized)
            return JSONResponse(content=json.loads(serialized))
        finally:
            if lock_acquired:
                await _release_redis_lock_best_effort(lock_key, lock_token)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/news/events: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.get("/api/news/markers/{symbol}")
async def get_news_markers(
    symbol: str,
    hours: int = None,
    min_importance: int = 3,
    before: Optional[str] = Query(default=None, description="ISO UTC timestamp cursor for older pages"),
    limit: int = Query(default=500, ge=50, le=1000),
    ctx=Depends(require_signals_context),
):
    """Get news markers for chart annotations
    
    Args:
        symbol: Trading pair (e.g., XAUUSD)
        hours: Time range in hours (default: None = all time)
        min_importance: Minimum importance score (1-5, default 3)
        before: Optional cursor. Returns rows strictly older than this timestamp.
        limit: Maximum rows returned.
    
    Returns:
        List of news events with timestamps for chart markers
    """
    import psycopg
    from psycopg.rows import dict_row
    from datetime import datetime, timedelta, timezone
    from .cache import NewsMarkersCache
    
    symbol = symbol.upper()
    
    # If hours not specified, use large default (1 year)
    if hours is None:
        hours = 8760  # 365 days
    
    cursor_before: Optional[datetime] = None
    if before:
        try:
            normalized_before = before.strip()
            if normalized_before.endswith("Z"):
                normalized_before = f"{normalized_before[:-1]}+00:00"
            parsed_before = datetime.fromisoformat(normalized_before)
            if parsed_before.tzinfo is None:
                parsed_before = parsed_before.replace(tzinfo=timezone.utc)
            cursor_before = parsed_before.astimezone(timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid 'before' cursor. Use ISO UTC timestamp.")

    logger.info(
        "GET /api/news/markers/%s?hours=%s&min_importance=%s&before=%s&limit=%s - User: %s",
        symbol,
        hours,
        min_importance,
        before,
        limit,
        ctx.get("user_id", "anonymous"),
    )
    
    use_cache = cursor_before is None and limit == 500

    # Try cache first (cache key includes importance filter) for first page only.
    if use_cache:
        cached_markers = NewsMarkersCache.get(symbol, hours, min_importance)
        if cached_markers is not None:
            # Filter by importance on cache hit
            filtered = [m for m in cached_markers if m.get('importance', 0) >= min_importance]
            has_more = len(filtered) >= limit
            cursor = filtered[-1]['time'] if filtered else None
            logger.info(f"Cache HIT: news markers for {symbol} ({len(filtered)}/{len(cached_markers)} after importance filter)")
            return {"markers": filtered, "has_more": has_more, "cursor_before": cursor}
    
    logger.info(f"Cache MISS: news markers for {symbol}, querying database")
    
    try:
        DATABASE_URL = os.getenv("DATABASE_URL")
        if not DATABASE_URL:
            pg_host = os.getenv("POSTGRES_HOST", "localhost")
            pg_port = os.getenv("POSTGRES_PORT", "5432")
            pg_db = os.getenv("POSTGRES_DB", "ai_trading_bot_data")
            pg_user = os.getenv("POSTGRES_USER", "postgres")
            pg_password = os.getenv("POSTGRES_PASSWORD", "")
            DATABASE_URL = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"
        
        # Calculate time range
        range_end = cursor_before or datetime.now(timezone.utc)
        start_time = range_end - timedelta(hours=hours)
        
        # Map symbol to instruments (handle different naming conventions)
        symbol_map = {
            'XAUUSD': ['XAU/USD', 'GOLD', 'XAUUSD'],
            'EURUSD': ['EUR/USD', 'EURUSD', 'EUR'],
            'GBPUSD': ['GBP/USD', 'GBPUSD', 'GBP'],
            'USDJPY': ['USD/JPY', 'USDJPY', 'JPY'],
            'USDCAD': ['USD/CAD', 'USDCAD', 'CAD'],
            'AUDUSD': ['AUD/USD', 'AUDUSD', 'AUD']
        }
        
        instruments = symbol_map.get(symbol, [symbol])
        
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Query news relevant to this symbol within time range
                query = """
                    SELECT 
                        email_id,
                        headline,
                        email_received_at as time,
                        importance_score,
                        sentiment_score,
                        market_impact_prediction,
                        volatility_expectation,
                        forex_instruments,
                        breaking_news,
                        central_bank_related,
                        news_category
                    FROM email_news_analysis
                    WHERE forex_relevant = true
                        AND email_received_at >= %s
                        AND email_received_at < %s
                        AND importance_score >= %s
                        AND (
                            primary_instrument = ANY(%s::text[])
                            OR COALESCE(forex_instruments, '[]'::jsonb) ?| %s::text[]
                        )
                    ORDER BY email_received_at DESC
                    LIMIT %s
                """
                
                cur.execute(query, (start_time, range_end, min_importance, instruments, instruments, limit + 1))
                news_items = cur.fetchall()

        has_more = len(news_items) > limit
        if has_more:
            news_items = news_items[:limit]
        
        # Format for chart markers
        markers = []
        for item in news_items:
            # Determine marker color based on sentiment and impact
            color = '#64748b'  # neutral grey
            if item['market_impact_prediction'] == 'bullish':
                color = '#22c55e'  # green
            elif item['market_impact_prediction'] == 'bearish':
                color = '#ef4444'  # red
            elif item['breaking_news'] or item['importance_score'] >= 5:
                color = '#f59e0b'  # orange for breaking/high importance
            
            # Marker shape based on type
            shape = 'circle'
            if item['central_bank_related']:
                shape = 'arrowDown'
            elif item['breaking_news']:
                shape = 'arrowUp'
            
            markers.append({
                'time': item['time'].isoformat() if item['time'] else None,
                'id': item['email_id'],
                'headline': item['headline'][:100],  # Truncate for marker
                'full_headline': item['headline'],
                'importance': item['importance_score'],
                'sentiment': float(item['sentiment_score']) if item['sentiment_score'] else 0,
                'impact': item['market_impact_prediction'],
                'volatility': item['volatility_expectation'],
                'instruments': item['forex_instruments'],
                'breaking': item['breaking_news'],
                'category': item['news_category'],
                'color': color,
                'shape': shape
            })
        
        # Cache only first page responses.
        if use_cache:
            NewsMarkersCache.set(symbol, markers, hours, min_importance)
        
        logger.info(f"Returning {len(markers)} news markers for {symbol}")
        return {
            "markers": markers,
            "has_more": has_more,
            "cursor_before": markers[-1]['time'] if markers else None,
        }
        
    except Exception as e:
        logger.error(f"Error fetching news markers for {symbol}: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to fetch news markers: {str(e)}")

# ============================================================================
# PERFORMANCE ANALYTICS
# ============================================================================

@app.get("/api/performance/{pair}")
async def get_performance(pair: str, ctx=Depends(require_session)):
    """Get performance metrics for a trading pair"""
    logger.info(f"[API] GET /api/performance/{pair} - User: {ctx.get('user_id', 'anonymous')}")
    
    try:
        metrics = await asyncio.to_thread(get_pair_performance, pair)
        if not metrics:
            logger.warning(f"[API] No performance data found for pair: {pair}")
            return JSONResponse(content={"message": f"No trade history for {pair}"})
        
        logger.info(f"[API] Performance for {pair}: {metrics.get('total_trades')} trades")
        serialized = json_dumps(metrics)
        return JSONResponse(content=json.loads(serialized))
    except Exception as e:
        logger.error(f"[API ERROR] /api/performance/{pair}: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

# ============================================================================
# MT5 TRADE TRACKING ENDPOINTS (Future)
# ============================================================================

@app.post("/api/trades/outcome")
async def record_trade_outcome(request: Request):
    """Record MT5 trade execution (called by EA when opening position)"""
    auth_source = _require_internal_api_key(
        request,
        "MT5_TRADE_WEBHOOK_KEY",
        "N8N_MARKET_DATA_KEY",
    )
    logger.info("[API] POST /api/trades/outcome - Internal auth=%s", auth_source)
    
    try:
        trade_data = await request.json()
        logger.info(f"[API] Recording trade outcome for ticket: {trade_data.get('ticket')}")
        
        result = await asyncio.to_thread(insert_trade_outcome, trade_data)
        logger.info(f"[API] Trade recorded with signal_id: {result['signal_id']}")
        
        return JSONResponse(content={"signal_id": result['signal_id'], "status": "recorded"})
    except Exception as e:
        logger.error(f"[API ERROR] /api/trades/outcome: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")

@app.put("/api/trades/{ticket}/close")
async def close_trade(ticket: int, request: Request):
    """Update trade when closed in MT5 (records P/L, exit price, outcome)"""
    auth_source = _require_internal_api_key(
        request,
        "MT5_TRADE_WEBHOOK_KEY",
        "N8N_MARKET_DATA_KEY",
    )
    logger.info("[API] PUT /api/trades/%s/close - Internal auth=%s", ticket, auth_source)
    
    try:
        outcome_data = await request.json()
        logger.info(f"[API] Closing trade {ticket} with P/L: {outcome_data.get('pnl')}")
        
        result = await asyncio.to_thread(update_trade_outcome, ticket, outcome_data)
        
        if not result:
            logger.warning(f"[API] No signal found with ticket: {ticket}")
            raise HTTPException(404, f"No signal found with ticket {ticket}")
        
        logger.info(f"[API] Trade {ticket} closed successfully")
        return JSONResponse(content={"signal_id": result['signal_id'], "status": "updated"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API ERROR] /api/trades/{ticket}/close: {str(e)}", exc_info=True)
        raise HTTPException(500, "Internal server error")
