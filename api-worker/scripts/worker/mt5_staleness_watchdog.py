import os
import sys
import time
import pytz
from datetime import datetime, timedelta, timezone
import psycopg
from psycopg.rows import dict_row

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
APP_ROOT = os.path.abspath(os.path.join(SCRIPTS_ROOT, ".."))
sys.path.insert(0, APP_ROOT)

from app.error_alerts import report_runtime_error
from app.db import POSTGRES_DSN

POLL_INTERVAL = 60
STALENESS_MINUTES = 5
ALERT_COOLDOWN_SECONDS = 3600

last_alert_time = None

def get_latest_candles():
    try:
        with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Query optimized to use group by for latest M1 candle per pair
                cur.execute("""
                    SELECT symbol, MAX(time) as last_candle_time
                    FROM candlesticks
                    WHERE timeframe = 'M1'
                    GROUP BY symbol
                """)
                return cur.fetchall()
    except Exception as e:
        print(f"Error querying Postgres for latest candles: {e}")
        return None

def check_staleness():
    global last_alert_time
    
    latest_data = get_latest_candles()
    if not latest_data:
        return
        
    btcusd_time = None
    for row in latest_data:
        if row['symbol'] == 'BTCUSD':
            btcusd_time = row['last_candle_time']
            break
            
    if not btcusd_time:
        print("BTCUSD not found in latest M1 candles. Skipping staleness check.")
        return
        
    now_utc = datetime.now(timezone.utc)
    # the time from db might be naive or tz-aware. Ensure UTC
    if btcusd_time.tzinfo is None:
        btcusd_time = btcusd_time.replace(tzinfo=timezone.utc)
        
    age_seconds = (now_utc - btcusd_time).total_seconds()
    
    if age_seconds > (STALENESS_MINUTES * 60):
        print(f"[STALENESS] BTCUSD is stale! age={age_seconds}s")
        # Check cooldown
        if last_alert_time and (time.time() - last_alert_time) < ALERT_COOLDOWN_SECONDS:
            print("[STALENESS] Alert cooldown active. Skipping alert.")
            return
            
        print("[STALENESS] Firing alert...")
        ist = pytz.timezone('Asia/Kolkata')
        
        # Build beautiful table of all pairs
        context_table = {}
        for row in latest_data:
            dt = row['last_candle_time']
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ist_time_str = dt.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")
            context_table[row['symbol']] = ist_time_str

        try:
            report_runtime_error(
                path="worker/staleness_watchdog",
                method="BACKGROUND",
                status_code=500,
                message_safe="Data ingestion stalled",
                message_internal="API worker ingestion stopped, MT5 possibly down",
                severity="critical",
                context={
                    "age_of_btcusd_candle": f"{int(age_seconds)} seconds",
                    "staleness_threshold_minutes": STALENESS_MINUTES,
                    "last_known_candles_ist": context_table
                }
            )
            last_alert_time = time.time()
        except Exception as e:
            print(f"[STALENESS] Error sending alert: {e}")
            
def run_monitor():
    print(f"Starting MT5 Staleness Watchdog (Threshold: {STALENESS_MINUTES}m)...")
    while True:
        try:
            check_staleness()
        except Exception as e:
            print(f"Staleness monitor loop error: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run_monitor()
