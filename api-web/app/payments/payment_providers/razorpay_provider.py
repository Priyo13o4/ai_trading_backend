import hmac
import hashlib
import json
import logging
import os
import razorpay
from datetime import datetime
from dateutil import parser
from typing import Dict, Any, Optional
from fastapi import HTTPException

from app.db import get_supabase_client
from app.payments.payment_providers.base import PaymentProvider
from app.payments.constants import PaymentTransactionStatus

logger = logging.getLogger(__name__)

class RazorpayProvider(PaymentProvider):
    def __init__(self):
        self.key_id = os.getenv("RAZORPAY_KEY_ID")
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        self.webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
        self.plan_id_core = os.getenv("RAZORPAY_PLAN_ID_CORE")
        
        if self.key_id and self.key_secret:
            self.client = razorpay.Client(auth=(self.key_id, self.key_secret))
        else:
            self.client = None

    async def create_checkout(self, user_id: str, plan_id: str, billing_period: str = "monthly") -> Dict[str, Any]:
        """
        Creates a Razorpay subscription for the user.
        Returns the subscription_id which the frontend uses to open the JS modal.
        """
        if not self.client:
            raise ValueError("Razorpay credentials not configured")
            
        if not self.plan_id_core:
            raise ValueError("Razorpay core plan ID not configured")

        # In Razorpay, we create an active subscription in "created" state.
        # The user then authorizes it with their card via the frontend modal.
        # We assume plan is 'core' right now.
        
        # 1. Fetch Plan details to get correct currency and amount
        rzp_plan_id = self.plan_id_core
        try:
            # We assume it is plan_id_core for now or use the one passed
            target_plan_id = rzp_plan_id 
            plan_details = self.client.plan.fetch(target_plan_id)
            currency = plan_details.get("item", {}).get("currency", "INR")
            # Razorpay amount is in paise, so 50000 = 500.00
            # We return it in decimal for internal tracking
            raw_amount = plan_details.get("item", {}).get("amount", 0)
            decimal_amount = float(raw_amount) / 100.0
        except Exception as e:
            logger.error(f"Could not fetch Razorpay plan details: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch Razorpay plan configuration")

        # 2. Check for active trial to defer billing (The "Delayed Charge" fix)
        start_at = None
        try:
            supabase = get_supabase_client()
            trial_query = supabase.table("user_subscriptions") \
                .select("expires_at") \
                .eq("user_id", user_id) \
                .eq("status", "trial") \
                .gt("expires_at", datetime.utcnow().isoformat()) \
                .execute()
            
            if trial_query.data:
                trial_expiry_str = trial_query.data[0]["expires_at"]
                # Convert ISO string (from Supabase) to Unix timestamp
                dt = parser.isoparse(trial_expiry_str)
                start_at = int(dt.timestamp())
                logger.info(f"Active trial found for user {user_id}. Deferring Razorpay charge until {trial_expiry_str} (Unix: {start_at})")
        except Exception as e:
            logger.warning(f"Failed to check trial status for user {user_id}: {e}. Proceeding with immediate charge.")

        sub_data = {
            "plan_id": rzp_plan_id,
            "total_count": 12 if billing_period == "yearly" else 120,
            "customer_notify": 1,
            "notes": {
                "user_id": user_id,
                "plan_id": plan_id
            }
        }
        
        # If user is on trial, tell Razorpay to wait before the first charge
        if start_at:
            sub_data["start_at"] = start_at
        
        try:
            subscription = self.client.subscription.create(data=sub_data)
            
            return {
                "checkout_url": None, 
                "provider_checkout_data": {
                    "subscription_id": subscription["id"],
                    "key_id": self.key_id,
                    "currency": currency 
                },
                "provider_payment_id": subscription["id"],
                "amount": decimal_amount
            }
        except Exception as e:
            logger.error(f"Failed to create Razorpay subscription: {e}")
            raise

    async def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verifies the Razorpay webhook signature (HMAC SHA256).
        """
        if not self.webhook_secret:
            logger.error("Razorpay webhook secret not configured")
            return False
            
        try:
            # razorpay.Utility().verify_webhook_signature() expects string payload
            result = self.client.utility.verify_webhook_signature(
                payload.decode('utf-8'),
                signature,
                self.webhook_secret
            )
            return result
        except razorpay.errors.SignatureVerificationError:
            return False
        except Exception as e:
            logger.error(f"Error verifying Razorpay signature: {e}")
            return False

    async def process_webhook(self, payload: Dict[str, Any]) -> None:
        """
        We don't do the actual DB processing here, this is handled in webhook_handler.py.
        This provides the mapping from provider event to internal state.
        """
        pass

    def map_event_to_state(self, event_type: str) -> Optional[PaymentTransactionStatus]:
        mapping = {
            "subscription.created": PaymentTransactionStatus.PENDING,
            "subscription.authenticated": PaymentTransactionStatus.PROCESSING,
            "subscription.activated": PaymentTransactionStatus.SUCCEEDED,
            "subscription.charged": PaymentTransactionStatus.SUCCEEDED,
            "subscription.cancelled": PaymentTransactionStatus.CANCELLED,
            "subscription.halted": PaymentTransactionStatus.FAILED,
            "subscription.pending": PaymentTransactionStatus.PENDING,
            "payment.captured": PaymentTransactionStatus.SUCCEEDED,
            "payment.failed": PaymentTransactionStatus.FAILED,
            "refund.processed": PaymentTransactionStatus.REFUNDED
        }
        return mapping.get(event_type)

    async def cancel_subscription(self, provider_subscription_id: str) -> bool:
        """
        Cancels the Razorpay subscription at the end of the current billing cycle.
        """
        if not self.client:
            logger.error("Razorpay client not initialized for cancellation")
            return False
            
        try:
            # cancel_at_cycle_end=1 ensures the user keeps access until the end of their paid period.
            self.client.subscription.cancel(provider_subscription_id, {"cancel_at_cycle_end": 1})
            logger.info(f"Successfully requested cancellation for Razorpay subscription: {provider_subscription_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel Razorpay subscription {provider_subscription_id}: {e}")
            return False
