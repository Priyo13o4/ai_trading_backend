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

RESUBSCRIBE_START_AT_BUFFER_SECONDS = 120

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

    async def _resolve_deferred_start_at(self, user_id: str) -> Optional[int]:
        """
        Returns a Unix timestamp to defer first charge when user already has
        current access that should not be double-billed immediately.
        """
        try:
            supabase = get_supabase_client()
            now_iso = datetime.utcnow().isoformat()
            sub_rows = (
                supabase.table("user_subscriptions")
                .select("status, expires_at, cancel_at_period_end")
                .eq("user_id", user_id)
                .in_("status", ["trial", "active"])
                .gt("expires_at", now_iso)
                .execute()
                .data
                or []
            )

            trial_start_at: Optional[int] = None
            resubscribe_start_at: Optional[int] = None

            for sub in sub_rows:
                expires_at_str = sub.get("expires_at")
                if not expires_at_str:
                    continue

                try:
                    expires_dt = parser.isoparse(expires_at_str)
                except Exception:
                    continue

                status = str(sub.get("status") or "").strip().lower()
                if status == "trial":
                    trial_ts = int(expires_dt.timestamp())
                    if trial_start_at is None or trial_ts > trial_start_at:
                        trial_start_at = trial_ts
                    continue

                if status == "active" and bool(sub.get("cancel_at_period_end")):
                    paid_ts = int(expires_dt.timestamp()) + RESUBSCRIBE_START_AT_BUFFER_SECONDS
                    if resubscribe_start_at is None or paid_ts > resubscribe_start_at:
                        resubscribe_start_at = paid_ts

            # Prefer the latest point in time if multiple valid defer signals exist.
            candidates = [v for v in [trial_start_at, resubscribe_start_at] if v is not None]
            if candidates:
                chosen = max(candidates)
                logger.info("Deferring Razorpay first charge for user %s until unix=%s", user_id, chosen)
                return chosen
        except Exception as exc:
            logger.warning(
                "Failed to resolve deferred start_at for user %s: %s. Falling back to immediate start.",
                user_id,
                exc,
            )

        return None

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
            # Razorpay amount is in paise, so 50000 = INR 500.00
            raw_amount = plan_details.get("item", {}).get("amount", 0)
        except Exception as e:
            logger.error(f"Could not fetch Razorpay plan details: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch Razorpay plan configuration")

        # 2. Defer the first charge if the user still has current access time.
        start_at = await self._resolve_deferred_start_at(user_id)

        sub_data = {
            "plan_id": rzp_plan_id,
            "total_count": 12 if billing_period == "yearly" else 120,
            "customer_notify": 1,
            "notes": {
                "user_id": user_id,
                "plan_id": plan_id
            }
        }
        
        # If user has active access window, ask Razorpay to wait before first charge.
        if start_at:
            sub_data["start_at"] = start_at
        
        try:
            subscription = self.client.subscription.create(data=sub_data)
            short_url = subscription.get("short_url")
            
            return {
                "checkout_url": short_url,
                "provider_checkout_data": {
                    "subscription_id": subscription["id"],
                    "key_id": self.key_id,
                    "currency": currency,
                    "short_url": short_url,
                },
                "provider_payment_id": subscription["id"],
                "amount": raw_amount,
                "currency": currency,
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
        Cancels the Razorpay subscription at cycle end (no further renewals).
        """
        if not self.client:
            logger.error("Razorpay client not initialized for cancellation")
            return False
            
        try:
            # Keep access through current period while stopping future auto-renewals.
            self.client.subscription.cancel(provider_subscription_id, {"cancel_at_cycle_end": 1})
            logger.info(f"Successfully scheduled Razorpay subscription cancellation at period end: {provider_subscription_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel Razorpay subscription {provider_subscription_id}: {e}")
            return False

    async def cancel_checkout_attempt(self, provider_payment_id: str) -> bool:
        """
        Best-effort cancellation for unresolved Razorpay subscription attempts.
        """
        if not self.client:
            logger.error("Razorpay client not initialized for checkout-attempt cancellation")
            return False

        if not provider_payment_id:
            return False

        try:
            sub = self.client.subscription.fetch(provider_payment_id)
            sub_status = str(sub.get("status") or "").strip().lower()
            if sub_status in {"cancelled", "completed", "halted", "expired"}:
                return True
        except Exception as exc:
            logger.warning("Razorpay fetch before cancel failed for %s: %s", provider_payment_id, exc)

        try:
            self.client.subscription.cancel(provider_payment_id, {"cancel_at_cycle_end": 0})
            logger.info("Cancelled unresolved Razorpay checkout attempt %s", provider_payment_id)
            return True
        except Exception as primary_exc:
            try:
                self.client.subscription.cancel(provider_payment_id)
                logger.info("Cancelled unresolved Razorpay checkout attempt (fallback) %s", provider_payment_id)
                return True
            except Exception as fallback_exc:
                logger.warning(
                    "Failed to cancel unresolved Razorpay attempt %s: %s | fallback: %s",
                    provider_payment_id,
                    primary_exc,
                    fallback_exc,
                )
                return False

    async def resolve_checkout_attempt_status(self, provider_payment_id: str) -> Optional[PaymentTransactionStatus]:
        """
        Provider lookup used by janitors for very old unresolved attempts.
        """
        if not self.client or not provider_payment_id:
            return None

        try:
            sub = self.client.subscription.fetch(provider_payment_id)
            sub_status = str(sub.get("status") or "").strip().lower()
            mapping = {
                "created": PaymentTransactionStatus.PENDING,
                "authenticated": PaymentTransactionStatus.PROCESSING,
                "active": PaymentTransactionStatus.SUCCEEDED,
                "completed": PaymentTransactionStatus.SUCCEEDED,
                "cancelled": PaymentTransactionStatus.CANCELLED,
                "halted": PaymentTransactionStatus.FAILED,
                "expired": PaymentTransactionStatus.EXPIRED,
            }
            return mapping.get(sub_status)
        except Exception as exc:
            logger.warning("Failed to resolve Razorpay attempt status for %s: %s", provider_payment_id, exc)
            return None
