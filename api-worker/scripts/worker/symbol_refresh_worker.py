import asyncio
import os
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
APP_ROOT = os.path.abspath(os.path.join(SCRIPTS_ROOT, ".."))
sys.path.insert(0, APP_ROOT)

from app.cache import redis_client
from app.db import POSTGRES_DSN
from trading_common.symbols import refresh_active_symbols_sync

POLL_INTERVAL = int(os.getenv("SYMBOLS_REFRESH_INTERVAL_SECONDS", "300"))

async def run_monitor():
    print(f"Starting Symbol Refresh Worker (Interval: {POLL_INTERVAL}s)...")
    while True:
        try:
            # We must use asyncio.to_thread because refresh_active_symbols_sync is synchronous 
            # and postgres_dsn connection inside it uses psycopg synchronous driver
            syms = await asyncio.to_thread(refresh_active_symbols_sync, redis_client, POSTGRES_DSN)
            if syms:
                print(f"[{datetime.now(timezone.utc).isoformat()}] Refreshed {len(syms)} symbols in Redis")
            else:
                print(f"[{datetime.now(timezone.utc).isoformat()}] Warning: No active symbols found")
        except Exception as e:
            print(f"Symbol refresh loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run_monitor())
