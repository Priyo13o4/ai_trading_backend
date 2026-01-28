"""Postgres LISTEN/NOTIFY based symbol hot-add.

Requirements:
- Trigger emits NOTIFY when a symbol appears for the first time.
- Backend listens, refreshes Redis symbols cache, broadcasts SUBSCRIBE to connected EA sessions.
- No polling and no HTTP from triggers.

Multi-worker note:
- Gunicorn may run multiple workers; only one should actively LISTEN.
- We use a Postgres advisory lock to ensure a single active listener.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

import psycopg

from .cache import redis_client
from .db import POSTGRES_DSN
from .mt5_ingest import mt5_ingest_server
from trading_common.symbols import refresh_active_symbols

logger = logging.getLogger(__name__)


LISTEN_CHANNEL = os.getenv("MT5_SYMBOL_NOTIFY_CHANNEL", "symbol_discovery")

# Stable 64-bit key for advisory locking (changeable via env).
_ADVISORY_LOCK_KEY = int(os.getenv("MT5_SYMBOL_NOTIFY_LOCK_KEY", "9153372143361"))


class _NotifyThread:
    def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mt5-symbol-notify", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
                        got = cur.fetchone()[0]
                    if not got:
                        logger.info("[MT5] Symbol NOTIFY listener skipped (lock held by another worker)")
                        return
                except Exception as e:
                    logger.warning("[MT5] Symbol NOTIFY lock acquisition failed; err=%s", e)
                    return

                try:
                    conn.execute(f"LISTEN {LISTEN_CHANNEL}")
                    logger.info("[MT5] Listening for NOTIFY on channel=%s", LISTEN_CHANNEL)
                except Exception as e:
                    logger.warning("[MT5] LISTEN failed channel=%s err=%s", LISTEN_CHANNEL, e)
                    return

                # Block forever, pushing payloads into the async queue.
                for n in conn.notifies():
                    try:
                        payload = (n.payload or "").strip().upper()
                        if not payload:
                            continue
                        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("[MT5] Symbol NOTIFY thread crashed; err=%s", e)


_listener_started = False


async def start_symbol_notify_listener() -> None:
    """Start the LISTEN loop once per process.

    Safe to call multiple times.
    """
    global _listener_started
    if _listener_started:
        return
    _listener_started = True

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    # Start blocking LISTEN in a background thread.
    t = _NotifyThread(queue=queue, loop=loop)
    t.start()

    # Async consumer.
    async def _consumer() -> None:
        while True:
            sym = await queue.get()
            try:
                # Refresh Redis cache from DB (single source of truth).
                await refresh_active_symbols(redis_client=redis_client, postgres_dsn=POSTGRES_DSN)

                # Broadcast subscribe to all connected bridge sessions.
                await mt5_ingest_server.broadcast_subscribe([sym])
                logger.info("[MT5] Hot-add symbol=%s broadcasted", sym)
            except Exception as e:
                logger.warning("[MT5] Hot-add handling failed sym=%s err=%s", sym, e)

    asyncio.create_task(_consumer())
