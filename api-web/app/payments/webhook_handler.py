import logging
import json
from datetime import datetime
from fastapi import APIRouter, Request, Header, HTTPException, Depends
from typing import Optional, Any

from app.db import get_supabase_client
from app.redis_cache import CACHE_REDIS
from app.authn.session_store import SESSION_REDIS
from app.payments.payment_providers.router import get_provider
from app.payments.constants import PaymentTransactionStatus

logger = logging.getLogger(__name__)

webhook_router = APIRouter()

# Note: /api/webhooks/{provider_name} paths must be exempt from CSRF in main.py
@webhook_router.post("/api/webhooks/{provider_name}")
async def handle_webhook(
    provider_name: str,
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None),
    x_nowpayments_sig: Optional[str] = Header(None)
):
    """
    Unified webhook handler for all payment providers.
    """
    try:
        provider = get_provider(provider_name)
    except HTTPException:
        return {"status": "ignored"}

    raw_body = await request.body()
    
    # Verify signature
    signature = None
    if provider_name == "razorpay":
        signature = x_razorpay_signature
    elif provider_name == "nowpayments":
        signature = x_nowpayments_sig
        
    if not signature:
        logger.warning(f"Missing signature for {provider_name} webhook")
        raise HTTPException(status_code=400, detail="Missing signature")
        
    is_valid = await provider.verify_webhook_signature(raw_body, signature)
    if not is_valid:
        logger.error(f"Invalid webhook signature for {provider_name}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode('utf-8'))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Extract event ID for idempotency
    event_id = None
    event_type = None
    
    if provider_name == "razorpay":
        # Razorpay sends a unique event ID in the header X-Razorpay-Event-Id
        event_id = x_razorpay_event_id or payload.get("id") # Fallback to body id if header is missing
        event_type = payload.get("event")
    elif provider_name == "nowpayments":
        # Supports both Scenario A (payment_id) and Scenario B (id)
        event_id = str(payload.get("id") or payload.get("payment_id"))
        event_type = payload.get("status") or payload.get("payment_status")
        
    if not event_id:
        logger.error(f"Could not extract event ID from {provider_name} webhook")
        return {"status": "error", "message": "Missing event ID"}

    # Fast-path idempotency check with Redis (cache for 24h)
    redis_key = f"webhook_event:{provider_name}:{event_id}"
    if await CACHE_REDIS.get(redis_key):
        logger.info(f"Webhook event already processed (Redis cache): {event_id}")
        return {"status": "ok", "message": "Already processed"}

    # Set temporary lock to prevent concurrent processing of the exact same event
    lock_key = f"webhook_lock:{provider_name}:{event_id}"
    lock_acquired = await CACHE_REDIS.set(lock_key, "locked", nx=True, ex=30)
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
            # If unique constraint violation, it was already processed
            logger.info(f"Webhook event already persists in DB: {event_id}")
            return {"status": "ok"}

        # Custom processing logic based on provider
        new_status = provider.map_event_to_state(event_type)
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
                
        elif provider_name == "nowpayments":
            provider_payment_id = str(payload.get("payment_id"))
            # Custom logic: NOWPayments allows setting `order_id` which we can use to match
            # But we already created an initial pending transaction with provider_payment_id
            
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
        
        # Update transaction status
        is_recurring_charge = payload.get("is_recurring_charge") or (provider_name == "razorpay" and event_type == "subscription.charged")
        if is_recurring_charge or transaction["status"] not in [PaymentTransactionStatus.SUCCEEDED.value, PaymentTransactionStatus.FAILED.value, PaymentTransactionStatus.EXPIRED.value]:
            supabase.table("payment_transactions").update({
                "status": new_status.value,
                "last_provider_event_time": datetime.utcnow().isoformat()
            }).eq("id", tx_id).execute()
            
            # Write audit log
            supabase.table("payment_audit_logs").insert({
                "transaction_id": tx_id,
                "entity_type": "payment_transaction",
                "entity_id": tx_id,
                "previous_state": transaction["status"],
                "new_state": new_status.value,
                "trigger_source": f"{provider_name}_webhook",
                "trigger_event_id": event_id
            }).execute()

            # If successful, we need to activate the subscription in `user_subscriptions`
            if new_status == PaymentTransactionStatus.SUCCEEDED:
                # We expect the `payment_transactions` to have a `metadata` containing `plan_id`
                tx_metadata = transaction.get("metadata", {})
                plan_id = tx_metadata.get("plan_id")
                
                if plan_id:
                    # Resolve plan_id name to UUID if necessary
                    import uuid
                    try:
                        uuid.UUID(str(plan_id))
                    except (ValueError, TypeError):
                        # Not a UUID, assume it's a plan name
                        plan_lookup = supabase.table("subscription_plans").select("id").eq("name", plan_id).execute()
                        if plan_lookup.data:
                            plan_id = plan_lookup.data[0]["id"]
                        else:
                            logger.error(f"Could not resolve plan name '{plan_id}' to a UUID")
                            plan_id = None

                if plan_id:
                    # Let's call the RPC create_subscription or update manually
                    # Since we have the RPC `create_subscription`
                    try:
                        # Call create_subscription via RPC
                        sub_response = supabase.rpc('create_subscription', {
                            'p_user_id': tx_user_id,
                            'p_plan_id': plan_id,
                            'p_payment_provider': provider_name,
                            'p_external_id': provider_payment_id,
                            'p_trial_days': 0
                        }).execute()
                        
                        new_sub_id = sub_response.data
                        
                        # Link transaction to new subscription
                        supabase.table("payment_transactions").update({
                            "subscription_id": new_sub_id
                        }).eq("id", tx_id).execute()
                        
                        # Invalidate permissions cache (from auth flow) so frontend gets new access
                        from app.authn.session_store import update_all_sessions_for_user_perms
                        await update_all_sessions_for_user_perms(tx_user_id, plan='core', permissions=['active_subscriber'])
                        
                    except Exception as sub_err:
                        logger.error(f"Failed to create subscription for user {tx_user_id}: {sub_err}")

        # Mark webhook event as processed
        supabase.table("webhook_events").update({
            "processed": True,
            "processed_at": datetime.utcnow().isoformat()
        }).eq("provider", provider_name).eq("event_id", event_id).execute()
        
        # Cache ID in Redis representing processed webhook
        await CACHE_REDIS.set(redis_key, "processed", ex=86400)
        
        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        await CACHE_REDIS.delete(lock_key)
