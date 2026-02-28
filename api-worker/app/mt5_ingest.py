"""MT5 candle ingest server (TCP, binary framed).

This runs inside the FastAPI process and accepts connections from the Python bridge.
The bridge relays frames from the MT5 EA.

Responsibilities:
- Decode frames (mt5_wire)
- Upsert live M1 candles into DB (candlesticks)
- Publish Redis/SSE updates (updates:candles) and invalidate candle cache
- Provide a command channel to request history/subscriptions by writing frames back to the bridge

Non-goals:
- No candle validation (MT5 broker feed is authoritative)
- No aggregation/indicator changes (existing scripts remain)
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import time
from typing import Optional, Deque

import psycopg

from .cache import CandleCache, PubSubManager
from .cache import redis_client
from .db import POSTGRES_DSN
from trading_common.symbols import get_active_symbols, refresh_active_symbols
from .mt5_wire import (
    Frame,
    ProtocolError,
    TF_M1,
    TF_D1,
    TF_W1,
    TF_MN1,
    TF_TO_NAME,
    MSG_LIVE_BAR,
    MSG_FORMING_BAR,
    MSG_HIST_BEGIN,
    MSG_HIST_CHUNK,
    MSG_HIST_END,
    MSG_HELLO,
    MSG_HEARTBEAT,
    MSG_ERROR,
    MSG_SUBSCRIBE,
    MSG_HISTORY_FETCH,
    HEADER_LEN,
    pack_frame,
    read_frame,
    unpack_live_bar,
    unpack_forming_bar,
    iter_hist_chunk,
    unpack_header,
)

logger = logging.getLogger(__name__)


_BOOTSTRAP_DEFAULT_START_UTC = datetime(2021, 1, 1, tzinfo=timezone.utc)


def _touch(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except Exception:
        pass


def _set_ready_redis_key(key: str, ttl_seconds: int) -> None:
    if not key:
        return
    try:
        redis_client.setex(key, int(ttl_seconds), "1")
    except Exception:
        # Best-effort only; never fail ingest on Redis errors.
        pass


_MSG_NAME: dict[int, str] = {
    MSG_HELLO: "HELLO",
    MSG_HEARTBEAT: "HEARTBEAT",
    MSG_ERROR: "ERROR",
    MSG_SUBSCRIBE: "SUBSCRIBE",
    MSG_HISTORY_FETCH: "HISTORY_FETCH",
    MSG_LIVE_BAR: "LIVE_BAR",
    MSG_FORMING_BAR: "FORMING_BAR",
    MSG_HIST_BEGIN: "HIST_BEGIN",
    MSG_HIST_CHUNK: "HIST_CHUNK",
    MSG_HIST_END: "HIST_END",
}


# Timeframe minutes mapping for forming aggregation (UI only).
_TF_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
    "MN1": 43200,
}


def _env_forming_timeframes() -> list[str]:
    raw = (os.getenv("MT5_FORMING_TIMEFRAMES") or "M1,M5,M15,M30,H1,H4,D1,W1,MN1").strip()
    tfs = [t.strip().upper() for t in raw.split(",") if t.strip()]
    # Keep only known.
    return [t for t in tfs if t in _TF_MINUTES]


def _floor_utc_bucket(dt: datetime, timeframe_minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)

    if timeframe_minutes >= 43200:  # MN1
        return dt.replace(day=1, hour=0, minute=0)

    if timeframe_minutes >= 10080:  # W1
        # Forex trading week: opens Sunday 22:00 UTC.
        # This keeps bucket boundaries aligned to broker weekly candles
        # (Sun 22:00 → Fri 22:00, with weekend gap inside the bucket).
        # Python weekday(): Mon=0 ... Sun=6
        days_since_sunday = (dt.weekday() + 1) % 7
        sunday = dt - timedelta(days=days_since_sunday)
        bucket = sunday.replace(hour=22, minute=0)
        if dt < bucket:
            bucket -= timedelta(days=7)
        return bucket

    if timeframe_minutes >= 1440:  # D1
        return dt.replace(hour=0, minute=0)

    total_minutes = dt.hour * 60 + dt.minute
    period_minute = (total_minutes // timeframe_minutes) * timeframe_minutes
    return dt.replace(hour=period_minute // 60, minute=period_minute % 60)


def _msg_name(msg_type: int) -> str:
    return _MSG_NAME.get(msg_type, f"UNKNOWN({msg_type})")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if not v:
        return default
    return int(v)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _forming_state_key(symbol: str, timeframe: str, bucket_start: datetime) -> str:
    return f"forming:bucket:{str(symbol).upper()}:{str(timeframe).upper()}:{int(bucket_start.timestamp())}"


def _forming_state_ttl_seconds(timeframe: str) -> int:
    tf = str(timeframe).upper()
    if tf == "MN1":
        return 120 * 86400
    if tf == "W1":
        return 21 * 86400
    if tf == "D1":
        return 3 * 86400
    minutes = _TF_MINUTES.get(tf, 60)
    return max(900, int(minutes) * 60 * 3)


def _update_forming_state_from_closed_m1_sync(*, symbol: str, closed_dt: datetime, candle: dict, timeframes: list[str]) -> None:
    """Incrementally maintain per-bucket OHLCV state for higher timeframes in Redis.

    This is used only to compute *forming* (ephemeral) candles for SSE/UI without
    reading historical M1s from Postgres on every forming tick.
    """
    try:
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c = float(candle["close"])
        v = int(candle.get("volume", 0) or 0)

        for tf in timeframes:
            tf = str(tf).upper()
            if tf == "M1":
                continue
            minutes = _TF_MINUTES.get(tf)
            if not minutes:
                continue

            bucket_start = _floor_utc_bucket(closed_dt, int(minutes))
            key = _forming_state_key(symbol, tf, bucket_start)
            state = redis_client.hgetall(key) or {}

            if not state:
                redis_client.hset(
                    key,
                    mapping={
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                        "last_ts": int(closed_dt.timestamp()),
                    },
                )
                redis_client.expire(key, _forming_state_ttl_seconds(tf))
                continue

            try:
                high_max = float(state.get("high"))
            except Exception:
                high_max = h
            try:
                low_min = float(state.get("low"))
            except Exception:
                low_min = l
            try:
                vol_sum = int(float(state.get("volume") or 0))
            except Exception:
                vol_sum = 0

            redis_client.hset(
                key,
                mapping={
                    "high": max(high_max, h),
                    "low": min(low_min, l),
                    "close": c,
                    "volume": int(vol_sum) + int(v),
                    "last_ts": int(closed_dt.timestamp()),
                },
            )
            # Keep TTL fresh while the bucket is active.
            redis_client.expire(key, _forming_state_ttl_seconds(tf))
    except Exception:
        # Never break live ingest if Redis is unavailable.
        return


async def _update_forming_state_from_closed_m1(*, symbol: str, ts_open: int, candle: dict, timeframes: list[str]) -> None:
    closed_dt = datetime.fromtimestamp(int(ts_open), tz=timezone.utc)
    await asyncio.to_thread(
        _update_forming_state_from_closed_m1_sync,
        symbol=str(symbol).upper(),
        closed_dt=closed_dt,
        candle=candle,
        timeframes=timeframes,
    )


def _get_latest_m1_ts_by_symbol_sync(symbols: list[str]) -> dict[str, int]:
    """Return latest DB M1 candle open timestamp per symbol (seconds since epoch)."""
    if not symbols:
        return {}

    out: dict[str, int] = {}
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT symbol, EXTRACT(EPOCH FROM MAX(time))::bigint AS ts
                    FROM candlesticks
                    WHERE timeframe='M1' AND symbol = ANY(%s)
                    GROUP BY symbol
                    """,
                    (symbols,),
                )
                for sym, ts in cur.fetchall():
                    if sym and ts:
                        out[str(sym).upper()] = int(ts)
    except Exception:
        return {}
    return out


