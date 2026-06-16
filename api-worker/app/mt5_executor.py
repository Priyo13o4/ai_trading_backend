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
from typing import Any, Optional

import redis.asyncio as aioredis
from sqlalchemy import select

from .db import AsyncSessionLocal
from .error_alerts import report_runtime_error
from .mt5_wire import (
    Frame,
    ProtocolError,
    MSG_HELLO,
    MSG_HEARTBEAT,
    MSG_ERROR,
    MSG_STRATEGY_PUSH,
    MSG_TRADE_EVENT,
    MSG_POSITION_SYNC_REQUEST,
    MSG_POSITION_SYNC_RESPONSE,
    HEADER_LEN,
    pack_frame,
    read_frame,
    unpack_header,
    pack_strategy_push,
    unpack_trade_event,
)

logger = logging.getLogger(__name__)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"1", "true", "yes", "y", "on"}:
            return True
        if normalised in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _parse_event_time(value: Any, fallback: datetime) -> datetime:
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return fallback
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return fallback

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_trade_status(status: Any, close_reason: Any = None) -> str:
    """Normalise the EA status string to a DB-valid signals status value.

    The EA sends a generic ``"closed"`` status for all position closures.
    The DB check constraint requires one of the specific variants:
      closed_tp | closed_sl | closed_manual | closed_expired | closed_breakeven

    We resolve the specific variant from ``close_reason`` (sent alongside the
    status) so the DB write never violates the constraint.
    """
    normalised = str(status or "open").strip().lower()
    if normalised in ("executed", "partial_close"):
        return "open"
        
    if normalised == "closed_early":
        return "closed_expired"

    # Resolve generic "closed" → specific DB-valid closed_* variant
    if normalised == "closed":
        reason = str(close_reason or "").strip().lower()
        if "tp" in reason or "take_profit" in reason:
            return "closed_tp"
        elif "sl" in reason or "stop_loss" in reason or "structure_break" in reason:
            return "closed_sl"
        elif "breakeven" in reason or "break_even" in reason:
            return "closed_breakeven"
        elif "expir" in reason or "time_stall" in reason or "fail_to_reach" in reason or "timeout" in reason:
            return "closed_expired"
        else:
            # Generic manual/EA-triggered close — covers early_exit rules etc.
            return "closed_manual"

    return normalised


def _is_lifecycle_only(status: str, ticket: int) -> bool:
    return ticket <= 0


def _is_closed_status(status: str) -> bool:
    return status == "closed" or status.startswith("closed_")


def _derive_exit_flags(status: str, close_reason: Any) -> tuple[bool | None, bool | None]:
    reason = str(close_reason or "").strip().lower()
    hit_tp = None
    hit_sl = None
    if "tp" in reason or "take_profit" in reason or status == "closed_tp":
        hit_tp = True
        hit_sl = False
    elif "sl" in reason or "stop_loss" in reason or status == "closed_sl" or "structure_break" in reason:
        hit_tp = False
        hit_sl = True
    return hit_tp, hit_sl


