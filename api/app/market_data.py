"""Market Data Module.

Only exports lightweight Twelve Data client + symbol/timeframe metadata.
Heavy in-memory fetch/compute helpers were removed to avoid accidental API fan-out.
"""

import logging
import os
from twelvedata import TDClient

logger = logging.getLogger(__name__)

# Configuration from environment
API_KEYS = [
    k
    for k in [os.getenv("TWELVE_DATA_API_KEY"), os.getenv("TWELVE_DATA_API_KEY_2")]
    if k
]
TWELVE_DATA_API_KEY = API_KEYS[0] if API_KEYS else ""

# Active trading symbols (REST API only)
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]

# Symbol metadata for frontend
SYMBOL_INFO = {
    "XAUUSD": {"name": "Gold", "type": "commodity", "precision": 2},
    "EURUSD": {"name": "EUR/USD", "type": "forex", "precision": 5},
    "GBPUSD": {"name": "GBP/USD", "type": "forex", "precision": 5},
    "USDJPY": {"name": "USD/JPY", "type": "forex", "precision": 3},
    "AUDUSD": {"name": "AUD/USD", "type": "forex", "precision": 5},
}

# Timeframe mapping: MT5 -> Twelve Data format
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

# Optional Twelve Data client used only by /api/market-data/test
td_client = None
if TWELVE_DATA_API_KEY:
    try:
        td_client = TDClient(apikey=TWELVE_DATA_API_KEY)
        logger.info("Twelve Data official SDK client initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize Twelve Data client: {e}")
        td_client = None
