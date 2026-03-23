import logging
import os
import json
import hashlib
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime
from fastapi import APIRouter, Request, Header, HTTPException, Depends
from typing import Optional, Any

from app.db import get_supabase_client
from app.redis_cache import CACHE_REDIS
from app.authn.session_store import SESSION_REDIS
from app.payments.payment_providers.router import get_provider
from app.payments.constants import PaymentTransactionStatus

logger = logging.getLogger(__name__)

AUTHDBG_ENABLED = (os.getenv("AUTHDBG_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on"}


def _plisio_debug(msg: str, *args: object) -> None:
    if AUTHDBG_ENABLED:
        logger.info(msg, *args)


def _is_unique_webhook_event_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code is not None and str(code) == "23505":
        return True

    msg = str(exc).lower()
    if "23505" in msg or "duplicate key" in msg or "already exists" in msg:
        return True

    args = getattr(exc, "args", ()) or ()
    for arg in args:
        if "23505" in str(arg):
            return True
    return False


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


async def _release_webhook_lock(lock_key: str, lock_token: str) -> None:
    if not lock_key or not lock_token:
        return

    # Compare-and-delete to avoid deleting a lock acquired by another worker.
    compare_delete_script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) "
        "else return 0 end"
    )
    try:
        await CACHE_REDIS.eval(compare_delete_script, 1, lock_key, lock_token)
    except Exception as exc:
        logger.warning("Failed to safely release webhook lock %s: %s", lock_key, exc)

webhook_router = APIRouter()

