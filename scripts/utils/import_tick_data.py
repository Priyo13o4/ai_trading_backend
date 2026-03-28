#!/usr/bin/env python3
"""
Tick Data Importer and Aggregator
----------------------------------
Imports raw tick data and aggregates into OHLC candlesticks at multiple timeframes.

Features:
- Processes massive tick CSV files efficiently (streaming chunks)
- Aggregates ticks to: M1, M5, M15, M30, H1, H4, D1, W1
- Direct database insertion (TimescaleDB)
- Uses UTC timestamps (no DST conversion needed)
- Handles natural market gaps (weekends, holidays)
- Memory-efficient: processes in chunks

CSV Format (from Exness):
"Exness","Symbol","Timestamp","Bid","Ask"
"exness","XAUUSD_Zero_Spread","2021-01-03 23:05:00.949Z",1909.448,1909.468

Usage:
    python scripts/utils/import_tick_data.py /path/to/tick_data_folder
    
    # Or for specific files:
    python scripts/utils/import_tick_data.py /path/to/Exness_XAUUSD_*.csv
"""

import psycopg
import logging
import csv
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from multiprocessing import Pool, Manager
import functools

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('tick_import.log')
    ]
)
logger = logging.getLogger(__name__)

# Database connection
DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("TRADING_BOT_DB") or os.getenv("POSTGRES_DB", "ai_trading_bot_data")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not user or password is None or password == "":
        raise RuntimeError("Set DATABASE_URL or POSTGRES_USER/POSTGRES_PASSWORD")
    DATABASE_URL = f"postgresql://{user}:{password}@{host}:{port}/{db}"

# Timeframe configurations (in minutes)
TIMEFRAMES = {
    'M1': 1,
    'M5': 5,
    'M15': 15,
    'M30': 30,
    'H1': 60,
    'H4': 240,
    'D1': 1440,
    'W1': 10080,  # 7 days
}

# Batch size for database inserts (optimized for speed)
BATCH_SIZE = 200000

# Chunk size for reading CSV (number of ticks) - large for maximum speed
CHUNK_SIZE = 2000000


