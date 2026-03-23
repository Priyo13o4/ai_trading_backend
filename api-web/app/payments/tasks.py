import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta

import httpx

from app.db import get_supabase_client, get_supabase_project_host, reset_supabase_client
from app.payments.constants import PaymentTransactionStatus
from app.payments.payment_providers.plisio_provider import PlisioProvider
from app.payments.payment_providers.router import get_provider
from app.redis_cache import CACHE_REDIS

logger = logging.getLogger(__name__)

DEFERRED_CANCELLATION_JANITOR_INTERVAL_SECONDS = 3600  # run every 1 hour
CANCELLATION_LEAD_TIME_HOURS = 24
PLISIO_RENEWAL_JANITOR_INTERVAL_SECONDS = 1800  # run every 30 minutes
PLISIO_RENEWAL_LEAD_TIME_HOURS = 24
STALE_PENDING_ATTEMPTS_MAX_AGE_HOURS = 72
STALE_PENDING_ATTEMPTS_MAX_ROWS_PER_TICK = 200
ACTIVATION_RETRY_MAX_ROWS_PER_TICK = 300
JANITOR_LEADER_LOCK_RETRY_SECONDS = int((os.getenv("JANITOR_LEADER_LOCK_RETRY_SECONDS") or "5").strip() or "5")


async def _acquire_janitor_tick_lock(lock_name: str, ttl_seconds: int) -> bool:
    token = uuid.uuid4().hex
    return bool(await CACHE_REDIS.set(f"janitor:leader:{lock_name}", token, nx=True, ex=ttl_seconds))


def _parse_iso_datetime(raw_value: str) -> datetime:
    parsed = raw_value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(parsed)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_optional_iso_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return _parse_iso_datetime(raw_value)
    except Exception:
        return None


def _coerce_metadata(value):
    return value if isinstance(value, dict) else {}


def _is_duplicate_transaction_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "duplicate key" in msg or "uq_payment_transactions_provider_payment_id" in msg


def _is_dns_or_connect_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, httpx.ConnectError) or "name or service not known" in msg


def _build_cycle_marker(expires_at_dt: datetime) -> str:
    return expires_at_dt.date().isoformat()


def _build_order_number(subscription_id: str, cycle_marker: str) -> str:
    sanitized_cycle = cycle_marker.replace("-", "")
    return f"renewal-{subscription_id}-{sanitized_cycle}"


def _resolve_plan_context(subscription_row: dict) -> tuple[str, str, str]:
    plan_snapshot = _coerce_metadata(subscription_row.get("plan_snapshot"))
    plan_id = str(subscription_row.get("plan_id") or "").strip()
    plan_name = str(plan_snapshot.get("plan_name") or plan_snapshot.get("display_name") or plan_id).strip()
    billing_period = str(plan_snapshot.get("billing_period") or "monthly").strip() or "monthly"
    return plan_id, plan_name, billing_period


def _has_pending_renewal_for_cycle(rows: list[dict], cycle_marker: str, order_number: str) -> bool:
    for row in rows:
        if str(row.get("provider_payment_id") or "") == order_number:
            return True
        metadata = _coerce_metadata(row.get("metadata"))
        if str(metadata.get("renewal_cycle_marker") or "") == cycle_marker:
            return True
    return False

