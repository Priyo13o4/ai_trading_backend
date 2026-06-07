# Referral Reward Evaluation Truth Note

This note describes the live referral reward evaluator wiring in `ai_trading_bot`.
The current implementation is in `api-web/app/referrals/reward_evaluator.py`.

## Current wiring

- Feature flag: `REFERRAL_REWARD_EVALUATION_ENABLED`
- Entry point: `evaluate_referral_reward(referred_user_id, trigger_payment_id)`
- Default RPC name: `qualify_referral_reward`
- Override env var: `REFERRAL_REWARD_EVALUATION_RPC_NAME`
- Hold window: 7 days

The evaluator is called internally after successful payment processing, once idempotency checks have already passed.
It does not change the public payment API surface.

## What the evaluator does today

1. Validates both UUID inputs.
2. Loads the pending referral row for the referred user.
3. Applies fraud checks.
4. Calls the qualification RPC through `supabase_db()` and the service-role path.
5. Returns a structured outcome for logs and downstream handling.

## Fraud and safety behavior

- Duplicate payment identity under the same referrer is treated as a hard block.
- Same-network IP/UA matches are logged as soft signals only.
- The current fraud check uses the `check_duplicate_payment_identity` RPC.
- The evaluator is fail-safe: if the fraud path errors, the main qualification flow still handles the request deterministically.

## Pause/resume cycle support

Referral free-month rewards are not just a single event.
The live backend also has a worker-driven pause/resume flow in `api-web/app/referrals/pause_resume.py`.
That worker:

- Pauses Razorpay referral subscriptions on schedule.
- Resumes them when the pause window expires.
- Uses CAS-style updates on `referral_reward_pause_cycles` to stay idempotent under concurrent workers.

## Current outcomes

The evaluator can return outcomes such as:

- `feature_disabled`
- `skip_invalid_input`
- `skip_no_pending_referral`
- `skip_not_first_success`
- `success_reward_created`
- `success_already_rewarded_reconciled`
- `fraud_blocked_duplicate_identity`
- `error_controlled`

## Truth note

- There are no provider payload schema changes required for referral reward evaluation.
- The integration is internal to the payment webhook-processing path.
- Use this file as the current behavior note, not as a future implementation spec.