class CandleBuilder:
    """Builds OHLC candles from tick data for a specific timeframe."""
    
    def __init__(self, timeframe_minutes):
        self.timeframe_minutes = timeframe_minutes
        self.current_candle = None
        self.candle_start_time = None
        self.finished_candles = []
    
    def get_candle_start_time(self, tick_time):
        """Calculate the candle start time for a given tick timestamp."""
        if self.timeframe_minutes == TIMEFRAMES['W1']:
            # Weekly: Start of week (Monday 00:00)
            days_from_monday = tick_time.weekday()
            week_start = tick_time.date() - timedelta(days=days_from_monday)
            return datetime.combine(week_start, datetime.min.time())
        
        elif self.timeframe_minutes == TIMEFRAMES['D1']:
            # Daily: Start of day (00:00)
            return tick_time.replace(hour=0, minute=0, second=0, microsecond=0)
        
        elif self.timeframe_minutes >= 60:
            # Hourly timeframes
            if self.timeframe_minutes == TIMEFRAMES['H4']:
                hour = (tick_time.hour // 4) * 4
            else:  # H1
                hour = tick_time.hour
            return tick_time.replace(hour=hour, minute=0, second=0, microsecond=0)
        
        else:
            # Minute timeframes (M1, M5, M15, M30)
            minute = (tick_time.minute // self.timeframe_minutes) * self.timeframe_minutes
            return tick_time.replace(minute=minute, second=0, microsecond=0)
    
    def process_tick(self, tick_time, price):
        """Process a single tick and update/create candles."""
        candle_time = self.get_candle_start_time(tick_time)
        
        if self.candle_start_time is None:
            # First tick - create first candle
            self.candle_start_time = candle_time
            self.current_candle = {
                'time': candle_time,
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 1
            }
        
        elif candle_time != self.candle_start_time:
            # New candle period - save current and start new
            self.finished_candles.append(self.current_candle)
            
            self.candle_start_time = candle_time
            self.current_candle = {
                'time': candle_time,
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 1
            }
        
        else:
            # Same candle - update OHLC
            self.current_candle['high'] = max(self.current_candle['high'], price)
            self.current_candle['low'] = min(self.current_candle['low'], price)
            self.current_candle['close'] = price
            self.current_candle['volume'] += 1
    
    def get_finished_candles(self):
        """Get and clear finished candles."""
        candles = self.finished_candles
        self.finished_candles = []
        return candles
    
    def finalize(self):
        """Get final candle (including current incomplete one)."""
        if self.current_candle:
            self.finished_candles.append(self.current_candle)
        return self.get_finished_candles()


class TickDataImporter:
    """Main importer that processes tick CSVs and writes to database."""
    
    def __init__(self, conn, symbol):
        self.conn = conn
        self.symbol = symbol
        self.builders = {
            tf_name: CandleBuilder(tf_minutes)
            for tf_name, tf_minutes in TIMEFRAMES.items()
        }
        self.stats = defaultdict(int)
        self.total_ticks = 0
    
    def parse_tick_row(self, row):
        """Parse tick CSV row.
        
        Expected format:
        ["exness", "XAUUSD_Zero_Spread", "2021-01-03 23:05:00.949Z", "1909.448", "1909.468"]
        
        Returns:
            tuple: (timestamp, mid_price) or None if invalid
        """
        try:
            # Columns: Exness, Symbol, Timestamp, Bid, Ask
            timestamp_str = row[2].strip()
            bid = float(row[3])
            ask = float(row[4])
            
            # Parse UTC timestamp
            # Format: "2021-01-03 23:05:00.949Z"
            if timestamp_str.endswith('Z'):
                timestamp_str = timestamp_str[:-1]  # Remove 'Z'
            
            # Handle milliseconds
            if '.' in timestamp_str:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S.%f")
            else:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            
            # Calculate mid price
            mid_price = (bid + ask) / 2.0
            
            return timestamp, mid_price
        
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse tick row: {e}")
            return None
    
    def process_tick_chunk(self, ticks):
        """Process a chunk of ticks through all builders."""
        for tick_time, price in ticks:
            self.total_ticks += 1
            
            # Feed tick to all timeframe builders
            for builder in self.builders.values():
                builder.process_tick(tick_time, price)
        
        # Flush finished candles to database
        self.flush_candles()
    
    def flush_candles(self):
        """Write finished candles from all builders to database."""
        for tf_name, builder in self.builders.items():
            candles = builder.get_finished_candles()
            if candles:
                inserted = self.batch_insert(tf_name, candles)
                self.stats[tf_name] += inserted
    
    def batch_insert(self, timeframe, candles):
        """Insert candles in batches.
        
        Args:
            timeframe: Timeframe name
            candles: List of candle dicts
            
        Returns:
            int: Number of rows inserted
        """
        if not candles:
            return 0
        
        total_inserted = 0
        
        for i in range(0, len(candles), BATCH_SIZE):
            batch = candles[i:i + BATCH_SIZE]
            
            values = [
                (
                    c['time'],
                    self.symbol,
                    timeframe,
                    c['open'],
                    c['high'],
                    c['low'],
                    c['close'],
                    c['volume']
                )
                for c in batch
            ]
            
            with self.conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO candlesticks (time, symbol, timeframe, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, timeframe, time) DO NOTHING
                    """,
                    values
                )
                total_inserted += cur.rowcount
            
            self.conn.commit()
        
        return total_inserted
    
    def finalize(self):
        """Finalize all builders and flush remaining candles."""
        logger.info("Finalizing all timeframes...")
        
        for tf_name, builder in self.builders.items():
            candles = builder.finalize()
            if candles:
                inserted = self.batch_insert(tf_name, candles)
                self.stats[tf_name] += inserted
        
        logger.info(f"\n{'=' * 80}")
        logger.info(f"{self.symbol} Import Summary")
        logger.info(f"{'=' * 80}")
        logger.info(f"Total ticks processed: {self.total_ticks:,}")
        logger.info(f"\nCandles created per timeframe:")
        logger.info("-" * 80)
        
        total_candles = 0
        for tf in TIMEFRAMES.keys():
            count = self.stats.get(tf, 0)
            total_candles += count
            logger.info(f"  {tf:6} : {count:,} candles")
        
        logger.info("-" * 80)
        logger.info(f"  Total  : {total_candles:,} candles")
        logger.info(f"{'=' * 80}\n")


def process_tick_file(csv_file):
    """Process a single tick CSV file.
    
    Args:
        csv_file: Path to tick CSV file
    """
    # Extract symbol from filename
    # Expected format: "Exness_XAUUSD_Zero_Spread_2021.csv" -> "XAUUSD"
    filename = csv_file.stem
    
    # Parse symbol from filename
    parts = filename.split('_')
    if len(parts) >= 2:
        symbol = parts[1]  # e.g., "XAUUSD"
    else:
        symbol = filename
    
    logger.info(f"\n{'=' * 80}")
    logger.info(f"Processing: {csv_file.name}")
    logger.info(f"Symbol: {symbol}")
    logger.info(f"{'=' * 80}\n")
    
    # Connect to database for this process
    conn = psycopg.connect(DATABASE_URL)
    importer = TickDataImporter(conn, symbol)
    
    # Read and process CSV in chunks
    tick_chunk = []
    row_count = 0
    
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        
        # Skip header
        next(reader, None)
        
        for row in reader:
            row_count += 1
            
            tick = importer.parse_tick_row(row)
            if tick:
                tick_chunk.append(tick)
            
            # Process chunk when full
            if len(tick_chunk) >= CHUNK_SIZE:
                logger.info(f"Processing ticks {row_count - len(tick_chunk):,} to {row_count:,}...")
                importer.process_tick_chunk(tick_chunk)
                tick_chunk = []
                logger.info(f"  ✓ {importer.total_ticks:,} ticks processed so far")
        
        # Process final chunk
        if tick_chunk:
            logger.info(f"Processing final {len(tick_chunk):,} ticks...")
            importer.process_tick_chunk(tick_chunk)
    
    # Finalize (write last incomplete candles)
    importer.finalize()
    conn.close()


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        logger.error("Usage: python import_tick_data.py <path_to_tick_files>")
        logger.error("Examples:")
        logger.error("  python import_tick_data.py /path/to/Tick_Data/")
        logger.error("  python import_tick_data.py /path/to/Exness_XAUUSD_*.csv")
        sys.exit(1)
    
    path_arg = sys.argv[1]
    path = Path(path_arg).expanduser()
    
    # Collect CSV files
    csv_files = []
    
    if path.is_dir():
        # Process all CSV files in directory
        csv_files = sorted(path.glob("*.csv"))
    elif path.exists():
        # Single file
        csv_files = [path]
    else:
        # Glob pattern
        csv_files = sorted(Path(path.parent).glob(path.name))
    
    if not csv_files:
        logger.error(f"No CSV files found at: {path}")
        sys.exit(1)
    
    logger.info(f"Found {len(csv_files)} tick file(s) to process")
    for f in csv_files:
        logger.info(f"  - {f.name}")
    
    # Test database connection
    logger.info(f"\nTesting database connection...")
    
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            logger.info("✓ Database connection OK")
    except Exception as e:
        logger.error(f"\n❌ Database connection failed: {e}", exc_info=True)
        sys.exit(1)
    
    # Process files in parallel
    num_processes = min(4, len(csv_files))  # Use up to 4 processes
    logger.info(f"\nProcessing {len(csv_files)} files with {num_processes} parallel workers...\n")
    
    try:
        with Pool(processes=num_processes) as pool:
            pool.map(process_tick_file, csv_files)
        
        logger.info("\n" + "=" * 80)
        logger.info("ALL TICK FILES PROCESSED SUCCESSFULLY")
        logger.info("=" * 80)
    
    except Exception as e:
        logger.error(f"\n❌ Error during import: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
