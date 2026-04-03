#!/usr/bin/env python3
"""
Referral Rewards State Transition Orchestrator - Scope C

Responsibilities:
- Transition on_hold -> available when hold_expires_at has expired
- Transition available -> applied
- Batch-safe and idempotent processing

This runs as part of the worker scheduler tick.
No changes to payment provider contracts.
"""

import os
import sys
import asyncio
from datetime import datetime, timezone

# Add parent directory to path for imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
APP_ROOT = os.path.abspath(os.path.join(SCRIPTS_ROOT, ".."))
sys.path.insert(0, APP_ROOT)

# Import after path setup
from app.referrals.reward_transitions import (
    ReferralTransitionConfigurationError,
    transition_rewards_on_hold_to_available,
    apply_available_rewards,
)


def _is_debug_enabled() -> bool:
    """Check if AUTHDBG_ENABLED is set."""
    return os.getenv("AUTHDBG_ENABLED", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def log(message: str) -> None:
    """Print timestamped log message."""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    prefix = "[REFERRAL_REWARDS]"
    print(f"[{timestamp}] {prefix} {message}", flush=True)


async def run_referral_reward_transitions() -> bool:
    """
    Execute referral reward state transitions in order:
    1. Transition on_hold -> available (when expired)
    2. Transition available -> applied
    
    Returns True if successful, False otherwise.
    """
    log("Starting referral reward transitions orchestration...")

    # Step 1: Transition expired on_hold to available
    log("Step 1: Transitioning expired on_hold rewards to available...")
    hold_result = await transition_rewards_on_hold_to_available()
    log(f"✓ Transitioned {hold_result.transitioned_count} on_hold rewards to available")

    # Step 2: Apply available rewards
    log("Step 2: Applying available rewards...")
    apply_result = await apply_available_rewards()
    log(f"✓ Applied {apply_result.transitioned_count} available rewards")

    log("Referral reward transitions orchestration completed")
    return True


def main() -> int:
    """Main entry point for the referral rewards transition orchestrator."""
    log("=" * 80)
    log("REFERRAL REWARDS TRANSITION ORCHESTRATOR")
    log("=" * 80)
    
    if _is_debug_enabled():
        log("✓ Debug logging enabled (AUTHDBG_ENABLED=1)")
    
    try:
        success = asyncio.run(run_referral_reward_transitions())
        if success:
            log("Orchestration completed successfully")
            return 0
        log("Orchestration failed")
        return 1
    except ReferralTransitionConfigurationError as e:
        log(f"✗ Blocking configuration error: {e}")
        return 2
    except Exception as e:
        log(f"✗ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
