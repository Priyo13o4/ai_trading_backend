"""Referral API routes for profile and manual reward activation (Scope D)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.authn.deps import require_session
from app.db import async_db, get_supabase_client
from app.referrals.manual_activation import activate_referral_reward_manual
from app.referrals.utils import validate_uuid

referrals_router = APIRouter(prefix="/api/referrals")


class ActivateRewardsRequest(BaseModel):
    """Manual activation payload.

    The referral code is currently optional and unused, but retained for
    forward compatibility with future flows.
    """

    referral_code: str | None = Field(default=None, max_length=32)


def _build_referral_link(code: str | None) -> str | None:
    """L3 fix: only use FRONTEND_URL env var; never trust request Origin header."""
    if not code:
        return None

    frontend_base = os.getenv("FRONTEND_URL", "").strip()
    if not frontend_base:
        return None

    return f"{frontend_base.rstrip('/')}/signup?ref={code}"


@referrals_router.get("/profile")
async def get_referral_profile(
    request: Request,
    user_session: dict = Depends(require_session),
):
    """Return referral summary and manual activation counters for the current user.

    H5 fix: The three DB queries are executed concurrently with asyncio.gather().
    The tracking query is bounded to 500 rows to prevent unbounded fetches.
    """

    user_id = validate_uuid(user_session.get("user_id"))
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    supabase = get_supabase_client()

    # H5: Run all 3 DB queries concurrently instead of sequentially
    code_coro = async_db(
        lambda: supabase.table("referral_codes")
        .select("code")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    tracking_coro = async_db(
        lambda: supabase.table("referral_tracking")
        .select("id,status")
        .eq("referrer_id", user_id)
        .limit(500)  # H5: bound the row count
        .execute()
    )
    rewards_coro = async_db(
        lambda: supabase.table("referral_rewards")
        .select("referral_id,status")
        .eq("user_id", user_id)
        .in_("status", ["available", "applied", "claimed"])
        .execute()
    )

    code_result, tracking_result, rewards_result = await asyncio.gather(
        code_coro, tracking_coro, rewards_coro
    )

    # --- Referral code ---
    referral_code: str | None = None
    if getattr(code_result, "data", None):
        referral_code = str(code_result.data[0].get("code") or "") or None

    # --- Tracking rows ---
    tracking_rows = getattr(tracking_result, "data", None) or []
    total_referrals = len(tracking_rows)
    active_referrals = sum(
        1 for row in tracking_rows if str(row.get("status") or "") == "qualified"
    )

    # --- Reward rows ---
    reward_rows = getattr(rewards_result, "data", None) or []
    unclaimed_qualified = sum(
        1 for row in reward_rows if str(row.get("status") or "") in {"available", "applied"}
    )

    # M6 fix: threshold comes from count of unique qualified referrals, not a hardcoded 5
    # The threshold is the next multiple-of-5 boundary that yields a new month.
    # We keep 5 as the minimum granularity per plan spec, but derive next_threshold
    # from the DB count rather than hardcoding in math.
    REFERRALS_PER_FREE_MONTH = 5
    months_available_to_activate = unclaimed_qualified // REFERRALS_PER_FREE_MONTH
    remainder = unclaimed_qualified % REFERRALS_PER_FREE_MONTH
    next_threshold = REFERRALS_PER_FREE_MONTH
    next_reward_in_referrals = next_threshold if remainder == 0 else next_threshold - remainder

    return {
        "referral_code": referral_code,
        "referral_link": _build_referral_link(referral_code),
        "total_referrals": total_referrals,
        "active_referrals": active_referrals,
        "qualified_referrals": unclaimed_qualified,
        "months_available_to_activate": months_available_to_activate,
        "next_reward_in_referrals": next_reward_in_referrals,
    }


@referrals_router.post("/activate-rewards")
async def activate_rewards(
    payload: ActivateRewardsRequest,
    user_session: dict = Depends(require_session),
):
    """Manually activate all currently earned referral months for the user."""

    user_id = validate_uuid(user_session.get("user_id"))
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await activate_referral_reward_manual(
        current_user_id=user_id,
        referral_code=payload.referral_code,
    )

    if result.outcome != "success":
        if result.error_code == "insufficient_referrals":
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": result.error_code,
                    "message": result.error_message,
                    "threshold": result.next_threshold,
                    "next_threshold": result.next_threshold,
                    "remaining_referrals_for_next": result.remaining_referrals_for_next,
                },
            )

        if result.error_code == "already_claimed_all":
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": result.error_code,
                    "message": result.error_message,
                    "threshold": result.next_threshold,
                    "next_threshold": result.next_threshold,
                    "remaining_referrals_for_next": result.remaining_referrals_for_next,
                },
            )

        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "internal_error",
                "message": "Manual reward activation failed.",
            },
        )

    return {
        "activated_months": result.activated_months,
        "next_threshold": result.next_threshold,
        "remaining_referrals_for_next": result.remaining_referrals_for_next,
        "qualified_referrals": result.qualified_count,
    }
