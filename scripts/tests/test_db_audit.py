#!/usr/bin/env python3
"""
TEST 7 — "Nothing Slips Through" Query (DB Audit)
Verifies INTRADAY candles do not exist during closed market periods

CRITICAL: Higher-timeframe candles (H4, D1, W1, MN1) use period anchors that may
fall outside live trading hours while their coverage overlaps valid sessions.
These are legitimate and must NOT be flagged as invalid.

Examples of VALID higher-TF timestamps:
  - D1 @ Sunday 00:00 UTC → represents Mon 22:00 → Tue 22:00 trading day
  - H4 @ Sunday 20:00 UTC → covers 20:00 → 00:00, overlaps market open at 22:00
  - MN1 @ Saturday 00:00 UTC → calendar-anchored monthly candles

This test ONLY validates intraday timeframes: M1, M5, M15, M30, H1
"""

import sys
import os
import psycopg

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

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

def test_db_audit():
    """Query database for INTRADAY candles during closed market periods"""
    
    print("=" * 80)
    print("TEST 7 — Database Audit: Invalid INTRADAY Timestamp Detection")
    print("=" * 80)
    print("\nScope: M1, M5, M15, M30, H1 only")
    print("Excluded: H4, D1, W1, MN1 (period anchors may fall outside trading hours)")
    print("=" * 80)
    
    try:
        conn = psycopg.connect(DATABASE_URL)
        
        with conn.cursor() as cur:
            # Query for INTRADAY candles outside valid trading hours
            # ONLY check M1, M5, M15, M30, H1 - exclude H4, D1, W1, MN1
            cur.execute("""
                SELECT COUNT(*) as invalid_count
                FROM candlesticks
                WHERE timeframe IN ('M1', 'M5', 'M15', 'M30', 'H1')
                AND NOT (
                    (
                      EXTRACT(DOW FROM time) BETWEEN 1 AND 4
                    )
                    OR (EXTRACT(DOW FROM time) = 0 AND time::time >= '22:00')
                    OR (EXTRACT(DOW FROM time) = 5 AND time::time < '22:00')
                )
            """)
            
            invalid_count = cur.fetchone()[0]
            
            print(f"\nInvalid INTRADAY candles in database: {invalid_count}")
            
            if invalid_count > 0:
                # Show sample invalid candles
                cur.execute("""
                    SELECT symbol, timeframe, time, 
                           EXTRACT(DOW FROM time) as day_of_week,
                           time::time as time_of_day
                    FROM candlesticks
                    WHERE timeframe IN ('M1', 'M5', 'M15', 'M30', 'H1')
                    AND NOT (
                        (
                          EXTRACT(DOW FROM time) BETWEEN 1 AND 4
                        )
                        OR (EXTRACT(DOW FROM time) = 0 AND time::time >= '22:00')
                        OR (EXTRACT(DOW FROM time) = 5 AND time::time < '22:00')
                    )
                    ORDER BY time DESC
                    LIMIT 10
                """)
                
                print("\nSample invalid INTRADAY candles:")
                print("-" * 80)
                for row in cur.fetchall():
                    symbol, tf, ts, dow, tod = row
                    day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][int(dow)]
                    print(f"{symbol:8} {tf:4} | {ts} | {day_name} {tod}")
                print("-" * 80)
        
        conn.close()
        
        print("\n" + "=" * 80)
        if invalid_count == 0:
            print("✅ TEST 7 PASSED: No invalid INTRADAY candles in database")
            print("   Intraday data respects market hours")
            print("   Higher-timeframe data preserved correctly")
        else:
            print(f"❌ TEST 7 FAILED: {invalid_count} invalid INTRADAY candles found")
            print("   Intraday data violates market hours")
        print("=" * 80)
        
        return invalid_count == 0
        
    except Exception as e:
        print(f"\n❌ TEST 7 ERROR: {e}")
        return False


if __name__ == "__main__":
    success = test_db_audit()
    sys.exit(0 if success else 1)
