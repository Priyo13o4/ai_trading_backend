"""
Server-Sent Events (SSE) endpoints for real-time updates.

Uses redis.asyncio for fully non-blocking pub/sub.
A single Uvicorn process (api-sse) can hold tens of thousands of
concurrent SSE connections since each is just an asyncio coroutine
waiting on an async Redis SUBSCRIBE — no threads, no event-loop blocking.

Auth is provided via `_sse_auth` sentinel:
  - In api-web: main.py overrides it with require_session
  - In api-sse: sse_main.py overrides it with require_session
This lets sse.py remain auth-agnostic and avoids circular imports.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import AsyncGenerator
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from app.utils import json_dumps

from .cache import NewsCache, StrategyCache, get_last_candle_update, PubSubManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])

HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("SSE_HEARTBEAT_INTERVAL_SECONDS", "15"))
ENABLE_SIGNAL_MUX_SSE = os.getenv("ENABLE_SIGNAL_MUX_SSE", "0").strip().lower() not in {"0", "false", "no", "off"}
SSE_PUBSUB_POOL_SIZE = int(os.getenv("SSE_PUBSUB_POOL_SIZE", "50"))
SSE_OBS_ENABLED = os.getenv("SSE_OBS_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}
SSE_OBS_TOPIC_SAMPLE_EVERY = max(1, int(os.getenv("SSE_OBS_TOPIC_SAMPLE_EVERY", "100")))
SSE_OBS_RECONNECT_WINDOW_SECONDS = max(0.0, float(os.getenv("SSE_OBS_RECONNECT_WINDOW_SECONDS", "30")))
SSE_OBS_RECONNECT_MAP_MAX_SIZE = max(128, int(os.getenv("SSE_OBS_RECONNECT_MAP_MAX_SIZE", "20000")))
SSE_OBS_RECONNECT_MAP_CLEANUP_INTERVAL_SECONDS = max(
    5.0,
    float(os.getenv("SSE_OBS_RECONNECT_MAP_CLEANUP_INTERVAL_SECONDS", "60")),
)
_SSE_EVENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Phase-3 observation mode (process-local only; no cross-process aggregation).
_MUX_ACTIVE_CONNECTIONS = 0
_MUX_LAST_DISCONNECT_AT: dict[str, float] = {}
_MUX_LAST_DISCONNECT_CLEANUP_AT = 0.0
_MUX_TOPIC_SAMPLE_COUNTS: dict[str, int] = {"news": 0, "strategy": 0, "candle": 0}
_MUX_TOPIC_SAMPLE_TOTAL = 0
_MUX_TOPIC_SAMPLE_STARTED_AT = time.monotonic()

# ── Async Redis client for pubsub (one per process, shared across all coroutines) ──
def _build_pubsub_url() -> str:
    url = os.getenv("PUBSUB_REDIS_URL") or os.getenv("CACHE_REDIS_URL")
    if url:
        return url
    host = os.getenv("REDIS_HOST", "redis")
    port = os.getenv("REDIS_PORT", "6379")
    db = os.getenv("REDIS_DB", "0")
    password = os.getenv("REDIS_PASSWORD", "")
    return f"redis://:{password}@{host}:{port}/{db}"


_PUBSUB_REDIS: aioredis.Redis | None = None


def _get_pubsub_redis() -> aioredis.Redis:
    """Return the shared async Redis client (lazy-init, process-singleton)."""
    global _PUBSUB_REDIS
    if _PUBSUB_REDIS is None:
        _PUBSUB_REDIS = aioredis.from_url(
            _build_pubsub_url(),
            decode_responses=True,
            # Use a dedicated connection pool for pubsub so it doesn't
            # compete with regular GET/SET commands.
            max_connections=SSE_PUBSUB_POOL_SIZE,
        )
    return _PUBSUB_REDIS


def _sse_obs_sanitize_ip(client_ip: str) -> str:
    if not client_ip:
        return "unknown"
    return f"h:{hashlib.sha256(client_ip.encode('utf-8')).hexdigest()[:12]}"


def _sse_obs_cleanup_reconnect_map(now: float) -> None:
    global _MUX_LAST_DISCONNECT_CLEANUP_AT

    if not _MUX_LAST_DISCONNECT_AT:
        _MUX_LAST_DISCONNECT_CLEANUP_AT = now
        return

    should_cleanup = (
        len(_MUX_LAST_DISCONNECT_AT) > SSE_OBS_RECONNECT_MAP_MAX_SIZE
        or (now - _MUX_LAST_DISCONNECT_CLEANUP_AT) >= SSE_OBS_RECONNECT_MAP_CLEANUP_INTERVAL_SECONDS
    )
    if not should_cleanup:
        return

    if SSE_OBS_RECONNECT_WINDOW_SECONDS > 0:
        cutoff = now - SSE_OBS_RECONNECT_WINDOW_SECONDS
        stale_keys = [k for k, ts in _MUX_LAST_DISCONNECT_AT.items() if ts < cutoff]
        for key in stale_keys:
            _MUX_LAST_DISCONNECT_AT.pop(key, None)

    overflow = len(_MUX_LAST_DISCONNECT_AT) - SSE_OBS_RECONNECT_MAP_MAX_SIZE
    if overflow > 0:
        # Keep the most recent entries only when map grows beyond bound.
        newest = sorted(_MUX_LAST_DISCONNECT_AT.items(), key=lambda item: item[1], reverse=True)
        _MUX_LAST_DISCONNECT_AT.clear()
        _MUX_LAST_DISCONNECT_AT.update(newest[:SSE_OBS_RECONNECT_MAP_MAX_SIZE])

    _MUX_LAST_DISCONNECT_CLEANUP_AT = now


def _sse_obs_track_mux_connect(client_key: str, *, pair: str, symbol: str, timeframe: str, client_ip: str) -> None:
    if not SSE_OBS_ENABLED:
        return

    global _MUX_ACTIVE_CONNECTIONS
    _MUX_ACTIVE_CONNECTIONS += 1

    now = time.monotonic()
    _sse_obs_cleanup_reconnect_map(now)
    last_disconnect = _MUX_LAST_DISCONNECT_AT.get(client_key)
    reconnect_within_window = (
        last_disconnect is not None
        and SSE_OBS_RECONNECT_WINDOW_SECONDS > 0
        and (now - last_disconnect) <= SSE_OBS_RECONNECT_WINDOW_SECONDS
    )

    logger.info(
        "[SSE][OBS] mux_connect active=%s reconnect_within_window=%s pair=%s symbol=%s tf=%s ip=%s",
        _MUX_ACTIVE_CONNECTIONS,
        reconnect_within_window,
        pair,
        symbol,
        timeframe,
        _sse_obs_sanitize_ip(client_ip),
    )


def _sse_obs_track_mux_disconnect(client_key: str, *, pair: str, symbol: str, timeframe: str, client_ip: str) -> None:
    if not SSE_OBS_ENABLED:
        return

    global _MUX_ACTIVE_CONNECTIONS
    _MUX_ACTIVE_CONNECTIONS = max(0, _MUX_ACTIVE_CONNECTIONS - 1)
    now = time.monotonic()
    _MUX_LAST_DISCONNECT_AT[client_key] = now
    _sse_obs_cleanup_reconnect_map(now)

    logger.info(
        "[SSE][OBS] mux_disconnect active=%s pair=%s symbol=%s tf=%s ip=%s",
        _MUX_ACTIVE_CONNECTIONS,
        pair,
        symbol,
        timeframe,
        _sse_obs_sanitize_ip(client_ip),
    )


def _sse_obs_track_mux_topic(topic: str) -> None:
    if not SSE_OBS_ENABLED:
        return
    if topic not in _MUX_TOPIC_SAMPLE_COUNTS:
        return

    global _MUX_TOPIC_SAMPLE_TOTAL, _MUX_TOPIC_SAMPLE_STARTED_AT
    _MUX_TOPIC_SAMPLE_COUNTS[topic] += 1
    _MUX_TOPIC_SAMPLE_TOTAL += 1

    if _MUX_TOPIC_SAMPLE_TOTAL < SSE_OBS_TOPIC_SAMPLE_EVERY:
        return

    now = time.monotonic()
    elapsed_s = max(0.001, now - _MUX_TOPIC_SAMPLE_STARTED_AT)
    news_count = _MUX_TOPIC_SAMPLE_COUNTS["news"]
    strategy_count = _MUX_TOPIC_SAMPLE_COUNTS["strategy"]
    candle_count = _MUX_TOPIC_SAMPLE_COUNTS["candle"]

    logger.info(
        "[SSE][OBS] mux_topic_sample window_s=%.2f total=%s news=%s strategy=%s candle=%s news_eps=%.2f strategy_eps=%.2f candle_eps=%.2f",
        elapsed_s,
        _MUX_TOPIC_SAMPLE_TOTAL,
        news_count,
        strategy_count,
        candle_count,
        news_count / elapsed_s,
        strategy_count / elapsed_s,
        candle_count / elapsed_s,
    )

    _MUX_TOPIC_SAMPLE_COUNTS["news"] = 0
    _MUX_TOPIC_SAMPLE_COUNTS["strategy"] = 0
    _MUX_TOPIC_SAMPLE_COUNTS["candle"] = 0
    _MUX_TOPIC_SAMPLE_TOTAL = 0
    _MUX_TOPIC_SAMPLE_STARTED_AT = now


# ── Helpers ────────────────────────────────────────────────────────────────────

def _server_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_pair_from_payload(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("symbol") or payload.get("pair") or payload.get("trading_pair")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _strategy_matches_pair(payload: dict, normalized_pair: str | None) -> bool:
    if not normalized_pair:
        return True
    return _strategy_pair_from_payload(payload) == normalized_pair


def _sanitize_sse_event_name(event_name: str | None) -> str | None:
    if not isinstance(event_name, str):
        return None
    candidate = event_name.strip()
    if not candidate or not _SSE_EVENT_NAME_RE.fullmatch(candidate):
        return None
    return candidate


def _format_typed_event(
    payload: dict,
    fallback_event: str | None = None,
    *,
    include_event_name: bool = False,
) -> str:
    if include_event_name:
        event_name = payload.get("type") if isinstance(payload, dict) else None
        event_name = _sanitize_sse_event_name(event_name) or _sanitize_sse_event_name(fallback_event)
        if event_name:
            return f"event: {event_name}\ndata: {json_dumps(payload)}\n\n"
    return f"data: {json_dumps(payload)}\n\n"


def _normalize_required_query_param(name: str, value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{name} must be a non-empty string")
    return normalized


async def _ensure_signals_mux_enabled() -> None:
    if not ENABLE_SIGNAL_MUX_SSE:
        raise HTTPException(status_code=503, detail="signals SSE stream is disabled")


def _is_candle_match(payload: dict, symbol: str, timeframe: str) -> bool:
    if not isinstance(payload, dict):
        return False
    payload_symbol = str(payload.get("symbol") or "").upper()
    payload_timeframe = str(payload.get("timeframe") or "").upper()
    if payload_symbol != symbol:
        return False
    if timeframe == "ALL":
        # Treat ALL as wildcard so mux clients can share one stream and filter client-side.
        return bool(payload_timeframe)
    return payload_timeframe == timeframe


async def multiplex_event_generator(
    request: Request,
    *,
    pair: str,
    symbol: str,
    timeframe: str,
    include_event_name: bool = False,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncGenerator[str, None]:
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    masked_client_ip = _sse_obs_sanitize_ip(client_ip)
    redis = _get_pubsub_redis()
    pubsub = redis.pubsub()
    channels = [
        PubSubManager.CHANNELS["candles"],
        PubSubManager.CHANNELS["news"],
        PubSubManager.CHANNELS["strategies"],
    ]
    client_key = f"{client_ip}:{pair}:{symbol}:{timeframe}"
    obs_connected = False

    try:
        await pubsub.subscribe(*channels)
        logger.info(
            "[SSE] +OPEN  channel=signals pair=%s symbol=%s tf=%s ip=%s",
            pair,
            symbol,
            timeframe,
            masked_client_ip,
        )
        _sse_obs_track_mux_connect(
            client_key,
            pair=pair,
            symbol=symbol,
            timeframe=timeframe,
            client_ip=client_ip,
        )
        obs_connected = True

        yield _format_typed_event(
            {"type": "connected", "channel": "signals"},
            fallback_event="connected",
            include_event_name=include_event_name,
        )

        candle_snapshot = await asyncio.to_thread(get_last_candle_update, symbol, timeframe, prefer_forming=True)
        if candle_snapshot is not None:
            candle_snapshot = {**candle_snapshot, "is_snapshot": True}
            yield _format_typed_event(candle_snapshot, include_event_name=include_event_name)
        else:
            yield _format_typed_event(
                {
                    "type": "candle_snapshot",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle": None,
                    "server_ts": _server_timestamp(),
                },
                include_event_name=include_event_name,
            )

        news_snapshot = await asyncio.to_thread(NewsCache.get, "all") or []
        yield _format_typed_event(
            {"type": "news_snapshot", "news": news_snapshot, "server_ts": _server_timestamp()},
            include_event_name=include_event_name,
        )

        strategy_snapshot = await asyncio.to_thread(StrategyCache.get, pair) or []
        strategy_snapshot = [item for item in strategy_snapshot if _strategy_matches_pair(item, pair)]
        yield _format_typed_event(
            {
                "type": "strategies_snapshot",
                "strategies": strategy_snapshot,
                "server_ts": _server_timestamp(),
            },
            include_event_name=include_event_name,
        )

        last_heartbeat = time.monotonic()

        while True:
            if await request.is_disconnected():
                logger.info(
                    "[SSE] -CLOSE channel=signals pair=%s symbol=%s tf=%s ip=%s",
                    pair,
                    symbol,
                    timeframe,
                    masked_client_ip,
                )
                break

            poll_timeout_s = min(5.0, max(0.25, heartbeat_interval_s if heartbeat_interval_s > 0 else 1.0))
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=poll_timeout_s,
            )

            if message and message.get("type") == "message":
                raw = message.get("data")
                if not isinstance(raw, str):
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type") if isinstance(data, dict) else None
                candle_match = _is_candle_match(data, symbol, timeframe)
                if event_type == "candle_update" or candle_match:
                    if candle_match:
                        _sse_obs_track_mux_topic("candle")
                        yield _format_typed_event(data, include_event_name=include_event_name)
                    continue

                if event_type == "news_update" or event_type == "news_snapshot":
                    _sse_obs_track_mux_topic("news")
                    yield _format_typed_event(data, include_event_name=include_event_name)
                    continue

                if event_type == "strategy_update":
                    strategy_payload = data.get("strategy") if isinstance(data.get("strategy"), dict) else data
                    if _strategy_matches_pair(strategy_payload, pair):
                        _sse_obs_track_mux_topic("strategy")
                        yield _format_typed_event(data, include_event_name=include_event_name)
                    continue

                if event_type == "strategies_snapshot":
                    strategies_payload = data.get("strategies")
                    if isinstance(strategies_payload, list):
                        filtered = [item for item in strategies_payload if _strategy_matches_pair(item, pair)]
                        data = {**data, "strategies": filtered}
                        _sse_obs_track_mux_topic("strategy")
                        yield _format_typed_event(data, include_event_name=include_event_name)
                    continue

                continue

            now = time.monotonic()
            if heartbeat_interval_s > 0 and now - last_heartbeat >= heartbeat_interval_s:
                heartbeat = {"type": "heartbeat", "server_ts": _server_timestamp()}
                yield _format_typed_event(
                    heartbeat,
                    fallback_event="heartbeat",
                    include_event_name=include_event_name,
                )
                last_heartbeat = now

    except asyncio.CancelledError:
        logger.info("[SSE] CANCEL channel=signals pair=%s symbol=%s tf=%s ip=%s", pair, symbol, timeframe, masked_client_ip)
    except Exception as exc:
        logger.error(
            "[SSE] ERROR  channel=signals pair=%s symbol=%s tf=%s ip=%s error=%s",
            pair,
            symbol,
            timeframe,
            masked_client_ip,
            exc,
        )
        yield _format_typed_event(
            {"type": "error", "message": str(exc)},
            fallback_event="error",
            include_event_name=include_event_name,
        )
    finally:
        if obs_connected:
            _sse_obs_track_mux_disconnect(
                client_key,
                pair=pair,
                symbol=symbol,
                timeframe=timeframe,
                client_ip=client_ip,
            )
        try:
            # Closing the pubsub connection is enough to release subscriptions.
            # Avoid unsubscribe() in finally because it can try to reconnect when
            # Redis is saturated and raise "Too many connections".
            await pubsub.aclose()
        except Exception as exc:
            logger.debug(
                "[SSE] pubsub close failed channel=signals pair=%s symbol=%s tf=%s ip=%s err=%s",
                pair,
                symbol,
                timeframe,
                masked_client_ip,
                exc,
            )
        logger.debug("[SSE] pubsub closed channel=signals pair=%s symbol=%s tf=%s ip=%s", pair, symbol, timeframe, masked_client_ip)


# ── Core async event generator ─────────────────────────────────────────────────

async def event_generator(
    channel: str,
    request: Request,
    *,
    initial_payloads: list[dict] | None = None,
    send_connected: bool = True,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncGenerator[str, None]:
    """
    Fully async SSE generator backed by redis.asyncio pubsub.

    Each call creates its own async pubsub connection; on disconnect the
    connection is closed immediately — no thread is ever blocked.
    """
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    masked_client_ip = _sse_obs_sanitize_ip(client_ip)
    redis = _get_pubsub_redis()
    pubsub = redis.pubsub()

    try:
        await pubsub.subscribe(channel)
        logger.info("[SSE] +OPEN  channel=%s ip=%s", channel, masked_client_ip)

        if send_connected:
            yield f"data: {json_dumps({'type': 'connected', 'channel': channel})}\n\n"

        if initial_payloads:
            for payload in initial_payloads:
                yield f"data: {json_dumps(payload)}\n\n"

        last_heartbeat = time.monotonic()

        while True:
            # Disconnect checks happen once per poll cycle to avoid tight idle loops.
            if await request.is_disconnected():
                logger.info("[SSE] -CLOSE channel=%s ip=%s", channel, masked_client_ip)
                break

            # Block up to a short timeout so idle connections do not spin CPU.
            poll_timeout_s = min(5.0, max(0.25, heartbeat_interval_s if heartbeat_interval_s > 0 else 1.0))
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=poll_timeout_s,
            )

            if message and message.get("type") == "message":
                yield f"data: {message['data']}\n\n"
                # Process next message immediately without sleeping
                continue

            # Heartbeat check
            now = time.monotonic()
            if heartbeat_interval_s > 0 and now - last_heartbeat >= heartbeat_interval_s:
                heartbeat = {"type": "heartbeat", "server_ts": _server_timestamp()}
                yield f"data: {json_dumps(heartbeat)}\n\n"
                last_heartbeat = now

    except asyncio.CancelledError:
        logger.info("[SSE] CANCEL channel=%s ip=%s", channel, masked_client_ip)
    except Exception as exc:
        logger.error("[SSE] ERROR  channel=%s ip=%s error=%s", channel, masked_client_ip, exc)
        yield f"data: {json_dumps({'type': 'error', 'message': str(exc)})}\n\n"
    finally:
        try:
            # Closing the pubsub connection releases channel subscriptions.
            # Avoid unsubscribe() here to prevent reconnect attempts during teardown.
            await pubsub.aclose()
        except Exception as exc:
            logger.debug("[SSE] pubsub close failed channel=%s ip=%s err=%s", channel, masked_client_ip, exc)
        logger.debug("[SSE] pubsub closed channel=%s ip=%s", channel, masked_client_ip)



# ── Auth sentinel ─────────────────────────────────────────────────────────────
# Named function (not a lambda) so callers can override via:
#   app.dependency_overrides[_sse_auth] = require_session

async def _sse_auth(request: Request):
    """Placeholder — overridden by the host app (api-web or api-sse)."""
    return None  # no-op: overridden at startup by main.py / sse_main.py


# ── SSE stream endpoints ───────────────────────────────────────────────────────

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/candles/{symbol}/{timeframe}")
async def stream_candles(
    symbol: str,
    timeframe: str,
    request: Request,
    _ctx=Depends(_sse_auth),
):
    """Stream real-time candle updates for a specific symbol and timeframe."""
    symbol = symbol.upper()
    timeframe = timeframe.upper()
    logger.debug("[SSE] candle stream requested symbol=%s tf=%s", symbol, timeframe)

    snapshot = await asyncio.to_thread(get_last_candle_update, symbol, timeframe, prefer_forming=True)
    if snapshot is not None:
        snapshot = {**snapshot, "is_snapshot": True}

    async def filtered_generator():
        async for event in event_generator(
            PubSubManager.CHANNELS["candles"],
            request,
            initial_payloads=[snapshot] if snapshot else None,
        ):
            if "data: " not in event or event.strip() == "data:":
                yield event
                continue
            try:
                data = json.loads(event[len("data: "):].strip())
                if data.get("type") == "connected" or (
                    data.get("symbol") == symbol and data.get("timeframe") == timeframe
                ):
                    yield event
            except json.JSONDecodeError:
                yield event

    return StreamingResponse(filtered_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/candles")
async def stream_all_candles(request: Request, _ctx=Depends(_sse_auth)):
    """Stream real-time candle updates for ALL symbols and timeframes."""
    logger.debug("[SSE] all-candles stream requested")
    return StreamingResponse(
        event_generator(PubSubManager.CHANNELS["candles"], request),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/news")
async def stream_news(request: Request, _ctx=Depends(_sse_auth)):
    """Stream real-time news updates."""
    logger.debug("[SSE] news stream requested")
    snapshot = await asyncio.to_thread(NewsCache.get, "all") or []
    initial_payloads = None
    if snapshot:
        initial_payloads = [{"type": "news_snapshot", "news": snapshot, "server_ts": _server_timestamp()}]

    return StreamingResponse(
        event_generator(PubSubManager.CHANNELS["news"], request, initial_payloads=initial_payloads),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/strategies")
async def stream_strategies(request: Request, pair: str | None = None, _ctx=Depends(_sse_auth)):
    """Stream real-time strategy updates, optionally filtered by pair."""
    logger.debug("[SSE] strategy stream requested pair=%s", pair or "all")
    normalized_pair = pair.upper() if pair else None
    snapshot = await asyncio.to_thread(StrategyCache.get, normalized_pair or "all") or []
    if normalized_pair:
        snapshot = [item for item in snapshot if _strategy_matches_pair(item, normalized_pair)]

    initial_payloads = [{"type": "strategies_snapshot", "strategies": snapshot, "server_ts": _server_timestamp()}]

    async def filtered_generator():
        async for event in event_generator(
            PubSubManager.CHANNELS["strategies"],
            request,
            initial_payloads=initial_payloads,
        ):
            if not normalized_pair:
                yield event
                continue
            if "data: " not in event or event.strip() == "data:":
                yield event
                continue
            try:
                data = json.loads(event[len("data: "):].strip())
            except json.JSONDecodeError:
                yield event
                continue

            event_type = data.get("type")
            if event_type in {"connected", "heartbeat", "error"}:
                yield event
                continue
            if event_type == "strategy_update":
                strategy_payload = data.get("strategy") if isinstance(data.get("strategy"), dict) else data
                if _strategy_matches_pair(strategy_payload, normalized_pair):
                    yield event
                continue
            if event_type == "strategies_snapshot":
                strategies_payload = data.get("strategies")
                if isinstance(strategies_payload, list):
                    filtered = [item for item in strategies_payload if _strategy_matches_pair(item, normalized_pair)]
                    data["strategies"] = filtered
                    yield f"data: {json_dumps(data)}\n\n"
                continue
            if _strategy_matches_pair(data, normalized_pair):
                yield event

    return StreamingResponse(filtered_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/signals", dependencies=[Depends(_ensure_signals_mux_enabled), Depends(_sse_auth)])
async def stream_signals(
    request: Request,
    pair: str,
    symbol: str,
    timeframe: str,
    named_events: bool = False,
):
    """Multiplex news, strategy, and candle updates for the Signals page."""
    normalized_pair = _normalize_required_query_param("pair", pair)
    normalized_symbol = _normalize_required_query_param("symbol", symbol)
    normalized_timeframe = _normalize_required_query_param("timeframe", timeframe)
    logger.debug(
        "[SSE] signals mux stream requested pair=%s symbol=%s tf=%s named_events=%s",
        normalized_pair,
        normalized_symbol,
        normalized_timeframe,
        named_events,
    )

    return StreamingResponse(
        multiplex_event_generator(
            request,
            pair=normalized_pair,
            symbol=normalized_symbol,
            timeframe=normalized_timeframe,
            include_event_name=named_events,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/health")
async def stream_health():
    """Health check for SSE endpoints."""
    from .cache import redis_client
    try:
        redis_client.ping()
        return {"status": "healthy", "redis": "connected", "channels": list(PubSubManager.CHANNELS.values())}
    except Exception as exc:
        return {"status": "unhealthy", "redis": "disconnected", "error": str(exc)}
