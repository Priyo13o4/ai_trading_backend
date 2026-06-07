# Future Scope: Multi-Plan and Multi-Cycle Subscriptions

This file is a roadmap note, not current-state documentation.
For the current payment contract, see `payment_integration.md`.

## Current truth

Today the backend supports:

- `POST /api/payments/create-checkout`
- `POST /api/payments/cancel-checkout-attempt`
- `POST /api/payments/cancel-subscription`
- `POST /api/payments/resume-subscription`
- `GET /api/payments/history`
- webhook processing at `/api/webhooks/{provider_name}`

Today there is **no** update-subscription or proration workflow.
There is also **no** `provider_prices` table in the live implementation yet.

## Future scope

The future upgrade/cycle model should cover:

- Upgrade to a higher tier mid-cycle
- Downgrade to a lower tier mid-cycle
- Switch billing cycle between monthly and yearly
- Generate preview pricing before confirm
- Apply proration where the provider supports it

## What still needs to exist before that work is real

- A provider price mapping table or equivalent config layer
- A backend preview endpoint for upgrade calculations
- A backend update-subscription endpoint
- Provider-specific logic for proration and scheduling
- Frontend controls for plan and billing-cycle changes

## Practical provider truth

- Razorpay can support deferred subscription changes and pause/resume flows.
- Crypto flows such as Plisio are invoice-driven and will likely need a different upgrade path.
- The provider abstraction in `api-web/app/payments/payment_providers/` is the correct place for that future logic.

## Recommendation

Treat this as a roadmap until the backend grows a real update/proration flow.
Do not use it as evidence that multi-cycle subscriptions are already implemented.
