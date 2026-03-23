-- ============================================================================
-- Phase 1: Replace NOWPayments provider checks with Plisio
-- Idempotent migration for provider CHECK constraints.
-- ============================================================================

BEGIN;

-- Safety remap for historical rows so new constraints can be applied.
UPDATE public.user_subscriptions
SET payment_provider = 'manual'
WHERE payment_provider = 'nowpayments';

UPDATE public.payment_transactions
SET provider = 'manual'
WHERE provider = 'nowpayments';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_subscriptions_payment_provider_check'
          AND conrelid = 'public.user_subscriptions'::regclass
    ) THEN
        ALTER TABLE public.user_subscriptions
            DROP CONSTRAINT user_subscriptions_payment_provider_check;
    END IF;

    ALTER TABLE public.user_subscriptions
        ADD CONSTRAINT user_subscriptions_payment_provider_check
        CHECK (
            payment_provider = ANY (
                ARRAY['razorpay','stripe','coinbase','plisio','manual']::text[]
            )
        );
END;
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_transactions_provider_check'
          AND conrelid = 'public.payment_transactions'::regclass
    ) THEN
        ALTER TABLE public.payment_transactions
            DROP CONSTRAINT payment_transactions_provider_check;
    END IF;

    ALTER TABLE public.payment_transactions
        ADD CONSTRAINT payment_transactions_provider_check
        CHECK (
            provider = ANY (
                ARRAY['razorpay','stripe','coinbase','plisio','manual']::text[]
            )
        );
END;
$$;

COMMIT;
