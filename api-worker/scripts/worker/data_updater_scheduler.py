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
import random
import subprocess
import threading
from datetime import datetime, timedelta
from typing import Optional

import redis

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
INDICATOR_SCRIPT = os.path.join(SCRIPTS_ROOT, "calculate_recent_indicators_v2.py")  # v2.0 - DST-safe with HTF checks
UPDATE_INTERVAL = 300  # 5 minutes in seconds
_LOCAL_SCHEDULER_LOCK = threading.Lock()


def _touch(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except Exception:
        pass


def _use_timescale_caggs() -> bool:
    return (os.getenv("USE_TIMESCALE_CAGGS") or "").strip().lower() in {"1", "true", "yes", "y"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def log(message):
    """Print timestamped log message"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}", flush=True)


def _get_redis_client() -> Optional[redis.Redis]:
    try:
        host = os.getenv("REDIS_HOST", "redis")
        port = int(os.getenv("REDIS_PORT", "6379"))
        password = os.getenv("REDIS_PASSWORD")
        db = int(os.getenv("REDIS_DB", "0"))
        return redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
    except Exception as e:
        log(f"⚠️  Redis client init failed: {e}")
        return None


def _acquire_scheduler_lock_with_status(
    client: Optional[redis.Redis],
    *,
    strict_lock_mode: bool,
) -> tuple[str, Optional[redis.lock.Lock]]:
    fallback_enabled = _env_bool("WORKER_LOCAL_FALLBACK_LOCK_ENABLED", True)
    if strict_lock_mode:
        fallback_enabled = False
    if client is None:
        if fallback_enabled and _LOCAL_SCHEDULER_LOCK.acquire(blocking=False):
            return "acquired_local", None
        if fallback_enabled:
            return "held_locally", None
        return "backend_unavailable", None
    lock_key = os.getenv("WORKER_LOCK_KEY", "locks:data_updater_scheduler")
    ttl_seconds = int(os.getenv("WORKER_LOCK_TTL_SECONDS", "420"))
    try:
        lock = client.lock(lock_key, timeout=ttl_seconds, blocking_timeout=0)
        if lock.acquire(blocking=False):
            return "acquired", lock
        return "held_elsewhere", None
    except Exception as e:
        log(f"⚠️  Failed to acquire scheduler lock: {e}")
        if fallback_enabled and _LOCAL_SCHEDULER_LOCK.acquire(blocking=False):
            log("⚠️  Using process-local fallback lock because distributed lock backend is unavailable")
            return "acquired_local", None
        if fallback_enabled:
            return "held_locally", None
        return "backend_unavailable", None


def _validate_lock_ttl(strict_lock_mode: bool) -> bool:
    lock_ttl_seconds = int(os.getenv("WORKER_LOCK_TTL_SECONDS", "420"))
    updater_timeout_seconds = int(os.getenv("INDICATOR_UPDATER_TIMEOUT_SECONDS", "240"))
    safety_margin_seconds = int(os.getenv("WORKER_LOCK_TTL_SAFETY_MARGIN_SECONDS", "30"))
    minimum_required = updater_timeout_seconds + safety_margin_seconds

    if lock_ttl_seconds >= minimum_required:
        return True

    log(
        "⚠️  Scheduler lock TTL too short: "
        f"WORKER_LOCK_TTL_SECONDS={lock_ttl_seconds} < "
        f"INDICATOR_UPDATER_TIMEOUT_SECONDS + margin ({minimum_required})"
    )
    if strict_lock_mode:
        log("🛑 Strict lock mode enabled; refusing to start scheduler with unsafe lock TTL")
        return False

    log("⚠️  Strict lock mode disabled; continuing with potentially unsafe lock TTL")
    return True


def _start_lock_renewal_thread(
    lock: Optional[redis.lock.Lock],
    *,
    ttl_seconds: int,
    stop_event: threading.Event,
    lost_event: threading.Event,
) -> Optional[threading.Thread]:
    if lock is None or ttl_seconds <= 0:
        return None

    interval_seconds = float(os.getenv("WORKER_LOCK_RENEW_INTERVAL_SECONDS", str(max(1, ttl_seconds // 3))))
    interval_seconds = max(1.0, min(interval_seconds, float(max(1, ttl_seconds - 1))))

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                try:
                    lock.extend(ttl_seconds, replace_ttl=True)
                except TypeError:
                    lock.extend(ttl_seconds)
            except Exception as e:
                log(f"⚠️  Failed to renew scheduler lock lease: {e}")
                lost_event.set()
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def _start_heartbeat_thread() -> None:
    heartbeat_file = os.getenv("WORKER_HEARTBEAT_FILE", "/tmp/worker_heartbeat")
    interval_s = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "15"))

    def _loop() -> None:
        while True:
            _touch(heartbeat_file)
            time.sleep(interval_s)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def log_mode_banner():
    if _use_timescale_caggs():
        log("🧱 Timescale CAGG mode enabled: higher TF candles computed in DB")


def _phase_env(phase: str, startup_name: str, scheduled_name: str, default: str) -> str:
    if phase == "startup":
        return os.getenv(startup_name, os.getenv(scheduled_name, default))
    return os.getenv(scheduled_name, default)


def run_indicator_updater(
    active_lock: Optional[redis.lock.Lock] = None,
    *,
    phase: str = "scheduled",
):
    """Calculate and store technical indicators for recent bars."""
    log("=" * 80)
    log(f"Starting indicator updater phase={phase} (recent bars -> technical_indicators)...")
    log("=" * 80)
    timeout_seconds = int(
        _phase_env(
            phase,
            "INDICATOR_UPDATER_STARTUP_TIMEOUT_SECONDS",
            "INDICATOR_UPDATER_TIMEOUT_SECONDS",
            "240",
        )
    )
    backfill_bars = _phase_env(
        phase,
        "INDICATOR_SAFETY_BACKFILL_BARS_STARTUP",
        "INDICATOR_SAFETY_BACKFILL_BARS",
        "2",
    )
    max_new_bars = _phase_env(
        phase,
        "INDICATOR_MAX_NEW_BARS_PER_CYCLE_STARTUP",
        "INDICATOR_MAX_NEW_BARS_PER_CYCLE",
        "8",
    )
    lookback_bars = _phase_env(
        phase,
        "INDICATOR_LOOKBACK_BARS_STARTUP",
        "INDICATOR_LOOKBACK_BARS",
        "300",
    )
    force_overlap_recompute_minutes = _phase_env(
        phase,
        "INDICATOR_FORCE_OVERLAP_RECOMPUTE_MINUTES_STARTUP",
        "INDICATOR_FORCE_OVERLAP_RECOMPUTE_MINUTES",
        "60",
    )
    strict_lock_mode = _env_bool("WORKER_LOCK_STRICT_MODE", True)
    lock_ttl_seconds = int(os.getenv("WORKER_LOCK_TTL_SECONDS", "420"))
    log(
        f"Updater profile: timeout={timeout_seconds}s max_new={max_new_bars} "
        f"lookback={lookback_bars} backfill={backfill_bars} "
        f"force_overlap={force_overlap_recompute_minutes}m"
    )
    cmd = [
        sys.executable,
        INDICATOR_SCRIPT,
        "--safety-backfill-bars",
        str(backfill_bars),
        "--max-new-bars-per-cycle",
        str(max_new_bars),
        "--lookback-bars",
        str(lookback_bars),
        "--force-overlap-recompute-minutes",
        str(force_overlap_recompute_minutes),
    ]
    proc: Optional[subprocess.Popen] = None
    lease_stop_event = threading.Event()
    lease_lost_event = threading.Event()
    renewal_thread = None

    try:
        proc = subprocess.Popen(cmd)

        if active_lock is not None:
            renewal_thread = _start_lock_renewal_thread(
            active_lock,
                ttl_seconds=lock_ttl_seconds,
                stop_event=lease_stop_event,
                lost_event=lease_lost_event,
            )

        start_time = time.monotonic()
        while True:
            if proc.poll() is not None:
                break

            elapsed = time.monotonic() - start_time
            if elapsed > timeout_seconds:
                log(f"✗ Indicator updater timed out after {timeout_seconds}s")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
                return False

            if lease_lost_event.is_set() and strict_lock_mode:
                log("🛑 Scheduler lock lease lost during run; terminating updater due to strict lock mode")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
                return False

            time.sleep(0.5)

        if proc.returncode == 0:
            log("✓ Indicator updater completed successfully")
        else:
            log(f"✗ Indicator updater failed with exit code {proc.returncode}")
        return proc.returncode == 0
    except Exception as e:
        log(f"✗ Error running indicator updater: {e}")
        return False
    finally:
        lease_stop_event.set()
        if renewal_thread is not None:
            renewal_thread.join(timeout=2)


def _sleep_cycle_jitter() -> None:
    max_jitter_seconds = float(os.getenv("INDICATOR_CYCLE_JITTER_SECONDS", "1.5"))
    if max_jitter_seconds <= 0:
        return
    jitter = random.uniform(0.0, max_jitter_seconds)
    if jitter > 0:
        log(f"⏱️  Applying jitter before cycle run: {jitter:.2f}s")
        time.sleep(jitter)


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


def wait_for_mt5_ready(redis_client: Optional[redis.Redis] = None):
    """Wait until MT5 EA is connected + subscribed.

    The API touches a file inside the container when it has sent SUBSCRIBE.
    Scheduler uses this as a simple cross-process readiness signal.
    """
    ready_file = os.getenv("MT5_READY_SUBSCRIBED_FILE", "/tmp/mt5_ready")
    ready_key = os.getenv("MT5_READY_SUBSCRIBED_REDIS_KEY", "mt5:ready:subscribed")
    timeout_s = int(os.getenv("MT5_READY_TIMEOUT_SECONDS", "900"))
    poll_s = float(os.getenv("MT5_READY_POLL_SECONDS", "1"))

    if timeout_s <= 0:
        return True

    log(f"⏳ MT5 mode: waiting for EA readiness (file={ready_file}, key={ready_key}, timeout={timeout_s}s)...")
    start = time.time()
    while True:
        if os.path.exists(ready_file):
            log("✅ MT5 ready: EA subscribed; starting processing jobs")
            return True
        if redis_client is not None:
            try:
                if redis_client.exists(ready_key):
                    log("✅ MT5 ready: EA subscribed (Redis)")
                    return True
            except Exception as e:
                log(f"⚠️  Redis MT5 ready check failed: {e}")
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

    log("=" * 80)
    log("DATA UPDATER SCHEDULER STARTED (MT5 BROKER MODE)")
    log("=" * 80)
    log(f"Update interval: {UPDATE_INTERVAL}s ({UPDATE_INTERVAL/60:.1f} minutes)")
    log(f"Indicator script: {INDICATOR_SCRIPT}")
    log("=" * 80)

    log_mode_banner()

    strict_lock_mode = _env_bool("WORKER_LOCK_STRICT_MODE", True)
    if not _validate_lock_ttl(strict_lock_mode):
        return 1

    try:
        failure_threshold = int(os.getenv("INDICATOR_UPDATER_CONSECUTIVE_FAILURE_THRESHOLD", "3"))
    except Exception:
        failure_threshold = 3
    failure_threshold = max(1, failure_threshold)
    consecutive_failures = 0

    def _record_updater_result(success: bool, phase: str) -> bool:
        nonlocal consecutive_failures
        if success:
            if consecutive_failures > 0:
                log(f"✅ Indicator updater recovered during {phase}; resetting consecutive failure counter")
            consecutive_failures = 0
            return False

        consecutive_failures += 1
        log(
            f"⚠️  Indicator updater failure during {phase} "
            f"(consecutive failures: {consecutive_failures}/{failure_threshold})"
        )
        if consecutive_failures >= failure_threshold:
            log(
                "🛑 Consecutive indicator updater failure threshold reached; "
                "exiting scheduler with non-zero status for supervision"
            )
            return True
        return False

    _start_heartbeat_thread()
    redis_client = _get_redis_client()

    # ============================================================================
    # STEP 1: Startup - Wait for MT5 ready, compute initial indicators
    # ============================================================================
    wait_for_mt5_ready(redis_client=redis_client)
    log("\n🚀 STARTUP: MT5 mode - Broker pushes data via TCP")
    log("🧮 STARTUP: Computing indicators...")
    startup_lock_status, startup_lock = _acquire_scheduler_lock_with_status(
        redis_client,
        strict_lock_mode=strict_lock_mode,
    )
    startup_local_lock_acquired = startup_lock_status == "acquired_local"
    if startup_lock_status in {"acquired", "acquired_local"}:
        try:
            startup_success = run_indicator_updater(active_lock=startup_lock, phase="startup")
            if _record_updater_result(startup_success, "startup"):
                return 2
        finally:
            if startup_lock is not None:
                try:
                    startup_lock.release()
                except Exception:
                    pass
            if startup_local_lock_acquired and _LOCAL_SCHEDULER_LOCK.locked():
                _LOCAL_SCHEDULER_LOCK.release()
    elif startup_lock_status == "held_elsewhere":
        log("⏸️  Scheduler lock held elsewhere; skipping startup run")
    elif startup_lock_status == "held_locally":
        log("⏸️  Process-local scheduler lock already held; skipping startup run")
    else:
        if strict_lock_mode:
            log("🛑 Lock backend unavailable in strict mode; skipping startup run")
            if _record_updater_result(False, "startup (strict lock backend unavailable)"):
                return 2
        else:
            log("⚠️  Lock backend unavailable; strict mode disabled, running startup updater unlocked")
            startup_success = run_indicator_updater()
            if _record_updater_result(startup_success, "startup"):
                return 2

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
            cycle_lock_status, cycle_lock = _acquire_scheduler_lock_with_status(
                redis_client,
                strict_lock_mode=strict_lock_mode,
            )
            if cycle_lock_status == "held_elsewhere":
                log("⏸️  Scheduler lock held elsewhere; skipping this cycle")
                continue
            if cycle_lock_status == "held_locally":
                log("⏸️  Process-local scheduler lock already held; skipping this cycle")
                continue
            if cycle_lock_status == "backend_unavailable" and strict_lock_mode:
                log("🛑 Lock backend unavailable in strict mode; skipping this cycle")
                if _record_updater_result(
                    False,
                    f"scheduled cycle #{run_count} (strict lock backend unavailable)",
                ):
                    return 2
                continue
            if cycle_lock_status == "backend_unavailable":
                log("⚠️  Lock backend unavailable; strict mode disabled, running this cycle unlocked")
            local_lock_acquired = cycle_lock_status == "acquired_local"

            try:
                _sleep_cycle_jitter()
                cycle_success = run_indicator_updater(active_lock=cycle_lock, phase="scheduled")
                if _record_updater_result(cycle_success, f"scheduled cycle #{run_count}"):
                    return 2
            finally:
                if cycle_lock is not None:
                    try:
                        cycle_lock.release()
                    except Exception:
                        pass
                if local_lock_acquired and _LOCAL_SCHEDULER_LOCK.locked():
                    _LOCAL_SCHEDULER_LOCK.release()

        except KeyboardInterrupt:
            log("\n\n⚠️  Received interrupt signal, shutting down...")
            break
        except Exception as e:
            log(f"\n✗ Unexpected error: {e}")
            if _record_updater_result(False, "outer scheduler loop exception"):
                return 2
            log("Continuing after 60 seconds...")
            time.sleep(60)

    log("=" * 80)
    log("DATA UPDATER SCHEDULER STOPPED")
    log("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
