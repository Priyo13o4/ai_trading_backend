"""Market Data Module.

Exports symbol/timeframe metadata dynamically from database.
"""

import logging
import os
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# Database connection for dynamic symbol discovery
_db_name = os.getenv('TRADING_BOT_DB') or os.getenv('POSTGRES_DB')
POSTGRES_DSN = f"host={os.getenv('POSTGRES_HOST')} port={os.getenv('POSTGRES_PORT')} dbname={_db_name} user={os.getenv('POSTGRES_USER')} password={os.getenv('POSTGRES_PASSWORD')}"

def _fetch_active_symbols():
    """Fetch active trading symbols from database"""
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT symbol FROM candlesticks ORDER BY symbol")
                symbols = [row["symbol"] for row in cur.fetchall()]
                if symbols:
                    logger.info(f"Loaded {len(symbols)} symbols from database: {symbols}")
                    return symbols
                else:
                    logger.warning("No symbols found in database, using fallback")
                    return ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
    except Exception as e:
        logger.error(f"Failed to fetch symbols from database: {e}")
        return ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]

# Active trading symbols (dynamically loaded from database)
SYMBOLS = _fetch_active_symbols()

# Symbol metadata for frontend
SYMBOL_INFO = {
    "XAUUSD": {"name": "Gold", "type": "commodity", "precision": 2},
    "EURUSD": {"name": "EUR/USD", "type": "forex", "precision": 5},
    "GBPUSD": {"name": "GBP/USD", "type": "forex", "precision": 5},
    "USDJPY": {"name": "USD/JPY", "type": "forex", "precision": 3},
    "AUDUSD": {"name": "AUD/USD", "type": "forex", "precision": 5},
}

# Timeframe mapping: MT5 format (for reference)
TIMEFRAME_MAP = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
    "W1": "1week",
}
