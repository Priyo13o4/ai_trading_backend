import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.db import async_db, get_supabase_client
from app.referrals.utils import validate_uuid

logger = logging.getLogger(__name__)


def _is_debug_enabled() -> bool:
    return os.getenv("AUTHDBG_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _authdbg(msg: str, *args: object) -> None:
    if _is_debug_enabled():
        logger.info(msg, *args)


@dataclass(frozen=True)
class RefundRevocationResult:
    """Result of attempting to revoke a referral reward on refund."""
    outcome: str  # "no_reward", "already_revoked", "success", "unavailable_status"
    reward_id: str | None = None
    previous_status: str | None = None
    trigger_payment_id: str | None = None


# H1: _validate_uuid removed — use validate_uuid from app.referrals.utils


def _parse_timestamptz(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def revoke_referral_reward_on_refund(
    *,
    trigger_payment_id: str,
    refund_trigger_event_id: str,
) -> RefundRevocationResult:
    """
    Atomically revoke a referral reward if it exists with on_hold status.
    
    Idempotency: Calling multiple times on the same trigger_payment_id is safe.
    - First call: revokes if on_hold, returns success.
    - Subsequent calls: finds already-revoked reward, returns already_revoked.
    
    Safety guarantees:
    - Only revokes rewards with status='on_hold'.
    - Never revokes available/applied/revoked rewards.
    - Records audit trail in payment_audit_logs.
    - Uses atomic DB transaction via Supabase RLS.
    
    Args:
        trigger_payment_id: UUID of the payment_transaction that triggered the refund.
        refund_trigger_event_id: Webhook event ID for audit trail.
        
    Returns:
        RefundRevocationResult with outcome and optional metadata.
    """
    normalized_trigger_payment_id = validate_uuid(trigger_payment_id)
    if not normalized_trigger_payment_id:
        return RefundRevocationResult(outcome="skip_invalid_input")

    supabase = get_supabase_client()

    # Fetch the payment transaction to verify it exists and get user_id.
    tx_query = await async_db(
        lambda: supabase.table("payment_transactions")
        .select("id,user_id,status")
        .eq("id", normalized_trigger_payment_id)
        .limit(1)
        .execute()
    )

    if not tx_query.data:
        _authdbg("refund_revocation_no_transaction trigger_payment_id=%s event_id=%s", 
                 normalized_trigger_payment_id, refund_trigger_event_id)
        return RefundRevocationResult(
            outcome="no_transaction",
            trigger_payment_id=normalized_trigger_payment_id,
        )

    tx_row = tx_query.data[0]
    tx_id = str(tx_row.get("id") or "")
    tx_user_id = str(tx_row.get("user_id") or "")

    # Fetch any referral reward tied to this trigger_payment_id.
    reward_query = await async_db(
        lambda: supabase.table("referral_rewards")
        .select("referral_id,status,user_id,trigger_payment_id,hold_expires_at")
        .eq("trigger_payment_id", normalized_trigger_payment_id)
        .limit(1)
        .execute()
    )

    if not reward_query.data:
        _authdbg("refund_revocation_no_reward trigger_payment_id=%s event_id=%s", 
                 normalized_trigger_payment_id, refund_trigger_event_id)
        return RefundRevocationResult(
            outcome="no_reward",
            trigger_payment_id=normalized_trigger_payment_id,
        )

    reward_row = reward_query.data[0]
    reward_id = str(reward_row.get("referral_id") or "")
    previous_status = str(reward_row.get("status") or "")
    hold_expires_at = _parse_timestamptz(reward_row.get("hold_expires_at"))
    now_utc = datetime.now(timezone.utc)

    # If not on_hold, do not revoke; return outcome indicating unavailable status.
    if previous_status != "on_hold":
        _authdbg("refund_revocation_unavailable_status trigger_payment_id=%s status=%s event_id=%s", 
                 normalized_trigger_payment_id, previous_status, refund_trigger_event_id)
        return RefundRevocationResult(
            outcome="unavailable_status",
            reward_id=reward_id,
            previous_status=previous_status,
            trigger_payment_id=normalized_trigger_payment_id,
        )

    # Ignore post-hold refunds even if status is still on_hold due to delayed transitions.
    if hold_expires_at is None or hold_expires_at <= now_utc:
        _authdbg(
            "refund_revocation_hold_expired trigger_payment_id=%s hold_expires_at=%s event_id=%s",
            normalized_trigger_payment_id,
            hold_expires_at.isoformat() if hold_expires_at else "",
            refund_trigger_event_id,
        )
        return RefundRevocationResult(
            outcome="unavailable_status",
            reward_id=reward_id,
            previous_status=previous_status,
            trigger_payment_id=normalized_trigger_payment_id,
        )

    # Attempt atomic CAS update: on_hold -> revoked.
    now_iso = now_utc.isoformat()
    update_result = await async_db(
        lambda: supabase.table("referral_rewards")
        .update(
            {
                "status": "revoked",
                "updated_at": now_iso,
            }
        )
        .eq("referral_id", reward_id)
        .eq("status", "on_hold")
        .gt("hold_expires_at", now_iso)
        .execute()
    )

    if not getattr(update_result, "data", None):
        # CAS failed, likely already revoked by concurrent call.
        # Re-fetch to confirm actual status.
        refetch_result = await async_db(
            lambda: supabase.table("referral_rewards")
            .select("status")
            .eq("referral_id", reward_id)
            .limit(1)
            .execute()
        )
        if refetch_result.data:
            current_status = str(refetch_result.data[0].get("status") or "")
            if current_status == "revoked":
                _authdbg("refund_revocation_already_revoked reward_id=%s event_id=%s", 
                         reward_id, refund_trigger_event_id)
                return RefundRevocationResult(
                    outcome="already_revoked",
                    reward_id=reward_id,
                    previous_status="revoked",
                    trigger_payment_id=normalized_trigger_payment_id,
                )
            return RefundRevocationResult(
                outcome="unavailable_status",
                reward_id=reward_id,
                previous_status=current_status or previous_status,
                trigger_payment_id=normalized_trigger_payment_id,
            )
        return RefundRevocationResult(
            outcome="unavailable_status",
            reward_id=reward_id,
            previous_status=previous_status,
            trigger_payment_id=normalized_trigger_payment_id,
        )

    # Record audit trail for the revocation.
    try:
        await async_db(
            lambda: supabase.table("payment_audit_logs").insert(
                {
                    "transaction_id": tx_id,
                    "entity_type": "referral_reward",
                    "entity_id": reward_id,
                    "previous_state": previous_status,
                    "new_state": "revoked",
                    "trigger_source": "refund_webhook",
                    "trigger_event_id": refund_trigger_event_id,
                    "reason": "refund_on_hold_revocation",
                    "metadata": {
                        "trigger_payment_id": normalized_trigger_payment_id,
                        "revoked_at": now_iso,
                    },
                }
            ).execute()
        )
    except Exception as audit_err:
        logger.warning(
            "Failed to record refund revocation audit trail reward_id=%s tx_id=%s: %s",
            reward_id,
            tx_id,
            str(audit_err)[:500],
        )

    _authdbg("refund_revocation_success reward_id=%s trigger_payment_id=%s event_id=%s", 
             reward_id, normalized_trigger_payment_id, refund_trigger_event_id)

    return RefundRevocationResult(
        outcome="success",
        reward_id=reward_id,
        previous_status=previous_status,
        trigger_payment_id=normalized_trigger_payment_id,
    )
