import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request

from app.authn.deps import require_session
from app.db import get_supabase_client, async_db
from app.payments.payment_providers.router import get_provider
from app.payments.constants import PaymentTransactionStatus
from app.redis_cache import CACHE_REDIS, safe_unlock

# We rate limit checkout creation heavily
from app.authn.rate_limit_auth import rate_limit

logger = logging.getLogger(__name__)

AUTHDBG_ENABLED = (os.getenv("AUTHDBG_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on"}
ACTIVE_CHECKOUT_STATUSES = [
    PaymentTransactionStatus.PENDING.value,
    PaymentTransactionStatus.PROCESSING.value,
    "created",
]


def _plisio_debug(msg: str, *args: object) -> None:
    if AUTHDBG_ENABLED:
        logger.info(msg, *args)


def _parse_optional_iso_datetime(raw_value: Optional[str]) -> Optional[datetime]:
    if not raw_value:
        return None
    try:
        normalized = str(raw_value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _is_activation_pending_or_retry_required(metadata: Dict[str, Any]) -> bool:
    if not isinstance(metadata, dict):
        return False

    if _coerce_bool(metadata.get("activation_retry_required")):
        return True
    if _coerce_bool(metadata.get("activation_pending")):
        return True

    activation_state = str(
        metadata.get("activation_state")
        or metadata.get("activation_status")
        or ""
    ).strip().lower()
    if activation_state in {"pending", "retry_required", "retry-required", "retry_required_pending"}:
        return True

    return False


def _is_active_checkout_conflict_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "uq_active_payment_per_user" in msg:
        return True
    if "23505" in msg and "payment_transactions" in msg and "user_id" in msg:
        return True
    return False

payments_router = APIRouter(prefix="/api/payments")

class CreateCheckoutRequest(BaseModel):
    plan_id: str
    provider: str
    billing_period: Optional[str] = "monthly"


class CancelCheckoutAttemptRequest(BaseModel):
    provider: str
    provider_payment_id: str

@payments_router.post("/create-checkout")
async def create_checkout(
    req: CreateCheckoutRequest,
    user_session: dict = Depends(require_session),
    request: Request = None
):
    """
    Creates a new checkout session/subscription with the given provider,
    returning structured info (URL, modal details) to the frontend.
    """
    user_id = user_session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Rate Limiting
    if request:
        await rate_limit(request, f"checkout:{user_id}", limit_per_minute=5)

    # Validate Plan against DB
    supabase = get_supabase_client()
    plan_response = await async_db(lambda: supabase.table("subscription_plans").select("*").eq("name", req.plan_id).eq("is_active", True).execute())
    
    if not plan_response.data:
        raise HTTPException(status_code=404, detail="Active subscription plan not found")
        
    plan = plan_response.data[0]
    
    # Check if a pending transaction already exists in recent time to avoid spam? 
    # Opted against strict preventing here, rate limit handles abuse.

    try:
        provider = get_provider(req.provider)
    except HTTPException as e:
        raise e

    if req.provider == "plisio":
        _plisio_debug(
            "PLISIO_CALL api.create_checkout.request user_id=%s plan_id=%s billing_period=%s",
            user_id,
            req.plan_id,
            req.billing_period,
        )

    renewal_subscription_id: Optional[str] = None
    renewal_cycle_marker: Optional[str] = None
    if req.provider == "plisio":
        renewal_candidate = (
            (await async_db(lambda: supabase.table("user_subscriptions")
            .select("id, expires_at, status, payment_provider")
            .eq("user_id", user_id)
            .eq("payment_provider", "plisio")
            .eq("status", "active")
            .order("expires_at", desc=True)
            .limit(1)
            .execute())).data
            or []
        )
        if renewal_candidate:
            renewal_subscription_id = str(renewal_candidate[0].get("id") or "") or None
            expires_at = str(renewal_candidate[0].get("expires_at") or "")
            if expires_at:
                renewal_cycle_marker = expires_at.split("T", 1)[0]

    if req.provider == "plisio":
        latest_same_cycle_rows = (
            (await async_db(lambda: supabase.table("payment_transactions")
            .select("id, provider_payment_id, status, metadata, created_at")
            .eq("user_id", user_id)
            .eq("provider", "plisio")
            .eq("payment_type", "subscription")
            .order("created_at", desc=True)
            .limit(25)
            .execute())).data
            or []
        )

        latest_same_cycle_row = None
        for row in latest_same_cycle_rows:
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            row_cycle_marker = str(metadata.get("renewal_cycle_marker") or "")
            row_plan_id = str(metadata.get("plan_id") or "")
            if row_cycle_marker != str(renewal_cycle_marker or ""):
                continue
            if row_plan_id and row_plan_id != str(req.plan_id):
                continue

            latest_same_cycle_row = row
            break

        if latest_same_cycle_row:
            latest_status = str(latest_same_cycle_row.get("status") or "").strip().lower()
            latest_metadata = latest_same_cycle_row.get("metadata")
            if not isinstance(latest_metadata, dict):
                latest_metadata = {}

            if (
                latest_status == PaymentTransactionStatus.SUCCEEDED.value
                and _is_activation_pending_or_retry_required(latest_metadata)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Payment already succeeded for this cycle, but subscription activation is still pending. Please wait for activation retry instead of creating another checkout.",
                )

        existing_same_cycle = (
            (await async_db(lambda: supabase.table("payment_transactions")
            .select("id, provider_payment_id, amount, currency, status, created_at, last_provider_event_time, metadata")
            .eq("user_id", user_id)
            .eq("provider", "plisio")
            .eq("payment_type", "subscription")
            .in_("status", [PaymentTransactionStatus.PENDING.value, PaymentTransactionStatus.PROCESSING.value])
            .order("created_at", desc=True)
            .limit(10)
            .execute())).data
            or []
        )

        now_utc = datetime.now(timezone.utc)
        reuse_age_seconds = int((os.getenv("PLISIO_PENDING_REUSE_MAX_AGE_SECONDS") or "86400").strip() or "86400")
        pending_settle_guard_seconds = int((os.getenv("PLISIO_PENDING_SETTLE_GUARD_SECONDS") or "180").strip() or "180")

        for row in existing_same_cycle:
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            row_cycle_marker = str(metadata.get("renewal_cycle_marker") or "")
            row_plan_id = str(metadata.get("plan_id") or "")
            if row_cycle_marker != str(renewal_cycle_marker or ""):
                continue
            if row_plan_id and row_plan_id != str(req.plan_id):
                continue

            created_dt = _parse_optional_iso_datetime(str(row.get("created_at") or ""))
            age_seconds = (now_utc - created_dt).total_seconds() if created_dt else None
            provider_event_dt = _parse_optional_iso_datetime(str(row.get("last_provider_event_time") or ""))
            provider_event_age_seconds = (
                (now_utc - provider_event_dt).total_seconds() if provider_event_dt else None
            )

            checkout_url = str(metadata.get("checkout_url") or metadata.get("invoice_url") or "")
            provider_checkout_data = metadata.get("provider_checkout_data")
            if not isinstance(provider_checkout_data, dict):
                provider_checkout_data = {}
            if not checkout_url:
                checkout_url = str(
                    provider_checkout_data.get("invoice_url")
                    or provider_checkout_data.get("checkout_url")
                    or provider_checkout_data.get("url")
                    or ""
                )

            if checkout_url and (age_seconds is None or age_seconds <= reuse_age_seconds):
                return {
                    "checkout_url": checkout_url,
                    "provider_checkout_data": provider_checkout_data,
                    "provider_payment_id": str(row.get("provider_payment_id") or ""),
                    "amount": row.get("amount", plan.get("price_usd")),
                    "currency": row.get("currency", "USD"),
                }

            if (
                (age_seconds is not None and age_seconds <= pending_settle_guard_seconds)
                or (
                    provider_event_age_seconds is not None
                    and provider_event_age_seconds <= pending_settle_guard_seconds
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A previous Plisio invoice is still settling. Please wait a couple of minutes before retrying.",
                )

    # For Razorpay recurring subscriptions, avoid parallel active billing streams.
    if req.provider == "razorpay":
        now_iso = datetime.now(timezone.utc).isoformat()
        active_subscription_rows = (
            (await async_db(lambda: supabase.table("user_subscriptions")
            .select("id, cancel_at_period_end, expires_at")
            .eq("user_id", user_id)
            .eq("status", "active")
            .gt("expires_at", now_iso)
            .order("expires_at", desc=True)
            .limit(1)
            .execute())).data
            or []
        )

        if active_subscription_rows and not bool(active_subscription_rows[0].get("cancel_at_period_end")):
            raise HTTPException(
                status_code=409,
                detail="An active subscription already exists. Cancel at period end first, then resubscribe.",
            )

    lock_key = f"checkout:create:{user_id}"
    lock_token = datetime.now(timezone.utc).isoformat()
    lock_acquired = await CACHE_REDIS.set(lock_key, lock_token, nx=True, ex=120)
    if not lock_acquired:
        raise HTTPException(status_code=429, detail="Checkout request already in progress. Please retry in a few seconds.")

    try:
        # Call provider SDK logic
        checkout_response = await provider.create_checkout(
            user_id=user_id,
            plan_id=req.plan_id,
            billing_period=req.billing_period
        )

        if req.provider == "plisio":
            _plisio_debug(
                "PLISIO_CALL api.create_checkout.provider_response user_id=%s provider_payment_id=%s checkout_url=%s",
                user_id,
                checkout_response.get("provider_payment_id"),
                checkout_response.get("checkout_url") or checkout_response.get("redirect_url"),
            )
        
        provider_payment_id = checkout_response.get("provider_payment_id")
        provider_checkout_data = checkout_response.get("provider_checkout_data")
        if not isinstance(provider_checkout_data, dict):
            provider_checkout_data = {}
        checkout_url = checkout_response.get("checkout_url") or checkout_response.get("redirect_url")
        management_url = provider_checkout_data.get("management_url") or provider_checkout_data.get("short_url")
        invoice_url = provider_checkout_data.get("invoice_url") or provider_checkout_data.get("hosted_invoice_url")

        # Supersede unresolved attempts across all providers so users can switch provider safely.
        now_iso = datetime.now(timezone.utc).isoformat()
        existing_attempts = await async_db(lambda: (
            supabase.table("payment_transactions")
            .select("id, provider, provider_payment_id, status, amount, currency, metadata, created_at, last_provider_event_time")
            .eq("user_id", user_id)
            .eq("payment_type", "subscription")
            .in_("status", ACTIVE_CHECKOUT_STATUSES)
            .execute()
        ))
        same_attempt = None
        for previous in existing_attempts.data or []:
            previous_provider = str(previous.get("provider") or "").strip().lower()
            previous_provider_payment_id = str(previous.get("provider_payment_id") or "")
            if (
                previous_provider == str(req.provider)
                and previous_provider_payment_id == str(provider_payment_id or "")
            ):
                same_attempt = previous
                continue

            # Keep settle guard only for same-provider Plisio retries.
            if req.provider == "plisio" and previous_provider == "plisio":
                previous_created_dt = _parse_optional_iso_datetime(str(previous.get("created_at") or ""))
                previous_event_dt = _parse_optional_iso_datetime(str(previous.get("last_provider_event_time") or ""))
                if previous_created_dt:
                    age = (datetime.now(timezone.utc) - previous_created_dt).total_seconds()
                    settle_guard_seconds = int(
                        (os.getenv("PLISIO_PENDING_SETTLE_GUARD_SECONDS") or "180").strip() or "180"
                    )
                    if age <= settle_guard_seconds:
                        logger.info(
                            "Skipping supersede for recent Plisio pending tx=%s age_seconds=%.1f",
                            previous.get("id"),
                            age,
                        )
                        continue
                if previous_event_dt:
                    event_age = (datetime.now(timezone.utc) - previous_event_dt).total_seconds()
                    settle_guard_seconds = int(
                        (os.getenv("PLISIO_PENDING_SETTLE_GUARD_SECONDS") or "180").strip() or "180"
                    )
                    if event_age <= settle_guard_seconds:
                        logger.info(
                            "Skipping supersede for recently updated Plisio pending tx=%s event_age_seconds=%.1f",
                            previous.get("id"),
                            event_age,
                        )
                        continue

            if previous_provider_payment_id and previous_provider:
                try:
                    previous_provider_client = get_provider(previous_provider)
                    await previous_provider_client.cancel_checkout_attempt(previous_provider_payment_id)
                except Exception as cancel_exc:
                    logger.warning(
                        "Provider-side superseded attempt cancellation failed for provider=%s payment_id=%s: %s",
                        previous_provider,
                        previous_provider_payment_id,
                        cancel_exc,
                    )

            await async_db(lambda prev=previous, ts=now_iso: (
                supabase.table("payment_transactions")
                .update(
                    {
                        "status": PaymentTransactionStatus.CANCELLED.value,
                        "last_provider_event_time": ts,
                        "updated_at": ts,
                    }
                )
                .eq("id", prev["id"])
                .in_("status", ACTIVE_CHECKOUT_STATUSES)
                .execute()
            ))

        if same_attempt:
            same_metadata = same_attempt.get("metadata")
            if not isinstance(same_metadata, dict):
                same_metadata = {}

            existing_provider_checkout_data = same_metadata.get("provider_checkout_data")
            if not isinstance(existing_provider_checkout_data, dict):
                existing_provider_checkout_data = provider_checkout_data

            return {
                **checkout_response,
                "provider_payment_id": str(same_attempt.get("provider_payment_id") or provider_payment_id),
                "amount": same_attempt.get("amount", checkout_response.get("amount")),
                "currency": same_attempt.get("currency", checkout_response.get("currency")),
                "checkout_url": same_metadata.get("checkout_url") or checkout_url,
                "provider_checkout_data": existing_provider_checkout_data,
            }
        
        # Create pending Payment Transaction in DB
        tx_data = {
            "user_id": user_id,
            "provider": req.provider,
            "provider_payment_id": provider_payment_id,
            "provider_subscription_id": provider_payment_id if req.provider == "razorpay" else None,
            "amount": checkout_response.get("amount", plan["price_usd"]),
            "currency": checkout_response.get("currency", "USD"),
            "status": PaymentTransactionStatus.PENDING.value,
            "payment_type": "subscription",
            "metadata": {
                "plan_id": req.plan_id,
                "billing_period": req.billing_period,
                "checkout_url": checkout_url,
                "management_url": management_url,
                "invoice_url": invoice_url,
                "provider_checkout_data": provider_checkout_data,
                "plisio_expected_currency": (
                    str(provider_checkout_data.get("currency") or os.getenv("PLISIO_CRYPTO_CURRENCY") or "").strip().upper()
                    if req.provider == "plisio"
                    else None
                ),
                "plisio_expected_source_currency": "USD" if req.provider == "plisio" else None,
                "renewal_intent": bool(renewal_subscription_id) if req.provider == "plisio" else False,
                "renewal_for_subscription_id": renewal_subscription_id if req.provider == "plisio" else None,
                "renewal_cycle_marker": renewal_cycle_marker if req.provider == "plisio" else None,
            }
        }

        if req.provider == "plisio" and renewal_subscription_id:
            tx_data["subscription_id"] = renewal_subscription_id
        
        try:
            if provider_payment_id:
                tx_insert_res = await async_db(
                    lambda: supabase.table("payment_transactions")
                    .upsert(tx_data, on_conflict="provider,provider_payment_id")
                    .execute()
                )
            else:
                tx_insert_res = await async_db(
                    lambda: supabase.table("payment_transactions").insert(tx_data).execute()
                )
        except Exception as insert_exc:
            if not _is_active_checkout_conflict_error(insert_exc):
                raise

            logger.warning(
                "Active checkout conflict hit for user=%s provider=%s. Running one-time supersede retry.",
                user_id,
                req.provider,
            )
            retry_now = datetime.now(timezone.utc).isoformat()
            await async_db(lambda: (
                supabase.table("payment_transactions")
                .update(
                    {
                        "status": PaymentTransactionStatus.CANCELLED.value,
                        "last_provider_event_time": retry_now,
                        "updated_at": retry_now,
                    }
                )
                .eq("user_id", user_id)
                .eq("payment_type", "subscription")
                .in_("status", ACTIVE_CHECKOUT_STATUSES)
                .execute()
            ))

            if provider_payment_id:
                tx_insert_res = await async_db(
                    lambda: supabase.table("payment_transactions")
                    .upsert(tx_data, on_conflict="provider,provider_payment_id")
                    .execute()
                )
            else:
                tx_insert_res = await async_db(
                    lambda: supabase.table("payment_transactions").insert(tx_data).execute()
                )

        if req.provider == "plisio":
            inserted_id = None
            if tx_insert_res.data and isinstance(tx_insert_res.data, list):
                inserted_id = tx_insert_res.data[0].get("id")
            _plisio_debug(
                "PLISIO_CALL api.create_checkout.tx_created user_id=%s tx_id=%s provider_payment_id=%s status=%s amount=%s currency=%s",
                user_id,
                inserted_id,
                provider_payment_id,
                tx_data.get("status"),
                tx_data.get("amount"),
                tx_data.get("currency"),
            )
        
        return checkout_response

    except Exception as e:
        logger.error(f"Error creating checkout for {req.provider}: {e}")
        raise HTTPException(status_code=400, detail="Failed to create checkout. Please try again.")
    finally:
        try:
            await safe_unlock(CACHE_REDIS, lock_key, lock_token)
        except Exception:
            # Lock expiry is short-lived; failures here are non-fatal.
            pass


@payments_router.post("/cancel-checkout-attempt")
async def cancel_checkout_attempt(
    req: CancelCheckoutAttemptRequest,
    user_session: dict = Depends(require_session),
):
    """Marks an in-progress checkout attempt as cancelled by the user."""
    user_id = user_session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not req.provider_payment_id.strip():
        raise HTTPException(status_code=400, detail="provider_payment_id is required")

    supabase = get_supabase_client()
    try:
        tx_query = await async_db(lambda: (
            supabase.table("payment_transactions")
            .select("id, status, provider, provider_payment_id")
            .eq("user_id", user_id)
            .eq("provider", req.provider)
            .eq("provider_payment_id", req.provider_payment_id)
            .in_("status", ACTIVE_CHECKOUT_STATUSES)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ))

        if not tx_query.data:
            return {"status": "ok", "updated": False, "message": "No active checkout attempt found"}

        tx = tx_query.data[0]
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            provider = get_provider(req.provider)
            await provider.cancel_checkout_attempt(req.provider_payment_id)
        except Exception as cancel_exc:
            logger.warning(
                "Provider-side cancel-checkout-attempt failed for provider=%s payment_id=%s: %s",
                req.provider,
                req.provider_payment_id,
                cancel_exc,
            )

        await async_db(lambda: (
            supabase.table("payment_transactions")
            .update(
                {
                    "status": PaymentTransactionStatus.CANCELLED.value,
                    "last_provider_event_time": now_iso,
                    "updated_at": now_iso,
                }
            )
            .eq("id", tx["id"])
            .in_("status", [PaymentTransactionStatus.PENDING.value, PaymentTransactionStatus.PROCESSING.value])
            .execute()
        ))

        return {"status": "ok", "updated": True, "transaction_id": tx["id"]}
    except Exception as e:
        logger.error("Error cancelling checkout attempt for user %s: %s", user_id, e)
        raise HTTPException(status_code=400, detail="Failed to cancel checkout attempt")

@payments_router.post("/cancel-subscription")
async def cancel_subscription(
    user_session: dict = Depends(require_session)
):
    """
    Cancels the user's active subscription renewal and keeps access active
    until the current billing period expires.
    """
    user_id = user_session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    supabase = get_supabase_client()
    
    # 1. Find active subscription that is not already flagged for cancellation
    try:
        sub_query = await async_db(lambda: supabase.table("user_subscriptions") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "active") \
            .is_("cancel_at_period_end", False) \
            .execute())
        
        if not sub_query.data:
            # Maybe it is already cancelled at period end
            check_query = await async_db(lambda: supabase.table("user_subscriptions") \
                .select("*") \
                .eq("user_id", user_id) \
                .eq("status", "active") \
                .is_("cancel_at_period_end", True) \
                .execute())
            if check_query.data:
                return {"status": "ok", "message": "Subscription is already scheduled for cancellation"}
            raise HTTPException(status_code=404, detail="No active cancelable subscription found")
            
        subscription = sub_query.data[0]
        
        provider_name = str(subscription.get("payment_provider") or "").strip().lower()
        provider_subscription_id = subscription.get("external_subscription_id")
        now_iso = datetime.now(timezone.utc).isoformat()

        provider_cancel_status = "not_attempted"
        provider_cancel_error = None

        update_payload = {
            "cancel_at_period_end": True,
        }

        if provider_name == "razorpay" and provider_subscription_id:
            try:
                provider = get_provider(provider_name)
                cancel_success = await provider.cancel_subscription(provider_subscription_id)
                if cancel_success:
                    provider_cancel_status = "succeeded"
                    update_payload["cancelled_at"] = now_iso
                else:
                    provider_cancel_status = "failed"
                    logger.warning(
                        "Provider %s failed to cancel subscription %s. Falling back to DB-only deferred cancellation.",
                        provider_name,
                        provider_subscription_id,
                    )
            except Exception as exc:
                provider_cancel_status = "failed"
                provider_cancel_error = str(exc)[:200]
                logger.warning(
                    "Could not cancel subscription via provider %s for %s: %s. Falling back to DB-only deferred cancellation.",
                    provider_name,
                    provider_subscription_id,
                    exc,
                )

            # Stop future renewals in DB regardless of provider API outcome.
            update_payload["auto_renew"] = False

        await async_db(lambda: supabase.table("user_subscriptions").update(update_payload).eq("id", subscription["id"]).execute())
        
        # Log the user intent
        await async_db(lambda: supabase.table("payment_audit_logs").insert({
            "entity_type": "user_subscription",
            "entity_id": subscription["id"],
            "previous_state": "active",
            "new_state": "cancelling_deferred",
            "trigger_source": "user_request",
            "metadata": {
                "external_id": subscription.get("external_subscription_id"),
                "provider_cancel_status": provider_cancel_status,
                "provider_cancel_error": provider_cancel_error,
            }
        }).execute())
        
        return {"status": "ok", "message": "Subscription scheduled for deferred cancellation"}
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error flagging subscription cancellation for user {user_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to cancel subscription: {str(e)}")

@payments_router.post("/resume-subscription")
async def resume_subscription(
    user_session: dict = Depends(require_session)
):
    """
    Resumes a subscription that was previously flagged for cancellation, 
    provided the backend cron job hasn't actually called the provider yet.
    """
    user_id = user_session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    supabase = get_supabase_client()
    
    try:
        sub_query = await async_db(lambda: supabase.table("user_subscriptions") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "active") \
            .is_("cancel_at_period_end", True) \
            .execute())
        
        if not sub_query.data:
            raise HTTPException(status_code=404, detail="No subscription pending cancellation found")
            
        subscription = sub_query.data[0]
        
        # If cancelled_at is NOT NULL, the cron job already told Razorpay it's over.
        # We cannot reverse a Razorpay cancellation once sent.
        if subscription.get("cancelled_at") is not None:
            raise HTTPException(status_code=400, detail="Subscription cancellation has already been finalized by the provider. You must wait for expiration to resubscribe.")
            
        # Revert the flag
        await async_db(lambda: supabase.table("user_subscriptions").update({
            "cancel_at_period_end": False
        }).eq("id", subscription["id"]).execute())
        
        await async_db(lambda: supabase.table("payment_audit_logs").insert({
            "entity_type": "user_subscription",
            "entity_id": subscription["id"],
            "previous_state": "cancelling_deferred",
            "new_state": "active_resumed",
            "trigger_source": "user_request",
            "metadata": {"external_id": subscription.get("external_subscription_id")}
        }).execute())
        
        return {"status": "ok", "message": "Subscription successfully resumed"}
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resuming subscription for user {user_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to resume subscription: {str(e)}")

@payments_router.get("/history")
async def payment_history(
    user_session: dict = Depends(require_session)
):
    """
    Fetches the payment transaction history for the current user.
    """
    user_id = user_session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    supabase = get_supabase_client()
    
    try:
        query = await async_db(lambda: supabase.table("payment_transactions") \
            .select("id, provider_payment_id, provider_subscription_id, amount, currency, status, created_at, payment_type, metadata, provider") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .execute())
            
        return {"status": "ok", "transactions": query.data}
    except Exception as e:
        logger.error(f"Error fetching payment history for user {user_id}: {e}")
        raise HTTPException(status_code=400, detail="Failed to fetch payment history")
