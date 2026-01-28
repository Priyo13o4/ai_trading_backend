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
import logging
import os
import sys
import time

# Add parent directory to path for imports
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
sys.path.insert(0, parent_dir)

from app.mt5_ingest import mt5_ingest_server
from app.mt5_symbol_notify import start_symbol_notify_listener

# Configure logging with UTC timestamps
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


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
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