async def _run_deferred_cancellation_tick() -> int:
    """
    Checks the database for subscriptions that are flagged to cancel_at_period_end
    but haven't actually been cancelled at the provider level yet (cancelled_at is null).
    If they are within 24 hours of expiration, it invokes the provider's cancel API.
    """
    try:
        supabase = get_supabase_client()
        
        # 1. Fetch active subscriptions flagged for cancellation
        query = supabase.table("user_subscriptions") \
            .select("id, user_id, payment_provider, external_subscription_id, expires_at") \
            .eq("status", "active") \
            .eq("cancel_at_period_end", True) \
            .is_("cancelled_at", "null") \
            .execute()
            
        subscriptions = query.data
        if not subscriptions:
            return 0
            
        cancelled_count = 0
        now_utc = datetime.now(timezone.utc)
        
        for sub in subscriptions:
            expires_at_str = sub.get("expires_at")
            if not expires_at_str:
                continue
                
            try:
                # Parse expires_at
                parsed = expires_at_str.strip().replace("Z", "+00:00")
                expires_at_dt = datetime.fromisoformat(parsed)
                if expires_at_dt.tzinfo is None:
                    expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                    
                # Check if within lead time (or already past)
                time_until_expiry = expires_at_dt - now_utc
                if time_until_expiry <= timedelta(hours=CANCELLATION_LEAD_TIME_HOURS):
                    
                    provider_name = sub.get("payment_provider")
                    external_id = sub.get("external_subscription_id")
                    
                    if provider_name == "manual" or not external_id:
                        # Manual subscriptions are just cancelled internally
                        supabase.table("user_subscriptions").update({
                            "cancelled_at": now_utc.isoformat(),
                            "auto_renew": False
                        }).eq("id", sub["id"]).execute()
                        cancelled_count += 1
                        continue

                    if provider_name == "plisio":
                        # Plisio path is invoice-based in Phase 1; skip provider-side cancellation calls.
                        logger.info("[DEFERRED CANCEL] Plisio skip provider cancel for sub %s", sub["id"])
                        supabase.table("user_subscriptions").update({
                            "cancelled_at": now_utc.isoformat(),
                            "auto_renew": False
                        }).eq("id", sub["id"]).execute()
                        cancelled_count += 1
                        continue
                        
                    # Call provider
                    provider = get_provider(provider_name)
                    success = await provider.cancel_subscription(external_id)
                    
                    if success:
                        # Update DB
                        # Note: status is still 'active', expire_subscriptions cron will flip it to 'expired'
                        supabase.table("user_subscriptions").update({
                            "cancelled_at": now_utc.isoformat(),
                            "auto_renew": False
                        }).eq("id", sub["id"]).execute()
                        
                        logger.info(f"[DEFERRED CANCEL] Successfully executed provider cancellation for sub {sub['id']}")
                        cancelled_count += 1
                    else:
                        logger.warning(f"[DEFERRED CANCEL] Provider failed to cancel sub {sub['id']}")
                        
            except Exception as e:
                logger.error(f"[DEFERRED CANCEL] Error processing sub {sub['id']}: {e}")
                continue
                
        return cancelled_count
        
    except Exception as e:
        logger.error(f"[DEFERRED CANCEL] Tick failed: {e}")
        return 0

async def deferred_cancellation_janitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("[JANITOR] Deferred cancellation janitor started")
    while not stop_event.is_set():
        try:
            leader_ttl = max(30, DEFERRED_CANCELLATION_JANITOR_INTERVAL_SECONDS - 5)
            if not await _acquire_janitor_tick_lock("deferred_cancellation", leader_ttl):
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=JANITOR_LEADER_LOCK_RETRY_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            cancelled = await _run_deferred_cancellation_tick()
            if cancelled > 0:
                logger.info(f"[JANITOR] Deferred cancellation processed {cancelled} subscriptions")

            stale_transitioned = await _run_stale_pending_attempts_tick()
            if stale_transitioned > 0:
                logger.info("[JANITOR] Stale pending attempts transitioned: %s", stale_transitioned)

            retried_activations = await _run_subscription_activation_retry_tick()
            if retried_activations > 0:
                logger.info("[JANITOR] Activation retries completed: %s", retried_activations)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[JANITOR] Deferred cancellation janitor tick failed: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=DEFERRED_CANCELLATION_JANITOR_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue

    logger.info("[JANITOR] Deferred cancellation janitor stopped")


