#!/usr/bin/env python3
"""Internal MT5 history-fetch command client.

Runs inside the api-worker container and sends a one-shot command to the
localhost-only MT5 control socket started by scripts/mt5_ingest_server.py.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone


def _parse_time(raw: str, *, name: str) -> int:
    s = str(raw or "").strip()
    if not s:
        raise ValueError(f"missing {name}")
    if s.isdigit():
        return int(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Request MT5 history fetch over internal control socket."
    )
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g. XAUUSD)")
    parser.add_argument("--timeframe", default="M1", choices=["M1", "D1", "W1", "MN1"])
    parser.add_argument("--from-ts", required=True, help="Unix seconds or ISO8601 UTC")
    parser.add_argument("--to-ts", required=True, help="Unix seconds or ISO8601 UTC")
    parser.add_argument("--max-bars", type=int, default=999999)
    parser.add_argument("--chunk-bars", type=int, default=1000)
    parser.add_argument("--upsert", dest="upsert", action="store_true", default=True)
    parser.add_argument("--no-upsert", dest="upsert", action="store_false")
    parser.add_argument("--job-id", type=int, default=0, help="Optional explicit job id")
    parser.add_argument(
        "--host",
        default=os.getenv("MT5_CONTROL_HOST", "127.0.0.1"),
        help="Control host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MT5_CONTROL_PORT", "9002")),
        help="Control port (default: 9002)",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    from_ts = _parse_time(args.from_ts, name="from-ts")
    to_ts = _parse_time(args.to_ts, name="to-ts")
    if from_ts >= to_ts:
        raise SystemExit("from-ts must be less than to-ts")

    payload = {
        "action": "history_fetch",
        "symbol": str(args.symbol).upper(),
        "timeframe": str(args.timeframe).upper(),
        "from_ts": int(from_ts),
        "to_ts": int(to_ts),
        "max_bars": int(max(1, args.max_bars)),
        "chunk_bars": int(max(1, args.chunk_bars)),
        "upsert": bool(args.upsert),
    }
    if int(args.job_id) > 0:
        payload["job_id"] = int(args.job_id)

    msg = (json.dumps(payload) + "\n").encode("utf-8")
    with socket.create_connection((args.host, int(args.port)), timeout=args.timeout) as sock:
        sock.settimeout(args.timeout)
        sock.sendall(msg)
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk

    if not raw:
        raise SystemExit("no response from control server")
    resp = json.loads(raw.decode("utf-8", errors="ignore").strip())
    print(json.dumps(resp, indent=2, sort_keys=True))
    return 0 if resp.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())

