-- Scope A (Qualification + Fraud Detection) schema additions.
-- Safe to rerun.

BEGIN;

ALTER TABLE IF EXISTS public.referral_tracking
    ADD COLUMN IF NOT EXISTS audit_metadata JSONB DEFAULT '{}'::jsonb;

ALTER TABLE IF EXISTS public.payment_transactions
    ADD COLUMN IF NOT EXISTS payment_identity_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_payment_transactions_identity_hash_user
    ON public.payment_transactions(payment_identity_hash, user_id)
    WHERE payment_identity_hash IS NOT NULL;

COMMIT;
