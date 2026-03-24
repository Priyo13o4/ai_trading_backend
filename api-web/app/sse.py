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
import itertools
import json
import logging
import os
import re
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import AsyncGenerator, Iterable
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from app.utils import json_dumps

from .cache import NewsCache, StrategyCache, get_last_candle_update, PubSubManager
from .authn.authz import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])

HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("SSE_HEARTBEAT_INTERVAL_SECONDS", "15"))
ENABLE_SIGNAL_MUX_SSE = os.getenv("ENABLE_SIGNAL_MUX_SSE", "0").strip().lower() not in {"0", "false", "no", "off"}
SSE_PUBSUB_POOL_SIZE = int(os.getenv("SSE_PUBSUB_POOL_SIZE", "50"))
SSE_CLIENT_QUEUE_SIZE = max(10, int(os.getenv("SSE_CLIENT_QUEUE_SIZE", "100")))
SSE_MAX_CONNECTIONS = max(1, int(os.getenv("SSE_MAX_CONNECTIONS", "1000")))
SSE_REDIS_LATENCY_THRESHOLD_MS = max(1.0, float(os.getenv("SSE_REDIS_LATENCY_THRESHOLD_MS", "100")))
SSE_ADMISSION_RETRY_AFTER_SECONDS = max(1, int(os.getenv("SSE_ADMISSION_RETRY_AFTER_SECONDS", "5")))
SSE_REPLAY_MAX_EVENTS = max(100, int(os.getenv("SSE_REPLAY_MAX_EVENTS", "1000")))
SSE_REPLAY_MAX_AGE_SECONDS = max(30.0, float(os.getenv("SSE_REPLAY_MAX_AGE_SECONDS", "300")))
SSE_REPLAY_REDIS_ENABLED = os.getenv("SSE_REPLAY_REDIS_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}
SSE_REPLAY_REDIS_KEY_PREFIX = os.getenv("SSE_REPLAY_REDIS_KEY_PREFIX", "sse:replay")
SSE_REPLAY_REDIS_OP_TIMEOUT_SECONDS = max(0.01, float(os.getenv("SSE_REPLAY_REDIS_OP_TIMEOUT_SECONDS", "0.2")))
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
_SSE_EVENT_ID_COUNTER = itertools.count(int(time.time() * 1000))
_SSE_EVENT_ID_LAST = 0

# ── Async Redis client for pubsub (one per process, shared across all coroutines) ──
def _build_pubsub_url() -> str:
    url = os.getenv("PUBSUB_REDIS_URL") or os.getenv("APP_REDIS_URL") or os.getenv("CACHE_REDIS_URL")
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


def _next_sse_event_id() -> str:
    # Keep IDs numeric for Last-Event-ID compatibility while improving
    # cross-process ordering by anchoring to wall-clock milliseconds.
    global _SSE_EVENT_ID_LAST
    candidate = int(time.time() * 1000)
    counter_value = next(_SSE_EVENT_ID_COUNTER)
    if counter_value > candidate:
        candidate = counter_value
    if candidate <= _SSE_EVENT_ID_LAST:
        candidate = _SSE_EVENT_ID_LAST + 1
    _SSE_EVENT_ID_LAST = candidate
    return str(candidate)


def _normalize_pubsub_channel(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


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


class EventReplayBuffer:
    """Bounded in-memory replay buffer for recent Redis-backed SSE events."""

    def __init__(self, max_events: int, max_age_seconds: float):
        self.max_events = max_events
        self.max_age_seconds = max_age_seconds
        self._events: OrderedDict[str, dict] = OrderedDict()

    def _prune(self, now: float) -> None:
        while len(self._events) > self.max_events:
            self._events.popitem(last=False)

        cutoff = now - self.max_age_seconds
        while self._events:
            oldest_id, entry = next(iter(self._events.items()))
            if entry["ts"] >= cutoff:
                break
            self._events.pop(oldest_id, None)

    def store(self, event_id: str, channel: str, payload: str) -> None:
        now = time.monotonic()
        self._events[event_id] = {
            "id": event_id,
            "channel": channel,
            "payload": payload,
            "ts": now,
        }
        self._prune(now)

    def get_events_since(self, last_event_id: str, channels: Iterable[str]) -> list[dict]:
        channel_set = set(channels)
        now = time.monotonic()
        self._prune(now)
        results: list[dict] = []
        try:
            last_seen = int(str(last_event_id).strip())
        except Exception:
            return results

        for event_id, entry in self._events.items():
            if entry["channel"] not in channel_set:
                continue
            try:
                if int(event_id) <= last_seen:
                    continue
            except Exception:
                continue
            results.append(entry)
        return results


class RedisEventReplayStore:
    """Optional Redis-backed replay store for cross-process Last-Event-ID recovery."""

    def __init__(self, key_prefix: str, max_events: int, max_age_seconds: float) -> None:
        self._key_prefix = key_prefix
        self._max_events = max_events
        self._max_age_seconds = max_age_seconds

    def _key(self, channel: str) -> str:
        return f"{self._key_prefix}:{channel}"

    @staticmethod
    def _parse_event_id(event_id: str | None) -> int | None:
        try:
            if event_id is None:
                return None
            return int(str(event_id).strip())
        except Exception:
            return None

    async def store(self, event_id: str, channel: str, payload: str) -> None:
        event_num = self._parse_event_id(event_id)
        if event_num is None:
            return

        event_entry = {
            "id": event_id,
            "channel": channel,
            "payload": payload,
            "ts": int(time.time() * 1000),
        }
        key = self._key(channel)
        redis = _get_pubsub_redis()
        cutoff = max(0, event_num - int(self._max_age_seconds * 1000))

        pipe = redis.pipeline(transaction=False)
        pipe.zadd(key, {json_dumps(event_entry): event_num})
        pipe.zremrangebyrank(key, 0, -(self._max_events + 1))
        if cutoff > 0:
            pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.expire(key, max(1, int(self._max_age_seconds * 2)))

        await asyncio.wait_for(pipe.execute(), timeout=SSE_REPLAY_REDIS_OP_TIMEOUT_SECONDS)

    async def get_events_since(self, last_event_id: str, channels: Iterable[str]) -> list[dict]:
        last_seen = self._parse_event_id(last_event_id)
        if last_seen is None:
            return []

        redis = _get_pubsub_redis()
        results: list[dict] = []

        for channel in set(channels):
            key = self._key(channel)
            raw_entries = await asyncio.wait_for(
                redis.zrangebyscore(key, min=last_seen + 1, max="+inf"),
                timeout=SSE_REPLAY_REDIS_OP_TIMEOUT_SECONDS,
            )
            for raw in raw_entries:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("channel") != channel:
                    continue
                event_num = self._parse_event_id(entry.get("id"))
                if event_num is None or event_num <= last_seen:
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, str):
                    continue
                results.append(
                    {
                        "id": str(entry.get("id")),
                        "channel": channel,
                        "payload": payload,
                    }
                )

        results.sort(key=lambda item: int(item["id"]))
        return results


class AdmissionController:
    """Protects the SSE service when Redis or connection pressure spikes."""

    def __init__(self) -> None:
        self._active_connections = 0
        self._last_redis_latency_ms = 0.0

    async def check_admission(self, redis_client: aioredis.Redis) -> bool:
        if self._active_connections >= SSE_MAX_CONNECTIONS:
            return False

        started = time.monotonic()
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=0.5)
        except Exception:
            return False

        self._last_redis_latency_ms = (time.monotonic() - started) * 1000
        return self._last_redis_latency_ms <= SSE_REDIS_LATENCY_THRESHOLD_MS

    def connection_opened(self) -> None:
        self._active_connections += 1

    def connection_closed(self) -> None:
        self._active_connections = max(0, self._active_connections - 1)

    @property
    def active_connections(self) -> int:
        return self._active_connections

    @property
    def last_redis_latency_ms(self) -> float:
        return self._last_redis_latency_ms