def _get_earliest_candle_ts_sync() -> Optional[int]:
    """Return earliest candle timestamp from database (any symbol/timeframe).
    Used as fallback for HTF history fetch when DB is empty."""
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXTRACT(EPOCH FROM MIN(time))::bigint AS earliest_ts
                    FROM candlesticks
                    """
                )
                row = cur.fetchone()
                if row and row[0]:
                    return int(row[0])
    except Exception:
        return None
    return None


def _get_latest_htf_ts_by_symbol_sync(symbols: list[str], timeframe: str) -> dict[str, int]:
    """Return latest DB HTF (D1/W1/MN1) candle open timestamp per symbol.
    Used to skip history fetch if HTF data is already fresh."""
    if not symbols or not timeframe:
        return {}

    out: dict[str, int] = {}
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT symbol, EXTRACT(EPOCH FROM MAX(time))::bigint AS ts
                    FROM candlesticks
                    WHERE timeframe=%s AND symbol = ANY(%s)
                    GROUP BY symbol
                    """,
                    (timeframe, symbols),
                )
                for sym, ts in cur.fetchall():
                    if sym and ts:
                        out[str(sym).upper()] = int(ts)
    except Exception:
        return {}
    return out


def _get_latest_ts_by_symbol_sync(symbols: list[str], timeframe: str) -> dict[str, int]:
    """Return latest DB candle open timestamp per symbol for a given timeframe."""
    return _get_latest_htf_ts_by_symbol_sync(symbols, timeframe)


@dataclass
class BridgeSession:
    session_id: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: str