# Note: /api/webhooks/{provider_name} paths must be exempt from CSRF in main.py
@webhook_router.post("/api/webhooks/{provider_name}")
async def handle_webhook(
    provider_name: str,
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None)
):
    """
    Unified webhook handler for all payment providers.
    """
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
    
    # Verify signature
    signature = None
    if provider_name == "razorpay":
        signature = x_razorpay_signature
        if not signature:
            logger.warning(f"Missing signature for {provider_name} webhook")
            raise HTTPException(status_code=400, detail="Missing signature")
    elif provider_name == "plisio":
        # Plisio callback authenticity is validated from raw body via SDK helper.
        signature = ""
    else:
        signature = ""
        
    is_valid = await provider.verify_webhook_signature(raw_body, signature)
    if not is_valid:
        logger.error(f"Invalid webhook signature for {provider_name}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode('utf-8'))
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

    # Extract event ID for idempotency
    event_id = None
    event_type = None
    
    if provider_name == "razorpay":
        # Razorpay sends a unique event ID in the header X-Razorpay-Event-Id
        event_id = x_razorpay_event_id or payload.get("id") # Fallback to body id if header is missing
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
        logger.error(f"Could not extract event ID from {provider_name} webhook")
        return {"status": "error", "message": "Missing event ID"}

    if provider_name == "plisio":
        _plisio_debug("PLISIO_CALLBACK event_resolved event_id=%s event_type=%s", event_id, event_type)

    # Fast-path idempotency check with Redis (cache for 24h)
    redis_key = f"webhook_event:{provider_name}:{event_id}"
    if await CACHE_REDIS.get(redis_key):
        logger.info(f"Webhook event already processed (Redis cache): {event_id}")
        return {"status": "ok", "message": "Already processed"}

    # Set temporary lock to prevent concurrent processing of the exact same event
    lock_key = f"webhook_lock:{provider_name}:{event_id}"
    lock_token = str(uuid.uuid4())
    lock_acquired = await CACHE_REDIS.set(lock_key, lock_token, nx=True, ex=30)
    if not lock_acquired:
        logger.info(f"Webhook event processing in another worker: {event_id}")
        return {"status": "ok", "message": "Processing in progress"}

    try:
        supabase = get_supabase_client()
        
        # Insert into webhook_events (unique constraint handles DB-level idempotency)
        try:
            event_row = supabase.table("webhook_events").insert({
                "provider": provider_name,
                "event_id": event_id,
                "event_type": event_type or "unknown",
                "payload": payload
            }).execute()
        except Exception as e:
            if _is_unique_webhook_event_error(e):
                existing_event = (
                    supabase.table("webhook_events")
                    .select("id,processed")
                    .eq("provider", provider_name)
                    .eq("event_id", event_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if existing_event and bool(existing_event[0].get("processed")):
                    logger.info(f"Webhook event already persists in DB: {event_id}")
                    return {"status": "ok"}
                logger.warning("Re-processing previously inserted but unprocessed webhook event: %s", event_id)
            else:
                logger.exception("Webhook event insert failed for %s:%s", provider_name, event_id)
                raise

        # Custom processing logic based on provider
        new_status = provider.map_event_to_state(event_type)
        if provider_name == "plisio":
            _plisio_debug(
                "PLISIO_CALLBACK status_mapped event_id=%s provider_status=%s mapped_status=%s",
                event_id,
                event_type,
                new_status.value if new_status else None,
            )
        if not new_status:
            logger.info(f"Ignoring unmapped event type {event_type} for {provider_name}")
            supabase.table("webhook_events").update({
                "processed": True,
                "processed_at": datetime.utcnow().isoformat()
            }).eq("provider", provider_name).eq("event_id", event_id).execute()
            await CACHE_REDIS.set(redis_key, "processed", ex=86400)
            return {"status": "ok"}
            
        provider_payment_id = None
        user_id = None
        
        if provider_name == "razorpay":
            # For razorpay, the event payload contains the target object
            payload_entity = payload.get("payload", {})
            if "subscription" in payload_entity:
                sub_obj = payload_entity["subscription"]["entity"]
                provider_payment_id = sub_obj.get("id") # sub_xxxx
                notes = sub_obj.get("notes", {})
                user_id = notes.get("user_id")
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
            logger.error(f"Could not map webhook event to a payment ID: {event_id}")
            supabase.table("webhook_events").update({
                "processed": True,
                "processing_error": "Missing provider_payment_id"
            }).eq("provider", provider_name).eq("event_id", event_id).execute()
            return {"status": "ok"}

        # Find the payment transaction
        tx_query = supabase.table("payment_transactions").select("*").eq("provider_payment_id", provider_payment_id).eq("provider", provider_name).execute()
        
        if not tx_query.data:
            # Maybe the transaction isn't created yet or we got the event out of order
            logger.warning(f"Transaction not found for provider_payment_id {provider_payment_id}")
            supabase.table("webhook_events").update({
                "processed": False,
                "processing_error": "Transaction not found"
            }).eq("provider", provider_name).eq("event_id", event_id).execute()
            raise HTTPException(status_code=404, detail="Transaction not found for provider payment ID. Deferring to trigger retry.")
            
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

                supabase.table("payment_audit_logs").insert({
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
                }).execute()

                supabase.table("webhook_events").update({
                    "processed": True,
                    "processed_at": datetime.utcnow().isoformat(),
                    "processing_error": mismatch_reason,
                }).eq("provider", provider_name).eq("event_id", event_id).execute()

                await CACHE_REDIS.set(redis_key, "processed", ex=86400)
                return {"status": "ok", "message": "Ignored mismatched callback currency"}

            amount_mismatch_reason = None
            if new_status == PaymentTransactionStatus.SUCCEEDED:
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
                    mismatches.append(
                        f"amount expected={expected_crypto_amount} got={callback_crypto_amount}"
                    )

                if (
                    expected_source_amount is not None
                    and callback_source_amount is not None
                    and abs(callback_source_amount - expected_source_amount) > tolerance
                ):
                    mismatches.append(
                        f"source_amount expected={expected_source_amount} got={callback_source_amount}"
                    )

                if mismatches:
                    amount_mismatch_reason = "; ".join(mismatches)
                    logger.warning(
                        "Plisio callback amount mismatch event_id=%s tx_id=%s details=%s",
                        event_id,
                        tx_id,
                        amount_mismatch_reason,
                    )

                    supabase.table("payment_audit_logs").insert({
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
                            "expected_crypto_amount": str(expected_crypto_amount) if expected_crypto_amount is not None else None,
                            "callback_crypto_amount": str(callback_crypto_amount) if callback_crypto_amount is not None else None,
                            "expected_source_amount": str(expected_source_amount) if expected_source_amount is not None else None,
                            "callback_source_amount": str(callback_source_amount) if callback_source_amount is not None else None,
                            "tolerance": str(tolerance),
                        },
                    }).execute()
        
        # Update transaction status
        is_recurring_charge = payload.get("is_recurring_charge") or (provider_name == "razorpay" and event_type == "subscription.charged")
        previous_status = transaction["status"]
        # Plisio can send a late "completed/success" callback for the same invoice after a prior "expired".
        # Allow only this narrow recovery path: expired -> succeeded on the same provider_payment_id row.
        allow_plisio_late_success = (
            provider_name == "plisio"
            and previous_status == PaymentTransactionStatus.EXPIRED.value
            and new_status == PaymentTransactionStatus.SUCCEEDED
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
        if (
            is_recurring_charge
            or allow_plisio_late_success
            or previous_status not in terminal_statuses
        ):
            update_result = supabase.table("payment_transactions").update({
                "status": new_status.value,
                "last_provider_event_time": datetime.utcnow().isoformat()
            }).eq("id", tx_id).eq("status", previous_status).execute()

            status_updated = bool(getattr(update_result, "data", None))

            refetched_rows = (
                supabase.table("payment_transactions")
                .select("id,status")
                .eq("id", tx_id)
                .limit(1)
                .execute()
                .data
                or []
            )
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
                # Write audit log for state transitions this worker actually applied.
                supabase.table("payment_audit_logs").insert({
                    "transaction_id": tx_id,
                    "entity_type": "payment_transaction",
                    "entity_id": tx_id,
                    "previous_state": previous_status,
                    "new_state": new_status.value,
                    "trigger_source": f"{provider_name}_webhook",
                    "trigger_event_id": event_id
                }).execute()

            # If successful, we need to activate the subscription in `user_subscriptions`
            if new_status == PaymentTransactionStatus.SUCCEEDED and status_updated:
                tx_metadata = transaction.get("metadata", {})
                if not isinstance(tx_metadata, dict):
                    tx_metadata = {}

                if provider_name == "plisio" and 'amount_mismatch_reason' in locals() and amount_mismatch_reason:
                    logger.warning(
                        "Blocking subscription activation due to amount mismatch event_id=%s tx_id=%s",
                        event_id,
                        tx_id,
                    )
                    supabase.table("payment_audit_logs").insert({
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
                    }).execute()
                else:
                    subscription_id = transaction.get("subscription_id")
                    renewal_requested = provider_name == "plisio" and (
                        bool(tx_metadata.get("renewal_intent")) or bool(subscription_id)
                    )

                    try:
                        if renewal_requested:
                            if not subscription_id:
                                raise ValueError(f"renewal transaction {tx_id} missing subscription_id")

                            renew_response = supabase.rpc(
                                "renew_subscription",
                                {
                                    "p_subscription_id": subscription_id,
                                    "p_payment_id": tx_id,
                                },
                            ).execute()
                            if renew_response.data is not True:
                                raise ValueError(
                                    f"renew_subscription returned non-true for tx={tx_id} subscription={subscription_id} payload={renew_response.data}"
                                )
                        else:
                            # First-time subscription flow.
                            plan_id = tx_metadata.get("plan_id")
                            if plan_id:
                                try:
                                    uuid.UUID(str(plan_id))
                                except (ValueError, TypeError):
                                    plan_lookup = supabase.table("subscription_plans").select("id").eq("name", plan_id).execute()
                                    if plan_lookup.data:
                                        plan_id = plan_lookup.data[0]["id"]
                                    else:
                                        raise ValueError(f"Could not resolve plan name '{plan_id}' to a UUID")

                            if not plan_id:
                                raise ValueError(f"first-time transaction {tx_id} missing plan_id for subscription creation")

                            sub_response = supabase.rpc("create_subscription", {
                                "p_user_id": tx_user_id,
                                "p_plan_id": plan_id,
                                "p_payment_provider": provider_name,
                                "p_external_id": provider_payment_id,
                                "p_trial_days": 0,
                            }).execute()

                            new_sub_id = sub_response.data
                            if not new_sub_id:
                                raise ValueError(f"create_subscription returned empty result for tx={tx_id}")

                            supabase.table("payment_transactions").update({
                                "subscription_id": new_sub_id
                            }).eq("id", tx_id).execute()

                            if provider_name == "plisio":
                                _plisio_debug(
                                    "PLISIO_CALLBACK subscription_created event_id=%s tx_id=%s subscription_id=%s",
                                    event_id,
                                    tx_id,
                                    new_sub_id,
                                )

                        # Invalidate permissions cache (from auth flow) so frontend gets new access.
                        from app.authn.session_store import update_all_sessions_for_user_perms
                        await update_all_sessions_for_user_perms(tx_user_id, plan="core", permissions=["active_subscriber"])
                    except Exception as sub_err:
                        logger.error("Failed to finalize subscription activation for tx %s user %s: %s", tx_id, tx_user_id, sub_err)
                        retry_metadata = dict(tx_metadata)
                        retry_metadata["activation_retry_required"] = True
                        retry_metadata["activation_retry_updated_at"] = datetime.utcnow().isoformat()
                        retry_metadata["activation_retry_last_error"] = str(sub_err)[:500]

                        supabase.table("payment_transactions").update({
                            "metadata": retry_metadata,
                            "updated_at": datetime.utcnow().isoformat(),
                        }).eq("id", tx_id).execute()

                        supabase.table("payment_audit_logs").insert({
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
                        }).execute()

        # Mark webhook event as processed
        supabase.table("webhook_events").update({
            "processed": True,
            "processed_at": datetime.utcnow().isoformat()
        }).eq("provider", provider_name).eq("event_id", event_id).execute()
        
        # Cache ID in Redis representing processed webhook
        await CACHE_REDIS.set(redis_key, "processed", ex=86400)

        if provider_name == "plisio":
            _plisio_debug("PLISIO_CALLBACK completed event_id=%s tx_id=%s", event_id, tx_id)
        
        return {"status": "ok"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        await _release_webhook_lock(lock_key, lock_token)