def _accumulate(existing: Any, added: Any) -> float:
    return _as_float(existing) + _as_float(added)

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
        self._processed_deals: set[int] = set()

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
                                expiry_time=data.get("expiry_time"),
                                execution_allowed=_as_bool(data.get("execution_allowed"), True),
                                trade_recommended=_as_bool(data.get("trade_recommended"), True),
                                risk_level=data.get("risk_level"),
                                trade_mode=data.get("trade_mode"),
                                pre_entry_rule=data.get("pre_entry_rule"),
                                post_entry_rule=data.get("post_entry_rule"),
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
                    status_val = event.get('status', 'open')
                    pnl_val = event.get('pnl', 0.0)
                    logger.info(f"[MT5-EXEC] TRADE_EVENT ticket={event['ticket']} strategy_id={event['strategy_id']} status={status_val} pnl={pnl_val}")
                    asyncio.create_task(self._handle_trade_event(event))
                    continue

                elif frame.msg_type == MSG_POSITION_SYNC_REQUEST:
                    asyncio.create_task(self._handle_position_sync_request(writer, frame.payload))
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
                from trading_common.models import Signal, Strategy, LiveTradeState

                now = datetime.now(timezone.utc)
                status = _normalise_trade_status(event.get("status"), event.get("close_reason"))
                ticket = _as_int(event.get("ticket"))
                strategy_id = _as_int(event.get("strategy_id"))
                event_time = _parse_event_time(event.get("event_time"), now)

                # Deduplicate deal events using in-memory set
                deal_id = event.get("deal_id")
                if deal_id and int(deal_id) > 0:
                    if int(deal_id) in self._processed_deals:
                        logger.info(f"[MT5-EXEC] Skipping duplicate deal_id={deal_id}")
                        return
                    self._processed_deals.add(int(deal_id))
                    if len(self._processed_deals) > 10000:
                        self._processed_deals.clear()

                # Fetch and lock strategy
                strategy = None
                if strategy_id > 0:
                    strat_stmt = select(Strategy).where(Strategy.strategy_id == strategy_id).with_for_update()
                    strategy = (await db.execute(strat_stmt)).scalars().first()

                if strategy:
                    strategy.execution_status = status or strategy.execution_status
                    if status in {
                        "open",
                        "partial_close",
                        "closed",
                        "expired",
                        "invalidated",
                        "rejected",
                        "unsupported_condition_type",
                    } or status.startswith("rejected_") or status.startswith("closed_"):
                        strategy.executed_at = now
                    # We no longer update strategy.status from MT5. The cron job owns it.

                if _is_lifecycle_only(status, ticket):
                    await db.commit()
                    logger.info(
                        "[MT5-EXEC] Saved strategy lifecycle event strategy_id=%s status=%s ticket=%s",
                        strategy_id,
                        status,
                        ticket,
                    )
                    return

                stmt = select(Signal).where(Signal.mt5_ticket == ticket).with_for_update()
                signal = (await db.execute(stmt)).scalars().first()

                if signal is None:
                    signal = Signal(
                        strategy_id=strategy_id or None,
                        mt5_ticket=ticket,
                        mt5_magic_number=event.get("magic_number"),
                        trading_pair=event.get("symbol", "UNKNOWN"),
                        direction=event.get("direction", "UNKNOWN"),
                        entry_price=_as_float(event.get("price")),
                        take_profit=_as_float(event.get("tp")),
                        stop_loss=_as_float(event.get("sl")),
                        lot_size=_as_float(event.get("volume"), 0.01),
                        entry_time=event_time,
                        status="open" if _is_closed_status(status) else status,
                        pnl=0.0,
                        commission=0.0,
                        swap=0.0,
                        partial_close_executed=False,
                        break_even_moved=False,
                        source="live_ea",
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(signal)

                signal.updated_at = now
                signal.mt5_magic_number = event.get("magic_number", signal.mt5_magic_number)
                signal.trading_pair = event.get("symbol", signal.trading_pair)
                signal.direction = event.get("direction", signal.direction)
                signal.source = signal.source or "live_ea"

                # Guard against out-of-order updates reverting a closed trade to open/partial_close
                is_currently_closed = signal.status is not None and signal.status.startswith("closed_")

                if status == "open":
                    if not is_currently_closed:
                        signal.status = "open"
                    signal.entry_price = _as_float(event.get("price"), _as_float(signal.entry_price))
                    signal.take_profit = _as_float(event.get("tp"), _as_float(signal.take_profit))
                    signal.stop_loss = _as_float(event.get("sl"), _as_float(signal.stop_loss))
                    signal.lot_size = _as_float(event.get("volume"), _as_float(signal.lot_size, 0.01))
                    signal.entry_time = signal.entry_time or event_time
                    if strategy and not is_currently_closed:
                        strategy.execution_status = "open"

                elif status == "partial_close":
                    if not is_currently_closed:
                        signal.status = "partial_close"
                    signal.partial_close_executed = bool(event.get("partial_close_executed", True))
                    if strategy and not is_currently_closed:
                        strategy.execution_status = "partial_close"

                elif _is_closed_status(status):
                    # Prevent overwriting a specific closure status with generic manual close
                    if status == "closed_manual" and signal.status in {"closed_tp", "closed_sl", "closed_expired", "closed_breakeven"}:
                        status = signal.status
                    else:
                        signal.status = status

                    signal.exit_price = _as_float(event.get("price"), _as_float(signal.exit_price))
                    signal.take_profit = _as_float(event.get("tp"), _as_float(signal.take_profit))
                    signal.stop_loss = _as_float(event.get("sl"), _as_float(signal.stop_loss))
                    signal.exit_time = event_time
                    hit_tp, hit_sl = _derive_exit_flags(status, event.get("close_reason"))
                    if hit_tp is not None:
                        signal.hit_tp = hit_tp
                    if hit_sl is not None:
                        signal.hit_sl = hit_sl
                    if strategy:
                        strategy.execution_status = status

                    if signal.entry_price and signal.exit_price and signal.trading_pair and signal.direction:
                        symbol_upper = signal.trading_pair.upper()
                        if "JPY" in symbol_upper:
                            multiplier = 100.0
                        elif "XAUUSD" in symbol_upper or "GOLD" in symbol_upper:
                            multiplier = 10.0  # 1 pip = 0.1 USD
                        elif "XAG" in symbol_upper:
                            multiplier = 100.0  # 1 pip = 0.01 USD
                        elif "BTC" in symbol_upper or "ETH" in symbol_upper:
                            multiplier = 1.0   # 1 pip = 1.0 USD
                        elif any(idx in symbol_upper for idx in ("US30", "SPX", "NAS100", "GER30", "EUSTX50", "UK100")):
                            multiplier = 1.0   # 1 pip = 1 index point
                        else:
                            multiplier = 10000.0
                        diff = float(signal.exit_price) - float(signal.entry_price)
                        if signal.direction.lower() in ("sell", "short"):
                            diff = -diff
                        signal.pnl_pips = round(diff * multiplier, 2)

                else:
                    signal.status = status

                # --- WS1 fix: write mae_pips/mfe_pips on EVERY event status ---
                # The EA populates these from the position's running max_favorable/max_adverse
                # price, so they are meaningful on open and partial_close events too.
                raw_event = event.get("raw", {})
                if event.get("mae_pips") is not None:
                    signal.mae_pips = _as_float(event.get("mae_pips"))
                if event.get("mfe_pips") is not None:
                    signal.mfe_pips = _as_float(event.get("mfe_pips"))

                # Update PnL, commission, and swap only if they were explicitly sent in the raw payload
                # to prevent overwriting with 0.0 during modification events.
                if "pnl" in raw_event:
                    signal.pnl = _accumulate(signal.pnl, event.get("pnl"))
                if "commission" in raw_event:
                    signal.commission = _accumulate(signal.commission, event.get("commission"))
                if "swap" in raw_event:
                    signal.swap = _accumulate(signal.swap, event.get("swap"))

                if event.get("partial_close_executed") is not None:
                    signal.partial_close_executed = bool(event.get("partial_close_executed"))
                if event.get("break_even_moved") is not None:
                    signal.break_even_moved = bool(event.get("break_even_moved"))

                # --- WS2A: persist / update live_trade_state ---
                await self._upsert_live_trade_state(db, event, status, strategy, now)

                await db.commit()
        except Exception as e:
            logger.error(f"[MT5-EXEC] DB Error saving trade event: {e}", exc_info=True)
            report_runtime_error(
                path="mt5_executor.py/_handle_trade_event",
                method="TCP",
                status_code=500,
                message_safe="Failed to save MT5 trade event to database",
                message_internal=str(e),
                context={"event": event},
                severity="error"
            )

    async def _upsert_live_trade_state(self, db, event: dict, status: str, strategy, now: datetime) -> None:
        """Persist or update the live_trade_state row for this ticket.

        Issued fields (direction, timeframe, entry_price, original_sl,
        original_tp, pre_entry_rule, post_entry_rule) are written only on
        the first ``open`` event and never overwritten.

        Running-state fields are updated on every event.
        """
        from trading_common.models import LiveTradeState

        ticket = _as_int(event.get("ticket"))
        if ticket <= 0:
            return

        lts_stmt = select(LiveTradeState).where(LiveTradeState.ticket == ticket).with_for_update()
        lts = (await db.execute(lts_stmt)).scalars().first()

        if lts is None and status == "open":
            # --- First open event: write issued fields from strategy + event ---
            timeframe = None
            original_sl = _as_float(event.get("sl")) or None
            original_tp = _as_float(event.get("tp")) or None
            pre_entry_rule = None
            post_entry_rule = None

            if strategy is not None:
                # Timeframe lives in strategy.entry_signal.timeframe (JSONB)
                entry_signal = strategy.entry_signal or {}
                timeframe = entry_signal.get("timeframe") or None
                # Original SL/TP: prefer strategy values (they are the signal-time prices)
                if strategy.stop_loss:
                    original_sl = float(strategy.stop_loss)
                if strategy.take_profit:
                    original_tp = float(strategy.take_profit)
                pre_entry_rule = strategy.pre_entry_rule
                post_entry_rule = strategy.post_entry_rule

            lts = LiveTradeState(
                ticket=ticket,
                signal_hash=None,  # EA doesn't send this in TRADE_EVENT JSON today
                strategy_id=_as_int(event.get("strategy_id")) or None,
                direction=event.get("direction", "long"),
                timeframe=timeframe,
                entry_price=_as_float(event.get("price")),
                original_sl=original_sl,
                original_tp=original_tp,
                pre_entry_rule=pre_entry_rule,
                post_entry_rule=post_entry_rule,
                partial_closed=False,
                break_even_moved=False,
                state="open",
                created_at=now,
                updated_at=now,
            )
            db.add(lts)
            logger.info(
                "[MT5-EXEC] LiveTradeState created ticket=%s strategy_id=%s timeframe=%s",
                ticket,
                lts.strategy_id,
                lts.timeframe,
            )
        elif lts is not None:
            # --- Subsequent events: update running state only ---
            lts.updated_at = now

            if event.get("partial_close_executed"):
                lts.partial_closed = True
            if event.get("break_even_moved"):
                lts.break_even_moved = True
            if event.get("mae_pips") is not None:
                lts.latest_mae_pips = _as_float(event.get("mae_pips"))
            if event.get("mfe_pips") is not None:
                lts.latest_mfe_pips = _as_float(event.get("mfe_pips"))

            # Derive max_favorable/max_adverse prices from EA's MAE/MFE pips
            # (price-level tracking would require per-symbol pip multiplier here;
            #  we store the pip metrics and leave max_favorable/max_adverse as
            #  an optional future enrichment).

            if _is_closed_status(status):
                lts.state = status
            elif status == "partial_close":
                lts.state = "partial_close"

    async def _handle_position_sync_request(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        """WS2B: Respond to a POSITION_SYNC_REQUEST from the EA.

        The EA sends a JSON list of {ticket, signal_hash} for its currently
        open positions.  The backend returns a POSITION_SYNC_RESPONSE with
        the full live_trade_state record for each matched ticket.

        Payload contract per position:
          {"signal_hash": str, "ticket": int, "strategy_id": int,
           "direction": "long|short", "timeframe": "M5|...",
           "entry_price": float, "original_sl": float, "original_tp": float,
           "partial_closed": bool, "break_even_moved": bool,
           "max_favorable_price": float, "max_adverse_price": float,
           "pre_entry_rule": {"rule_type": "none", "params": {}},
           "post_entry_rule": {"rule_type": "none", "params": {}}}
        """
        try:
            from trading_common.models import LiveTradeState

            request_list = json.loads(payload.decode("utf-8", errors="ignore"))
            if not isinstance(request_list, list):
                request_list = []

            tickets = [_as_int(item.get("ticket")) for item in request_list if _as_int(item.get("ticket")) > 0]

            async with AsyncSessionLocal() as db:
                results = []
                if tickets:
                    stmt = select(LiveTradeState).where(
                        LiveTradeState.ticket.in_(tickets),
                    )
                    rows = (await db.execute(stmt)).scalars().all()
                else:
                    # No tickets supplied: return all currently-open records
                    stmt = select(LiveTradeState).where(
                        LiveTradeState.state.in_(["open", "partial_close"])
                    )
                    rows = (await db.execute(stmt)).scalars().all()

                _NONE_RULE = {"rule_type": "none", "params": {}}

                for row in rows:
                    results.append({
                        "signal_hash":         row.signal_hash or "",
                        "ticket":              row.ticket,
                        "strategy_id":         row.strategy_id or 0,
                        "direction":           row.direction,
                        "timeframe":           row.timeframe or "M15",
                        "entry_price":         float(row.entry_price) if row.entry_price else 0.0,
                        "original_sl":         float(row.original_sl) if row.original_sl else 0.0,
                        "original_tp":         float(row.original_tp) if row.original_tp else 0.0,
                        "partial_closed":      bool(row.partial_closed),
                        "break_even_moved":    bool(row.break_even_moved),
                        "max_favorable_price": float(row.max_favorable_price) if row.max_favorable_price else 0.0,
                        "max_adverse_price":   float(row.max_adverse_price) if row.max_adverse_price else 0.0,
                        "pre_entry_rule":      row.pre_entry_rule or _NONE_RULE,
                        "post_entry_rule":     row.post_entry_rule or _NONE_RULE,
                    })

            response_payload = json.dumps(results).encode("utf-8")
            frame = pack_frame(MSG_POSITION_SYNC_RESPONSE, response_payload)
            try:
                writer.write(frame)
                await writer.drain()
            except Exception as send_err:
                logger.warning("[MT5-EXEC] POSITION_SYNC_RESPONSE send failed: %s", send_err)
            logger.info(
                "[MT5-EXEC] POSITION_SYNC_RESPONSE sent tickets=%s positions=%s",
                tickets,
                len(results),
            )
        except Exception as e:
            logger.error("[MT5-EXEC] _handle_position_sync_request error: %s", e, exc_info=True)


mt5_executor_server = MT5ExecutorServer()
