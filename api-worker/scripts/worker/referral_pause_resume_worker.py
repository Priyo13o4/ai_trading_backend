#!/usr/bin/env python3
"""Referral pause/resume orchestrator worker.

Runs Scope E state machine processing for referral rewards that are already
claimed and eligible for free-month pause/resume execution.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
APP_ROOT = os.path.abspath(os.path.join(SCRIPTS_ROOT, ".."))
sys.path.insert(0, APP_ROOT)

from app.referrals.pause_resume import (
    ReferralPauseResumeConfigurationError,
    run_referral_pause_resume_cycle,
)


def _is_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_debug_enabled() -> bool:
    return _is_truthy("AUTHDBG_ENABLED")


def _log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [REFERRAL_PAUSE_RESUME] {message}", flush=True)


def main() -> int:
    if not _is_truthy("REFERRAL_PAUSE_RESUME_WORKER_ENABLED"):
        _log("Worker disabled via REFERRAL_PAUSE_RESUME_WORKER_ENABLED")
        return 0

    _log("Starting referral pause/resume orchestration run")
    if _is_debug_enabled():
        _log("AUTHDBG_ENABLED active")

    try:
        stats = run_referral_pause_resume_cycle()
        _log(
            "Completed run "
            f"seeded={stats.seeded_cycles} "
            f"paused={stats.paused_success} "
            f"resumed={stats.resumed_success} "
            f"failed_marked={stats.failed_marked}"
        )
        return 0
    except ReferralPauseResumeConfigurationError as exc:
        _log(f"Blocking configuration error during run: {exc}")
        return 2
    except Exception as exc:
        _log(f"Fatal error during run: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
