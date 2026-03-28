"""Manual referral reward activation service (Scope D).

Implements the backend service layer for manually activating earned referral
months once users cross the configured threshold.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import async_db, get_supabase_client
from app.referrals.utils import validate_uuid

logger = logging.getLogger(__name__)


def _is_debug_enabled() -> bool:
    return os.getenv("AUTHDBG_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}

REFERRAL_MANUAL_ACTIVATION_RPC_ENV = "REFERRAL_REWARD_MANUAL_ACTIVATION_RPC_NAME"
DEFAULT_REFERRAL_MANUAL_ACTIVATION_RPC_NAME = "activate_referral_reward_manual"


def _authdbg(msg: str, *args: object) -> None:
    if _is_debug_enabled():
        logger.info("AUTHDBG " + msg, *args)


# H1: _validate_uuid removed — use validate_uuid from app.referrals.utils


def _normalize_rpc_result(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        if not data:
            return None
        first = data[0]
        return first if isinstance(first, dict) else None
    if isinstance(data, dict):
        return data
    return None


def _normalize_uuid_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    normalized: list[str] = []
    for raw_value in values:
        normalized_value = validate_uuid(str(raw_value) if raw_value is not None else None)
        if normalized_value:
            normalized.append(normalized_value)
    return normalized


def get_manual_activation_rpc_name() -> str:
    raw = (os.getenv(REFERRAL_MANUAL_ACTIVATION_RPC_ENV) or DEFAULT_REFERRAL_MANUAL_ACTIVATION_RPC_NAME).strip()
    return raw or DEFAULT_REFERRAL_MANUAL_ACTIVATION_RPC_NAME


@dataclass(frozen=True)
class ManualActivationResult:
    """Result of manual referral reward activation attempt."""

    outcome: str
    activated_months: int = 0
    qualified_count: int = 0
    next_threshold: int = 5
    remaining_referrals_for_next: int = 5
    claimed_reward_ids: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


async def activate_referral_reward_manual(*, current_user_id: str, referral_code: str | None = None) -> ManualActivationResult:
    """Activate earned referral months for the current user.

    Args:
        current_user_id: Authenticated user UUID.
        referral_code: Optional referral code from request payload (reserved for future use).

    Returns:
        ManualActivationResult with activation and threshold details.
    """

    normalized_user_id = validate_uuid(current_user_id)
    if not normalized_user_id:
        return ManualActivationResult(
            outcome="error",
            error_code="internal_error",
            error_message="invalid_user_id",
        )

    rpc_name = get_manual_activation_rpc_name()
    supabase = get_supabase_client()
    payload = {
        "p_user_id": normalized_user_id,
        "p_referral_code": referral_code,
    }

    try:
        response = await async_db(lambda: supabase.rpc(rpc_name, payload).execute())
        row = _normalize_rpc_result(getattr(response, "data", None))
        if not row:
            raise RuntimeError("empty_manual_activation_result")

        result_code = str(row.get("result_code") or "").strip().lower()
        activated_months = max(int(row.get("activated_months") or 0), 0)
        qualified_count = max(int(row.get("qualified_count") or 0), 0)
        next_threshold = max(int(row.get("next_threshold") or 5), 5)
        remaining_referrals_for_next = max(int(row.get("remaining_referrals_for_next") or 5), 0)
        claimed_reward_ids = _normalize_uuid_list(row.get("claimed_reward_ids"))

        logger.info(
            "event=referral_activation_result",
            extra={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "referral_activation_result",
                "user_id": normalized_user_id,
                "result_code": result_code,
                "qualified_count": qualified_count,
                "activated_months": activated_months,
                "claimed_count": len(claimed_reward_ids),
            },
        )
        _authdbg(
            "event=referral_activation_decision user_id=%s result_code=%s qualified_count=%s activated_months=%s next_threshold=%s remaining_referrals_for_next=%s",
            normalized_user_id,
            result_code,
            qualified_count,
            activated_months,
            next_threshold,
            remaining_referrals_for_next,
        )

        if result_code == "success":
            return ManualActivationResult(
                outcome="success",
                activated_months=activated_months,
                qualified_count=qualified_count,
                next_threshold=next_threshold,
                remaining_referrals_for_next=remaining_referrals_for_next,
                claimed_reward_ids=claimed_reward_ids,
            )

        if result_code == "insufficient_referrals":
            return ManualActivationResult(
                outcome="error",
                error_code="insufficient_referrals",
                error_message="Not enough qualified referrals to activate a free month yet.",
                activated_months=0,
                qualified_count=qualified_count,
                next_threshold=next_threshold,
                remaining_referrals_for_next=remaining_referrals_for_next,
            )

        if result_code == "already_claimed_all":
            return ManualActivationResult(
                outcome="error",
                error_code="already_claimed_all",
                error_message="All currently earned referral months are already claimed.",
                activated_months=0,
                qualified_count=qualified_count,
                next_threshold=next_threshold,
                remaining_referrals_for_next=remaining_referrals_for_next,
            )

        return ManualActivationResult(
            outcome="error",
            error_code="internal_error",
            error_message="unexpected_manual_activation_result",
            activated_months=activated_months,
            qualified_count=qualified_count,
            next_threshold=next_threshold,
            remaining_referrals_for_next=remaining_referrals_for_next,
            claimed_reward_ids=claimed_reward_ids,
        )
    except Exception:
        logger.exception(
            "event=referral_activation_failed user_id=%s rpc=%s",
            normalized_user_id,
            rpc_name,
        )
        return ManualActivationResult(
            outcome="error",
            error_code="internal_error",
            error_message="manual_activation_failed",
        )
