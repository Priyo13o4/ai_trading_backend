from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class PaymentProvider(ABC):
    @abstractmethod
    async def create_checkout(self, user_id: str, plan_id: str, billing_period: str = "monthly") -> Dict[str, Any]:
        """
        Creates a checkout session, payment intent, or invoice.
        Returns provider-specific data (e.g., checkout URL, integration keys, order ID).
        """
        pass

    @abstractmethod
    async def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verifies the authenticity of a webhook payload.
        """
        pass

    @abstractmethod
    async def process_webhook(self, payload: Dict[str, Any]) -> None:
        """
        Processes a webhook event according to the provider's logic.
        Handles idempotency and maps provider events to internal state transitions.
        """
        pass

    @abstractmethod
    async def cancel_subscription(self, subscription_id: str) -> bool:
        """
        Cancels an active subscription at the provider level.
        Returns True if successful.
        """
        pass
