#!/usr/bin/env python3
"""
Standalone MT5 Executor TCP server.

This server handles:
- MT5 EA connections via binary protocol for Trade Execution
- Receiving TRADE_EVENT from MT5 EA
- Broadcasting STRATEGY_PUSH to MT5 EA

Listens on port 9002.
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

from app.mt5_executor import mt5_executor_server
from app.error_alerts import report_runtime_error

# Configure logging with UTC timestamps
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

async def main():
    """Start MT5 executor server."""
    logger.info("=" * 80)
    logger.info("MT5 EXECUTOR SERVER STARTING (TCP ONLY - NO HTTP)")
    logger.info("=" * 80)
    logger.info("Port: 9002 (TCP)")
    logger.info("Protocol: Binary framed (mt5_wire)")
    logger.info("=" * 80)
    
    try:
        await mt5_executor_server.start()
        logger.info("=" * 80)
        logger.info("MT5 EXECUTOR SERVER READY")
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
        logger.info("MT5 executor server stopped by user")
        logger.info("=" * 80)
    except Exception as e:
        report_runtime_error(
            path="/worker/mt5-executor",
            method="PROCESS",
            status_code=500,
            message_safe="Worker runtime error",
            message_internal=f"{e.__class__.__name__}: {e}",
            context={
                "script": "mt5_executor_server.py",
                "phase": "entrypoint",
            },
        )
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
