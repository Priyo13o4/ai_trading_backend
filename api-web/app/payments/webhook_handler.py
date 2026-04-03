import logging
import os
import json
import hashlib
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Header, HTTPException
from typing import Optional, Any

from app.observability.debug import debug_log
from app.db import get_supabase_client, async_db
from app.config.retry_policies import get_provider_webhook_policy
from app.notifications.dead_letter import notify_dead_letter
from app.redis_cache import CACHE_REDIS
from app.payments.payment_providers.router import get_provider
from app.payments.constants import PaymentTransactionStatus
from app.referrals.reward_evaluator import evaluate_referral_reward
from app.referrals.reward_revocation import revoke_referral_reward_on_refund

logger = logging.getLogger(__name__)

WEBHOOK_CACHE_HINT_TTL_SECONDS = int((os.getenv("WEBHOOK_CACHE_HINT_TTL_SECONDS") or "86400").strip() or "86400")
WEBHOOK_PROCESSING_LEASE_SECONDS = int((os.getenv("WEBHOOK_PROCESSING_LEASE_SECONDS") or "300").strip() or "300")


def _plisio_debug(msg: str, *args: object) -> None:
    debug_log(logger, "payments.plisio", msg, *args)


def _normalize_upper(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _normalize_plisio_currency(value: Any) -> str:
    return _normalize_upper(value).replace("-", "_").replace(" ", "")


def _as_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _webhook_now() -> datetime:
    return datetime.utcnow()


async def _fetch_webhook_event(
    supabase,
    provider_name: str,
    event_id: str,
) -> Optional[dict]:
    query = await async_db(
        lambda: supabase.table("webhook_events")
        .select("*")
        .eq("provider", provider_name)
        .eq("event_id", event_id)
        .limit(1)
        .execute()
    )
    if query.data:
        return query.data[0]
    return None


def _is_webhook_processing_stale(event_row: Optional[dict], lease_seconds: int) -> bool:
    if not event_row or not event_row.get("processing"):
        return False

    raw_started_at = str(event_row.get("processing_started_at") or "").strip()
    if not raw_started_at:
        return True

    try:
        started_at = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
    except Exception:
        return True

    if started_at.tzinfo is not None:
        started_at = started_at.astimezone(tz=None).replace(tzinfo=None)

    return started_at < (_webhook_now() - timedelta(seconds=lease_seconds))


async def _set_webhook_cache_hint(redis_key: str) -> None:
    await CACHE_REDIS.set(redis_key, "processed", ex=WEBHOOK_CACHE_HINT_TTL_SECONDS)


async def _mark_webhook_processed(
    supabase,
    provider_name: str,
    event_id: str,
    *,
    processing_error: Optional[str] = None,
) -> None:
    now_iso = _webhook_now().isoformat()
    update_payload = {
        "processed": True,
        "processed_at": now_iso,
        "processing": False,
        "processing_started_at": None,
        "last_error": processing_error,
        "processing_error": processing_error,
    }
    await async_db(
        lambda: supabase.table("webhook_events")
        .update(update_payload)
        .eq("provider", provider_name)
        .eq("event_id", event_id)
        .execute()
    )


async def _record_webhook_failure(
    supabase,
    provider_name: str,
    event_id: str,
    event_row: dict,
    exc: Exception,
) -> bool:
    policy = get_provider_webhook_policy(provider_name)
    retry_count = int(event_row.get("retry_count") or 0)
    next_retry_count = retry_count + 1
    now = _webhook_now()
    error_message = str(exc)[:500]
    base_payload = {
        "processing": False,
        "processing_started_at": None,
        "retry_count": next_retry_count,
        "last_error": error_message,
        "processing_error": error_message,
    }

    if next_retry_count >= policy.max_retries:
        await async_db(
            lambda: supabase.table("webhook_events")
            .update(
                {
                    **base_payload,
                    "processed": True,
                    "processed_at": now.isoformat(),
                    "next_retry_at": now.isoformat(),
                }
            )
            .eq("provider", provider_name)
            .eq("event_id", event_id)
            .execute()
        )
        await notify_dead_letter(
            {
                **event_row,
                "retry_count": next_retry_count,
                "last_error": error_message,
                "processed": True,
                "processed_at": now.isoformat(),
            },
            exc,
        )
        return True

    backoff_seconds = policy.calculate_backoff(retry_count)
    next_retry_at = now + timedelta(seconds=backoff_seconds)
    await async_db(
        lambda: supabase.table("webhook_events")
        .update(
            {
                **base_payload,
                "processed": False,
                "processed_at": None,
                "next_retry_at": next_retry_at.isoformat(),
            }
        )
        .eq("provider", provider_name)
        .eq("event_id", event_id)
        .execute()
    )
    return False


async def process_claimed_webhook_event(event_row: dict) -> None:
    """Process a webhook row that was already atomically claimed by RPC."""
    provider_name = str(event_row.get("provider") or "").strip().lower()
    event_id = str(event_row.get("event_id") or "").strip()
    event_type = event_row.get("event_type")
    payload = event_row.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if not provider_name or not event_id:
        logger.warning("Skipping malformed claimed webhook row: provider=%s event_id=%s", provider_name, event_id)
        return

    supabase = get_supabase_client()
    redis_key = f"webhook_event:{provider_name}:{event_id}"

    try:
        provider = get_provider(provider_name)

        new_status = provider.map_event_to_state(event_type)
        if provider_name == "plisio":
            _plisio_debug(
                "PLISIO_CALLBACK status_mapped event_id=%s provider_status=%s mapped_status=%s",
                event_id,
                event_type,
                new_status.value if new_status else None,
            )
        if not new_status:
            logger.info("Ignoring unmapped event type %s for %s", event_type, provider_name)
            await _mark_webhook_processed(supabase, provider_name, event_id)
            await _set_webhook_cache_hint(redis_key)
            return

        provider_payment_id = None

        if provider_name == "razorpay":
            payload_entity = payload.get("payload", {})
            if "subscription" in payload_entity:
                sub_obj = payload_entity["subscription"]["entity"]
                provider_payment_id = sub_obj.get("id")
        elif provider_name == "plisio":
            provider_payment_id = str(
                payload.get("order_number")
                or payload.get("invoice_id")
                or payload.get("txn_id")
                or payload.get("id")
                or ""
            )

        if provider_name == "plisio":
            _plisio_debug(
                "PLISIO_CALLBACK correlation event_id=%s provider_payment_id=%s",
                event_id,
                provider_payment_id,
            )

        if not provider_payment_id:
            error_message = "Missing provider_payment_id"
            logger.error("Could not map webhook event to a payment ID: %s", event_id)
            await _mark_webhook_processed(
                supabase,
                provider_name,
                event_id,
                processing_error=error_message,
            )
            await _set_webhook_cache_hint(redis_key)
            return

        tx_query = await async_db(
            lambda: supabase.table("payment_transactions")
            .select("*")
            .eq("provider_payment_id", provider_payment_id)
            .eq("provider", provider_name)
            .execute()
        )

        if not tx_query.data:
            logger.warning("Transaction not found for provider_payment_id %s", provider_payment_id)
            raise HTTPException(status_code=404, detail="Transaction not found for provider payment ID. Deferring to retry.")

        transaction = tx_query.data[0]
        tx_id = transaction["id"]
        tx_user_id = transaction["user_id"]

        if provider_name == "plisio":
            _plisio_debug(
                "PLISIO_CALLBACK tx_found event_id=%s tx_id=%s user_id=%s previous_status=%s",
                event_id,
                tx_id,
                tx_user_id,
                transaction.get("status"),
            )

            tx_metadata = transaction.get("metadata", {})
            if not isinstance(tx_metadata, dict):
                tx_metadata = {}

            expected_crypto = _normalize_upper(tx_metadata.get("plisio_expected_currency"))
            expected_source = _normalize_upper(tx_metadata.get("plisio_expected_source_currency") or "USD")
            callback_crypto = _normalize_upper(payload.get("currency"))
            callback_source = _normalize_upper(payload.get("source_currency"))

            expected_crypto_norm = _normalize_plisio_currency(expected_crypto)
            callback_crypto_norm = _normalize_plisio_currency(callback_crypto)

            crypto_mismatch = bool(expected_crypto_norm and callback_crypto_norm != expected_crypto_norm)
            source_mismatch = bool(expected_source and callback_source != expected_source)
            if crypto_mismatch or source_mismatch:
                mismatch_reason = (
                    f"Plisio callback currency mismatch: expected crypto={expected_crypto or '-'} "
                    f"source={expected_source or '-'} got crypto={callback_crypto or '-'} "
                    f"source={callback_source or '-'}"
                )
                logger.warning("%s event_id=%s tx_id=%s", mismatch_reason, event_id, tx_id)

                await async_db(
                    lambda: supabase.table("payment_audit_logs").insert(
                        {
                            "transaction_id": tx_id,
                            "entity_type": "payment_transaction",
                            "entity_id": tx_id,
                            "previous_state": transaction.get("status"),
                            "new_state": transaction.get("status"),
                            "trigger_source": "plisio_webhook_currency_guard",
                            "trigger_event_id": event_id,
                            "reason": "currency_mismatch",
                            "metadata": {
                                "expected_crypto": expected_crypto,
                                "expected_source": expected_source,
                                "callback_crypto": callback_crypto,
                                "callback_source": callback_source,
                            },
                        }
                    ).execute()
                )

                await _mark_webhook_processed(
                    supabase,
                    provider_name,
                    event_id,
                    processing_error=mismatch_reason,
                )
                await _set_webhook_cache_hint(redis_key)
                return

        amount_mismatch_reason = None
        if provider_name == "plisio" and new_status == PaymentTransactionStatus.SUCCEEDED:
            tx_metadata = transaction.get("metadata", {})
            if not isinstance(tx_metadata, dict):
                tx_metadata = {}

            provider_checkout_data = tx_metadata.get("provider_checkout_data")
            if not isinstance(provider_checkout_data, dict):
                provider_checkout_data = {}

            expected_crypto_amount = (
                _as_decimal(tx_metadata.get("plisio_expected_amount"))
                or _as_decimal(provider_checkout_data.get("amount"))
            )
            expected_source_amount = (
                _as_decimal(tx_metadata.get("plisio_expected_source_amount"))
                or _as_decimal(provider_checkout_data.get("source_amount"))
            )

            callback_crypto_amount = _as_decimal(payload.get("amount"))
            callback_source_amount = _as_decimal(payload.get("source_amount"))

            tolerance = _as_decimal(os.getenv("PLISIO_CALLBACK_AMOUNT_TOLERANCE") or "0.000001") or Decimal("0.000001")
            mismatches = []

            if (
                expected_crypto_amount is not None
                and callback_crypto_amount is not None
                and abs(callback_crypto_amount - expected_crypto_amount) > tolerance
            ):
                mismatches.append(f"amount expected={expected_crypto_amount} got={callback_crypto_amount}")

            if (
                expected_source_amount is not None
                and callback_source_amount is not None
                and abs(callback_source_amount - expected_source_amount) > tolerance
            ):
                mismatches.append(f"source_amount expected={expected_source_amount} got={callback_source_amount}")

            if mismatches:
                amount_mismatch_reason = "; ".join(mismatches)
                logger.warning(
                    "Plisio callback amount mismatch event_id=%s tx_id=%s details=%s",
                    event_id,
                    tx_id,
                    amount_mismatch_reason,
                )

                await async_db(
                    lambda: supabase.table("payment_audit_logs").insert(
                        {
                            "transaction_id": tx_id,
                            "entity_type": "payment_transaction",
                            "entity_id": tx_id,
                            "previous_state": transaction.get("status"),
                            "new_state": transaction.get("status"),
                            "trigger_source": "plisio_webhook_amount_guard",
                            "trigger_event_id": event_id,
                            "reason": "amount_mismatch",
                            "metadata": {
                                "details": amount_mismatch_reason,
                                "expected_crypto_amount": str(expected_crypto_amount)
                                if expected_crypto_amount is not None
                                else None,
                                "callback_crypto_amount": str(callback_crypto_amount)
                                if callback_crypto_amount is not None
                                else None,
                                "expected_source_amount": str(expected_source_amount)
                                if expected_source_amount is not None
                                else None,
                                "callback_source_amount": str(callback_source_amount)
                                if callback_source_amount is not None
                                else None,
                                "tolerance": str(tolerance),
                            },
                        }
                    ).execute()
                )

        is_recurring_charge = payload.get("is_recurring_charge") or (
            provider_name == "razorpay" and event_type == "subscription.charged"
        )
        previous_status = transaction["status"]
        allow_plisio_late_success = (
            provider_name == "plisio"
            and previous_status == PaymentTransactionStatus.EXPIRED.value
            and new_status == PaymentTransactionStatus.SUCCEEDED
        )
        allow_refund_on_terminal = (
            new_status == PaymentTransactionStatus.REFUNDED
            and previous_status != PaymentTransactionStatus.REFUNDED.value
        )
        terminal_statuses = {
            PaymentTransactionStatus.SUCCEEDED.value,
            PaymentTransactionStatus.FAILED.value,
            PaymentTransactionStatus.EXPIRED.value,
            PaymentTransactionStatus.CANCELLED.value,
            PaymentTransactionStatus.REFUNDED.value,
        }
        status_updated = False
        latest_status = previous_status

        if is_recurring_charge or allow_plisio_late_success or allow_refund_on_terminal or previous_status not in terminal_statuses:
            update_result = await async_db(
                lambda: supabase.table("payment_transactions")
                .update(
                    {
                        "status": new_status.value,
                        "last_provider_event_time": datetime.utcnow().isoformat(),
                    }
                )
                .eq("id", tx_id)
                .eq("status", previous_status)
                .execute()
            )

            status_updated = bool(getattr(update_result, "data", None))

            refetched_rows = (
                await async_db(
                    lambda: supabase.table("payment_transactions")
                    .select("id,status")
                    .eq("id", tx_id)
                    .limit(1)
                    .execute()
                )
            ).data or []
            if refetched_rows:
                latest_status = str(refetched_rows[0].get("status") or "")

            if not status_updated and latest_status != new_status.value:
                logger.info(
                    "CAS update skipped for tx=%s event_id=%s expected_previous=%s latest=%s target=%s",
                    tx_id,
                    event_id,
                    previous_status,
                    latest_status,
                    new_status.value,
                )

            if provider_name == "plisio":
                _plisio_debug(
                    "PLISIO_CALLBACK tx_update_attempt event_id=%s tx_id=%s previous_status=%s target_status=%s applied=%s latest_status=%s",
                    event_id,
                    tx_id,
                    previous_status,
                    new_status.value,
                    status_updated,
                    latest_status,
                )

            if status_updated:
                await async_db(
                    lambda: supabase.table("payment_audit_logs").insert(
                        {
                            "transaction_id": tx_id,
                            "entity_type": "payment_transaction",
                            "entity_id": tx_id,
                            "previous_state": previous_status,
                            "new_state": new_status.value,
                            "trigger_source": f"{provider_name}_webhook",
                            "trigger_event_id": event_id,
                        }
                    ).execute()
                )

            effective_succeeded = (
                new_status == PaymentTransactionStatus.SUCCEEDED
                and latest_status == PaymentTransactionStatus.SUCCEEDED.value
            )
            effective_refunded = (
                new_status == PaymentTransactionStatus.REFUNDED
                and latest_status == PaymentTransactionStatus.REFUNDED.value
            )

            if new_status == PaymentTransactionStatus.SUCCEEDED and status_updated:
                tx_metadata = transaction.get("metadata", {})
                if not isinstance(tx_metadata, dict):
                    tx_metadata = {}

                if provider_name == "plisio" and amount_mismatch_reason:
                    logger.warning(
                        "Blocking subscription activation due to amount mismatch event_id=%s tx_id=%s",
                        event_id,
                        tx_id,
                    )
                    await async_db(
                        lambda: supabase.table("payment_audit_logs").insert(
                            {
                                "transaction_id": tx_id,
                                "entity_type": "payment_transaction",
                                "entity_id": tx_id,
                                "previous_state": new_status.value,
                                "new_state": new_status.value,
                                "trigger_source": f"{provider_name}_webhook",
                                "trigger_event_id": event_id,
                                "reason": "activation_blocked_amount_mismatch",
                                "metadata": {
                                    "details": amount_mismatch_reason,
                                },
                            }
                        ).execute()
                    )
                else:
                    subscription_id = transaction.get("subscription_id")
                    renewal_requested = provider_name == "plisio" and (
                        bool(tx_metadata.get("renewal_intent")) or bool(subscription_id)
                    )
                    subscription_state_changed = False

                    try:
                        if renewal_requested:
                            if not subscription_id:
                                raise ValueError(f"renewal transaction {tx_id} missing subscription_id")

                            existing_sub = await async_db(
                                lambda: supabase.table("user_subscriptions")
                                .select("id,status")
                                .eq("id", subscription_id)
                                .limit(1)
                                .execute()
                            )

                            existing_sub_data = existing_sub.data[0] if existing_sub.data else None
                            if existing_sub_data and existing_sub_data.get("status") == "active":
                                logger.info(
                                    "Subscription already active, skipping renewal side effects tx=%s subscription=%s",
                                    tx_id,
                                    subscription_id,
                                )
                                subscription_state_changed = False
                            else:
                                renew_response = await async_db(
                                    lambda: supabase.rpc(
                                        "renew_subscription",
                                        {
                                            "p_subscription_id": subscription_id,
                                            "p_payment_id": tx_id,
                                        },
                                    ).execute()
                                )
                                if renew_response.data is not True:
                                    raise ValueError(
                                        f"renew_subscription returned non-true for tx={tx_id} subscription={subscription_id} payload={renew_response.data}"
                                    )
                                subscription_state_changed = True
                        else:
                            plan_id = tx_metadata.get("plan_id")
                            if plan_id:
                                try:
                                    uuid.UUID(str(plan_id))
                                except (ValueError, TypeError):
                                    plan_lookup = await async_db(
                                        lambda: supabase.table("subscription_plans")
                                        .select("id")
                                        .eq("name", plan_id)
                                        .execute()
                                    )
                                    if plan_lookup.data:
                                        plan_id = plan_lookup.data[0]["id"]
                                    else:
                                        raise ValueError(f"Could not resolve plan name '{plan_id}' to a UUID")

                            if not plan_id:
                                raise ValueError(
                                    f"first-time transaction {tx_id} missing plan_id for subscription creation"
                                )

                            sub_response = await async_db(
                                lambda: supabase.rpc(
                                    "create_subscription",
                                    {
                                        "p_user_id": tx_user_id,
                                        "p_plan_id": plan_id,
                                        "p_payment_provider": provider_name,
                                        "p_external_id": provider_payment_id,
                                        "p_trial_days": 0,
                                    },
                                ).execute()
                            )

                            new_sub_id = sub_response.data
                            if not new_sub_id:
                                raise ValueError(f"create_subscription returned empty result for tx={tx_id}")

                            await async_db(
                                lambda: supabase.table("payment_transactions")
                                .update({"subscription_id": new_sub_id})
                                .eq("id", tx_id)
                                .execute()
                            )

                            if provider_name == "plisio":
                                _plisio_debug(
                                    "PLISIO_CALLBACK subscription_created event_id=%s tx_id=%s subscription_id=%s",
                                    event_id,
                                    tx_id,
                                    new_sub_id,
                                )

                            subscription_state_changed = True

                        if subscription_state_changed:
                            from app.authn.session_store import update_all_sessions_for_user_perms

                            await update_all_sessions_for_user_perms(
                                tx_user_id,
                                plan="core",
                                permissions=["active_subscriber"],
                            )
                    except Exception as sub_err:
                        logger.error(
                            "Failed to finalize subscription activation for tx %s user %s: %s",
                            tx_id,
                            tx_user_id,
                            sub_err,
                        )
                        retry_metadata = dict(tx_metadata)
                        retry_metadata["activation_retry_required"] = True
                        retry_metadata["activation_retry_updated_at"] = datetime.utcnow().isoformat()
                        retry_metadata["activation_retry_last_error"] = str(sub_err)[:500]

                        await async_db(
                            lambda: supabase.table("payment_transactions")
                            .update(
                                {
                                    "metadata": retry_metadata,
                                    "updated_at": datetime.utcnow().isoformat(),
                                }
                            )
                            .eq("id", tx_id)
                            .execute()
                        )

                        await async_db(
                            lambda: supabase.table("payment_audit_logs").insert(
                                {
                                    "transaction_id": tx_id,
                                    "entity_type": "payment_transaction",
                                    "entity_id": tx_id,
                                    "previous_state": new_status.value,
                                    "new_state": new_status.value,
                                    "trigger_source": f"{provider_name}_webhook",
                                    "trigger_event_id": event_id,
                                    "reason": "subscription_activation_retry_required",
                                    "metadata": {
                                        "retry_required": True,
                                        "error": str(sub_err)[:500],
                                    },
                                }
                            ).execute()
                        )

            if effective_succeeded and tx_id and tx_user_id:
                try:
                    referral_eval_result = await evaluate_referral_reward(
                        referred_user_id=tx_user_id,
                        trigger_payment_id=tx_id,
                    )
                    referral_outcome = str(getattr(referral_eval_result, "outcome", "") or "")
                    referral_id_value = getattr(referral_eval_result, "referral_id", None)
                    if referral_outcome == "fraud_blocked_duplicate_identity":
                        logger.warning(
                            "event=referral_fraud_gate_blocked provider=%s event_id=%s tx_id=%s user_id=%s outcome=%s referral_id=%s",
                            provider_name,
                            event_id,
                            tx_id,
                            tx_user_id,
                            referral_outcome,
                            referral_id_value,
                        )
                    logger.info(
                        "event=referral_reward_evaluation_result provider=%s event_id=%s tx_id=%s user_id=%s outcome=%s referral_id=%s",
                        provider_name,
                        event_id,
                        tx_id,
                        tx_user_id,
                        referral_outcome,
                        referral_id_value,
                    )
                except Exception as referral_err:
                    logger.warning(
                        "event=referral_reward_evaluation_failed provider=%s event_id=%s tx_id=%s user_id=%s error=%s",
                        provider_name,
                        event_id,
                        tx_id,
                        tx_user_id,
                        str(referral_err)[:500],
                    )

            if effective_refunded and tx_id:
                try:
                    refund_revocation_result = await revoke_referral_reward_on_refund(
                        trigger_payment_id=tx_id,
                        refund_trigger_event_id=event_id,
                    )
                    logger.info(
                        "event=refund_revocation_result provider=%s event_id=%s tx_id=%s outcome=%s reward_id=%s previous_status=%s",
                        provider_name,
                        event_id,
                        tx_id,
                        refund_revocation_result.outcome,
                        refund_revocation_result.reward_id,
                        refund_revocation_result.previous_status,
                    )
                except Exception as revoke_err:
                    logger.warning(
                        "event=refund_revocation_failed provider=%s event_id=%s tx_id=%s error=%s",
                        provider_name,
                        event_id,
                        tx_id,
                        str(revoke_err)[:500],
                    )

        await _mark_webhook_processed(supabase, provider_name, event_id)
        await _set_webhook_cache_hint(redis_key)

        if provider_name == "plisio":
            _plisio_debug("PLISIO_CALLBACK completed event_id=%s tx_id=%s", event_id, tx_id)
    except HTTPException as exc:
        if exc.status_code in {404, 500, 503}:
            dead_lettered = await _record_webhook_failure(
                supabase,
                provider_name,
                event_id,
                event_row,
                exc,
            )
            if dead_lettered:
                logger.warning("Webhook dead-lettered after retries: provider=%s event_id=%s", provider_name, event_id)
            return

        await _mark_webhook_processed(
            supabase,
            provider_name,
            event_id,
            processing_error=str(exc)[:500],
        )
        logger.error("Webhook failed with non-retryable HTTP error provider=%s event_id=%s: %s", provider_name, event_id, exc)
    except Exception as exc:
        logger.error("Error processing claimed webhook provider=%s event_id=%s: %s", provider_name, event_id, exc)
        await _record_webhook_failure(
            supabase,
            provider_name,
            event_id,
            event_row,
            exc,
        )


webhook_router = APIRouter()

# Note: /api/webhooks/{provider_name} paths must be exempt from CSRF in main.py
@webhook_router.post("/api/webhooks/{provider_name}")
async def handle_webhook(
    provider_name: str,
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None),
):
    """Ingestion-first webhook endpoint: verify, queue in DB, return immediately."""
    try:
        provider = get_provider(provider_name)
    except HTTPException:
        return {"status": "ignored"}

    raw_body = await request.body()

    if provider_name == "plisio":
        _plisio_debug(
            "PLISIO_CALLBACK incoming path=%s payload_bytes=%s",
            str(request.url.path),
            len(raw_body or b""),
        )

    signature = None
    if provider_name == "razorpay":
        signature = x_razorpay_signature
        if not signature:
            logger.warning("Missing signature for %s webhook", provider_name)
            raise HTTPException(status_code=400, detail="Missing signature")
    elif provider_name == "plisio":
        signature = ""
    else:
        signature = ""

    is_valid = await provider.verify_webhook_signature(raw_body, signature)
    if not is_valid:
        logger.error("Invalid webhook signature for %s", provider_name)
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if provider_name == "plisio":
        _plisio_debug(
            "PLISIO_CALLBACK parsed status=%s order_number=%s txn_id=%s invoice_id=%s amount=%s source_amount=%s currency=%s source_currency=%s",
            payload.get("status"),
            payload.get("order_number"),
            payload.get("txn_id"),
            payload.get("invoice_id") or payload.get("id"),
            payload.get("amount"),
            payload.get("source_amount"),
            payload.get("currency"),
            payload.get("source_currency"),
        )

    event_id = None
    event_type = None
    if provider_name == "razorpay":
        event_id = x_razorpay_event_id or payload.get("id")
        event_type = payload.get("event")
    elif provider_name == "plisio":
        stable_order = str(payload.get("order_number") or "").strip()
        stable_status = str(payload.get("status") or "").strip()
        stable_txn = str(payload.get("txn_id") or payload.get("invoice_id") or payload.get("id") or "").strip()
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        payload_fingerprint = hashlib.sha256(canonical_payload).hexdigest()
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            composite = "|".join([stable_order, stable_status, stable_txn]).strip("|")
            event_id = composite or payload_fingerprint
        event_type = payload.get("status")

    if not event_id:
        logger.error("Could not extract event ID from %s webhook", provider_name)
        raise HTTPException(status_code=400, detail="Missing event ID")

    if provider_name == "plisio":
        _plisio_debug("PLISIO_CALLBACK event_resolved event_id=%s event_type=%s", event_id, event_type)

    supabase = get_supabase_client()

    await async_db(
        lambda: supabase.table("webhook_events")
        .upsert(
            {
                "provider": provider_name,
                "event_id": event_id,
                "event_type": event_type or "unknown",
                "payload": payload,
            },
            on_conflict="provider,event_id",
        )
        .execute()
    )

    queued_event = await _fetch_webhook_event(supabase, provider_name, event_id)
    if queued_event and queued_event.get("processed"):
        await _set_webhook_cache_hint(f"webhook_event:{provider_name}:{event_id}")
        return {"status": "ok", "message": "Already processed"}

    return {"status": "accepted", "message": "Queued for background processing", "event_id": event_id}
