# NOWPayments Integration Guide

This document outlines the technical integration of NOWPayments (Crypto) into our AI Trading Bot ecosystem.
**CRITICAL:** This guide strictly adheres to the provider-agnostic sleeper architecture defined in `payment_integration.md`.

## 1. Provider Abstraction Implementation

NOWPayments is our primary crypto provider. It must be implemented behind the `payment_providers/base.py` contract.
*Do not hardcode NOWPayments logic directly in the FastAPI router scripts.*

### Implementation Location: `api-web/app/payments/payment_providers/nowpayments_provider.py`

```python
import hmac
import hashlib
import json
import httpx
from .base import PaymentProvider

class NowPaymentsProvider(PaymentProvider):
    def __init__(self, api_key: str, ipn_secret: str):
        self.api_key = api_key
        self.ipn_secret = ipn_secret
        self.base_url = "https://api.nowpayments.io/v1/"

    async def create_checkout(self, plan_id: str, amount: float, currency: str, idempotency_key: str, **kwargs):
        # Call NOWPayments to generate a hosted invoice widget
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        data = {
            "price_amount": amount,
            "price_currency": currency, # e.g. "usd"
            "order_id": idempotency_key,
            "success_url": "https://pipfactor.com/?payment=success",
            "cancel_url": "https://pipfactor.com/?payment=cancelled"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{self.base_url}invoice", headers=headers, json=data)
            response.raise_for_status()
            invoice = response.json()
            
        return {
            "provider_payment_id": invoice["id"],
            "checkout_url": invoice["invoice_url"], # Hosted UI for crypto transfers
            "provider_checkout_data": invoice
        }

    def verify_webhook(self, raw_body: bytes, headers: dict):
        # 1. HMAC SHA512 Signature Validation
        signature = headers.get("x-nowpayments-sig")
        if not signature:
            raise ValueError("Missing signature")
            
        # NOWPayments requires sorting the JSON keys before digesting!
        payload = json.loads(raw_body)
        sorted_payload = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        
        expected_sig = hmac.new(
            self.ipn_secret.encode('utf-8'),
            sorted_payload.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        
        if not hmac.compare_digest(expected_sig, signature):
            raise ValueError("Invalid signature")
            
        return True
```

## 2. API Endpoints & State Machine

Crypto differs from fiat via confirmation lag. Ensure the DB `crypto_invoices` reflects this:
- **`waiting`**: User generated invoice.
- **`confirming`**: Seen on blockchain, waiting for standard confirmations (e.g. 1 for BTC, 12 for ETH).
- **`confirmed`**: **THIS** is when we trigger `create_subscription` and call `invalidate_perms(user_id)`.

### Webhook Flow (`POST /api/webhooks/nowpayments`):
1. **Auth:** NONE (But CSRF Exempted).
2. Validate IPN hash via provider.
3. Update `crypto_invoices` block status.
4. If status `finished` or `confirmed`:
    - Update `payment_transactions`.
    - Supabase RPC: `record_payment` & `create_subscription`.
    - Drop caching: `await invalidate_perms(user_id)`.

## 3. Frontend Checkout Initiation (React)

Instead of a modal SDK, we redirect to the provider's invoice URL or display it in an iframe.

```tsx
// src/components/Pricing.tsx
import { api } from '@/services/api';

export const handleSubscribe = async (planId: string, provider: 'razorpay' | 'nowpayments' = 'nowpayments') => {
    if (import.meta.env.VITE_PAYMENTS_ENABLED !== 'true') {
        toast.info('Checkout is coming soon.');
        return;
    }

    // Fetch hosted invoice URL from unified endpoint
    const { checkout_url } = await api.createCheckout(planId, provider);
    
    // Redirect user to the crypto payment gateway
    if (checkout_url) {
        window.location.href = checkout_url;
    }
};
```

## 4. Crucial Infrastructure Details

### 4.1. CSRF Middleware Exemption (`main.py`)
NOWPayments webhooks do not hold `X-CSRF-Token` headers.
**File:** `api-web/app/main.py`
```python
AUTH_CSRF_EXEMPT_PATHS = {
    "/auth/exchange",
    "/auth/logout",
    "/auth/logout-all",
    "/auth/invalidate",
    "/api/webhooks/razorpay",
    "/api/webhooks/nowpayments" # <- Required!
}
```

### 4.2. Cloudflared Tunnel Whitelisting
NOWPayments IPN calls will originate from their servers. Ensure Cloudflare Bot Fight Mode allows their IP subnet:
- `130.162.59.88`
- `130.162.59.39`
*(Check official NOWPayments docs for recent IP block additions).*
