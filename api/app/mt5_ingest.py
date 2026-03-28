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
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Optional

import psycopg

from .cache import CandleCache, PubSubManager
from .db import POSTGRES_DSN
from .mt5_wire import (
    Frame,
    ProtocolError,
    TF_M1,
    MSG_LIVE_BAR,
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
    iter_hist_chunk,
    unpack_header,
)

logger = logging.getLogger(__name__)


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


_MSG_NAME: dict[int, str] = {
    MSG_HELLO: "HELLO",
    MSG_HEARTBEAT: "HEARTBEAT",
    MSG_ERROR: "ERROR",
    MSG_SUBSCRIBE: "SUBSCRIBE",
    MSG_HISTORY_FETCH: "HISTORY_FETCH",
    MSG_LIVE_BAR: "LIVE_BAR",
    MSG_HIST_BEGIN: "HIST_BEGIN",
    MSG_HIST_CHUNK: "HIST_CHUNK",
    MSG_HIST_END: "HIST_END",
}


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
        # Prefer explicit env configuration; otherwise discover dynamically from DB.
        self._bootstrap_symbols = [
            s.strip().upper()
            for s in (os.getenv("MT5_SUBSCRIBE_SYMBOLS") or "").split(",")
            if s.strip()
        ]
        self._bootstrap_symbols_from_db = _env_bool("MT5_SUBSCRIBE_FROM_DB", True)
        self._bootstrap_symbols_cache_ttl_s = _env_int("MT5_SYMBOL_DISCOVERY_CACHE_TTL_SECONDS", 30)
        self._bootstrap_symbols_cache: tuple[float, list[str]] = (0.0, [])
        self._bootstrap_symbols_by_sid: dict[int, list[str]] = {}
        self._bootstrap_history_enable = _env_bool("MT5_BOOTSTRAP_HISTORY_ENABLE", True)
        self._bootstrap_history_lookback_minutes = _env_int("MT5_BOOTSTRAP_HISTORY_LOOKBACK_MINUTES", 5760)
        self._bootstrap_history_max_bars = _env_int("MT5_BOOTSTRAP_HISTORY_MAX_BARS", 6000)
        self._bootstrap_history_chunk_bars = _env_int("MT5_BOOTSTRAP_HISTORY_CHUNK_BARS", 1000)
        self._bootstrap_subscribe_after_history = _env_bool("MT5_BOOTSTRAP_SUBSCRIBE_AFTER_HISTORY", True)

        self._bootstrap_pending_jobs: dict[int, set[int]] = {}
        self._bootstrap_subscribed: set[int] = set()
        self._job_id_seq = 1

        self._ready_connected_file = os.getenv("MT5_READY_CONNECTED_FILE", "/tmp/mt5_connected")
        self._ready_subscribed_file = os.getenv("MT5_READY_SUBSCRIBED_FILE", "/tmp/mt5_ready")

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        host = os.getenv("MT5_INGEST_HOST", "0.0.0.0")
        port = _env_int("MT5_INGEST_PORT", 9001)

        self._server = await asyncio.start_server(self._handle_client, host, port)
        sockets = self._server.sockets or []
        bind = ", ".join(str(s.getsockname()) for s in sockets)
        logger.info("[MT5] Ingest TCP listening on %s", bind)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

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

        # Optional bootstrap: request history first, then subscribe.
        if self._bootstrap_enable and self._bootstrap_symbols:
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
                    if self._debug:
                        logger.debug(
                            "[MT5] LIVE_BAR sid=%s sym=%s ts_open=%s o=%.5f h=%.5f l=%.5f c=%.5f v=%s",
                            sid,
                            candle["symbol"],
                            candle["ts_open"],
                            float(candle["open"]),
                            float(candle["high"]),
                            float(candle["low"]),
                            float(candle["close"]),
                            int(candle.get("volume", 0)),
                        )
                    await _write_candle(
                        symbol=candle["symbol"],
                        timeframe="M1",
                        ts_open=int(candle["ts_open"]),
                        open_=float(candle["open"]),
                        high=float(candle["high"]),
                        low=float(candle["low"]),
                        close=float(candle["close"]),
                        volume=int(candle["volume"]),
                        upsert=live_upsert,
                    )
                    _publish_candle(symbol=candle["symbol"], timeframe="M1", ts_open=int(candle["ts_open"]), candle=candle)

                elif frame.msg_type == MSG_HIST_CHUNK:
                    meta, rows = iter_hist_chunk(frame.payload)
                    tf_s = "M1" if meta["timeframe"] == TF_M1 else "M1"

                    if self._debug:
                        first_ts = rows[0]["ts_open"] if rows else None
                        last_ts = rows[-1]["ts_open"] if rows else None
                        logger.debug(
                            "[MT5] HIST_CHUNK sid=%s sym=%s chunk=%s count=%s first_ts=%s last_ts=%s",
                            sid,
                            meta["symbol"],
                            meta["chunk_index"],
                            meta["count"],
                            first_ts,
                            last_ts,
                        )

                    # Write rows in a single DB transaction, but no full-range buffering.
                    await _write_candles_bulk(
                        symbol=meta["symbol"],
                        timeframe=tf_s,
                        rows=rows,
                        upsert=history_upsert,
                    )

                elif frame.msg_type in {MSG_HIST_BEGIN, MSG_HIST_END}:
                    # For now: informational. Could be used for metrics.
                    if self._debug:
                        logger.debug("[MT5] %s sid=%s job=%s", _msg_name(frame.msg_type), sid, frame.job_id)

                    # If we're bootstrapping history, mark jobs complete on HIST_END and subscribe once done.
                    if frame.msg_type == MSG_HIST_END:
                        pending = self._bootstrap_pending_jobs.get(sid)
                        if pending is not None and int(frame.job_id) in pending:
                            pending.discard(int(frame.job_id))
                            if self._debug:
                                logger.debug("[MT5] Bootstrap job done sid=%s job=%s remaining=%s", sid, frame.job_id, len(pending))
                            if not pending:
                                await self._bootstrap_send_subscribe_if_needed(sid=sid, writer=writer)
                    continue

                elif frame.msg_type in {MSG_HELLO, MSG_HEARTBEAT}:
                    # Keepalive / handshake.
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

        # If history is disabled, subscribe immediately.
        if not self._bootstrap_history_enable or not self._bootstrap_subscribe_after_history:
            await self._bootstrap_send_subscribe_if_needed(sid=sid, writer=writer)
            return

        now = int(datetime.now(timezone.utc).timestamp())
        lookback = max(1, int(self._bootstrap_history_lookback_minutes))
        from_ts = now - lookback * 60
        to_ts = now

        pending: set[int] = set()
        for sym in symbols:
            job_id = int(self._job_id_seq)
            self._job_id_seq += 1
            pending.add(job_id)

            payload = _pack_history_fetch_payload(
                symbol=sym,
                tf=TF_M1,
                from_ts=from_ts,
                to_ts=to_ts,
                max_bars=int(self._bootstrap_history_max_bars),
                chunk_bars=int(self._bootstrap_history_chunk_bars),
            )
            frame = pack_frame(MSG_HISTORY_FETCH, payload, job_id=job_id)
            writer.write(frame)

            if self._debug:
                logger.debug(
                    "[MT5] Bootstrap HISTORY_FETCH sid=%s sym=%s from=%s to=%s job=%s",
                    sid,
                    sym,
                    from_ts,
                    to_ts,
                    job_id,
                )

        await writer.drain()
        self._bootstrap_pending_jobs[sid] = pending
        logger.info("[MT5] Bootstrap history queued sid=%s symbols=%s jobs=%s", sid, len(symbols), len(pending))


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
        logger.info("[MT5] Bootstrap SUBSCRIBE sent sid=%s symbols=%s", sid, len(symbols))


    async def _get_bootstrap_symbols(self) -> list[str]:
        # Explicit env wins.
        if self._bootstrap_symbols:
            return list(self._bootstrap_symbols)

        # Cached DB discovery.
        now = time.monotonic()
        cached_at, cached_syms = self._bootstrap_symbols_cache
        if cached_syms and (now - cached_at) < float(self._bootstrap_symbols_cache_ttl_s):
            return list(cached_syms)

        syms: list[str] = []
        if self._bootstrap_symbols_from_db:
            syms = await asyncio.to_thread(_discover_symbols_from_db_sync)

        if not syms:
            # Safe fallback for first boot / empty DB.
            syms = ["EURUSD"]

        # Normalize and cap to protocol limit.
        syms = [s.strip().upper() for s in syms if s and str(s).strip()]
        if len(syms) > 255:
            syms = syms[:255]

        self._bootstrap_symbols_cache = (now, list(syms))
        if self._debug:
            logger.debug("[MT5] Discovered bootstrap symbols count=%s symbols=%s", len(syms), syms)
        return list(syms)


def _discover_symbols_from_db_sync() -> list[str]:
    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT symbol FROM candlesticks WHERE symbol IS NOT NULL AND symbol <> '' ORDER BY symbol"
                )
                return [str(r[0]).strip().upper() for r in cur.fetchall() if r and r[0]]
    except Exception as e:
        logger.warning("[MT5] Symbol discovery failed; falling back. err=%s", e)
        return []


def _publish_candle(*, symbol: str, timeframe: str, ts_open: int, candle: dict) -> None:
    # Invalidate cache for this symbol/timeframe.
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
    PubSubManager.publish_candle_update(symbol, timeframe, candle_msg)


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
