#!/usr/bin/env python3
"""
Standalone MT5 ingest TCP server.

This server handles:
- MT5 EA connections via binary protocol
- Candle ingestion (M1, D1, W1, MN1)
- Redis/SSE pub/sub updates
- Symbol hot-add notifications (Postgres LISTEN/NOTIFY)

No HTTP overhead - pure TCP socket server on port 9001.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Add parent directory to path for imports
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
sys.path.insert(0, parent_dir)

from app.mt5_ingest import mt5_ingest_server
from app.mt5_symbol_notify import start_symbol_notify_listener
from app.mt5_wire import TF_M1, TF_D1, TF_W1, TF_MN1
from app.error_alerts import report_runtime_error

# Configure logging with UTC timestamps
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


_CONTROL_HOST = os.getenv("MT5_CONTROL_HOST", "127.0.0.1")
_CONTROL_PORT = int(os.getenv("MT5_CONTROL_PORT", "9002"))

_TF_NAME_TO_CODE = {
    "M1": TF_M1,
    "D1": TF_D1,
    "W1": TF_W1,
    "MN1": TF_MN1,
}


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _parse_time_to_ts(raw, *, field_name: str) -> int:
    """Accept unix seconds (int-like) or ISO8601 UTC strings."""
    if raw is None:
        raise ValueError(f"missing {field_name}")
    if isinstance(raw, (int, float)):
        return int(raw)

    s = str(raw).strip()
    if not s:
        raise ValueError(f"empty {field_name}")
    if s.isdigit():
        return int(s)

    # Support "...Z" and offset-aware ISO strings.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


async def _handle_control_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not raw:
            raise ValueError("empty request")
        req = json.loads(raw.decode("utf-8", errors="ignore"))

        action = str(req.get("action") or "").strip().lower()
        if action != "history_fetch":
            raise ValueError("unsupported action")

        symbol = str(req.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("missing symbol")

        tf_name = str(req.get("timeframe") or "M1").strip().upper()
        if tf_name not in _TF_NAME_TO_CODE:
            raise ValueError("timeframe must be one of M1/D1/W1/MN1")

        from_ts = _parse_time_to_ts(req.get("from_ts"), field_name="from_ts")
        to_ts = _parse_time_to_ts(req.get("to_ts"), field_name="to_ts")
        if from_ts >= to_ts:
            raise ValueError("from_ts must be less than to_ts")

        upsert = bool(req.get("upsert", True))
        max_bars = max(1, _safe_int(req.get("max_bars", 999999), 999999))
        chunk_bars = max(1, _safe_int(req.get("chunk_bars", 1000), 1000))
        job_id = req.get("job_id")
        req_job_id = _safe_int(job_id, 0) if job_id is not None else None

        sent, actual_job_id = await mt5_ingest_server.request_history_fetch(
            symbol=symbol,
            from_ts=from_ts,
            to_ts=to_ts,
            tf=_TF_NAME_TO_CODE[tf_name],
            max_bars=max_bars,
            chunk_bars=chunk_bars,
            upsert=upsert,
            job_id=req_job_id,
        )
        if sent <= 0:
            raise RuntimeError("no connected MT5 bridge sessions")

        resp = {
            "ok": True,
            "action": "history_fetch",
            "symbol": symbol,
            "timeframe": tf_name,
            "from_ts": from_ts,
            "to_ts": to_ts,
            "upsert": upsert,
            "job_id": actual_job_id,
            "sent_sessions": sent,
        }
        writer.write((json.dumps(resp) + "\n").encode("utf-8"))
        await writer.drain()
        logger.info(
            "[MT5 CTRL] history_fetch accepted peer=%s sym=%s tf=%s from=%s to=%s upsert=%s job=%s sessions=%s",
            peer, symbol, tf_name, from_ts, to_ts, upsert, actual_job_id, sent
        )
    except Exception as e:
        err = {"ok": False, "error": str(e)}
        try:
            writer.write((json.dumps(err) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        logger.warning("[MT5 CTRL] request failed peer=%s err=%s", peer, e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_control_server() -> asyncio.base_events.Server:
    server = await asyncio.start_server(_handle_control_client, _CONTROL_HOST, _CONTROL_PORT)
    sockets = server.sockets or []
    bind = ", ".join(str(s.getsockname()) for s in sockets if s is not None) or f"{_CONTROL_HOST}:{_CONTROL_PORT}"
    logger.info("✓ MT5 control server started on %s (internal-only)", bind)
    return server


async def main():
    """Start MT5 ingest server and symbol notify listener."""
    logger.info("=" * 80)
    logger.info("MT5 INGEST SERVER STARTING (TCP ONLY - NO HTTP)")
    logger.info("=" * 80)
    logger.info("Port: 9001 (TCP)")
    logger.info("Protocol: Binary framed (mt5_wire)")
    logger.info("=" * 80)
    
    try:
        # Start TCP server on port 9001
        await mt5_ingest_server.start()
        logger.info("✓ MT5 TCP server started on port 9001")

        # Start local-only control server for manual backfill commands.
        control_server = await _start_control_server()
        
        # Start symbol hot-add listener (Postgres LISTEN/NOTIFY)
        asyncio.create_task(start_symbol_notify_listener())
        logger.info("✓ Symbol notify listener started")
        
        logger.info("=" * 80)
        logger.info("MT5 INGEST SERVER READY")
        logger.info("Waiting for EA connections...")
        logger.info("=" * 80)
        
        # Keep alive forever
        await asyncio.Event().wait()
        
    except Exception as e:
        logger.error(f"Fatal error during startup: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("=" * 80)
        logger.info("MT5 ingest server stopped by user")
        logger.info("=" * 80)
    except Exception as e:
        report_runtime_error(
            path="/worker/mt5-ingest",
            method="PROCESS",
            status_code=500,
            message_safe="Worker runtime error",
            message_internal=f"{e.__class__.__name__}: {e}",
            context={
                "script": "mt5_ingest_server.py",
                "phase": "entrypoint",
            },
        )
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
