#!/usr/bin/env python3
"""
Data Updater Scheduler
======================
MT5 Mode: Runs indicator calculator every 5 minutes (broker pushes data via TCP)
TwelveData Mode: DEPRECATED - use MT5 broker integration instead

FIX 5 COMPLIANCE:
- Scheduler contains ZERO market logic
- No market status checks
- No trading calendar imports
- Pure orchestration only
- Ingestion scripts handle all market checks internally
"""

import os
import sys
import time
import subprocess
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDICATOR_SCRIPT = os.path.join(SCRIPT_DIR, "calculate_recent_indicators_v2.py")  # v2.0 - DST-safe with HTF checks
UPDATE_INTERVAL = 300  # 5 minutes in seconds


def _use_timescale_caggs() -> bool:
    return (os.getenv("USE_TIMESCALE_CAGGS") or "").strip().lower() in {"1", "true", "yes", "y"}

def log(message):
    """Print timestamped log message"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}", flush=True)

def log_mode_banner():
    if _use_timescale_caggs():
        log("🧱 Timescale CAGG mode enabled: higher TF candles computed in DB")


def run_indicator_updater():
    """Calculate and store technical indicators for recent bars."""
    log("="*80)
    log("Starting indicator updater (recent bars -> technical_indicators)...")
    log("="*80)
    try:
        result = subprocess.run(
            [sys.executable, INDICATOR_SCRIPT],
            capture_output=False,
            text=True,
        )
        if result.returncode == 0:
            log("✓ Indicator updater completed successfully")
        else:
            log(f"✗ Indicator updater failed with exit code {result.returncode}")
        return result.returncode == 0
    except Exception as e:
        log(f"✗ Error running indicator updater: {e}")
        return False


def wait_for_candle_close():
    """Wait until 1 second after the next 5-minute mark"""
    now = datetime.now()
    current_minute = now.minute
    current_second = now.second
    
    # Calculate next 5-minute mark
    next_5min_mark = ((current_minute // 5) + 1) * 5
    if next_5min_mark >= 60:
        next_5min_mark = 0
    
    # Calculate seconds to wait
    if next_5min_mark == 0:
        # Next hour
        minutes_to_wait = 60 - current_minute
    else:
        minutes_to_wait = next_5min_mark - current_minute
    
    seconds_to_wait = (minutes_to_wait * 60) - current_second + 1  # +1 for 1 second after
    
    if seconds_to_wait > 0:
        next_time = now.replace(second=0, microsecond=0)
        if next_5min_mark == 0:
            from datetime import timedelta
            next_time = (next_time + timedelta(hours=1)).replace(minute=0)
        else:
            next_time = next_time.replace(minute=next_5min_mark)
        
        log(f"⏱️  Waiting {seconds_to_wait}s until candle closes at {next_time.strftime('%H:%M:%S')}...")
        time.sleep(seconds_to_wait)
        log(f"✅ Candle closed, starting fetch...")


def wait_for_next_5min_mark():
    """
    Wait until 1 second after the next 5-minute mark.
    This ensures we start the clock BEFORE processing, not after.
    
    Returns the number of seconds waited.
    """
    from datetime import timezone
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    current_second = now.second
    current_microsecond = now.microsecond
    
    # Calculate next 5-minute mark
    next_5min_mark = ((current_minute // 5) + 1) * 5
    
    if next_5min_mark >= 60:
        # Roll to next hour
        next_time = now.replace(hour=(now.hour + 1) % 24, minute=0, second=1, microsecond=0)
        if now.hour == 23:
            next_time = next_time + timedelta(days=1)
    else:
        next_time = now.replace(minute=next_5min_mark, second=1, microsecond=0)
    
    seconds_to_wait = (next_time - now).total_seconds()
    
    if seconds_to_wait > 0:
        log(f"⏱️  Waiting {seconds_to_wait:.1f}s until next 5-min mark at {next_time.strftime('%H:%M:%S')} UTC...")
        time.sleep(seconds_to_wait)
        return seconds_to_wait
    
    return 0


def wait_for_mt5_ready():
    """Wait until MT5 EA is connected + subscribed.

    The API touches a file inside the container when it has sent SUBSCRIBE.
    Scheduler uses this as a simple cross-process readiness signal.
    """
    ready_file = os.getenv("MT5_READY_SUBSCRIBED_FILE", "/tmp/mt5_ready")
    timeout_s = int(os.getenv("MT5_READY_TIMEOUT_SECONDS", "900"))
    poll_s = float(os.getenv("MT5_READY_POLL_SECONDS", "1"))

    if timeout_s <= 0:
        return True

    log(f"⏳ MT5 mode: waiting for EA readiness file {ready_file} (timeout={timeout_s}s)...")
    start = time.time()
    while True:
        if os.path.exists(ready_file):
            log("✅ MT5 ready: EA subscribed; starting processing jobs")
            return True
        if (time.time() - start) > timeout_s:
            log("⚠️  MT5 ready wait timed out; continuing anyway")
            return False
        time.sleep(poll_s)

def main():
    """Main scheduler loop - MT5 MODE ONLY
    
    MT5 Mode: Broker pushes data via TCP → Just run indicator calculator every 5 minutes
    TwelveData Mode: DEPRECATED (use MT5 broker integration)
    """
    data_source = (os.getenv("DATA_SOURCE") or "MT5").strip().upper()
    mt5_mode = data_source in {"MT5", "MT5_ONLY", "BROKER"}

    log("="*80)
    log("DATA UPDATER SCHEDULER STARTED (MT5 BROKER MODE)")
    log("="*80)
    log(f"Update interval: {UPDATE_INTERVAL}s ({UPDATE_INTERVAL/60:.1f} minutes)")
    log(f"Indicator script: {INDICATOR_SCRIPT}")
    log("="*80)

    log_mode_banner()
    
    # ============================================================================
    # STEP 1: Startup - Wait for MT5 ready, compute initial indicators
    # ============================================================================
    wait_for_mt5_ready()
    log("\n🚀 STARTUP: MT5 mode - Broker pushes data via TCP")
    log("🧮 STARTUP: Computing indicators...")
    run_indicator_updater()
    
    # ============================================================================
    # STEP 2: Continuous 5-minute indicator updates
    # ============================================================================
    log(f"\n⏰ Starting processing loop (every {UPDATE_INTERVAL/60:.1f} minutes)...")
    log("Waiting for next 5-minute mark before first cycle...")
    
    run_count = 0
    
    while True:
        try:
            # Wait for next 5-minute candle close FIRST (before processing)
            wait_for_next_5min_mark()
            
            run_count += 1
            log(f"\n🔄 SCHEDULED UPDATE #{run_count}")

            # MT5 mode: Broker pushes candles continuously → Just compute indicators
            run_indicator_updater()
            
        except KeyboardInterrupt:
            log("\n\n⚠️  Received interrupt signal, shutting down...")
            break
        except Exception as e:
            log(f"\n✗ Unexpected error: {e}")
            log("Continuing after 60 seconds...")
            time.sleep(60)
    
    log("="*80)
    log("DATA UPDATER SCHEDULER STOPPED")
    log("="*80)


if __name__ == "__main__":
    main()
