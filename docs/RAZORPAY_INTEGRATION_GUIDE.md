# Razorpay Integration Guide

This document outlines the technical integration of Razorpay into our AI Trading Bot ecosystem.
**CRITICAL:** This guide strictly adheres to the provider-agnostic sleeper architecture defined in `payment_integration.md`.

## 1. Provider Abstraction Implementation

Razorpay is our primary fiat provider. It must be implemented behind the `payment_providers/base.py` contract.
*Do not hardcode Razorpay logic directly in the FastAPI router scripts.*

### Implementation Location: `api-web/app/payments/payment_providers/razorpay_provider.py`

```python
import razorpay
import hmac
from .base import PaymentProvider

class RazorpayProvider(PaymentProvider):
    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        self.client = razorpay.Client(auth=(key_id, key_secret))
        self.webhook_secret = webhook_secret

    async def create_checkout(self, plan_id: str, amount: float, currency: str, idempotency_key: str, **kwargs):
        # amount is received in USD/fiat, must convert to paise for Razorpay
        order_data = {
            "amount": int(amount * 100), 
            "currency": currency,
            "receipt": idempotency_key,
            "payment_capture": 1
        }
        # Call Razorpay SDK
        order = self.client.order.create(data=order_data)
        
        return {
            "provider_payment_id": order["id"],
            "checkout_url": None, # Razorpay checkout happens via frontend JS, not a hosted URL
            "provider_checkout_data": {"order_id": order["id"], "amount": order["amount"], "currency": order["currency"]}
        }

    def verify_webhook(self, raw_body: bytes, headers: dict):
        # 1. HMAC SHA256 Signature Validation
        signature = headers.get("x-razorpay-signature")
        if not signature:
            raise ValueError("Missing signature")
            
        try:
            self.client.utility.verify_webhook_signature(
                raw_body.decode('utf-8'), 
                signature, 
                self.webhook_secret
            )
        except razorpay.errors.SignatureVerificationError:
            raise ValueError("Invalid signature")
            
        return True # Handled in unified webhook route wrapper
```

## 2. API Endpoints (FastAPI)

All endpoints must respect the `PAYMENTS_ENABLED` feature flag and route through the generic tables (`payment_transactions`, `webhook_events`).

### `POST /api/payments/create-checkout`
- **Auth:** Require cookie session via `Depends(auth_context)`.
- **Feature Gate:** `Depends(require_payments_enabled)`.
- **Flow:** Maps `plan_id` -> generates `payment_transactions` generic row (status: pending) -> delegates to `get_provider('razorpay').create_checkout()`.

### `POST /api/webhooks/{provider}`
- **Auth:** NONE (But CSRF Exempted).
- **Flow:**
    1. Verify signature via `get_provider('razorpay').verify_webhook(raw_body, headers)`.
    2. Idempotency Check: `redis_cache` and `webhook_events` insert.
    3. Update `payment_transactions` DB State Machine using Supabase `service_role`.
    4. Call Supabase RPCs (`record_payment`, `create_subscription`).
    5. Invalidate User Session Cache! `await invalidate_perms(user_id)`.

## 3. Frontend Checkout Initiation (React / Vite)

The frontend triggers the checkout modal using the Razorpay JavaScript SDK, verifying the `VITE_PAYMENTS_ENABLED` flag first.

```tsx
// src/components/Pricing.tsx
import useRazorpay from 'react-razorpay';
import { api } from '@/services/api';

export const handleSubscribe = async (planId: string, provider: 'razorpay' | 'crypto' = 'razorpay') => {
    if (import.meta.env.VITE_PAYMENTS_ENABLED !== 'true') {
        toast.info('Checkout is coming soon.');
        return;
    }

    // 1. Fetch order ID from unified endpoint
    const { provider_checkout_data } = await api.createCheckout(planId, provider);

    // 2. Initialize Razorpay Checkout
    const options = {
        key: import.meta.env.VITE_RAZORPAY_KEY_ID,
        amount: provider_checkout_data.amount,
        currency: provider_checkout_data.currency,
        name: "PipFactor AI",
        order_id: provider_checkout_data.order_id,
        handler: function (res) {
            // NEVER trust this success visually. Show "Confirming..." 
            // True confirmation happens via Webhook -> PostgREST -> invalidate_perms cache drop
            window.location.href = `/?payment=success`;
        }
    };
    
    const rzp = new window.Razorpay(options);
    rzp.open();
};
```

## 4. Crucial Infrastructure Details

### 4.1. CSRF Middleware Exemption (`main.py`)
Because of our strict Double-Submit Cookie protection enforcing `X-CSRF-Token` on mutations, Razorpay's webhook MUST be conditionally exempted.
**File:** `api-web/app/main.py`
```python
AUTH_CSRF_EXEMPT_PATHS = {
    "/auth/exchange",
    "/auth/logout",
    "/auth/logout-all",
    "/auth/invalidate",
    "/api/webhooks/razorpay" # <- Required!
}
```

### 4.2. Cloudflared Tunnel Whitelisting
Cloudflare's Bot Fight Mode will block Razorpay's IPN requests. Whitelist these Razorpay dispatch IPs in your WAF pointing to the tunnel:
- `52.66.111.41`
- `52.66.82.164`
- `52.66.113.111`
- `13.235.6.21`
