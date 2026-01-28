"""Market data constants.

Symbol metadata and timeframe mappings.
"""

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
