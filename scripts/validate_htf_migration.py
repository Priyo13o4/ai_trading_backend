#!/usr/bin/env python3
"""
D1/W1/MN1 Migration Validation Script
======================================
Validates that broker-provided HTF candles are correctly stored and aligned.

Checks:
1. No D1/W1/MN1 continuous aggregates exist
2. D1/W1/MN1 candles present in base candlesticks table
3. D1 timestamps align with broker sessions (not UTC midnight)
4. W1 candles start on Sunday 22:00 UTC
5. No duplicate candles
6. Indicator coverage matches candle coverage

Usage:
    python scripts/validate_htf_migration.py
    python scripts/validate_htf_migration.py --symbol XAUUSD
"""

import os
import sys
import argparse
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row

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


def check_caggs_removed(conn):
    """Verify D1/W1/MN1 CAGGs are removed."""
    print("\n" + "="*80)
    print("CHECK 1: Continuous Aggregates")
    print("="*80)
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT view_name 
            FROM timescaledb_information.continuous_aggregates
            WHERE view_name IN ('candlesticks_d1', 'candlesticks_w1', 'candlesticks_mn1')
        """)
        bad_caggs = [row[0] for row in cur.fetchall()]
        
        if bad_caggs:
            print(f"❌ FAIL: Found unexpected CAGGs: {bad_caggs}")
            print("   These should be removed. Run:")
            for cagg in bad_caggs:
                print(f"   DROP MATERIALIZED VIEW {cagg} CASCADE;")
            return False
        else:
            print("✅ PASS: No D1/W1/MN1 continuous aggregates found")
            
        # Check valid CAGGs still exist
        cur.execute("""
            SELECT view_name 
            FROM timescaledb_information.continuous_aggregates
            WHERE view_name IN ('candlesticks_m5', 'candlesticks_m15', 'candlesticks_m30', 
                               'candlesticks_h1', 'candlesticks_h4')
            ORDER BY view_name
        """)
        good_caggs = [row[0] for row in cur.fetchall()]
        
        if good_caggs:
            print(f"✅ Valid CAGGs present: {', '.join(good_caggs)}")
        else:
            print("⚠️  WARNING: No M5-H4 CAGGs found (may not be enabled)")
    
    return True


def check_htf_candles_exist(conn, symbol=None):
    """Verify D1/W1/MN1 candles exist in base table."""
    print("\n" + "="*80)
    print("CHECK 2: Broker-Provided HTF Candles")
    print("="*80)
    
    where_clause = ""
    if symbol:
        where_clause = f"WHERE symbol = '{symbol}'"
    
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT timeframe, COUNT(*) AS rows, 
                   MIN(time) AS earliest, 
                   MAX(time) AS latest
            FROM candlesticks
            {where_clause}
            GROUP BY timeframe
            ORDER BY 
                CASE timeframe
                    WHEN 'M1' THEN 1
                    WHEN 'M5' THEN 2
                    WHEN 'M15' THEN 3
                    WHEN 'M30' THEN 4
                    WHEN 'H1' THEN 5
                    WHEN 'H4' THEN 6
                    WHEN 'D1' THEN 7
                    WHEN 'W1' THEN 8
                    WHEN 'MN1' THEN 9
                END
        """)
        results = cur.fetchall()
        
        found_htf = {'D1': False, 'W1': False, 'MN1': False}
        
        print(f"\nCandle counts{' for ' + symbol if symbol else ''}:")
        print("-" * 80)
        for timeframe, rows, earliest, latest in results:
            print(f"{timeframe:6} | {rows:>8,} rows | {earliest} → {latest}")
            if timeframe in found_htf:
                found_htf[timeframe] = rows > 0
        
        all_found = all(found_htf.values())
        if all_found:
            print("\n✅ PASS: All HTF timeframes (D1/W1/MN1) have candles")
        else:
            missing = [tf for tf, found in found_htf.items() if not found]
            print(f"\n❌ FAIL: Missing HTF candles: {missing}")
            print("   Connect MT5 EA to backfill broker data")
            return False
    
    return True