async def _run_stale_pending_attempts_tick() -> int:
    """
    Transitions very old unresolved payment attempts away from pending/processing.
    This provides an internal liveness path even if callbacks are missing forever.
    """
    try:
        supabase = get_supabase_client()
        now_utc = datetime.now(timezone.utc)
        cutoff_dt = now_utc - timedelta(hours=STALE_PENDING_ATTEMPTS_MAX_AGE_HOURS)
        cutoff_iso = cutoff_dt.isoformat()

        stale_candidates = (
            supabase.table("payment_transactions")
            .select("id, provider, provider_payment_id, status, created_at, last_provider_event_time")
            .in_(
                "status",
                [
                    PaymentTransactionStatus.PENDING.value,
                    PaymentTransactionStatus.PROCESSING.value,
                ],
            )
            .lt("created_at", cutoff_iso)
            .order("created_at", desc=False)
            .limit(STALE_PENDING_ATTEMPTS_MAX_ROWS_PER_TICK)
            .execute()
            .data
            or []
        )

        if not stale_candidates:
            return 0

        transitioned = 0
        now_iso = now_utc.isoformat()

        for tx in stale_candidates:
            tx_id = tx.get("id")
            if not tx_id:
                continue

            # If provider emitted a fresh event recently, leave it for webhooks.
            last_event_dt = _parse_optional_iso_datetime(tx.get("last_provider_event_time"))
            if last_event_dt and last_event_dt > cutoff_dt:
                continue

            provider_name = str(tx.get("provider") or "").strip().lower()
            provider_payment_id = str(tx.get("provider_payment_id") or "")
            previous_status = str(tx.get("status") or "").strip().lower()

            target_status = PaymentTransactionStatus.EXPIRED
            resolve_failed = False

            if provider_name == "razorpay" and provider_payment_id:
                try:
                    provider = get_provider("razorpay")
                    resolved_status = await provider.resolve_checkout_attempt_status(provider_payment_id)

                    if resolved_status in {
                        PaymentTransactionStatus.SUCCEEDED,
                        PaymentTransactionStatus.FAILED,
                        PaymentTransactionStatus.CANCELLED,
                        PaymentTransactionStatus.REFUNDED,
                        PaymentTransactionStatus.EXPIRED,
                    }:
                        target_status = resolved_status
                    elif resolved_status in {
                        PaymentTransactionStatus.PENDING,
                        PaymentTransactionStatus.PROCESSING,
                    }:
                        cancelled = await provider.cancel_checkout_attempt(provider_payment_id)
                        target_status = (
                            PaymentTransactionStatus.CANCELLED if cancelled else PaymentTransactionStatus.EXPIRED
                        )
                except Exception as exc:
                    resolve_failed = True
                    logger.warning(
                        "[STALE PENDING] Razorpay resolve/cancel failed for tx=%s payment_id=%s: %s",
                        tx_id,
                        provider_payment_id,
                        exc,
                    )

            if provider_name == "razorpay" and resolve_failed:
                continue

            if target_status.value == previous_status:
                continue

            update_result = (
                supabase.table("payment_transactions")
                .update(
                    {
                        "status": target_status.value,
                        "last_provider_event_time": now_iso,
                        "updated_at": now_iso,
                    }
                )
                .eq("id", tx_id)
                .in_(
                    "status",
                    [
                        PaymentTransactionStatus.PENDING.value,
                        PaymentTransactionStatus.PROCESSING.value,
                    ],
                )
                .execute()
            )

            if not update_result.data:
                continue

            supabase.table("payment_audit_logs").insert(
                {
                    "transaction_id": tx_id,
                    "entity_type": "payment_transaction",
                    "entity_id": tx_id,
                    "previous_state": previous_status,
                    "new_state": target_status.value,
                    "trigger_source": "stale_pending_janitor",
                    "metadata": {
                        "reason": "stale_unresolved_attempt",
                        "max_age_hours": STALE_PENDING_ATTEMPTS_MAX_AGE_HOURS,
                    },
                }
            ).execute()

            transitioned += 1

        return transitioned
    except Exception as exc:
        logger.error("[STALE PENDING] Tick failed: %s", exc, exc_info=True)
        return 0