class SSEFanoutManager:
    """Maintains one Redis pubsub reader per process and fans out to client queues."""

    def __init__(self) -> None:
        self._subscriptions: dict[asyncio.Queue, tuple[str, ...]] = {}
        self._queues_by_channel: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._pubsub: aioredis.client.PubSub | None = None
        self._reader_task: asyncio.Task | None = None

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._reader_task and not self._reader_task.done():
                return
            redis = _get_pubsub_redis()
            self._pubsub = redis.pubsub()
            await self._pubsub.subscribe(*PubSubManager.CHANNELS.values())
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        assert self._pubsub is not None
        pubsub = self._pubsub
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message or message.get("type") != "message":
                    continue

                channel = _normalize_pubsub_channel(message.get("channel"))
                payload = message.get("data")
                if not isinstance(payload, str):
                    continue

                event_id = _next_sse_event_id()
                _REPLAY_BUFFER.store(event_id, channel, payload)
                if _REDIS_REPLAY_STORE is not None:
                    try:
                        await _REDIS_REPLAY_STORE.store(event_id, channel, payload)
                    except Exception:
                        logger.debug("[SSE] redis replay store failed", exc_info=True)
                item = {"id": event_id, "channel": channel, "payload": payload}

                for queue in list(self._queues_by_channel.get(channel, ())):
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        logger.debug("[SSE] fanout queue remained full channel=%s", channel)
                    except Exception:
                        logger.debug("[SSE] fanout queue delivery failed channel=%s", channel, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[SSE] shared fanout reader crashed")
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("[SSE] shared fanout pubsub close failed", exc_info=True)

    async def subscribe(self, channels: Iterable[str]) -> asyncio.Queue:
        await self.ensure_started()
        queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_CLIENT_QUEUE_SIZE)
        channel_tuple = tuple(dict.fromkeys(channels))
        self._subscriptions[queue] = channel_tuple
        for channel in channel_tuple:
            self._queues_by_channel[channel].add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        channels = self._subscriptions.pop(queue, ())
        for channel in channels:
            subscribers = self._queues_by_channel.get(channel)
            if not subscribers:
                continue
            subscribers.discard(queue)
            if not subscribers:
                self._queues_by_channel.pop(channel, None)


