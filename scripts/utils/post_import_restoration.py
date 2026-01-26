#!/usr/bin/env python3
"""
Post-Import Restoration Script
-------------------------------
Restores database optimizations and recalculates dependent data after import:
1. Re-enables TimescaleDB compression policy on candlesticks
2. Refreshes materialized views (daily_candlesticks)
3. Recalculates technical indicators for all symbols/timeframes
4. Updates data_metadata table
5. Verifies data integrity

Usage: python scripts/post_import_restoration.py
"""

import psycopg
import logging
import os
from datetime import datetime
import subprocess
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
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


def main():
    """Execute post-import restoration operations."""
    
    logger.info("=" * 80)
    logger.info("POST-IMPORT RESTORATION STARTED")
    logger.info("=" * 80)
    
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            
            # Step 1: Re-enable compression policy
            logger.info("\n[STEP 1/6] Re-enabling TimescaleDB compression policy...")
            with conn.cursor() as cur:
                # Add compression policy (compress data older than 7 days)
                cur.execute("""
                    SELECT add_compression_policy('candlesticks', 
                        INTERVAL '7 days', 
                        if_not_exists => TRUE
                    )
                """)
                conn.commit()
                logger.info("✓ Compression policy re-enabled (7-day threshold)")
                
                # Optionally compress existing chunks immediately
                logger.info("  Checking for chunks to compress...")
                cur.execute("""
                    SELECT COUNT(*)
                    FROM timescaledb_information.chunks
                    WHERE hypertable_name = 'candlesticks'
                        AND range_end < NOW() - INTERVAL '7 days'
                        AND NOT is_compressed
                """)
                uncompressed = cur.fetchone()[0]
                
                if uncompressed > 0:
                    logger.info(f"  Found {uncompressed} chunks eligible for compression")
                    logger.info("  Starting manual compression (this may take 10-30 minutes)...")
                    
                    # Compress chunks older than 7 days
                    cur.execute("""
                        SELECT compress_chunk(i)
                        FROM show_chunks('candlesticks', older_than => INTERVAL '7 days') i
                    """)
                    conn.commit()
                    logger.info(f"✓ Compressed {uncompressed} chunks")
                else:
                    logger.info("  No chunks need compression yet (all data is recent)")
            
            # Step 2: Refresh materialized views
            logger.info("\n[STEP 2/6] Refreshing materialized views...")
            with conn.cursor() as cur:
                try:
                    logger.info("  Refreshing daily_candlesticks (from M5 data)...")
                    cur.execute("REFRESH MATERIALIZED VIEW daily_candlesticks")
                    conn.commit()
                    logger.info("✓ daily_candlesticks refreshed")
                except Exception as e:
                    logger.warning(f"Could not refresh daily_candlesticks: {e}")
                    logger.info("  Skipping materialized view refresh (may not exist)")
                    conn.rollback()
            
            # Step 3: Update data_metadata table
            logger.info("\n[STEP 3/6] Updating data_metadata table...")
            with conn.cursor() as cur:
                # Clear old metadata
                cur.execute("TRUNCATE TABLE data_metadata")
                
                # Populate with fresh data
                cur.execute("""
                    INSERT INTO data_metadata (
                        symbol, timeframe, earliest_timestamp, latest_timestamp,
                        total_bars, expected_bars, data_completeness, last_updated
                    )
                    SELECT
                        symbol,
                        timeframe,
                        MIN(time),
                        MAX(time),
                        COUNT(*),
                        0,
                        calculate_data_completeness(symbol, timeframe),
                        NOW()
                    FROM candlesticks
                    GROUP BY symbol, timeframe
                """)
                conn.commit()
                
                rows_updated = cur.rowcount
                logger.info(f"✓ Updated metadata for {rows_updated} symbol/timeframe combinations")
            
            # Step 4: Verify data integrity
            logger.info("\n[STEP 4/6] Verifying data integrity...")
            with conn.cursor() as cur:
                # Check total row count
                cur.execute("SELECT COUNT(*) FROM candlesticks")
                total_rows = cur.fetchone()[0]
                logger.info(f"  Total candlesticks: {total_rows:,} rows")
                
                # Check data by symbol and timeframe
                cur.execute("""
                    SELECT 
                        symbol, 
                        timeframe, 
                        COUNT(*) AS rows,
                        MIN(time) AS earliest,
                        MAX(time) AS latest
                    FROM candlesticks
                    GROUP BY symbol, timeframe
                    ORDER BY symbol, 
                        CASE timeframe
                            WHEN 'M1' THEN 1 WHEN 'M5' THEN 2 WHEN 'M15' THEN 3
                            WHEN 'M30' THEN 4 WHEN 'H1' THEN 5 WHEN 'H4' THEN 6
                            WHEN 'D1' THEN 7 WHEN 'W1' THEN 8 WHEN 'MN1' THEN 9
                        END
                """)
                results = cur.fetchall()
                
                logger.info("\n  Data Distribution:")
                logger.info("  " + "-" * 76)
                logger.info(f"  {'Symbol':<8} {'TF':<5} {'Rows':>12} {'Earliest':<20} {'Latest':<20}")
                logger.info("  " + "-" * 76)
                
                symbols = set()
                timeframes = set()
                for row in results:
                    symbol, tf, count, earliest, latest = row
                    symbols.add(symbol)
                    timeframes.add(tf)
                    logger.info(f"  {symbol:<8} {tf:<5} {count:>12,} {str(earliest):<20} {str(latest):<20}")
                
                logger.info("  " + "-" * 76)
                logger.info(f"  Symbols: {len(symbols)} ({', '.join(sorted(symbols))})")
                logger.info(f"  Timeframes: {len(timeframes)} ({', '.join(sorted(timeframes))})")
                logger.info(f"  Total: {total_rows:,} rows")
            
            # Step 5: Recalculate technical indicators
            logger.info("\n[STEP 5/6] Recalculating technical indicators...")
            logger.info("  This will run the calculate_recent_indicators_v2.py script (v2.0 - DST-safe)...")
            
            try:
                # Run indicator calculation script
                result = subprocess.run(
                    ['python', '/app/scripts/calculate_recent_indicators_v2.py'],
                    capture_output=True,
                    text=True,
                    timeout=600  # 10 minute timeout
                )
                
                if result.returncode == 0:
                    logger.info("✓ Technical indicators recalculated successfully")
                    # Log last few lines of output
                    output_lines = result.stdout.strip().split('\n')
                    for line in output_lines[-10:]:
                        logger.info(f"    {line}")
                else:
                    logger.warning(f"⚠ Indicator calculation had issues (exit code {result.returncode})")
                    logger.warning("  This is non-critical - indicators can be recalculated later")
                    if result.stderr:
                        logger.warning(f"  Error: {result.stderr[:500]}")
                        
            except subprocess.TimeoutExpired:
                logger.warning("⚠ Indicator calculation timed out after 10 minutes")
                logger.warning("  This is non-critical - indicators can be recalculated later")
            except FileNotFoundError:
                logger.warning("⚠ calculate_recent_indicators_v2.py not found")
                logger.warning("  Skipping indicator calculation - run manually if needed")
            
            # Step 6: Final verification
            logger.info("\n[STEP 6/6] Final verification...")
            with conn.cursor() as cur:
                # Check for any obvious data quality issues
                cur.execute("""
                    SELECT symbol, timeframe, COUNT(*) 
                    FROM candlesticks 
                    WHERE open = 0 OR high = 0 OR low = 0 OR close = 0
                    GROUP BY symbol, timeframe
                """)
                zero_price_issues = cur.fetchall()
                
                if zero_price_issues:
                    logger.warning("⚠ Found candles with zero prices:")
                    for symbol, tf, count in zero_price_issues:
                        logger.warning(f"    {symbol} {tf}: {count} candles")
                else:
                    logger.info("✓ No zero-price candles found")
                
                # Check for duplicate timestamps
                cur.execute("""
                    SELECT symbol, timeframe, time, COUNT(*) 
                    FROM candlesticks 
                    GROUP BY symbol, timeframe, time 
                    HAVING COUNT(*) > 1
                    LIMIT 10
                """)
                duplicates = cur.fetchall()
                
                if duplicates:
                    logger.warning("⚠ Found duplicate timestamps:")
                    for symbol, tf, time, count in duplicates:
                        logger.warning(f"    {symbol} {tf} {time}: {count} duplicates")
                else:
                    logger.info("✓ No duplicate timestamps found")
        
        logger.info("\n" + "=" * 80)
        logger.info("POST-IMPORT RESTORATION COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info("\nDatabase is now fully optimized and ready for use.")
        logger.info("\nRecommended next steps:")
        logger.info("1. Run sample queries to verify data quality")
        logger.info("2. Start the realtime_updater.py to resume live data collection")
        logger.info("3. Monitor compression progress over next 24 hours")
        
    except Exception as e:
        logger.error(f"\n❌ ERROR during restoration: {e}")
        logger.error("Some restoration steps may have failed. Check logs above.")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