async def _run_subscription_activation_retry_tick() -> int:
    """Retry subscription activation for succeeded transactions marked as retry-required."""
    try:
        supabase = get_supabase_client()
        candidates = (
            supabase.table("payment_transactions")
            .select("id, user_id, provider, provider_payment_id, subscription_id, metadata, updated_at")
            .eq("status", PaymentTransactionStatus.SUCCEEDED.value)
            .contains("metadata", {"activation_retry_required": True})
            .order("updated_at", desc=True)
            .limit(ACTIVATION_RETRY_MAX_ROWS_PER_TICK)
            .execute()
            .data
            or []
        )
        if not candidates:
            return 0

        retried = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        for tx in candidates:
            tx_id = tx.get("id")
            tx_user_id = tx.get("user_id")
            provider_name = str(tx.get("provider") or "").strip().lower()
            provider_payment_id = str(tx.get("provider_payment_id") or "")
            subscription_id = tx.get("subscription_id")
            tx_metadata = _coerce_metadata(tx.get("metadata"))
            tx_updated_at = str(tx.get("updated_at") or "").strip()
            if not tx_id or not tx_user_id or not tx_metadata.get("activation_retry_required") or not tx_updated_at:
                continue

            claim_token = uuid.uuid4().hex
            claimed_metadata = dict(tx_metadata)
            claimed_metadata["activation_retry_claim_token"] = claim_token
            claimed_metadata["activation_retry_claimed_at"] = now_iso

            claim_result = (
                supabase.table("payment_transactions")
                .update(
                    {
                        "metadata": claimed_metadata,
                        "updated_at": now_iso,
                    }
                )
                .eq("id", tx_id)
                .eq("status", PaymentTransactionStatus.SUCCEEDED.value)
                .eq("updated_at", tx_updated_at)
                .contains("metadata", {"activation_retry_required": True})
                .execute()
            )
            if not claim_result.data:
                # Another worker already claimed or updated this row.
                continue

            try:
                renewal_requested = provider_name == "plisio" and (
                    bool(tx_metadata.get("renewal_intent")) or bool(subscription_id)
                )

                if renewal_requested:
                    if not subscription_id:
                        raise ValueError("renewal retry missing subscription_id")

                    renew_response = supabase.rpc(
                        "renew_subscription",
                        {
                            "p_subscription_id": subscription_id,
                            "p_payment_id": tx_id,
                        },
                    ).execute()
                    if renew_response.data is not True:
                        raise ValueError(f"renew_subscription returned non-true: {renew_response.data}")
                else:
                    plan_id = tx_metadata.get("plan_id")
                    if not plan_id:
                        raise ValueError("first-time activation retry missing plan_id")

                    try:
                        uuid.UUID(str(plan_id))
                    except (ValueError, TypeError):
                        plan_lookup = (
                            supabase.table("subscription_plans")
                            .select("id")
                            .eq("name", plan_id)
                            .limit(1)
                            .execute()
                        )
                        if plan_lookup.data:
                            plan_id = plan_lookup.data[0]["id"]
                        else:
                            raise ValueError(f"could not resolve plan name to UUID: {plan_id}")

                    sub_response = supabase.rpc(
                        "create_subscription",
                        {
                            "p_user_id": tx_user_id,
                            "p_plan_id": plan_id,
                            "p_payment_provider": provider_name,
                            "p_external_id": provider_payment_id,
                            "p_trial_days": 0,
                        },
                    ).execute()

                    new_sub_id = sub_response.data
                    if not new_sub_id:
                        raise ValueError("create_subscription returned empty result")

                    supabase.table("payment_transactions").update(
                        {
                            "subscription_id": new_sub_id,
                        }
                    ).eq("id", tx_id).execute()

                from app.authn.session_store import update_all_sessions_for_user_perms

                await update_all_sessions_for_user_perms(
                    tx_user_id,
                    plan="core",
                    permissions=["active_subscriber"],
                )

                latest_metadata = _coerce_metadata((claim_result.data[0] or {}).get("metadata"))
                cleared_metadata = dict(latest_metadata)
                cleared_metadata["activation_retry_required"] = False
                cleared_metadata["activation_retry_completed_at"] = now_iso
                cleared_metadata.pop("activation_retry_last_error", None)
                cleared_metadata.pop("activation_retry_claim_token", None)
                cleared_metadata.pop("activation_retry_claimed_at", None)

                complete_update = (
                    supabase.table("payment_transactions")
                    .update(
                        {
                            "metadata": cleared_metadata,
                            "updated_at": now_iso,
                        }
                    )
                    .eq("id", tx_id)
                    .eq("status", PaymentTransactionStatus.SUCCEEDED.value)
                    .contains("metadata", {"activation_retry_claim_token": claim_token})
                    .execute()
                )
                if not complete_update.data:
                    logger.info("[ACTIVATION RETRY] Lost claim before completion tx=%s", tx_id)
                    continue

                supabase.table("payment_audit_logs").insert(
                    {
                        "transaction_id": tx_id,
                        "entity_type": "payment_transaction",
                        "entity_id": tx_id,
                        "previous_state": PaymentTransactionStatus.SUCCEEDED.value,
                        "new_state": PaymentTransactionStatus.SUCCEEDED.value,
                        "trigger_source": "activation_retry_janitor",
                        "reason": "subscription_activation_retry_succeeded",
                        "metadata": {
                            "retry_required": False,
                        },
                    }
                ).execute()

                retried += 1
            except Exception as exc:
                failed_metadata = dict(claimed_metadata)
                failed_metadata["activation_retry_required"] = True
                failed_metadata["activation_retry_updated_at"] = now_iso
                failed_metadata["activation_retry_last_error"] = str(exc)[:500]
                failed_metadata.pop("activation_retry_claim_token", None)
                failed_metadata.pop("activation_retry_claimed_at", None)

                failed_update = (
                    supabase.table("payment_transactions")
                    .update(
                        {
                            "metadata": failed_metadata,
                            "updated_at": now_iso,
                        }
                    )
                    .eq("id", tx_id)
                    .eq("status", PaymentTransactionStatus.SUCCEEDED.value)
                    .contains("metadata", {"activation_retry_claim_token": claim_token})
                    .execute()
                )
                if not failed_update.data:
                    logger.info("[ACTIVATION RETRY] Lost claim before failure write tx=%s", tx_id)
                    continue

                supabase.table("payment_audit_logs").insert(
                    {
                        "transaction_id": tx_id,
                        "entity_type": "payment_transaction",
                        "entity_id": tx_id,
                        "previous_state": PaymentTransactionStatus.SUCCEEDED.value,
                        "new_state": PaymentTransactionStatus.SUCCEEDED.value,
                        "trigger_source": "activation_retry_janitor",
                        "reason": "subscription_activation_retry_failed",
                        "metadata": {
                            "retry_required": True,
                            "error": str(exc)[:500],
                        },
                    }
                ).execute()

                logger.warning("[ACTIVATION RETRY] Failed tx=%s: %s", tx_id, exc)

        return retried
    except Exception as exc:
        logger.error("[ACTIVATION RETRY] Tick failed: %s", exc, exc_info=True)
        return 0