def check_d1_alignment(conn, symbol=None):
    """Check that D1 candles don't all start at UTC midnight."""
    print("\n" + "="*80)
    print("CHECK 3: D1 Session Alignment")
    print("="*80)
    
    where_clause = ""
    if symbol:
        where_clause = f"AND symbol = '{symbol}'"
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT 
                EXTRACT(HOUR FROM time AT TIME ZONE 'UTC') AS hour,
                COUNT(*) AS count
            FROM candlesticks
            WHERE timeframe = 'D1' {where_clause}
            GROUP BY hour
            ORDER BY hour
        """)
        results = cur.fetchall()
        
        if not results:
            print("⚠️  No D1 candles found for alignment check")
            return True
        
        print("\nD1 candle start hours (UTC):")
        for row in results:
            hour = int(row['hour'])
            count = row['count']
            marker = "✅" if hour in (22, 23) else "❌"
            print(f"  {marker} Hour {hour:02d}:00 → {count:,} candles")
        
        # Check if majority are at 22:00 or 23:00 (broker session start)
        broker_aligned = sum(r['count'] for r in results if int(r['hour']) in (22, 23))
        midnight_aligned = sum(r['count'] for r in results if int(r['hour']) == 0)
        total = sum(r['count'] for r in results)
        
        if broker_aligned > midnight_aligned:
            print(f"\n✅ PASS: {broker_aligned}/{total} D1 candles aligned to broker session start")
        else:
            print(f"\n❌ FAIL: {midnight_aligned}/{total} D1 candles at UTC midnight")
            print("   D1 candles should start at 22:00/23:00 UTC (broker session)")
            print("   Current data appears to be aggregated, not broker-provided")
            return False
    
    return True


def check_w1_alignment(conn, symbol=None):
    """Check that W1 candles start on Sunday."""
    print("\n" + "="*80)
    print("CHECK 4: W1 Week Boundary Alignment")
    print("="*80)
    
    where_clause = ""
    if symbol:
        where_clause = f"AND symbol = '{symbol}'"
    
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            SELECT 
                TO_CHAR(time AT TIME ZONE 'UTC', 'Day') AS day_name,
                EXTRACT(DOW FROM time AT TIME ZONE 'UTC') AS day_of_week,
                COUNT(*) AS count
            FROM candlesticks
            WHERE timeframe = 'W1' {where_clause}
            GROUP BY day_name, day_of_week
            ORDER BY day_of_week
        """)
        results = cur.fetchall()
        
        if not results:
            print("⚠️  No W1 candles found for alignment check")
            return True
        
        print("\nW1 candle start day distribution:")
        for row in results:
            day_name = row['day_name'].strip()
            day_of_week = int(row['day_of_week'])  # 0=Sunday
            count = row['count']
            marker = "✅" if day_of_week == 0 else "❌"
            print(f"  {marker} {day_name} → {count:,} candles")
        
        sunday_count = sum(r['count'] for r in results if int(r['day_of_week']) == 0)
        total = sum(r['count'] for r in results)
        
        if sunday_count == total:
            print(f"\n✅ PASS: All {total} W1 candles start on Sunday (broker week boundary)")
        else:
            print(f"\n❌ FAIL: {total - sunday_count}/{total} W1 candles don't start on Sunday")
            print("   W1 candles should start on Sunday 22:00 UTC")
            return False
    
    return True


