"""MT5 Trade Executor server (TCP, binary framed).

This runs as a standalone process and accepts connections from the Python bridge.
The bridge relays frames from the MT5 EA specifically for trade execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from sqlalchemy import select

from .db import AsyncSessionLocal
from .mt5_wire import (
    Frame,
    ProtocolError,
    MSG_HELLO,
    MSG_HEARTBEAT,
    MSG_ERROR,
    MSG_STRATEGY_PUSH,
    MSG_TRADE_EVENT,
    HEADER_LEN,
    pack_frame,
    read_frame,
    unpack_header,
    pack_strategy_push,
    unpack_trade_event,
)

logger = logging.getLogger(__name__)

class BridgeSession:
    def __init__(self, session_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str):
        self.session_id = session_id
        self.reader = reader
        self.writer = writer
        self.peer = peer


class MT5ExecutorServer:
    def __init__(self) -> None:
        self._server: Optional[asyncio.base_events.Server] = None
        self._sessions: dict[int, BridgeSession] = {}
        self._next_session_id = 1
        self._lock = asyncio.Lock()
        self._debug = os.getenv("MT5_PROTOCOL_DEBUG", "0").lower() in ("1", "true", "yes")

        self._heartbeat_file = os.getenv("MT5_EXECUTOR_HEARTBEAT_FILE", "/tmp/mt5_executor_heartbeat")
        self._heartbeat_interval_seconds = int(os.getenv("MT5_HEARTBEAT_INTERVAL_SECONDS", "15"))
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._strategy_pubsub_task: Optional[asyncio.Task] = None

    def _touch(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        try:
            with open(path, "a", encoding="utf-8"):
                os.utime(path, None)
        except Exception:
            pass

    async def start(self) -> None:
        host = os.getenv("MT5_EXECUTOR_HOST", "0.0.0.0")
        port = int(os.getenv("MT5_EXECUTOR_PORT", "9002"))

        self._server = await asyncio.start_server(self._handle_client, host, port)
        sockets = self._server.sockets or []
        valid_sockets = [s for s in sockets if s is not None]
        if valid_sockets:
            bind = ", ".join(str(s.getsockname()) for s in valid_sockets)
        else:
            bind = f"{host}:{port}"
        logger.info("[MT5-EXEC] Executor TCP listening on %s", bind)

        self._touch(self._heartbeat_file)
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        self._strategy_pubsub_task = asyncio.create_task(self._strategy_pubsub_loop())

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._strategy_pubsub_task is not None:
            self._strategy_pubsub_task.cancel()
            self._strategy_pubsub_task = None

    async def _heartbeat_loop(self) -> None:
        while True:
            self._touch(self._heartbeat_file)
            await asyncio.sleep(self._heartbeat_interval_seconds)

    async def _strategy_pubsub_loop(self) -> None:
        try:
            redis_url = (
                os.getenv("APP_REDIS_URL") 
                or os.getenv("CACHE_REDIS_URL") 
                or os.getenv("REDIS_URL") 
                or "redis://redis-app:6379/0"
            )
            logger.info(f"[MT5-EXEC] _strategy_pubsub_loop starting with {redis_url}")
            redis = aioredis.from_url(redis_url)
            pubsub = redis.pubsub()
            await pubsub.subscribe("mt5:strategy_push")
            logger.info("[MT5-EXEC] _strategy_pubsub_loop successfully subscribed to mt5:strategy_push")
            
            while True:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except Exception as get_err:
                    logger.error(f"Error getting message from pubsub: {get_err}")
                    await asyncio.sleep(1.0)
                    continue
                    
                if msg and msg["type"] == "message":
                    try:
                        data = json.loads(msg["data"].decode("utf-8"))
                        frame = pack_frame(
                            MSG_STRATEGY_PUSH,
                            pack_strategy_push(
                                strategy_id=data["strategy_id"],
                                strategy_name=data.get("strategy_name", ""),
                                symbol=data["symbol"],
                                direction=data["direction"],
                                take_profit=float(data.get("take_profit", 0) or 0),
                                stop_loss=float(data.get("stop_loss", 0) or 0),
                                entry_signal=data.get("entry_signal"),
                                confidence=data.get("confidence", "High"),
                                expiry_minutes=data.get("expiry_minutes", 240),
                                risk_reward_ratio=float(data.get("risk_reward_ratio", 0) or 0),
                                timestamp=data.get("timestamp"),
                                expiry_time=data.get("expiry_time")
                            )
                        )
                        sent = await self._broadcast(frame)
                        logger.info(f"[MT5-EXEC] Broadcast strategy_push id={data['strategy_id']} to {sent} clients")
                    except Exception as e:
                        logger.error(f"[MT5-EXEC] Error packing/sending strategy_push: {e}", exc_info=True)
        except Exception as outer_e:
            logger.error(f"[MT5-EXEC] FATAL error in _strategy_pubsub_loop: {outer_e}", exc_info=True)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe("mt5:strategy_push")
            await redis.aclose()

    async def _broadcast(self, data: bytes) -> int:
        async with self._lock:
            sessions = list(self._sessions.values())

        sent = 0
        for s in sessions:
            try:
                s.writer.write(data)
                await s.writer.drain()
                sent += 1
            except Exception:
                continue
        return sent

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_s = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)

        async with self._lock:
            sid = self._next_session_id
            self._next_session_id += 1
            self._sessions[sid] = BridgeSession(session_id=sid, reader=reader, writer=writer, peer=peer_s)

        logger.info("[MT5-EXEC] Bridge connected sid=%s peer=%s", sid, peer_s)

        try:
            writer.write(pack_frame(MSG_HELLO, b""))
            await writer.drain()
        except Exception as e:
            logger.warning("[MT5-EXEC] HELLO send failed sid=%s peer=%s err=%s", sid, peer_s, e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        try:
            await self._session_loop(sid, reader, writer)
        finally:
            async with self._lock:
                self._sessions.pop(sid, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("[MT5-EXEC] Bridge disconnected sid=%s peer=%s", sid, peer_s)

    async def _session_loop(self, sid: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                frame = await read_frame(reader, timeout=60.0)
            except ProtocolError as e:
                logger.warning("[MT5-EXEC] Protocol error sid=%s err=%s", sid, e)
                return
            except asyncio.TimeoutError:
                continue

            if self._debug:
                logger.debug(
                    "[MT5-EXEC] RX sid=%s type=%s seq=%s job=%s payload_len=%s",
                    sid,
                    frame.msg_type,
                    frame.seq,
                    frame.job_id,
                    len(frame.payload),
                )

            try:
                if frame.msg_type == MSG_TRADE_EVENT:
                    event = unpack_trade_event(frame.payload)
                    logger.info(f"[MT5-EXEC] TRADE_EVENT ticket={event['ticket']} strategy_id={event['strategy_id']} status={event['status']} pnl={event['pnl']}")
                    asyncio.create_task(self._handle_trade_event(event))
                    continue

                elif frame.msg_type == MSG_ERROR:
                    try:
                        error_data = json.loads(frame.payload.decode('utf-8', errors='ignore'))
                        logger.error(
                            "[MT5-EXEC] ERROR from EA sid=%s data=%s",
                            sid, error_data
                        )
                    except Exception as e:
                        logger.error("[MT5-EXEC] Failed to parse EA ERROR payload sid=%s payload=%s err=%s", sid, frame.payload, e)
                    continue

                elif frame.msg_type in {MSG_HELLO, MSG_HEARTBEAT}:
                    self._touch(self._heartbeat_file)
                    continue

                else:
                    if self._debug:
                        logger.debug("[MT5-EXEC] Ignoring msg sid=%s type=%s", sid, frame.msg_type)
                    continue

            except Exception as e:
                logger.error("[MT5-EXEC] Handler error sid=%s type=%s err=%s", sid, frame.msg_type, e, exc_info=True)

    async def _handle_trade_event(self, event: dict) -> None:
        try:
            async with AsyncSessionLocal() as db:
                from app.models import Signal
                now = datetime.now(timezone.utc)
                stmt = select(Signal).where(Signal.mt5_ticket == event['ticket'])
                signal = (await db.execute(stmt)).scalars().first()
                if signal:
                    signal.status = event['status']
                    signal.pnl = event['pnl']
                    signal.updated_at = now
                else:
                    new_signal = Signal(
                        strategy_id=event['strategy_id'],
                        mt5_ticket=event['ticket'],
                        trading_pair=event['symbol'],
                        direction=event.get('direction', 'UNKNOWN'),
                        entry_price=event['price'],
                        lot_size=event.get('volume', 0.01),
                        entry_time=now,
                        status=event['status'],
                        pnl=event.get('pnl', 0.0),
                        source='MT5_TRADE_EVENT'
                    )
                    db.add(new_signal)
                await db.commit()
        except Exception as e:
            logger.error(f"[MT5-EXEC] DB Error saving trade event: {e}")

mt5_executor_server = MT5ExecutorServer()