class MT5IngestServer:
    def __init__(self) -> None:
        self._server: Optional[asyncio.base_events.Server] = None
        self._sessions: dict[int, BridgeSession] = {}
        self._next_session_id = 1
        self._lock = asyncio.Lock()
        self._debug = _env_bool("MT5_PROTOCOL_DEBUG", False)

        # Bootstrap orchestration (history -> subscribe)
        self._bootstrap_enable = _env_bool("MT5_BOOTSTRAP_ENABLE", True)
        # DB is the source of truth. Optional env override is opt-in for debugging.
        self._bootstrap_allow_env_symbols = _env_bool("MT5_SUBSCRIBE_ALLOW_ENV", False)
        self._bootstrap_symbols = []
        if self._bootstrap_allow_env_symbols:
            self._bootstrap_symbols = [
                s.strip().upper()
                for s in (os.getenv("MT5_SUBSCRIBE_SYMBOLS") or "").split(",")
                if s.strip()
            ]
        self._bootstrap_symbols_from_db = _env_bool("MT5_SUBSCRIBE_FROM_DB", True)
        self._bootstrap_symbols_by_sid: dict[int, list[str]] = {}
        self._bootstrap_history_enable = _env_bool("MT5_BOOTSTRAP_HISTORY_ENABLE", True)
        self._bootstrap_history_lookback_minutes = _env_int("MT5_BOOTSTRAP_HISTORY_LOOKBACK_MINUTES", 525600)  # 1 year in minutes
        self._bootstrap_history_max_bars = _env_int("MT5_BOOTSTRAP_HISTORY_MAX_BARS", 999999)  # No practical limit
        self._bootstrap_history_chunk_bars = _env_int("MT5_BOOTSTRAP_HISTORY_CHUNK_BARS", 1000)
        self._bootstrap_subscribe_after_history = _env_bool("MT5_BOOTSTRAP_SUBSCRIBE_AFTER_HISTORY", True)

        self._bootstrap_pending_jobs: dict[int, set[int]] = {}
        self._bootstrap_job_frames: dict[int, dict[int, bytes]] = {}
        self._bootstrap_job_specs: dict[int, dict[int, dict]] = {}
        self._bootstrap_queue: dict[int, Deque[int]] = {}
        self._bootstrap_inflight: dict[int, set[int]] = {}
        self._bootstrap_send_task: dict[int, asyncio.Task] = {}
        self._bootstrap_subscribed: set[int] = set()
        self._job_id_seq = 1
        # Job IDs that must upsert history rows regardless of global env.
        # Used by internal/manual backfill requests.
        self._force_upsert_jobs: set[int] = set()

        # Bootstrap flow-control and retry
        self._bootstrap_max_inflight = _env_int("MT5_BOOTSTRAP_MAX_INFLIGHT", 5)
        self._bootstrap_retry_delay_seconds = _env_int("MT5_BOOTSTRAP_RETRY_DELAY_SECONDS", 2)
        self._bootstrap_job_timeout_seconds = _env_int("MT5_BOOTSTRAP_JOB_TIMEOUT_SECONDS", 120)
        self._bootstrap_overlap_bars = _env_int("MT5_BOOTSTRAP_OVERLAP_BARS", 1)

        self._ready_connected_file = os.getenv("MT5_READY_CONNECTED_FILE", "/tmp/mt5_connected")
        self._ready_subscribed_file = os.getenv("MT5_READY_SUBSCRIBED_FILE", "/tmp/mt5_ready")
        self._ready_connected_redis_key = os.getenv("MT5_READY_CONNECTED_REDIS_KEY", "mt5:ready:connected")
        self._ready_subscribed_redis_key = os.getenv("MT5_READY_SUBSCRIBED_REDIS_KEY", "mt5:ready:subscribed")
        self._ready_redis_ttl_seconds = _env_int("MT5_READY_REDIS_TTL_SECONDS", 180)

        self._heartbeat_file = os.getenv("MT5_HEARTBEAT_FILE", "/tmp/mt5_ingest_heartbeat")
        self._heartbeat_interval_seconds = _env_int("MT5_HEARTBEAT_INTERVAL_SECONDS", 15)
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        host = os.getenv("MT5_INGEST_HOST", "0.0.0.0")
        port = _env_int("MT5_INGEST_PORT", 9001)

        self._server = await asyncio.start_server(self._handle_client, host, port)
        sockets = self._server.sockets or []
        # Filter out None sockets (shouldn't happen but be defensive)
        valid_sockets = [s for s in sockets if s is not None]
        if valid_sockets:
            bind = ", ".join(str(s.getsockname()) for s in valid_sockets)
        else:
            bind = f"{host}:{port}"
        logger.info("[MT5] Ingest TCP listening on %s", bind)

        _touch(self._heartbeat_file)
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while True:
            _touch(self._heartbeat_file)
            await asyncio.sleep(self._heartbeat_interval_seconds)

    async def broadcast_subscribe(self, symbols: list[str]) -> int:
        payload = _pack_subscribe_payload(symbols)
        frame = pack_frame(MSG_SUBSCRIBE, payload)
        return await self._broadcast(frame)

    async def broadcast_history_fetch(
        self,
        *,
        symbol: str,
        from_ts: int,
        to_ts: int,
        tf: int = TF_M1,
        max_bars: int = 2000,
        chunk_bars: int = 1000,
        job_id: int = 0,
    ) -> int:
        payload = _pack_history_fetch_payload(
            symbol=symbol,
            tf=tf,
            from_ts=from_ts,
            to_ts=to_ts,
            max_bars=max_bars,
            chunk_bars=chunk_bars,
        )
        frame = pack_frame(MSG_HISTORY_FETCH, payload, job_id=job_id)
        return await self._broadcast(frame)

    def allocate_job_id(self) -> int:
        """Allocate a process-unique job id for history fetch requests."""
        job_id = int(self._job_id_seq)
        self._job_id_seq += 1
        return job_id

    async def request_history_fetch(
        self,
        *,
        symbol: str,
        from_ts: int,
        to_ts: int,
        tf: int = TF_M1,
        max_bars: int = 2000,
        chunk_bars: int = 1000,
        upsert: bool = False,
        job_id: Optional[int] = None,
    ) -> tuple[int, int]:
        """Send a history fetch request to all connected bridge sessions.

        Returns:
            (sent_sessions_count, job_id)
        """
        req_job_id = int(job_id) if job_id and int(job_id) > 0 else self.allocate_job_id()
        if upsert:
            self._force_upsert_jobs.add(req_job_id)

        sent = await self.broadcast_history_fetch(
            symbol=symbol,
            from_ts=int(from_ts),
            to_ts=int(to_ts),
            tf=int(tf),
            max_bars=int(max_bars),
            chunk_bars=int(chunk_bars),
            job_id=req_job_id,
        )

        # No recipients => cleanup forced-upsert marker.
        if sent <= 0 and upsert:
            self._force_upsert_jobs.discard(req_job_id)

        return sent, req_job_id

    async def _broadcast(self, data: bytes) -> int:
        async with self._lock:
            sessions = list(self._sessions.values())

        if self._debug:
            # Best-effort decode of the outgoing frame header for logging.
            try:
                if len(data) >= HEADER_LEN:
                    msg_type, _flags, payload_len, seq, job_id, _csum = unpack_header(data[:HEADER_LEN])
                    logger.debug(
                        "[MT5] TX broadcast sessions=%s type=%s seq=%s job=%s payload_len=%s",
                        len(sessions),
                        _msg_name(msg_type),
                        seq,
                        job_id,
                        payload_len,
                    )
            except Exception:
                pass

        sent = 0
        for s in sessions:
            try:
                s.writer.write(data)
                await s.writer.drain()
                sent += 1
            except Exception:
                # Session cleanup happens in reader loop.
                continue
        return sent

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_s = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)

        async with self._lock:
            sid = self._next_session_id
            self._next_session_id += 1
            self._sessions[sid] = BridgeSession(session_id=sid, reader=reader, writer=writer, peer=peer_s)

        logger.info("[MT5] Bridge connected sid=%s peer=%s", sid, peer_s)

        # Mark connection for other processes (scheduler) inside container.
        _touch(self._ready_connected_file)
        _set_ready_redis_key(self._ready_connected_redis_key, self._ready_redis_ttl_seconds)

        # Proactively send a HELLO frame so the EA/bridge sees immediate activity.
        # Some EA implementations wait for server-initiated handshake before sending.
        try:
            writer.write(pack_frame(MSG_HELLO, b""))
            await writer.drain()
        except Exception as e:
            logger.warning("[MT5] HELLO send failed sid=%s peer=%s err=%s", sid, peer_s, e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        # Optional bootstrap: request history first, then subscribe.
        if self._bootstrap_enable:
            try:
                await self._bootstrap_on_connect(sid=sid, writer=writer)
            except Exception as e:
                logger.warning("[MT5] Bootstrap error sid=%s err=%s", sid, e)

        try:
            await self._session_loop(sid, reader, writer)
        finally:
            async with self._lock:
                self._sessions.pop(sid, None)
            self._bootstrap_pending_jobs.pop(sid, None)
            self._bootstrap_job_frames.pop(sid, None)
            self._bootstrap_job_specs.pop(sid, None)
            self._bootstrap_queue.pop(sid, None)
            self._bootstrap_inflight.pop(sid, None)
            t = self._bootstrap_send_task.pop(sid, None)
            if t is not None:
                t.cancel()
            self._bootstrap_subscribed.discard(sid)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("[MT5] Bridge disconnected sid=%s peer=%s", sid, peer_s)

    async def _session_loop(self, sid: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        live_upsert = True
        history_upsert = _env_bool("MT5_HISTORY_UPSERT", False)
        forming_enable = _env_bool("MT5_FORMING_ENABLE", True)
        forming_timeframes = _env_forming_timeframes()

        while True:
            try:
                frame = await read_frame(reader, timeout=60.0)
            except ProtocolError as e:
                logger.warning("[MT5] Protocol error sid=%s err=%s", sid, e)
                return
            except asyncio.TimeoutError:
                # Keep TCP alive; bridge should also send heartbeats.
                continue

            if self._debug:
                logger.debug(
                    "[MT5] RX sid=%s type=%s seq=%s job=%s payload_len=%s",
                    sid,
                    _msg_name(frame.msg_type),
                    frame.seq,
                    frame.job_id,
                    len(frame.payload),
                )

            try:
                if frame.msg_type == MSG_LIVE_BAR:
                    candle = unpack_live_bar(frame.payload)
                    
                    # Map timeframe code to database name
                    tf_code = candle.get("timeframe", TF_M1)
                    tf_name = TF_TO_NAME.get(tf_code, "M1")
                    
                    if self._debug:
                        logger.debug(
                            "[MT5] LIVE_BAR sid=%s sym=%s tf=%s ts_open=%s o=%.5f h=%.5f l=%.5f c=%.5f v=%s",
                            sid,
                            candle["symbol"],
                            tf_name,
                            candle["ts_open"],
                            float(candle["open"]),
                            float(candle["high"]),
                            float(candle["low"]),
                            float(candle["close"]),
                            int(candle.get("volume", 0)),
                        )
                    
                    await _write_candle(
                        symbol=candle["symbol"],
                        timeframe=tf_name,
                        ts_open=int(candle["ts_open"]),
                        open_=float(candle["open"]),
                        high=float(candle["high"]),
                        low=float(candle["low"]),
                        close=float(candle["close"]),
                        volume=int(candle["volume"]),
                        upsert=live_upsert,
                    )
                    _publish_candle(
                        symbol=candle["symbol"],
                        timeframe=tf_name,
                        ts_open=int(candle["ts_open"]),
                        candle=candle,
                        is_forming=False,
                    )

                    # Update forming state only for M1 candles
                    # D1/W1/MN1 are authoritative from broker and never aggregated
                    if tf_name == "M1" and forming_enable and forming_timeframes:
                        asyncio.create_task(
                            _update_forming_state_from_closed_m1(
                                symbol=str(candle["symbol"]).upper(),
                                ts_open=int(candle["ts_open"]),
                                candle=candle,
                                timeframes=forming_timeframes,
                            )
                        )

                elif frame.msg_type == MSG_FORMING_BAR and forming_enable:
                    candle = unpack_forming_bar(frame.payload)
                    if self._debug:
                        logger.debug(
                            "[MT5] FORMING_BAR sid=%s sym=%s ts_open=%s o=%.5f h=%.5f l=%.5f c=%.5f v=%s",
                            sid,
                            candle["symbol"],
                            candle["ts_open"],
                            float(candle["open"]),
                            float(candle["high"]),
                            float(candle["low"]),
                            float(candle["close"]),
                            int(candle.get("volume", 0)),
                        )

                    # Publish forming M1 snapshot (ephemeral; not written to DB).
                    _publish_candle(
                        symbol=candle["symbol"],
                        timeframe="M1",
                        ts_open=int(candle["ts_open"]),
                        candle=candle,
                        is_forming=True,
                    )

                    # Trigger ephemeral forming aggregation for other timeframes.
                    if forming_timeframes:
                        asyncio.create_task(
                            _publish_forming_aggregates(
                                symbol=str(candle["symbol"]).upper(),
                                forming_m1=candle,
                                timeframes=forming_timeframes,
                            )
                        )

                elif frame.msg_type == MSG_HIST_CHUNK:
                    meta, rows = iter_hist_chunk(frame.payload)
                    tf_code = meta.get("timeframe", TF_M1)
                    tf_name = TF_TO_NAME.get(tf_code, "M1")
                    force_upsert = int(frame.job_id) in self._force_upsert_jobs

                    if self._debug:
                        first_ts = rows[0]["ts_open"] if rows else None
                        last_ts = rows[-1]["ts_open"] if rows else None
                        logger.debug(
                            "[MT5] HIST_CHUNK sid=%s sym=%s tf=%s chunk=%s count=%s first_ts=%s last_ts=%s",
                            sid,
                            meta["symbol"],
                            tf_name,
                            meta["chunk_index"],
                            meta["count"],
                            first_ts,
                            last_ts,
                        )

                    # Write rows in a single DB transaction, but no full-range buffering.
                    await _write_candles_bulk(
                        symbol=meta["symbol"],
                        timeframe=tf_name,
                        rows=rows,
                        upsert=(history_upsert or force_upsert),
                    )

                elif frame.msg_type in {MSG_HIST_BEGIN, MSG_HIST_END}:
                    # For now: informational. Could be used for metrics.
                    if self._debug:
                        logger.debug("[MT5] %s sid=%s job=%s", _msg_name(frame.msg_type), sid, frame.job_id)

                    # If we're bootstrapping history, mark jobs complete on HIST_END and subscribe once done.
                    if frame.msg_type == MSG_HIST_END:
                        # Cleanup manual upsert marker when job fully finishes.
                        self._force_upsert_jobs.discard(int(frame.job_id))
                        pending = self._bootstrap_pending_jobs.get(sid)
                        if pending is not None and int(frame.job_id) in pending:
                            pending.discard(int(frame.job_id))
                            inflight = self._bootstrap_inflight.get(sid)
                            if inflight is not None:
                                inflight.discard(int(frame.job_id))
                            # Free frame/spec memory for this job.
                            frames = self._bootstrap_job_frames.get(sid)
                            if frames is not None:
                                frames.pop(int(frame.job_id), None)
                            specs = self._bootstrap_job_specs.get(sid)
                            if specs is not None:
                                specs.pop(int(frame.job_id), None)
                            if self._debug:
                                logger.debug("[MT5] Bootstrap job done sid=%s job=%s remaining=%s", sid, frame.job_id, len(pending))
                            if not pending:
                                await self._bootstrap_send_subscribe_if_needed(sid=sid, writer=writer)
                            else:
                                self._bootstrap_kick_sender(sid=sid)
                    continue

                elif frame.msg_type == MSG_ERROR:
                    # EA sent an error message (queue full, invalid symbol, etc.)
                    try:
                        import json
                        error_data = json.loads(frame.payload.decode('utf-8', errors='ignore'))
                        error_type = error_data.get("error", "unknown")
                        symbol = error_data.get("symbol", "?")
                        tf = error_data.get("tf", "?")
                        request_id = error_data.get("request_id", "?")
                        
                        logger.error(
                            "[MT5] ERROR from EA sid=%s error=%s symbol=%s tf=%s request_id=%s job=%s data=%s",
                            sid, error_type, symbol, tf, request_id, frame.job_id, error_data
                        )
                        
                        # Handle specific errors
                        if error_type == "history_queue_full":
                            # EA could not enqueue this request. Backend must retry.
                            pending = self._bootstrap_pending_jobs.get(sid)
                            if pending is not None and int(frame.job_id) in pending:
                                inflight = self._bootstrap_inflight.get(sid)
                                if inflight is not None:
                                    inflight.discard(int(frame.job_id))
                                # Re-queue the job for later send.
                                q = self._bootstrap_queue.get(sid)
                                if q is not None:
                                    q.append(int(frame.job_id))
                                logger.warning("[MT5] bootstrap queue_full sid=%s job=%s sym=%s tf=%s", sid, frame.job_id, symbol, tf)
                                self._bootstrap_kick_sender(sid=sid, delay=self._bootstrap_retry_delay_seconds)
                        elif error_type == "copyrates_failed" or error_type == "symbol_select_failed":
                            # Fatal error for this job - remove from pending
                            pending = self._bootstrap_pending_jobs.get(sid)
                            if pending is not None and int(frame.job_id) in pending:
                                pending.discard(int(frame.job_id))
                                inflight = self._bootstrap_inflight.get(sid)
                                if inflight is not None:
                                    inflight.discard(int(frame.job_id))
                                frames = self._bootstrap_job_frames.get(sid)
                                if frames is not None:
                                    frames.pop(int(frame.job_id), None)
                                specs = self._bootstrap_job_specs.get(sid)
                                if specs is not None:
                                    specs.pop(int(frame.job_id), None)
                                logger.error(
                                    "[MT5] bootstrap fatal sid=%s job=%s err=%s remaining=%s",
                                    sid, frame.job_id, len(pending)
                                )
                                if not pending:
                                    # All jobs done (some failed) - subscribe anyway
                                    await self._bootstrap_send_subscribe_if_needed(sid=sid, writer=writer)
                                else:
                                    self._bootstrap_kick_sender(sid=sid)
                    except Exception as e:
                        logger.error("[MT5] Failed to parse ERROR payload sid=%s err=%s", sid, e)
                    continue

                elif frame.msg_type in {MSG_HELLO, MSG_HEARTBEAT}:
                    # Keepalive / handshake.
                    _touch(self._heartbeat_file)
                    continue

                else:
                    # Ignore unknown message types for forward-compat.
                    if self._debug:
                        logger.debug("[MT5] Ignoring msg sid=%s type=%s", sid, _msg_name(frame.msg_type))
                    continue

            except Exception as e:
                logger.error("[MT5] Handler error sid=%s type=%s err=%s", sid, frame.msg_type, e, exc_info=True)


    async def _bootstrap_on_connect(self, *, sid: int, writer: asyncio.StreamWriter) -> None:
        symbols = await self._get_bootstrap_symbols()
        self._bootstrap_symbols_by_sid[sid] = symbols

        if not symbols:
            return

        # If history is disabled, subscribe immediately.
        if not self._bootstrap_history_enable or not self._bootstrap_subscribe_after_history:
            await self._bootstrap_send_subscribe_if_needed(sid=sid, writer=writer)
            return

        now = int(datetime.now(timezone.utc).timestamp())
        lookback_minutes = max(1, int(self._bootstrap_history_lookback_minutes))
        to_ts = now

        overlap_bars = max(0, int(self._bootstrap_overlap_bars))

        # For M1: check latest timestamps per symbol
        latest_m1 = await asyncio.to_thread(_get_latest_m1_ts_by_symbol_sync, symbols)

        # For HTF: latest timestamps per symbol
        latest_d1 = await asyncio.to_thread(_get_latest_ts_by_symbol_sync, symbols, "D1")
        latest_w1 = await asyncio.to_thread(_get_latest_ts_by_symbol_sync, symbols, "W1")
        latest_mn1 = await asyncio.to_thread(_get_latest_ts_by_symbol_sync, symbols, "MN1")

        def _tf_overlap_seconds(tf_code: int) -> int:
            tf_name = TF_TO_NAME.get(tf_code, "M1")
            minutes = _TF_MINUTES.get(tf_name, 1)
            return int(minutes) * 60 * int(overlap_bars)

        pending: set[int] = set()
        frames_by_job: dict[int, bytes] = {}
        specs_by_job: dict[int, dict] = {}
        q: Deque[int] = deque()

        # Fetch order preserved: D1 → W1 → MN1 → M1
        timeframes_ordered = [TF_D1, TF_W1, TF_MN1, TF_M1]

        for tf in timeframes_ordered:
            for sym in symbols:
                sym_u = str(sym).upper()

                if tf == TF_M1:
                    latest_ts = latest_m1.get(sym_u)
                    # Incremental if present; otherwise bounded lookback.
                    if latest_ts is None or latest_ts <= 0:
                        from_ts = now - lookback_minutes * 60
                    else:
                        from_ts = int(latest_ts) - _tf_overlap_seconds(TF_M1)
                else:
                    # HTF: only full backfill if EMPTY; otherwise incremental from last candle.
                    latest_map = latest_d1 if tf == TF_D1 else (latest_w1 if tf == TF_W1 else latest_mn1)
                    latest_ts = latest_map.get(sym_u)
                    if latest_ts is None or latest_ts <= 0:
                        from_ts = int(_BOOTSTRAP_DEFAULT_START_UTC.timestamp())
                    else:
                        from_ts = int(latest_ts) - _tf_overlap_seconds(tf)

                if from_ts < 0:
                    from_ts = 0
                if from_ts >= to_ts:
                    continue

                job_id = int(self._job_id_seq)
                self._job_id_seq += 1
                pending.add(job_id)

                payload = _pack_history_fetch_payload(
                    symbol=sym_u,
                    tf=tf,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    max_bars=int(self._bootstrap_history_max_bars),
                    chunk_bars=int(self._bootstrap_history_chunk_bars),
                )
                frame = pack_frame(MSG_HISTORY_FETCH, payload, job_id=job_id)
                frames_by_job[job_id] = frame
                specs_by_job[job_id] = {
                    "symbol": sym_u,
                    "tf": TF_TO_NAME.get(tf, str(tf)),
                    "from_ts": int(from_ts),
                    "to_ts": int(to_ts),
                    "sent_at": 0,
                    "attempts": 0,
                }
                q.append(job_id)

        if not pending:
            await self._bootstrap_send_subscribe_if_needed(sid=sid, writer=writer)
            return

        self._bootstrap_pending_jobs[sid] = pending
        self._bootstrap_job_frames[sid] = frames_by_job
        self._bootstrap_job_specs[sid] = specs_by_job
        self._bootstrap_queue[sid] = q
        self._bootstrap_inflight[sid] = set()

        logger.info("[MT5] bootstrap queued sid=%s symbols=%s jobs=%s", sid, len(symbols), len(pending))

        # Kick async sender (flow-controlled). Sender uses the shared writer.
        self._bootstrap_kick_sender(sid=sid)


    def _bootstrap_kick_sender(self, *, sid: int, delay: int = 0) -> None:
        # Avoid creating multiple concurrent sender tasks per session.
        existing = self._bootstrap_send_task.get(sid)
        if existing is not None and not existing.done():
            return

        async def _runner() -> None:
            if delay:
                await asyncio.sleep(float(delay))
            try:
                await self._bootstrap_send_loop(sid=sid)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[MT5] bootstrap sender crashed sid=%s err=%s", sid, e)

        self._bootstrap_send_task[sid] = asyncio.create_task(_runner())


    async def _bootstrap_send_loop(self, *, sid: int) -> None:
        # Best-effort: find current writer for sid.
        async with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            return
        writer = sess.writer

        pending = self._bootstrap_pending_jobs.get(sid)
        frames = self._bootstrap_job_frames.get(sid)
        specs = self._bootstrap_job_specs.get(sid)
        q = self._bootstrap_queue.get(sid)
        inflight = self._bootstrap_inflight.get(sid)
        if pending is None or frames is None or specs is None or q is None or inflight is None:
            return

        max_inflight = max(1, int(self._bootstrap_max_inflight))

        # Drain as much as possible under inflight limit.
        while q and len(inflight) < max_inflight:
            job_id = int(q.popleft())
            if job_id not in pending:
                continue

            data = frames.get(job_id)
            if data is None:
                # Unknown job - drop.
                pending.discard(job_id)
                continue

            spec = specs.get(job_id) or {}
            # Timeout watchdog: if we've tried too long, drop the job so bootstrap can finish.
            sent_at = int(spec.get("sent_at") or 0)
            attempts = int(spec.get("attempts") or 0)
            if sent_at and (int(time.time()) - sent_at) > int(self._bootstrap_job_timeout_seconds) and attempts >= 3:
                pending.discard(job_id)
                inflight.discard(job_id)
                frames.pop(job_id, None)
                specs.pop(job_id, None)
                logger.error("[MT5] bootstrap timeout sid=%s job=%s sym=%s tf=%s", sid, job_id, spec.get("symbol"), spec.get("tf"))
                continue

            try:
                writer.write(data)
                await writer.drain()
                inflight.add(job_id)
                spec["sent_at"] = int(time.time())
                spec["attempts"] = attempts + 1
                specs[job_id] = spec
                if self._debug:
                    logger.debug(
                        "[MT5] bootstrap tx sid=%s job=%s sym=%s tf=%s from=%s to=%s",
                        sid,
                        job_id,
                        spec.get("symbol"),
                        spec.get("tf"),
                        spec.get("from_ts"),
                        spec.get("to_ts"),
                    )
            except Exception as e:
                # Put back in queue for retry.
                q.appendleft(job_id)
                logger.warning("[MT5] bootstrap tx failed sid=%s job=%s err=%s", sid, job_id, e)
                return


    async def _bootstrap_send_subscribe_if_needed(self, *, sid: int, writer: asyncio.StreamWriter) -> None:
        if sid in self._bootstrap_subscribed:
            return

        symbols = self._bootstrap_symbols_by_sid.get(sid)
        if symbols is None:
            symbols = await self._get_bootstrap_symbols()
            self._bootstrap_symbols_by_sid[sid] = symbols

        if not symbols:
            return

        payload = _pack_subscribe_payload(symbols)
        frame = pack_frame(MSG_SUBSCRIBE, payload)
        writer.write(frame)
        await writer.drain()
        self._bootstrap_subscribed.add(sid)
        _touch(self._ready_subscribed_file)
        _set_ready_redis_key(self._ready_subscribed_redis_key, self._ready_redis_ttl_seconds)
        _touch(self._heartbeat_file)
        logger.info("[MT5] Bootstrap SUBSCRIBE sent sid=%s symbols=%s", sid, len(symbols))


    async def _get_bootstrap_symbols(self) -> list[str]:
        # Explicit env wins.
        if self._bootstrap_symbols:
            return list(self._bootstrap_symbols)

        # Shared Redis cache (backed by DB). DB remains the source of truth.
        if not self._bootstrap_symbols_from_db:
            return ["EURUSD"]

        # Refresh the shared symbol cache on connect so SUBSCRIBE covers all known symbols.
        # DB remains the source of truth.
        await refresh_active_symbols(redis_client=redis_client, postgres_dsn=POSTGRES_DSN)
        syms = await get_active_symbols(redis_client=redis_client, postgres_dsn=POSTGRES_DSN, fallback=["EURUSD"])
        if len(syms) > 255:
            syms = syms[:255]
        if self._debug:
            logger.debug("[MT5] Active symbols count=%s symbols=%s", len(syms), syms)
        return list(syms)


def _publish_candle(*, symbol: str, timeframe: str, ts_open: int, candle: dict, is_forming: bool) -> None:
    # Do not invalidate DB candle cache for forming candles (they are ephemeral and not stored).
    if not is_forming:
        CandleCache.invalidate(symbol=symbol, timeframe=timeframe)

    dt = datetime.fromtimestamp(ts_open, tz=timezone.utc)
    candle_msg = {
        "time": dt.isoformat(),
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": int(candle.get("volume", 0)),
    }
    PubSubManager.publish_candle_update(symbol, timeframe, candle_msg, is_forming=is_forming)


async def _publish_forming_aggregates(*, symbol: str, forming_m1: dict, timeframes: list[str]) -> None:
    """Compute forming candles for higher timeframes and publish via Redis/SSE.

    Notes:
    - Uses Redis-maintained bucket state (closed M1) + overlays latest forming M1 snapshot.
    - Does NOT write any forming candles to DB.
    """
    try:
        ts_open = int(forming_m1["ts_open"])
        forming_dt = datetime.fromtimestamp(ts_open, tz=timezone.utc)

        # Always include M1 in the list for completeness, but M1 was already published.
        for tf in timeframes:
            if tf == "M1":
                continue
            minutes = _TF_MINUTES.get(tf)
            if not minutes:
                continue

            candle_msg = await asyncio.to_thread(
                _compute_forming_candle_sync,
                symbol,
                tf,
                minutes,
                forming_dt,
                forming_m1,
            )
            if candle_msg is None:
                continue
            PubSubManager.publish_candle_update(symbol, tf, candle_msg, is_forming=True)
    except Exception as e:
        logger.debug("[MT5] Forming aggregate failed sym=%s err=%s", symbol, e)


def _compute_forming_candle_sync(
    symbol: str,
    timeframe: str,
    timeframe_minutes: int,
    forming_dt: datetime,
    forming_m1: dict,
) -> Optional[dict]:
    bucket_start = _floor_utc_bucket(forming_dt, int(timeframe_minutes))

    open_first = None
    high_max = None
    low_min = None
    vol_sum = 0

    try:
        key = _forming_state_key(symbol, timeframe, bucket_start)
        state = redis_client.hgetall(key) or {}
        if state:
            if state.get("open") is not None:
                open_first = float(state["open"])
            if state.get("high") is not None:
                high_max = float(state["high"])
            if state.get("low") is not None:
                low_min = float(state["low"])
            if state.get("volume") is not None:
                vol_sum = int(float(state["volume"]))
    except Exception:
        # Redis issues should not break live streaming.
        open_first = None
        high_max = None
        low_min = None
        vol_sum = 0

    f_open = float(forming_m1["open"])
    f_high = float(forming_m1["high"])
    f_low = float(forming_m1["low"])
    f_close = float(forming_m1["close"])
    f_vol = int(forming_m1.get("volume", 0) or 0)

    o = open_first if open_first is not None else f_open
    h = max([x for x in [high_max, f_high] if x is not None])
    l = min([x for x in [low_min, f_low] if x is not None])
    v = int(vol_sum) + int(f_vol)

    return {
        "time": bucket_start.isoformat(),
        "open": float(o),
        "high": float(h),
        "low": float(l),
        "close": float(f_close),
        "volume": int(v),
    }


async def _write_candle(
    *,
    symbol: str,
    timeframe: str,
    ts_open: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    upsert: bool,
) -> None:
    await asyncio.to_thread(
        _write_candle_sync,
        symbol,
        timeframe,
        int(ts_open),
        float(open_),
        float(high),
        float(low),
        float(close),
        int(volume),
        bool(upsert),
    )


async def _write_candles_bulk(*, symbol: str, timeframe: str, rows: list[dict], upsert: bool) -> None:
    if not rows:
        return

    await asyncio.to_thread(_write_candles_bulk_sync, symbol, timeframe, rows, bool(upsert))


def _write_candle_sync(
    symbol: str,
    timeframe: str,
    ts_open: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    upsert: bool,
) -> None:
    # Fail fast: the base candlesticks table is reserved for broker-provided TFs only.
    from trading_common.timeframes import assert_timeframe_policy
    assert_timeframe_policy(timeframe, "broker_raw")

    dt = datetime.fromtimestamp(int(ts_open), tz=timezone.utc)

    if upsert:
        sql = """
            INSERT INTO candlesticks(time, symbol, timeframe, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, timeframe, time)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
        """
    else:
        sql = """
            INSERT INTO candlesticks(time, symbol, timeframe, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, timeframe, time)
            DO NOTHING
        """

    with psycopg.connect(POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (dt, symbol, timeframe, open_, high, low, close, volume))
        conn.commit()


def _write_candles_bulk_sync(symbol: str, timeframe: str, rows: list[dict], upsert: bool) -> None:
    # Fail fast: the base candlesticks table is reserved for broker-provided TFs only.
    from trading_common.timeframes import assert_timeframe_policy
    assert_timeframe_policy(timeframe, "broker_raw")

    if upsert:
        sql = """
            INSERT INTO candlesticks(time, symbol, timeframe, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, timeframe, time)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
        """
    else:
        sql = """
            INSERT INTO candlesticks(time, symbol, timeframe, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, timeframe, time)
            DO NOTHING
        """

    values = []
    for r in rows:
        dt = datetime.fromtimestamp(int(r["ts_open"]), tz=timezone.utc)
        values.append(
            (
                dt,
                symbol,
                timeframe,
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                int(r.get("volume", 0)),
            )
        )

    with psycopg.connect(POSTGRES_DSN) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, values)
        conn.commit()


def _pack_subscribe_payload(symbols: list[str]) -> bytes:
    # count:u16, then symbols[16]*count
    from .mt5_wire import pack_symbol
    import struct

    syms = [s.upper() for s in symbols if s]
    if len(syms) > 255:
        syms = syms[:255]
    buf = bytearray()
    buf += struct.pack("<H", len(syms))
    for s in syms:
        buf += pack_symbol(s)
    return bytes(buf)


def _pack_history_fetch_payload(
    *,
    symbol: str,
    tf: int,
    from_ts: int,
    to_ts: int,
    max_bars: int,
    chunk_bars: int,
) -> bytes:
    from .mt5_wire import pack_symbol
    import struct

    # symbol[16], tf:u8, rsv[3], from:i64, to:i64, max_bars:u32, chunk_bars:u32
    return struct.pack(
        "<16sB3sqqII",
        pack_symbol(symbol),
        int(tf),
        b"\x00\x00\x00",
        int(from_ts),
        int(to_ts),
        int(max_bars),
        int(chunk_bars),
    )


mt5_ingest_server = MT5IngestServer()