async def _run_plisio_renewal_invoice_janitor_tick() -> int:
    """Create Plisio renewal invoices for subscriptions nearing expiration."""
    try:
        now_utc = datetime.now(timezone.utc)
        lead_cutoff = now_utc + timedelta(hours=PLISIO_RENEWAL_LEAD_TIME_HOURS)

        candidates = []
        for attempt in range(2):
            try:
                supabase = get_supabase_client()
                query = (
                    supabase.table("user_subscriptions")
                    .select("id, user_id, plan_id, plan_snapshot, expires_at, payment_provider, auto_renew, status")
                    .eq("payment_provider", "plisio")
                    .eq("status", "active")
                    .eq("auto_renew", True)
                    .is_("cancel_at_period_end", False)
                    .execute()
                )
                candidates = query.data or []
                break
            except Exception as exc:
                if attempt == 0 and _is_dns_or_connect_error(exc):
                    reset_supabase_client()
                    logger.warning(
                        "[PLISIO RENEWAL] Supabase connect/DNS issue host=%s; retrying once: %s",
                        get_supabase_project_host(),
                        exc,
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if not candidates:
            return 0

        provider = get_provider("plisio")
        if not isinstance(provider, PlisioProvider):
            logger.error("[PLISIO RENEWAL] Plisio provider instance unavailable")
            return 0

        created_count = 0
        for sub in candidates:
            sub_id = str(sub.get("id") or "")
            user_id = str(sub.get("user_id") or "")
            if not sub_id or not user_id:
                continue

            expires_at_str = sub.get("expires_at")
            if not expires_at_str:
                continue

            try:
                expires_at_dt = _parse_iso_datetime(expires_at_str)
            except Exception:
                logger.warning("[PLISIO RENEWAL] Invalid expires_at for sub %s", sub_id)
                continue

            if expires_at_dt > lead_cutoff:
                continue

            plan_id, plan_name, billing_period = _resolve_plan_context(sub)
            if not plan_id:
                logger.warning("[PLISIO RENEWAL] Missing plan_id for sub %s", sub_id)
                continue

            cycle_marker = _build_cycle_marker(expires_at_dt)
            order_number = _build_order_number(sub_id, cycle_marker)
            marker = f"plisio:renewal:{sub_id}:{cycle_marker}"

            pending_rows = (
                supabase.table("payment_transactions")
                .select("id, provider_payment_id, metadata")
                .eq("provider", "plisio")
                .eq("payment_type", "subscription")
                .eq("subscription_id", sub_id)
                .in_(
                    "status",
                    [
                        PaymentTransactionStatus.PENDING.value,
                        PaymentTransactionStatus.PROCESSING.value,
                    ],
                )
                .execute()
                .data
                or []
            )
            if _has_pending_renewal_for_cycle(pending_rows, cycle_marker, order_number):
                continue

            lock_key = f"janitor:{marker}"
            lock_acquired = await CACHE_REDIS.set(lock_key, "1", nx=True, ex=7 * 24 * 3600)
            if not lock_acquired:
                continue

            try:
                # Double-check after lock to avoid duplicate inserts when workers race.
                pending_rows_post_lock = (
                    supabase.table("payment_transactions")
                    .select("id, provider_payment_id, metadata")
                    .eq("provider", "plisio")
                    .eq("payment_type", "subscription")
                    .eq("subscription_id", sub_id)
                    .in_(
                        "status",
                        [
                            PaymentTransactionStatus.PENDING.value,
                            PaymentTransactionStatus.PROCESSING.value,
                        ],
                    )
                    .execute()
                    .data
                    or []
                )
                if _has_pending_renewal_for_cycle(pending_rows_post_lock, cycle_marker, order_number):
                    continue

                invoice_response = await provider.create_invoice(
                    user_id=user_id,
                    plan_id=plan_id,
                    billing_period=billing_period,
                    order_number=order_number,
                    description=f"PipFactor {plan_name} renewal ({billing_period})",
                    order_name=f"pipfactor-renewal-{plan_name}-{billing_period}",
                    ttl_minutes=int((os.getenv("PLISIO_RENEWAL_INVOICE_TTL_MINUTES") or "1440").strip() or "1440"),
                )

                provider_checkout_data = invoice_response.get("provider_checkout_data")
                if not isinstance(provider_checkout_data, dict):
                    provider_checkout_data = {}
                checkout_url = invoice_response.get("checkout_url") or provider_checkout_data.get("invoice_url")
                invoice_url = provider_checkout_data.get("invoice_url") or checkout_url

                tx_data = {
                    "user_id": user_id,
                    "subscription_id": sub_id,
                    "provider": "plisio",
                    "provider_payment_id": invoice_response.get("provider_payment_id") or order_number,
                    "amount": invoice_response.get("amount"),
                    "currency": invoice_response.get("currency", "USD"),
                    "status": PaymentTransactionStatus.PENDING.value,
                    "payment_type": "subscription",
                    "metadata": {
                        "plan_id": plan_id,
                        "plan_name": plan_name,
                        "billing_period": billing_period,
                        "checkout_url": checkout_url,
                        "invoice_url": invoice_url,
                        "provider_checkout_data": provider_checkout_data,
                        "renewal_intent": True,
                        "renewal_cycle_marker": cycle_marker,
                        "renewal_for_subscription_id": sub_id,
                        "plisio_expected_currency": str(getattr(provider, "crypto_currency", "") or "").strip().upper(),
                        "plisio_expected_source_currency": "USD",
                    },
                }

                supabase.table("payment_transactions").insert(tx_data).execute()
                created_count += 1
                logger.info(
                    "[PLISIO RENEWAL] Created renewal invoice user=%s sub=%s cycle=%s order=%s",
                    user_id,
                    sub_id,
                    cycle_marker,
                    tx_data["provider_payment_id"],
                )
            except Exception as exc:
                if _is_duplicate_transaction_error(exc):
                    logger.info("[PLISIO RENEWAL] Duplicate transaction skipped for sub=%s cycle=%s", sub_id, cycle_marker)
                    continue

                # Release Redis marker on hard failures so the next janitor tick can retry.
                await CACHE_REDIS.delete(lock_key)
                logger.error("[PLISIO RENEWAL] Failed sub=%s cycle=%s: %s", sub_id, cycle_marker, exc, exc_info=True)
                continue

        return created_count
    except Exception as exc:
        logger.error("[PLISIO RENEWAL] Tick failed: %s", exc, exc_info=True)
        return 0


async def plisio_renewal_invoice_janitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("[JANITOR] Plisio renewal invoice janitor started")
    while not stop_event.is_set():
        try:
            leader_ttl = max(30, PLISIO_RENEWAL_JANITOR_INTERVAL_SECONDS - 5)
            if not await _acquire_janitor_tick_lock("plisio_renewal", leader_ttl):
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=JANITOR_LEADER_LOCK_RETRY_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            prepared = await _run_plisio_renewal_invoice_janitor_tick()
            if prepared > 0:
                logger.info("[JANITOR] Plisio renewal janitor marked %s candidate(s)", prepared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[JANITOR] Plisio renewal janitor tick failed: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=PLISIO_RENEWAL_JANITOR_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue

    logger.info("[JANITOR] Plisio renewal invoice janitor stopped")
