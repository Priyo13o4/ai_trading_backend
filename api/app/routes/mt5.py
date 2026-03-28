from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from ..mt5_ingest import mt5_ingest_server
from ..mt5_wire import TF_M1

router = APIRouter(prefix="/api/mt5", tags=["mt5"])


def _require_token(x_mt5_token: Optional[str]) -> None:
    expected = os.getenv("MT5_CONTROL_TOKEN")
    if expected:
        if not x_mt5_token or x_mt5_token != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/status")
async def status(x_mt5_token: Optional[str] = Header(None)):
    _require_token(x_mt5_token)
    return {
        "bridge_sessions": mt5_ingest_server.session_count,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/subscribe")
async def subscribe(payload: dict, x_mt5_token: Optional[str] = Header(None)):
    _require_token(x_mt5_token)

    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must be a non-empty list")

    sent = await mt5_ingest_server.broadcast_subscribe([str(s).upper() for s in symbols])
    return {"sent_to_bridges": sent, "symbols": symbols}


@router.post("/history-fetch")
async def history_fetch(payload: dict, x_mt5_token: Optional[str] = Header(None)):
    _require_token(x_mt5_token)

    symbol = str(payload.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    from_ts = payload.get("from_ts")
    to_ts = payload.get("to_ts")
    if not isinstance(from_ts, int) or not isinstance(to_ts, int):
        raise HTTPException(status_code=400, detail="from_ts and to_ts must be unix seconds (int)")

    max_bars = int(payload.get("max_bars") or 2000)
    chunk_bars = int(payload.get("chunk_bars") or 1000)
    job_id = int(payload.get("job_id") or 0)

    sent = await mt5_ingest_server.broadcast_history_fetch(
        symbol=symbol,
        from_ts=from_ts,
        to_ts=to_ts,
        tf=TF_M1,
        max_bars=max_bars,
        chunk_bars=chunk_bars,
        job_id=job_id,
    )

    return {
        "sent_to_bridges": sent,
        "symbol": symbol,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "max_bars": max_bars,
        "chunk_bars": chunk_bars,
        "job_id": job_id,
    }
