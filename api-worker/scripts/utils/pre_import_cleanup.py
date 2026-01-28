#!/usr/bin/env python3
"""
Pre-Import Cleanup Script
--------------------------
Prepares database for clean historical data reimport:
1. Backs up current candlestick count
2. Disables TimescaleDB compression on candlesticks table
3. Truncates candlesticks, technical_indicators, market_structure tables
4. Clears related cache/metadata

**IMPORTANT NOTE (v2.0):**
- After cleanup, D1/W1/MN1 candles must be fetched from MT5 broker (not aggregated)
- Tick import script will only create M1-H4 timeframes
- Connect MT5 EA after import to backfill D1/W1/MN1 historical data

Usage: python scripts/utils/pre_import_cleanup.py
"""

import psycopg
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Database connection
import os

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


def main():
    """Execute pre-import cleanup operations."""
    
    logger.info("=" * 80)
    logger.info("PRE-IMPORT CLEANUP STARTED")
    logger.info("=" * 80)
    
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                
                # Step 1: Backup current data counts
                logger.info("\n[STEP 1/5] Recording current data counts...")
                cur.execute("""
                    SELECT 
                        symbol, 
                        timeframe, 
                        COUNT(*) AS rows,
                        MIN(time) AS earliest,
                        MAX(time) AS latest
                    FROM candlesticks
                    GROUP BY symbol, timeframe
                    ORDER BY symbol, timeframe
                """)
                results = cur.fetchall()
                
                total_rows = 0
                logger.info("\nCurrent Database State:")
                logger.info("-" * 80)
                for row in results:
                    symbol, timeframe, count, earliest, latest = row
                    total_rows += count
                    logger.info(f"{symbol:8} {timeframe:4} | {count:,} rows | {earliest} to {latest}")
                logger.info("-" * 80)
                logger.info(f"TOTAL ROWS TO BE DELETED: {total_rows:,}")
                
                # Step 2: Check compression status
                logger.info("\n[STEP 2/5] Checking TimescaleDB compression status...")
                try:
                    cur.execute("""
                        SELECT COUNT(*) 
                        FROM timescaledb_information.compressed_chunk_stats
                        WHERE hypertable_name = 'candlesticks'
                    """)
                    compressed_chunks = cur.fetchone()[0]
                    logger.info(f"Found {compressed_chunks} compressed chunks for candlesticks table")
                except Exception as e:
                    logger.warning(f"Could not query compression stats (older TimescaleDB version): {e}")
                    compressed_chunks = 0
                
                # Step 3: Decompress all chunks (required before truncate)
                if compressed_chunks > 0:
                    logger.info("\n[STEP 3/5] Decompressing all candlesticks chunks...")
                    logger.info("This may take several minutes for large datasets...")
                    
                    cur.execute("""
                        SELECT decompress_chunk(i, if_compressed => true)
                        FROM show_chunks('candlesticks') i
                    """)
                    decompressed = cur.rowcount
                    conn.commit()
                    logger.info(f"✓ Decompressed {decompressed} chunks")
                else:
                    logger.info("\n[STEP 3/5] No compression found, skipping decompression")
                
                # Step 4: Remove compression policy (will be re-added after import)
                logger.info("\n[STEP 4/5] Removing compression policy...")
                try:
                    cur.execute("""
                        SELECT remove_compression_policy('candlesticks', if_exists => true)
                    """)
                    conn.commit()
                    logger.info("✓ Compression policy removed")
                except Exception as e:
                    logger.warning(f"No compression policy to remove (this is OK): {e}")
                    conn.rollback()  # Rollback failed transaction to continue
                
                # Step 5: Truncate tables
                logger.info("\n[STEP 5/5] Truncating tables...")
                
                # Truncate dependent tables first
                logger.info("Truncating technical_indicators...")
                cur.execute("TRUNCATE TABLE technical_indicators")
                conn.commit()
                logger.info("✓ technical_indicators truncated")
                
                logger.info("Truncating market_structure...")
                cur.execute("TRUNCATE TABLE market_structure")
                conn.commit()
                logger.info("✓ market_structure truncated")
                
                # Truncate main candlesticks table
                logger.info("Truncating candlesticks (this may take a minute)...")
                cur.execute("TRUNCATE TABLE candlesticks")
                conn.commit()
                logger.info("✓ candlesticks truncated")
                
                # Verify truncation
                cur.execute("SELECT COUNT(*) FROM candlesticks")
                remaining = cur.fetchone()[0]
                if remaining == 0:
                    logger.info(f"✓ Verification: candlesticks table is empty")
                else:
                    logger.error(f"⚠ WARNING: {remaining} rows still remain!")
                
        logger.info("\n" + "=" * 80)
        logger.info("PRE-IMPORT CLEANUP COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info("\nDatabase is now ready for historical data import.")
        logger.info("Compression policy has been removed and will be re-added after import.")
        logger.info(f"\nDeleted {total_rows:,} total rows from candlesticks")
        logger.info("\nNext steps:")
        logger.info("  1. Run tick import script (creates M1-H4 only)")
        logger.info("  2. Connect MT5 EA to backfill D1/W1/MN1 from broker")
        logger.info("  3. Run indicator calculation script")
        logger.info("\n⚠ IMPORTANT: D1/W1/MN1 are sourced from broker only (DST-aware)")
        
    except Exception as e:
        logger.error(f"\n❌ ERROR during cleanup: {e}")
        logger.error("Database may be in inconsistent state. Check manually before proceeding.")
        raise


if __name__ == "__main__":
    main()
