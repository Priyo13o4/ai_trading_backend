# Razorpay Integration Guide

This guide reflects the live Razorpay implementation in `ai_trading_bot`.
The active provider class is `api-web/app/payments/payment_providers/razorpay_provider.py`.

## Where Razorpay lives

- Provider factory: `api-web/app/payments/payment_providers/router.py`
- Provider class: `api-web/app/payments/payment_providers/razorpay_provider.py`
- Payment routes: `api-web/app/payments/routes.py`
- Webhook route: `api-web/app/payments/webhook_handler.py` at `/api/webhooks/razorpay`
- Referral pause/resume worker: `api-web/app/referrals/pause_resume.py`

## Environment variables

- `RAZORPAY_KEY_ID`
- `RAZORPAY_KEY_SECRET`
- `RAZORPAY_WEBHOOK_SECRET`
- `RAZORPAY_PLAN_ID_CORE`

## Current provider behavior

### `create_checkout(user_id, plan_id, billing_period)`

- Creates a Razorpay subscription checkout session.
- Uses `RAZORPAY_PLAN_ID_CORE` as the active plan source.
- Returns a `short_url` checkout link plus provider metadata.
- Defers the first charge when the user already has active or trial access.
- Uses the user and plan identifiers as notes for correlation.

### `verify_webhook_signature(payload, signature)`

- Verifies the Razorpay webhook HMAC signature.
- Returns `True` or `False`; the webhook handler owns the DB side effects.

### `map_event_to_state(event_type)`

Current mapping includes subscription and payment lifecycle events such as:

- `subscription.created`
- `subscription.authenticated`
- `subscription.activated`
- `subscription.charged`
- `subscription.cancelled`
- `subscription.halted`
- `payment.captured`
- `payment.failed`
- `refund.processed`

### `cancel_subscription(provider_subscription_id)`

- Schedules cancellation at period end.
- Keeps access active through the current billing cycle.
- This is the behavior used by `/api/payments/cancel-subscription`.

### `cancel_checkout_attempt(provider_payment_id)`

- Best-effort cancellation for unresolved checkout attempts.
- If the attempt is already terminal, it is treated as successfully handled.
- Used by the checkout supersede path in `api-web/app/payments/routes.py`.

### `pause_subscription(subscription_id, pause_at_timestamp)` and `resume_subscription(subscription_id, pause_id)`

- These direct subscription actions are used by the referral reward pause/resume worker.
- They run the blocking Razorpay HTTP calls in a thread pool so the FastAPI event loop stays responsive.
- These actions are not part of the public payment router, but they are part of the live backend behavior.

## Current API routes that touch Razorpay

- `POST /api/payments/create-checkout`
- `POST /api/payments/cancel-checkout-attempt`
- `POST /api/payments/cancel-subscription`
- `POST /api/payments/resume-subscription`
- `POST /api/webhooks/razorpay`

## Webhook flow

1. Razorpay webhook hits `/api/webhooks/razorpay`.
2. The signature is verified through the provider class.
3. The webhook handler maps the event into internal payment states.
4. The handler updates the Supabase-backed payment tables through the service-role path.
5. Referral evaluation and cache invalidation run as internal follow-up steps.

## Important truth notes

- Razorpay is implemented behind the payment provider abstraction.
- There is no separate Supabase Edge Function implementation for Razorpay.
- Do not hardcode Razorpay logic directly into the router or webhook handler.
- The current codebase supports checkout creation, unresolved-attempt cancellation, deferred cancellation, and referral-driven pause/resume.
