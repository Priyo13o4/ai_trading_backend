# Referral Reward Evaluator Integration Note

This note documents the current referral reward evaluator wiring and contract boundaries.

## Current State

- Module added: `api-web/app/referrals/reward_evaluator.py`
- Wiring is active in webhook success handling.
- Evaluation remains feature-gated by `REFERRAL_REWARD_EVALUATION_ENABLED=true`.
- Hard reject policy is payment-identity collision only; IP/UA/device are soft-signal logs.

## Wiring Point

The integration call is:

- `evaluate_referral_reward(referred_user_id=<user_id>, trigger_payment_id=<payment_tx_id>)`

This runs after payment success is finalized and idempotency checks are complete in internal payment processing flow.

## Safety / Transaction Semantics

Current implementation uses deterministic qualification flow with idempotent guards.
For production-safe exactly-once semantics, implement one SQL transaction (or RPC) that does:

1. Lock `referral_tracking` pending row for referred user.
2. Lock/select first succeeded payment deterministically (`ORDER BY created_at ASC, id ASC LIMIT 1`).
3. Insert reward row with `status=on_hold` and `hold_expires_at=now()+7 days` (or configured hold days).
4. Update tracking status to `qualified`.

## Contract Impact

No payment callback/request schema changes are required.

- No provider webhook payload contract changes
- No provider request payload changes
- Integration remains internal to existing webhook-processing paths.