def check_duplicates(conn, symbol=None):
    """Check for duplicate candles."""
    print("\n" + "="*80)
    print("CHECK 5: Duplicate Candles")
    print("="*80)
    
    where_clause = ""
    if symbol:
        where_clause = f"WHERE symbol = '{symbol}'"
    
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT symbol, timeframe, time, COUNT(*) AS dups
            FROM candlesticks
            {where_clause}
            GROUP BY symbol, timeframe, time
            HAVING COUNT(*) > 1
            LIMIT 10
        """)
        duplicates = cur.fetchall()
        
        if duplicates:
            print(f"❌ FAIL: Found {len(duplicates)} duplicate candles (showing first 10):")
            for symbol, timeframe, time, dups in duplicates:
                print(f"  {symbol} {timeframe} {time} → {dups} copies")
            return False
        else:
            print("✅ PASS: No duplicate candles found")
    
    return True


def check_indicator_coverage(conn, symbol=None):
    """Check that indicators exist for all candle timeframes."""
    print("\n" + "="*80)
    print("CHECK 6: Indicator Coverage")
    print("="*80)
    
    where_clause = ""
    if symbol:
        where_clause = f"WHERE symbol = '{symbol}'"
    
    with conn.cursor() as cur:
        # Get candle counts
        cur.execute(f"""
            SELECT timeframe, COUNT(*) AS candle_count
            FROM candlesticks
            {where_clause}
            GROUP BY timeframe
        """)
        candles = {row[0]: row[1] for row in cur.fetchall()}
        
        # Get indicator counts
        cur.execute(f"""
            SELECT timeframe, COUNT(*) AS indicator_count
            FROM technical_indicators
            {where_clause}
            GROUP BY timeframe
        """)
        indicators = {row[0]: row[1] for row in cur.fetchall()}
        
        print("\nTimeframe coverage:")
        print("-" * 80)
        all_ok = True
        for tf in ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', 'MN1']:
            candle_count = candles.get(tf, 0)
            indicator_count = indicators.get(tf, 0)
            
            if candle_count == 0:
                continue
            
            # Indicators need warm-up period, so expect slightly fewer
            coverage = (indicator_count / candle_count * 100) if candle_count > 0 else 0
            marker = "✅" if coverage > 80 else "⚠️ " if coverage > 50 else "❌"
            
            print(f"{marker} {tf:6} | Candles: {candle_count:>6,} | Indicators: {indicator_count:>6,} | Coverage: {coverage:>5.1f}%")
            
            if coverage < 80:
                all_ok = False
        
        if all_ok:
            print("\n✅ PASS: Good indicator coverage (>80% for all timeframes)")
        else:
            print("\n⚠️  WARNING: Some timeframes have low indicator coverage")
            print("   Run: python scripts/calculate_recent_indicators.py")
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Validate D1/W1/MN1 migration")
    parser.add_argument("--symbol", help="Check specific symbol only (e.g., XAUUSD)")
    args = parser.parse_args()
    
    print("="*80)
    print("D1/W1/MN1 BROKER-PROVIDED HTF MIGRATION VALIDATION")
    print("="*80)
    
    if args.symbol:
        print(f"\nValidating symbol: {args.symbol}")
    else:
        print("\nValidating all symbols")
    
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            checks = [
                ("Continuous Aggregates Removed", lambda: check_caggs_removed(conn)),
                ("HTF Candles Exist", lambda: check_htf_candles_exist(conn, args.symbol)),
                ("D1 Session Alignment", lambda: check_d1_alignment(conn, args.symbol)),
                ("W1 Week Boundaries", lambda: check_w1_alignment(conn, args.symbol)),
                ("No Duplicates", lambda: check_duplicates(conn, args.symbol)),
                ("Indicator Coverage", lambda: check_indicator_coverage(conn, args.symbol)),
            ]
            
            results = []
            for name, check_fn in checks:
                try:
                    passed = check_fn()
                    results.append((name, passed))
                except Exception as e:
                    print(f"\n❌ ERROR in {name}: {e}")
                    results.append((name, False))
            
            # Summary
            print("\n" + "="*80)
            print("VALIDATION SUMMARY")
            print("="*80)
            
            passed = sum(1 for _, p in results if p)
            total = len(results)
            
            for name, p in results:
                marker = "✅" if p else "❌"
                print(f"{marker} {name}")
            
            print("-" * 80)
            print(f"\nResult: {passed}/{total} checks passed")
            
            if passed == total:
                print("\n🎉 SUCCESS: Migration validated successfully!")
                print("   Broker-provided D1/W1/MN1 architecture is correctly implemented.")
                sys.exit(0)
            else:
                print("\n⚠️  ISSUES FOUND: Review failures above")
                print("   See docs/DST_FIX_MIGRATION.md for troubleshooting")
                sys.exit(1)
                
    except Exception as e:
        print(f"\n❌ DATABASE ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
