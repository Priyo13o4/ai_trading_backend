# Payment System Truth Snapshot

This is the current backend truth for payments and referral monetization in `ai_trading_bot`.
The old sleeper PRD has been superseded by the live FastAPI implementation under `api-web/app/payments/` and `api-web/app/referrals/`.

## What exists today

- Payment providers are routed through `api-web/app/payments/payment_providers/router.py`.
- The active provider names are `razorpay`, `plisio`, and `manual`.
- Manual payments cannot be triggered from the API.
- Payment routes live under `api-web/app/payments/routes.py` with prefix `/api/payments`.
- Webhook handling lives in `api-web/app/payments/webhook_handler.py` with route `/api/webhooks/{provider_name}`.
- Referral monetization wiring lives in `api-web/app/referrals/`.

## Current payment endpoints

- `POST /api/payments/create-checkout`
- `POST /api/payments/cancel-checkout-attempt`
- `POST /api/payments/cancel-subscription`
- `POST /api/payments/resume-subscription`
- `GET /api/payments/history`
- `POST /api/webhooks/{provider_name}`

## Current provider contract

The base contract in `api-web/app/payments/payment_providers/base.py` requires:

- `create_checkout(user_id, plan_id, billing_period)`
- `verify_webhook_signature(payload, signature)`
- `process_webhook(payload)`
- `cancel_subscription(subscription_id)`
- `cancel_checkout_attempt(provider_payment_id)`

### Razorpay

- Implemented in `api-web/app/payments/payment_providers/razorpay_provider.py`.
- Uses env vars such as `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, and `RAZORPAY_PLAN_ID_CORE`.
- Creates Razorpay subscriptions and returns the subscription short URL for checkout.
- Can defer the first charge when a user already has active or trial access.
- Supports webhook signature verification, subscription pause/resume, and period-end cancellation.

### Plisio

- Implemented in `api-web/app/payments/payment_providers/plisio_provider.py`.
- Uses invoice-driven checkout with callback URLs and crypto currency env vars.
- Verifies callback signatures through the provider SDK.
- Maps invoice states to internal payment transaction states.
- Provider-side cancellation is currently a noop in this flow.

## Current state machine

The live payment flow uses Supabase-backed tables and RPCs through `supabase_db()` / service-role access.
Current processing centers on:

- `payment_transactions`
- `webhook_events`
- `payment_audit_logs`
- `user_subscriptions`

Webhook processing is idempotent and replay-safe. Status changes are applied through the payment webhook handler, then referral evaluation and cache invalidation run as internal follow-up steps.

## Current referral wiring

- Feature flag: `REFERRAL_REWARD_EVALUATION_ENABLED`
- Entry point: `evaluate_referral_reward(referred_user_id, trigger_payment_id)`
- Pause/resume cycle worker: `run_referral_pause_resume_cycle()`
- Referral reward lifecycle uses deterministic, idempotent transitions and RPC-backed qualification.

## What is not implemented yet

- No `preview-update` endpoint exists yet.
- No `update-subscription` endpoint exists yet.
- No `provider_prices` table or proration workflow is implemented yet.
- No Supabase Edge Function runtime is used for payments.

## Related docs

- [RAZORPAY_INTEGRATION_GUIDE.md](RAZORPAY_INTEGRATION_GUIDE.md)
- [REFERRAL_REWARD_EVALUATION_INTEGRATION_NOTE.md](REFERRAL_REWARD_EVALUATION_INTEGRATION_NOTE.md)
- [future_subscriptions.md](future_subscriptions.md)

## Historical note

The old PRD content described a planned monetization system before the live backend existed.
Use that material only as historical context; it is no longer the source of truth for current implementation.
