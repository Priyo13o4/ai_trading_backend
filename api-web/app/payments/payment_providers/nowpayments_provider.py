import hmac
import hashlib
import json
import logging
import os
import httpx
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from app.payments.payment_providers.base import PaymentProvider
from app.payments.constants import PaymentTransactionStatus
from app.db import get_supabase_client

logger = logging.getLogger(__name__)

class NowPaymentsProvider(PaymentProvider):
    def __init__(self):
        self.api_key = os.getenv("NOWPAYMENTS_API_KEY")
        self.webhook_secret = os.getenv("NOWPAYMENTS_WEBHOOK_SECRET")
        self.plan_id_core = os.getenv("NOWPAYMENTS_PLAN_ID_CORE", "1062307590") # Default to doc example if missing
        self.email = os.getenv("NOWPAYMENTS_EMAIL")
        self.password = os.getenv("NOWPAYMENTS_PASSWORD")
        self.api_url = "https://api.nowpayments.io/v1"
        
        self._auth_token = None
        self._token_expires_at = None

    async def _get_auth_token(self) -> str:
        """
        Fetches a JWT token from NOWPayments. 
        Tokens are valid for 5 minutes.
        """
        if self._auth_token and self._token_expires_at and datetime.now() < self._token_expires_at:
            return self._auth_token

        if not self.email or not self.password:
            raise ValueError("NOWPAYMENTS_EMAIL or NOWPAYMENTS_PASSWORD not configured in .env")

        url = f"{self.api_url}/auth"
        data = {
            "email": self.email,
            "password": self.password
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=data)
                if response.status_code != 200:
                    logger.error(f"NOWPayments auth failed: {response.text}")
                    raise Exception(f"Failed to authenticate with NOWPayments: {response.status_code}")
                
                result = response.json()
                self._auth_token = result.get("token")
                # Set local cache expiry to 4 minutes for safety margin
                self._token_expires_at = datetime.now() + timedelta(minutes=4)
                return self._auth_token
            except Exception as e:
                logger.error(f"NOWPayments auth exception: {e}")
                raise

    async def create_checkout(self, user_id: str, plan_id: str, billing_period: str = "monthly") -> Dict[str, Any]:
        """
        Scenario B: Creates a subscription by email.
        The user receives a payment link in their inbox.
        """
        if not self.api_key:
            raise ValueError("NOWPayments API key not configured")

        # 1. Get JWT Auth Token
        token = await self._get_auth_token()
        
        # 2. Fetch User Email from Profiles
        supabase = get_supabase_client()
        profile_res = supabase.table("profiles").select("email").eq("id", user_id).execute()
        if not profile_res.data:
            raise ValueError(f"User profile not found for ID: {user_id}")
        user_email = profile_res.data[0]["email"]

        # 3. Create Subscription
        # Endpoint: POST /v1/subscriptions
        url = f"{self.api_url}/subscriptions"
        headers = {
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "subscription_plan_id": int(self.plan_id_core),
            "email": user_email
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            
            if response.status_code not in (200, 201):
                logger.error(f"NOWPayments create subscription failed: {response.text}")
                raise Exception(f"Failed to create NOWPayments subscription: {response.text}")
                
            result = response.json().get("result", {})
            
            # Scenario B status is usually 'WAITING_PAY'
            return {
                "checkout_url": None, # NOWPayments sends the link via email
                "provider_checkout_data": result,
                "provider_payment_id": str(result.get("id")),
                "amount": float(result.get("amount") or 5.00)
            }

    async def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verifies the NOWPayments webhook signature (HMAC SHA512).
        Keys must be sorted alphabetically before hashing.
        """
        if not self.webhook_secret:
            logger.error("NOWPayments webhook secret not configured")
            return False
            
        try:
            body_dict = json.loads(payload.decode('utf-8'))
            sorted_body = json.dumps(body_dict, separators=(',', ':'), sort_keys=True)
            
            mac = hmac.new(
                bytes(self.webhook_secret, "utf-8"),
                bytes(sorted_body, "utf-8"),
                hashlib.sha512
            )
            
            expected_signature = mac.hexdigest()
            return hmac.compare_digest(expected_signature, signature)
        except Exception as e:
            logger.error(f"Error verifying NOWPayments signature: {e}")
            return False

    async def process_webhook(self, payload: Dict[str, Any]) -> None:
        pass

    def map_event_to_state(self, status: str) -> Optional[PaymentTransactionStatus]:
        mapping = {
            "WAITING_PAY": PaymentTransactionStatus.PENDING,
            "PAID": PaymentTransactionStatus.SUCCEEDED,
            "PARTIALLY_PAID": PaymentTransactionStatus.PROCESSING,
            "EXPIRED": PaymentTransactionStatus.EXPIRED,
            "FINISHED": PaymentTransactionStatus.SUCCEEDED,
            "FAILED": PaymentTransactionStatus.FAILED,
            "REFUNDED": PaymentTransactionStatus.REFUNDED
        }
        return mapping.get(status)

    async def cancel_subscription(self, subscription_id: str) -> bool:
        """
        Scenario B: Delete/Cancel a subscription using JWT auth.
        """
        token = await self._get_auth_token()
        url = f"{self.api_url}/subscriptions/{subscription_id}"
        headers = {
            "Authorization": f"Bearer {token}"
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.delete(url, headers=headers)
                if response.status_code in (200, 204):
                    logger.info(f"NOWPayments subscription {subscription_id} cancelled via API")
                    return True
                else:
                    logger.error(f"Failed to cancel NOWPayments subscription {subscription_id}: {response.text}")
                    return False
            except Exception as e:
                logger.error(f"NOWPayments cancellation exception: {e}")
                return False