_REPLAY_BUFFER = EventReplayBuffer(SSE_REPLAY_MAX_EVENTS, SSE_REPLAY_MAX_AGE_SECONDS)
_REDIS_REPLAY_STORE = RedisEventReplayStore(
    SSE_REPLAY_REDIS_KEY_PREFIX,
    SSE_REPLAY_MAX_EVENTS,
    SSE_REPLAY_MAX_AGE_SECONDS,
) if SSE_REPLAY_REDIS_ENABLED else None
_ADMISSION_CONTROLLER = AdmissionController()
_FANOUT_MANAGER = SSEFanoutManager()


async def _get_replay_events_since(last_event_id: str, channels: Iterable[str]) -> list[dict]:
    local_events = _REPLAY_BUFFER.get_events_since(last_event_id, channels)
    if _REDIS_REPLAY_STORE is None:
        return local_events

    try:
        redis_events = await _REDIS_REPLAY_STORE.get_events_since(last_event_id, channels)
    except Exception:
        logger.debug("[SSE] redis replay fetch failed; falling back to process-local replay", exc_info=True)
        return local_events

    if not redis_events:
        return local_events

    by_id: dict[str, dict] = {}
    for item in redis_events:
        by_id[item["id"]] = item
    for item in local_events:
        by_id.setdefault(item["id"], item)

    merged = list(by_id.values())
    merged.sort(key=lambda item: int(item["id"]))
    return merged


async def startup_sse_resources() -> None:
    """Pre-warm shared SSE resources for the current worker."""
    await _FANOUT_MANAGER.ensure_started()


async def shutdown_sse_resources() -> None:
    """Tear down shared SSE resources for the current worker."""
    global _PUBSUB_REDIS
    reader_task = _FANOUT_MANAGER._reader_task
    if reader_task is not None:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
    pubsub = _FANOUT_MANAGER._pubsub
    if pubsub is not None:
        try:
            await pubsub.aclose()
        except Exception:
            logger.debug("[SSE] shared fanout pubsub shutdown failed", exc_info=True)
    _FANOUT_MANAGER._reader_task = None
    _FANOUT_MANAGER._pubsub = None
    _FANOUT_MANAGER._subscriptions.clear()
    _FANOUT_MANAGER._queues_by_channel.clear()

    redis_client = _PUBSUB_REDIS
    _PUBSUB_REDIS = None
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception:
            logger.debug("[SSE] pubsub redis shutdown failed", exc_info=True)


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


