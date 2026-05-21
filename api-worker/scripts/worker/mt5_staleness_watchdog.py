import asyncio
import os
import sys
import time
import pytz
from datetime import datetime, timezone
from sqlalchemy import text

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
APP_ROOT = os.path.abspath(os.path.join(SCRIPTS_ROOT, ".."))
sys.path.insert(0, APP_ROOT)

from app.error_alerts import report_runtime_error
from app.db import AsyncSessionLocal

POLL_INTERVAL = 60
STALENESS_MINUTES = 5
ALERT_COOLDOWN_SECONDS = 3600

last_alert_time = None


async def get_latest_candles():
    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                text("""
                    SELECT symbol, MAX(time) as last_candle_time
                    FROM candlesticks
                    WHERE timeframe = 'M1'
                    GROUP BY symbol
                """)
            )
            return [dict(r) for r in res.mappings().fetchall()]
    except Exception as e:
        print(f"Error querying Postgres for latest candles: {e}")
        return None


async def check_staleness():
    global last_alert_time

    latest_data = await get_latest_candles()
    if not latest_data:
        return

    btcusd_time = None
    for row in latest_data:
        if row["symbol"] == "BTCUSD":
            btcusd_time = row["last_candle_time"]
            break

    if not btcusd_time:
        print("BTCUSD not found in latest M1 candles. Skipping staleness check.")
        return

    now_utc = datetime.now(timezone.utc)
    if btcusd_time.tzinfo is None:
        btcusd_time = btcusd_time.replace(tzinfo=timezone.utc)

    age_seconds = (now_utc - btcusd_time).total_seconds()

    if age_seconds > (STALENESS_MINUTES * 60):
        print(f"[STALENESS] BTCUSD is stale! age={age_seconds}s")
        if last_alert_time and (time.time() - last_alert_time) < ALERT_COOLDOWN_SECONDS:
            print("[STALENESS] Alert cooldown active. Skipping alert.")
            return

        print("[STALENESS] Firing alert...")
        ist = pytz.timezone("Asia/Kolkata")

        context_table = {}
        for row in latest_data:
            dt = row["last_candle_time"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            context_table[row["symbol"]] = dt.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")

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
                    "last_known_candles_ist": context_table,
                },
            )
            last_alert_time = time.time()
        except Exception as e:
            print(f"[STALENESS] Error sending alert: {e}")


async def run_monitor():
    print(f"Starting MT5 Staleness Watchdog (Threshold: {STALENESS_MINUTES}m)...")
    while True:
        try:
            await check_staleness()
        except Exception as e:
            print(f"Staleness monitor loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_monitor())
