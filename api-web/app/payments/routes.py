import logging
from datetime import datetime
from typing import Dict, Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request

from app.authn.deps import require_session
from app.db import get_supabase_client
from app.payments.payment_providers.router import get_provider
from app.payments.constants import PaymentTransactionStatus

# We rate limit checkout creation heavily
from app.authn.rate_limit_auth import rate_limit

logger = logging.getLogger(__name__)

payments_router = APIRouter(prefix="/api/payments")

class CreateCheckoutRequest(BaseModel):
    plan_id: str
    provider: str
    billing_period: Optional[str] = "monthly"

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
    plan_response = supabase.table("subscription_plans").select("*").eq("name", req.plan_id).eq("is_active", True).execute()
    
    if not plan_response.data:
        raise HTTPException(status_code=404, detail="Active subscription plan not found")
        
    plan = plan_response.data[0]
    
    # Check if a pending transaction already exists in recent time to avoid spam? 
    # Opted against strict preventing here, rate limit handles abuse.

    try:
        provider = get_provider(req.provider)
    except HTTPException as e:
        raise e

    try:
        # Call provider SDK logic
        checkout_response = await provider.create_checkout(
            user_id=user_id,
            plan_id=req.plan_id,
            billing_period=req.billing_period
        )
        
        provider_payment_id = checkout_response.get("provider_payment_id")
        
        # Create pending Payment Transaction in DB
        tx_data = {
            "user_id": user_id,
            "provider": req.provider,
            "provider_payment_id": provider_payment_id,
            "amount": checkout_response.get("amount", plan["price_usd"]),
            "currency": checkout_response.get("currency", "USD"),
            "status": PaymentTransactionStatus.PENDING.value,
            "payment_type": "subscription",
            "metadata": {
                "plan_id": req.plan_id,
                "billing_period": req.billing_period
            }
        }
        
        supabase.table("payment_transactions").insert(tx_data).execute()
        
        return checkout_response

    except Exception as e:
        logger.error(f"Error creating checkout for {req.provider}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@payments_router.post("/cancel-subscription")
async def cancel_subscription(
    user_session: dict = Depends(require_session)
):
    """
    Flags the user's active subscription to be cancelled at the end of the period.
    Calls the actual provider API to cancel natively, and also updates the DB.
    """
    user_id = user_session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    supabase = get_supabase_client()
    
    # 1. Find active subscription that is not already flagged for cancellation
    try:
        sub_query = supabase.table("user_subscriptions") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "active") \
            .is_("cancel_at_period_end", False) \
            .execute()
        
        if not sub_query.data:
            # Maybe it is already cancelled at period end
            check_query = supabase.table("user_subscriptions") \
                .select("*") \
                .eq("user_id", user_id) \
                .eq("status", "active") \
                .is_("cancel_at_period_end", True) \
                .execute()
            if check_query.data:
                return {"status": "ok", "message": "Subscription is already scheduled for cancellation"}
            raise HTTPException(status_code=404, detail="No active cancelable subscription found")
            
        subscription = sub_query.data[0]
        
        provider_name = subscription.get("payment_provider")
        provider_subscription_id = subscription.get("external_subscription_id")

        if provider_name and provider_subscription_id:
            try:
                provider = get_provider(provider_name)
                # Attempt to cancel at the provider level natively
                cancel_success = await provider.cancel_subscription(provider_subscription_id)
                if not cancel_success:
                    logger.warning(f"Provider {provider_name} failed to cancel subscription {provider_subscription_id}. Falling back to DB-only deferred cancellation.")
            except Exception as e:
                logger.warning(f"Could not cancel subscription via provider {provider_name}: {e}")
        
        # We only flag it in the DB
        supabase.table("user_subscriptions").update({
            "cancel_at_period_end": True
        }).eq("id", subscription["id"]).execute()
        
        # Log the user intent
        supabase.table("payment_audit_logs").insert({
            "entity_type": "user_subscription",
            "entity_id": subscription["id"],
            "previous_state": "active",
            "new_state": "cancelling_deferred",
            "trigger_source": "user_request",
            "metadata": {"external_id": subscription.get("external_subscription_id")}
        }).execute()
        
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
        sub_query = supabase.table("user_subscriptions") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "active") \
            .is_("cancel_at_period_end", True) \
            .execute()
        
        if not sub_query.data:
            raise HTTPException(status_code=404, detail="No subscription pending cancellation found")
            
        subscription = sub_query.data[0]
        
        # If cancelled_at is NOT NULL, the cron job already told Razorpay it's over.
        # We cannot reverse a Razorpay cancellation once sent.
        if subscription.get("cancelled_at") is not None:
            raise HTTPException(status_code=400, detail="Subscription cancellation has already been finalized by the provider. You must wait for expiration to resubscribe.")
            
        # Revert the flag
        supabase.table("user_subscriptions").update({
            "cancel_at_period_end": False
        }).eq("id", subscription["id"]).execute()
        
        supabase.table("payment_audit_logs").insert({
            "entity_type": "user_subscription",
            "entity_id": subscription["id"],
            "previous_state": "cancelling_deferred",
            "new_state": "active_resumed",
            "trigger_source": "user_request",
            "metadata": {"external_id": subscription.get("external_subscription_id")}
        }).execute()
        
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
        query = supabase.table("payment_transactions") \
            .select("id, provider_payment_id, amount, currency, status, created_at, payment_type, metadata, provider") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .execute()
            
        return {"status": "ok", "transactions": query.data}
    except Exception as e:
        logger.error(f"Error fetching payment history for user {user_id}: {e}")
        raise HTTPException(status_code=400, detail="Failed to fetch payment history")