def _render_sse_message(
    payload: str,
    *,
    event_id: str | None = None,
    event_name: str | None = None,
) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    if event_name:
        lines.append(f"event: {event_name}")
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _format_typed_event(
    payload: dict,
    fallback_event: str | None = None,
    *,
    include_event_name: bool = False,
    event_id: str | None = None,
) -> str:
    event_name = None
    if include_event_name:
        event_name = payload.get("type") if isinstance(payload, dict) else None
        event_name = _sanitize_sse_event_name(event_name) or _sanitize_sse_event_name(fallback_event)
    return _render_sse_message(json_dumps(payload), event_id=event_id, event_name=event_name)


def _extract_sse_payload(event: str) -> dict | None:
    data_lines = [line[6:] for line in event.splitlines() if line.startswith("data: ")]
    if not data_lines:
        return None
    try:
        return json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None


async def _ensure_admission() -> None:
    redis = _get_pubsub_redis()
    if not await _ADMISSION_CONTROLLER.check_admission(redis):
        raise HTTPException(
            status_code=503,
            detail="SSE service busy, retry shortly",
            headers={"Retry-After": str(SSE_ADMISSION_RETRY_AFTER_SECONDS)},
        )
    _ADMISSION_CONTROLLER.connection_opened()


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
    channels = [
        PubSubManager.CHANNELS["candles"],
        PubSubManager.CHANNELS["news"],
        PubSubManager.CHANNELS["strategies"],
    ]
    client_key = f"{client_ip}:{pair}:{symbol}:{timeframe}"
    obs_connected = False
    queue: asyncio.Queue | None = None

    try:
        queue = await _FANOUT_MANAGER.subscribe(channels)
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
            event_id=_next_sse_event_id(),
        )

        replayed = False
        last_event_id = (request.headers.get("last-event-id") or "").strip()
        if last_event_id:
            for replay_item in await _get_replay_events_since(last_event_id, channels):
                try:
                    data = json.loads(replay_item["payload"])
                except json.JSONDecodeError:
                    continue
                event_type = data.get("type") if isinstance(data, dict) else None
                candle_match = _is_candle_match(data, symbol, timeframe)
                if event_type == "candle_update" or candle_match:
                    if candle_match:
                        replayed = True
                        yield _format_typed_event(data, include_event_name=include_event_name, event_id=replay_item["id"])
                    continue
                if event_type in {"news_update", "news_snapshot"}:
                    replayed = True
                    yield _format_typed_event(data, include_event_name=include_event_name, event_id=replay_item["id"])
                    continue
                if event_type == "strategy_update":
                    strategy_payload = data.get("strategy") if isinstance(data.get("strategy"), dict) else data
                    if _strategy_matches_pair(strategy_payload, pair):
                        replayed = True
                        yield _format_typed_event(data, include_event_name=include_event_name, event_id=replay_item["id"])
                    continue
                if event_type == "strategies_snapshot":
                    strategies_payload = data.get("strategies")
                    if isinstance(strategies_payload, list):
                        filtered = [item for item in strategies_payload if _strategy_matches_pair(item, pair)]
                        replayed = True
                        yield _format_typed_event(
                            {**data, "strategies": filtered},
                            include_event_name=include_event_name,
                            event_id=replay_item["id"],
                        )
                    continue

        if not replayed:
            candle_snapshot = await asyncio.to_thread(get_last_candle_update, symbol, timeframe, prefer_forming=True)
            if candle_snapshot is not None:
                candle_snapshot = {**candle_snapshot, "is_snapshot": True}
                yield _format_typed_event(candle_snapshot, include_event_name=include_event_name, event_id=_next_sse_event_id())
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
                    event_id=_next_sse_event_id(),
                )

            news_snapshot = await asyncio.to_thread(NewsCache.get, "all") or []
            yield _format_typed_event(
                {"type": "news_snapshot", "news": news_snapshot, "server_ts": _server_timestamp()},
                include_event_name=include_event_name,
                event_id=_next_sse_event_id(),
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
                event_id=_next_sse_event_id(),
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
            try:
                item = await asyncio.wait_for(queue.get(), timeout=poll_timeout_s)
            except asyncio.TimeoutError:
                item = None

            if item:
                raw = item.get("payload")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type") if isinstance(data, dict) else None
                candle_match = _is_candle_match(data, symbol, timeframe)
                if event_type == "candle_update" or candle_match:
                    if candle_match:
                        _sse_obs_track_mux_topic("candle")
                        yield _format_typed_event(data, include_event_name=include_event_name, event_id=item["id"])
                    continue

                if event_type == "news_update" or event_type == "news_snapshot":
                    _sse_obs_track_mux_topic("news")
                    yield _format_typed_event(data, include_event_name=include_event_name, event_id=item["id"])
                    continue

                if event_type == "strategy_update":
                    strategy_payload = data.get("strategy") if isinstance(data.get("strategy"), dict) else data
                    if _strategy_matches_pair(strategy_payload, pair):
                        _sse_obs_track_mux_topic("strategy")
                        yield _format_typed_event(data, include_event_name=include_event_name, event_id=item["id"])
                    continue

                if event_type == "strategies_snapshot":
                    strategies_payload = data.get("strategies")
                    if isinstance(strategies_payload, list):
                        filtered = [item for item in strategies_payload if _strategy_matches_pair(item, pair)]
                        data = {**data, "strategies": filtered}
                        _sse_obs_track_mux_topic("strategy")
                        yield _format_typed_event(data, include_event_name=include_event_name, event_id=item["id"])
                    continue

                continue

            now = time.monotonic()
            if heartbeat_interval_s > 0 and now - last_heartbeat >= heartbeat_interval_s:
                heartbeat = {"type": "heartbeat", "server_ts": _server_timestamp()}
                yield _format_typed_event(
                    heartbeat,
                    fallback_event="heartbeat",
                    include_event_name=include_event_name,
                    event_id=_next_sse_event_id(),
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
        if queue is not None:
            await _FANOUT_MANAGER.unsubscribe(queue)
        _ADMISSION_CONTROLLER.connection_closed()
        logger.debug("[SSE] fanout queue closed channel=signals pair=%s symbol=%s tf=%s ip=%s", pair, symbol, timeframe, masked_client_ip)


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
    queue: asyncio.Queue | None = None

    try:
        queue = await _FANOUT_MANAGER.subscribe([channel])
        logger.info("[SSE] +OPEN  channel=%s ip=%s", channel, masked_client_ip)

        if send_connected:
            yield _format_typed_event(
                {"type": "connected", "channel": channel},
                fallback_event="connected",
                event_id=_next_sse_event_id(),
            )

        replayed = False
        last_event_id = (request.headers.get("last-event-id") or "").strip()
        if last_event_id:
            for replay_item in await _get_replay_events_since(last_event_id, [channel]):
                replayed = True
                try:
                    data = json.loads(replay_item["payload"])
                except json.JSONDecodeError:
                    continue
                yield _format_typed_event(data, event_id=replay_item["id"])

        if initial_payloads and not replayed:
            for payload in initial_payloads:
                yield _format_typed_event(payload, event_id=_next_sse_event_id())

        last_heartbeat = time.monotonic()

        while True:
            # Disconnect checks happen once per poll cycle to avoid tight idle loops.
            if await request.is_disconnected():
                logger.info("[SSE] -CLOSE channel=%s ip=%s", channel, masked_client_ip)
                break

            # Block up to a short timeout so idle connections do not spin CPU.
            poll_timeout_s = min(5.0, max(0.25, heartbeat_interval_s if heartbeat_interval_s > 0 else 1.0))
            try:
                item = await asyncio.wait_for(queue.get(), timeout=poll_timeout_s)
            except asyncio.TimeoutError:
                item = None

            if item:
                try:
                    data = json.loads(item["payload"])
                except json.JSONDecodeError:
                    continue
                yield _format_typed_event(data, event_id=item["id"])
                continue

            # Heartbeat check
            now = time.monotonic()
            if heartbeat_interval_s > 0 and now - last_heartbeat >= heartbeat_interval_s:
                heartbeat = {"type": "heartbeat", "server_ts": _server_timestamp()}
                yield _format_typed_event(heartbeat, fallback_event="heartbeat", event_id=_next_sse_event_id())
                last_heartbeat = now

    except asyncio.CancelledError:
        logger.info("[SSE] CANCEL channel=%s ip=%s", channel, masked_client_ip)
    except Exception as exc:
        logger.error("[SSE] ERROR  channel=%s ip=%s error=%s", channel, masked_client_ip, exc)
        yield f"data: {json_dumps({'type': 'error', 'message': str(exc)})}\n\n"
    finally:
        if queue is not None:
            await _FANOUT_MANAGER.unsubscribe(queue)
        _ADMISSION_CONTROLLER.connection_closed()
        logger.debug("[SSE] fanout queue closed channel=%s ip=%s", channel, masked_client_ip)



# ── Auth sentinel ─────────────────────────────────────────────────────────────
# Named function (not a lambda) so callers can override via:
#   app.dependency_overrides[_sse_auth] = require_session

async def _sse_auth(request: Request):
    """Placeholder — overridden by the host app (api-web or api-sse)."""
    return None  # no-op: overridden at startup by main.py / sse_main.py


def _require_signals_stream_access(ctx: object) -> None:
    if not isinstance(ctx, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    require_permission(ctx, "signals")


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
    await _ensure_admission()

    async def filtered_generator():
        async for event in event_generator(
            PubSubManager.CHANNELS["candles"],
            request,
            initial_payloads=[snapshot] if snapshot else None,
        ):
            data = _extract_sse_payload(event)
            if data is None:
                yield event
                continue
            if data.get("type") in {"connected", "heartbeat", "error"} or (
                data.get("symbol") == symbol and data.get("timeframe") == timeframe
            ):
                yield event

    return StreamingResponse(filtered_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/candles")
async def stream_all_candles(request: Request, _ctx=Depends(_sse_auth)):
    """Stream real-time candle updates for ALL symbols and timeframes."""
    logger.debug("[SSE] all-candles stream requested")
    await _ensure_admission()
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
    await _ensure_admission()

    return StreamingResponse(
        event_generator(PubSubManager.CHANNELS["news"], request, initial_payloads=initial_payloads),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/strategies")
async def stream_strategies(request: Request, pair: str | None = None, _ctx=Depends(_sse_auth)):
    """Stream real-time strategy updates, optionally filtered by pair."""
    _require_signals_stream_access(_ctx)
    logger.debug("[SSE] strategy stream requested pair=%s", pair or "all")
    normalized_pair = pair.upper() if pair else None
    snapshot = await asyncio.to_thread(StrategyCache.get, normalized_pair or "all") or []
    if normalized_pair:
        snapshot = [item for item in snapshot if _strategy_matches_pair(item, normalized_pair)]

    initial_payloads = [{"type": "strategies_snapshot", "strategies": snapshot, "server_ts": _server_timestamp()}]
    await _ensure_admission()

    async def filtered_generator():
        async for event in event_generator(
            PubSubManager.CHANNELS["strategies"],
            request,
            initial_payloads=initial_payloads,
        ):
            if not normalized_pair:
                yield event
                continue
            data = _extract_sse_payload(event)
            if data is None:
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
                    yield _format_typed_event({**data, "strategies": filtered})
                continue
            if _strategy_matches_pair(data, normalized_pair):
                yield event

    return StreamingResponse(filtered_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/signals", dependencies=[Depends(_ensure_signals_mux_enabled)])
async def stream_signals(
    request: Request,
    pair: str,
    symbol: str,
    timeframe: str,
    named_events: bool = False,
    _ctx=Depends(_sse_auth),
):
    """Multiplex news, strategy, and candle updates for the Signals page."""
    _require_signals_stream_access(_ctx)
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
    await _ensure_admission()

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
    try:
        await asyncio.wait_for(_FANOUT_MANAGER.ensure_started(), timeout=1.0)
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": f"fanout_unavailable: {str(exc)[:200]}"},
        )

    try:
        await asyncio.wait_for(_get_pubsub_redis().ping(), timeout=0.5)
        return {
            "status": "healthy",
            "redis": "connected",
            "channels": list(PubSubManager.CHANNELS.values()),
            "active_connections": _ADMISSION_CONTROLLER.active_connections,
            "redis_latency_ms": round(_ADMISSION_CONTROLLER.last_redis_latency_ms, 2),
            "replay_buffer_size": len(_REPLAY_BUFFER._events),
        }
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "redis": "disconnected", "error": str(exc)[:200]},
        )
