import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.db import async_db, get_supabase_client
from app.observability.debug import debug_log
from app.referrals.utils import validate_uuid

logger = logging.getLogger(__name__)

REFERRAL_REWARD_EVALUATION_ENABLED_ENV = "REFERRAL_REWARD_EVALUATION_ENABLED"
REFERRAL_REWARD_RPC_ENV = "REFERRAL_REWARD_EVALUATION_RPC_NAME"
DEFAULT_REFERRAL_REWARD_RPC_NAME = "qualify_referral_reward"
DEFAULT_FRAUD_IDENTITY_RPC_NAME = "check_duplicate_payment_identity"
REFERRAL_REWARD_FIXED_HOLD_DAYS = 7


class ReferralRewardEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReferralEvaluationResult:
    outcome: str
    referral_id: str | None = None
    trigger_payment_id: str | None = None


@dataclass(frozen=True)
class FraudDetectionResult:
    blocked: bool
    outcome: str | None = None
    reason: str | None = None
    referral_id: str | None = None


def _is_truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_reward_evaluation_enabled() -> bool:
    return _is_truthy_env(os.getenv(REFERRAL_REWARD_EVALUATION_ENABLED_ENV))


# H1: _validate_uuid removed — use validate_uuid from app.referrals.utils


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _normalize_rpc_result(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        if not data:
            return None
        first = data[0]
        return first if isinstance(first, dict) else None
    if isinstance(data, dict):
        return data
    return None


def _authdbg(message: str, *args: object) -> None:
    debug_log(logger, "referrals", message, *args)


def _extract_security_value(
    row: dict[str, Any],
    *,
    field_name: str,
    audit_key: str,
) -> str:
    direct = str(row.get(field_name) or "").strip()
    if direct:
        return direct

    audit_metadata = row.get("audit_metadata")
    if isinstance(audit_metadata, dict):
        attribution_security = audit_metadata.get("attribution_security")
        if isinstance(attribution_security, dict):
            return str(attribution_security.get(audit_key) or "").strip()

    return ""


async def _get_pending_referral_row(*, supabase: Any, referred_user_id: str) -> dict[str, Any] | None:
    query = await async_db(
        lambda: supabase.table("referral_tracking")
        .select("id,referrer_id,referred_id,registration_ip_prefix,registration_ua_hash,audit_metadata")
        .eq("referred_id", referred_user_id)
        .eq("status", "pending")
        .order("attributed_at", desc=True)
        .limit(1)
        .execute()
    )
    if not query.data:
        return None
    first = query.data[0]
    return first if isinstance(first, dict) else None


async def _get_referrer_signup_security(*, supabase: Any, referrer_id: str) -> tuple[str, str]:
    query = await async_db(
        lambda: supabase.table("referral_tracking")
        .select("registration_ip_prefix,registration_ua_hash,audit_metadata")
        .eq("referred_id", referrer_id)
        .order("attributed_at", desc=True)
        .limit(1)
        .execute()
    )
    if not query.data:
        return ("", "")

    row = query.data[0] if isinstance(query.data[0], dict) else {}
    return (
        _extract_security_value(row, field_name="registration_ip_prefix", audit_key="ip_prefix"),
        _extract_security_value(row, field_name="registration_ua_hash", audit_key="ua_hash"),
    )


async def _get_payment_identity_hash(*, supabase: Any, trigger_payment_id: str) -> str:
    query = await async_db(
        lambda: supabase.table("payment_transactions")
        .select("payment_identity_hash")
        .eq("id", trigger_payment_id)
        .limit(1)
        .execute()
    )
    if not query.data:
        return ""
    first = query.data[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("payment_identity_hash") or "").strip()


async def _has_duplicate_identity_under_referrer(
    *,
    supabase: Any,
    referrer_id: str,
    referred_user_id: str,
    payment_identity_hash: str,
) -> bool:
    """H3 fix: single DB-side JOIN RPC replaces 200-row Python cap.
    
    Uses check_duplicate_payment_identity(referrer_id, hash, exclude_user_id)
    which performs an EXISTS JOIN internally — no row limit, O(1) queries.
    """
    result = await async_db(
        lambda: supabase.rpc(
            DEFAULT_FRAUD_IDENTITY_RPC_NAME,
            {
                "p_referrer_id": referrer_id,
                "p_payment_identity_hash": payment_identity_hash,
                "p_exclude_user_id": referred_user_id,
            },
        ).execute()
    )
    return bool(result.data)


async def fraud_detect_referral_pattern(*, referred_user_id: str, trigger_payment_id: str) -> FraudDetectionResult:
    """Apply deterministic fraud rules before referral reward qualification.

    The checks are read-only and safe to rerun. Any exception handling is done
    by the caller to preserve fail-safe reward evaluation behavior.
    """
    supabase = get_supabase_client()
    pending_referral = await _get_pending_referral_row(supabase=supabase, referred_user_id=referred_user_id)
    if not pending_referral:
        _authdbg("event=referral_fraud_debug step=no_pending_referral referred_user_id=%s", referred_user_id)
        return FraudDetectionResult(blocked=False)

    referral_id = str(pending_referral.get("id") or "").strip() or None
    referrer_id = str(pending_referral.get("referrer_id") or "").strip()
    referred_ip_prefix = _extract_security_value(
        pending_referral,
        field_name="registration_ip_prefix",
        audit_key="ip_prefix",
    )
    referred_ua_hash = _extract_security_value(
        pending_referral,
        field_name="registration_ua_hash",
        audit_key="ua_hash",
    )

    if referrer_id and referred_ip_prefix and referred_ua_hash:
        referrer_ip_prefix, referrer_ua_hash = await _get_referrer_signup_security(
            supabase=supabase,
            referrer_id=referrer_id,
        )
        if referrer_ip_prefix and referrer_ua_hash:
            network_match = referrer_ip_prefix == referred_ip_prefix and referrer_ua_hash == referred_ua_hash
            _authdbg(
                "event=referral_fraud_debug step=same_network_check referral_id=%s referrer_id=%s referred_user_id=%s matched=%s",
                referral_id,
                referrer_id,
                referred_user_id,
                int(network_match),
            )
            if network_match:
                logger.info(
                    "event=referral_fraud_soft_signal reason=same_network referral_id=%s referrer_id=%s referred_user_id=%s trigger_payment_id=%s",
                    referral_id,
                    referrer_id,
                    referred_user_id,
                    trigger_payment_id,
                )

    payment_identity_hash = await _get_payment_identity_hash(
        supabase=supabase,
        trigger_payment_id=trigger_payment_id,
    )
    _authdbg(
        "event=referral_fraud_debug step=identity_hash_check referral_id=%s referred_user_id=%s has_identity_hash=%s",
        referral_id,
        referred_user_id,
        int(bool(payment_identity_hash)),
    )
    if payment_identity_hash and referrer_id:
        duplicate_identity = await _has_duplicate_identity_under_referrer(
            supabase=supabase,
            referrer_id=referrer_id,
            referred_user_id=referred_user_id,
            payment_identity_hash=payment_identity_hash,
        )
        if duplicate_identity:
            outcome = "fraud_blocked_duplicate_identity"
            logger.warning(
                "event=referral_fraud_detected reason=duplicate_payment_identity outcome=%s referral_id=%s referrer_id=%s referred_user_id=%s trigger_payment_id=%s",
                outcome,
                referral_id,
                referrer_id,
                referred_user_id,
                trigger_payment_id,
            )
            return FraudDetectionResult(
                blocked=True,
                outcome=outcome,
                reason="duplicate_payment_identity",
                referral_id=referral_id,
            )

    return FraudDetectionResult(blocked=False)


def _map_rpc_result(*, row: dict[str, Any], trigger_payment_id: str) -> ReferralEvaluationResult:
    result_code = str(row.get("result_code") or "").strip()
    referral_id_value = row.get("referral_id")
    referral_id = str(referral_id_value) if isinstance(referral_id_value, (str, uuid.UUID)) else None
    reward_created = _to_bool(row.get("reward_created"))
    qualified_updated = _to_bool(row.get("qualified_updated"))

    if result_code == "skip_no_pending_referral":
        return ReferralEvaluationResult(
            outcome="skip_no_pending_referral",
            referral_id=referral_id,
            trigger_payment_id=trigger_payment_id,
        )

    if result_code == "skip_not_first_success":
        return ReferralEvaluationResult(
            outcome="skip_not_first_success",
            referral_id=referral_id,
            trigger_payment_id=trigger_payment_id,
        )

    if result_code == "success_reward_created":
        return ReferralEvaluationResult(
            outcome="success_reward_created",
            referral_id=referral_id,
            trigger_payment_id=trigger_payment_id,
        )

    if result_code == "success_already_rewarded_reconciled":
        outcome = "success_already_rewarded_reconciled"
        # Keep idempotent duplicate semantics explicit for downstream logs.
        if not reward_created and not qualified_updated:
            outcome = "success_already_rewarded_reconciled"
        return ReferralEvaluationResult(
            outcome=outcome,
            referral_id=referral_id,
            trigger_payment_id=trigger_payment_id,
        )

    raise ReferralRewardEvaluationError("unexpected_referral_reward_rpc_result")


async def evaluate_referral_reward(*, referred_user_id: str, trigger_payment_id: str) -> ReferralEvaluationResult:
    if not is_reward_evaluation_enabled():
        return ReferralEvaluationResult(outcome="feature_disabled")

    normalized_referred_user_id = validate_uuid(referred_user_id)
    normalized_trigger_payment_id = validate_uuid(trigger_payment_id)
    if not normalized_referred_user_id or not normalized_trigger_payment_id:
        return ReferralEvaluationResult(outcome="skip_invalid_input")

    try:
        fraud_result = await fraud_detect_referral_pattern(
            referred_user_id=normalized_referred_user_id,
            trigger_payment_id=normalized_trigger_payment_id,
        )
        if fraud_result.blocked and fraud_result.outcome:
            return ReferralEvaluationResult(
                outcome=fraud_result.outcome,
                referral_id=fraud_result.referral_id,
                trigger_payment_id=normalized_trigger_payment_id,
            )
    except Exception as fraud_exc:
        logger.warning(
            "event=referral_fraud_detection_error referred_user_id=%s trigger_payment_id=%s error=%s",
            normalized_referred_user_id,
            normalized_trigger_payment_id,
            str(fraud_exc)[:500],
        )

    rpc_name = get_referral_reward_rpc_name()
    supabase = get_supabase_client()
    payload = {
        "referred_user_id": normalized_referred_user_id,
        "trigger_payment_id": normalized_trigger_payment_id,
        "hold_days": REFERRAL_REWARD_FIXED_HOLD_DAYS,
    }

    try:
        response = await async_db(
            lambda: supabase.rpc(rpc_name, payload).execute()
        )
        row = _normalize_rpc_result(getattr(response, "data", None))
        if not row:
            raise ReferralRewardEvaluationError("empty_referral_reward_rpc_result")
        return _map_rpc_result(
            row=row,
            trigger_payment_id=normalized_trigger_payment_id,
        )
    except Exception:
        logger.exception(
            "referral_reward_evaluation_failed rpc=%s referred_user_id=%s",
            rpc_name,
            normalized_referred_user_id,
        )
        return ReferralEvaluationResult(
            outcome="error_controlled",
            trigger_payment_id=normalized_trigger_payment_id,
        )


def get_referral_reward_rpc_name() -> str:
    raw = (os.getenv(REFERRAL_REWARD_RPC_ENV) or DEFAULT_REFERRAL_REWARD_RPC_NAME).strip()
    return raw or DEFAULT_REFERRAL_REWARD_RPC_NAME
