"""Referral reward pause/resume cycle logic — Scope E implementation.

This module implements the worker-driven pause/resume cycle for referral free-month rewards.
It is imported by the referral_pause_resume_worker.py script.

State machine for referral_reward_pause_cycles:
  reward_pending -> pause_pending -> paused -> resume_pending -> resumed

All functions are idempotent and safe for repeated polling.
"""

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.db import async_db, get_supabase_client

logger = logging.getLogger(__name__)


def _is_debug_enabled() -> bool:
    return os.getenv("AUTHDBG_ENABLED", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _debug_log(message: str) -> None:
    if _is_debug_enabled():
        logger.info("[REFERRAL_PAUSE_RESUME] %s", message)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _idempotency_key(prefix: str, reward_id: str, cycle_number: int) -> str:
    """Generate a stable idempotency key for a given reward+cycle action."""
    raw = f"{prefix}:{reward_id}:{cycle_number}"
    return uuid.uuid5(uuid.NAMESPACE_URL, raw).hex


@dataclass(frozen=True)
class PauseResumeResult:
    outcome: str  # 'success', 'partial', 'noop', 'error_controlled'
    paused_count: int = 0
    resumed_count: int = 0
    failed_count: int = 0
    errors: list[str] = field(default_factory=list)


async def _get_cycles_by_status(
    supabase,
    status: str,
    limit: int = 50,
) -> list[dict]:
    """Fetch pause cycles in a given status, ordered by creation time."""
    response = await async_db(
        lambda: supabase.table("referral_reward_pause_cycles")
        .select(
            "reward_id, cycle_number, status, razorpay_subscription_id, "
            "razorpay_pause_id, pause_start_time, pause_end_time, created_at"
        )
        .eq("status", status)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return list(response.data or [])


async def _cas_cycle_status(
    supabase,
    reward_id: str,
    cycle_number: int,
    expected_status: str,
    new_status: str,
    extra_fields: Optional[dict] = None,
) -> bool:
    """Compare-and-swap cycle status. Returns True if exactly 1 row was updated."""
    payload: dict[str, Any] = {
        "status": new_status,
        "updated_at": _now_utc().isoformat(),
    }
    if extra_fields:
        payload.update(extra_fields)

    result = await async_db(
        lambda: supabase.table("referral_reward_pause_cycles")
        .update(payload)
        .eq("reward_id", reward_id)
        .eq("cycle_number", cycle_number)
        .eq("status", expected_status)
        .execute()
    )
    updated = result.data or []
    return len(updated) > 0


async def _process_pause_pending_cycles(supabase) -> tuple[int, int, list[str]]:
    """
    Process all cycles in 'pause_pending' state:
    - Call Razorpay pause_subscription
    - Transition cycle to 'paused' on success, 'pause_failed' on error.

    Returns (paused_count, failed_count, error_messages)
    """
    from app.payments.payment_providers.razorpay_provider import RazorpayProvider

    provider = RazorpayProvider()
    cycles = await _get_cycles_by_status(supabase, "pause_pending")
    paused_count = 0
    failed_count = 0
    errors: list[str] = []

    for cycle in cycles:
        reward_id = cycle["reward_id"]
        cycle_number = cycle["cycle_number"]
        sub_id = cycle.get("razorpay_subscription_id") or ""

        if not sub_id:
            msg = f"cycle reward_id={reward_id} cycle={cycle_number} has no razorpay_subscription_id"
            logger.warning("event=referral_pause_skip_no_sub_id %s", msg)
            await _cas_cycle_status(
                supabase, reward_id, cycle_number, "pause_pending", "pause_failed",
                {"updated_at": _now_utc().isoformat()},
            )
            failed_count += 1
            errors.append(msg)
            continue

        pause_at = int(_now_utc().timestamp())
        idempotency_key = _idempotency_key("pause", reward_id, cycle_number)

        try:
            _debug_log(f"pausing subscription sub_id={sub_id} reward_id={reward_id} cycle={cycle_number}")
            pause_result = await provider.pause_subscription(
                sub_id,
                pause_at,
                idempotency_key=idempotency_key,
            )
            pause_id = pause_result.get("pause_id")
            now_iso = _now_utc().isoformat()

            transitioned = await _cas_cycle_status(
                supabase, reward_id, cycle_number, "pause_pending", "paused",
                {
                    "razorpay_pause_id": pause_id,
                    "pause_start_time": now_iso,
                    "updated_at": now_iso,
                },
            )
            if transitioned:
                paused_count += 1
                logger.info(
                    "event=referral_pause_success reward_id=%s cycle=%s sub_id=%s pause_id=%s",
                    reward_id, cycle_number, sub_id, pause_id,
                )
            else:
                logger.warning(
                    "event=referral_pause_cas_skipped reward_id=%s cycle=%s "
                    "(row already transitioned by another worker)",
                    reward_id, cycle_number,
                )
        except Exception as exc:
            error_msg = str(exc)[:500]
            errors.append(f"reward_id={reward_id} cycle={cycle_number}: {error_msg}")
            logger.error(
                "event=referral_pause_error reward_id=%s cycle=%s sub_id=%s error=%s",
                reward_id, cycle_number, sub_id, error_msg,
            )
            await _cas_cycle_status(
                supabase, reward_id, cycle_number, "pause_pending", "pause_failed",
                {"updated_at": _now_utc().isoformat()},
            )
            failed_count += 1

    return paused_count, failed_count, errors


async def _process_resume_pending_cycles(supabase) -> tuple[int, int, list[str]]:
    """
    Process all cycles in 'resume_pending' state where pause_end_time <= now:
    - Call Razorpay resume_subscription
    - Transition cycle to 'resumed' on success, 'resume_failed' on error.

    Returns (resumed_count, failed_count, error_messages)
    """
    from app.payments.payment_providers.razorpay_provider import RazorpayProvider

    provider = RazorpayProvider()
    cycles = await _get_cycles_by_status(supabase, "resume_pending")
    now = _now_utc()
    resumed_count = 0
    failed_count = 0
    errors: list[str] = []

    for cycle in cycles:
        reward_id = cycle["reward_id"]
        cycle_number = cycle["cycle_number"]
        sub_id = cycle.get("razorpay_subscription_id") or ""
        pause_id = cycle.get("razorpay_pause_id")

        # Only resume if the pause window has expired
        raw_end = str(cycle.get("pause_end_time") or "").strip()
        if raw_end:
            try:
                pause_end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
                if pause_end.tzinfo is None:
                    pause_end = pause_end.replace(tzinfo=timezone.utc)
                if now < pause_end:
                    _debug_log(
                        f"skipping resume reward_id={reward_id} cycle={cycle_number}: "
                        f"pause_end_time={pause_end} > now={now}"
                    )
                    continue
            except Exception:
                logger.warning(
                    "event=referral_resume_invalid_pause_end reward_id=%s cycle=%s raw=%s",
                    reward_id, cycle_number, raw_end,
                )

        if not sub_id:
            msg = f"cycle reward_id={reward_id} cycle={cycle_number} has no razorpay_subscription_id"
            logger.warning("event=referral_resume_skip_no_sub_id %s", msg)
            await _cas_cycle_status(
                supabase, reward_id, cycle_number, "resume_pending", "resume_failed",
                {"updated_at": _now_utc().isoformat()},
            )
            failed_count += 1
            errors.append(msg)
            continue

        idempotency_key = _idempotency_key("resume", reward_id, cycle_number)

        try:
            _debug_log(f"resuming subscription sub_id={sub_id} reward_id={reward_id} cycle={cycle_number}")
            resume_result = await provider.resume_subscription(
                sub_id,
                pause_id,
                idempotency_key=idempotency_key,
            )
            now_iso = _now_utc().isoformat()
            transitioned = await _cas_cycle_status(
                supabase, reward_id, cycle_number, "resume_pending", "resumed",
                {
                    "pause_end_time": now_iso,
                    "updated_at": now_iso,
                },
            )
            if transitioned:
                resumed_count += 1
                logger.info(
                    "event=referral_resume_success reward_id=%s cycle=%s sub_id=%s provider_status=%s",
                    reward_id, cycle_number, sub_id, resume_result.get("status"),
                )

                # Update the parent referral_rewards status to 'applied'
                # (confirms the free month was delivered)
                await async_db(
                    lambda: supabase.table("referral_rewards")
                    .update({"status": "applied", "updated_at": now_iso})
                    .eq("referral_id", reward_id)
                    .eq("status", "claimed")
                    .execute()
                )
            else:
                logger.warning(
                    "event=referral_resume_cas_skipped reward_id=%s cycle=%s "
                    "(row already transitioned by another worker)",
                    reward_id, cycle_number,
                )
        except Exception as exc:
            error_msg = str(exc)[:500]
            errors.append(f"reward_id={reward_id} cycle={cycle_number}: {error_msg}")
            logger.error(
                "event=referral_resume_error reward_id=%s cycle=%s sub_id=%s error=%s",
                reward_id, cycle_number, sub_id, error_msg,
            )
            await _cas_cycle_status(
                supabase, reward_id, cycle_number, "resume_pending", "resume_failed",
                {"updated_at": _now_utc().isoformat()},
            )
            failed_count += 1

    return resumed_count, failed_count, errors


async def run_referral_pause_resume_cycle() -> PauseResumeResult:
    """
    Entry point for the referral pause/resume worker.

    Processes:
    1. pause_pending cycles -> calls Razorpay pause, transitions to paused
    2. resume_pending cycles (where pause_end_time <= now) -> calls Razorpay resume, transitions to resumed

    Idempotent: safe to call repeatedly from a cron or worker loop.
    Uses CAS (Compare-And-Swap) to prevent double-processing under concurrent workers.
    """
    supabase = get_supabase_client()
    all_errors: list[str] = []

    try:
        paused_count, pause_failures, pause_errors = await _process_pause_pending_cycles(supabase)
        all_errors.extend(pause_errors)
    except Exception as exc:
        logger.exception("event=referral_pause_resume_cycle_pause_phase_failed error=%s", exc)
        paused_count = 0
        pause_failures = 0
        all_errors.append(f"pause_phase: {str(exc)[:500]}")

    try:
        resumed_count, resume_failures, resume_errors = await _process_resume_pending_cycles(supabase)
        all_errors.extend(resume_errors)
    except Exception as exc:
        logger.exception("event=referral_pause_resume_cycle_resume_phase_failed error=%s", exc)
        resumed_count = 0
        resume_failures = 0
        all_errors.append(f"resume_phase: {str(exc)[:500]}")

    total_failures = pause_failures + resume_failures
    total_actions = paused_count + resumed_count

    outcome = "noop"
    if total_failures > 0 and total_actions == 0:
        outcome = "error_controlled"
    elif total_failures > 0:
        outcome = "partial"
    elif total_actions > 0:
        outcome = "success"

    logger.info(
        "event=referral_pause_resume_cycle_done outcome=%s paused=%s resumed=%s failed=%s",
        outcome, paused_count, resumed_count, total_failures,
    )

    return PauseResumeResult(
        outcome=outcome,
        paused_count=paused_count,
        resumed_count=resumed_count,
        failed_count=total_failures,
        errors=all_errors,
    )
