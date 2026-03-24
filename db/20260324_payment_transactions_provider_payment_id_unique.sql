-- Migration: Replace partial unique index with real unique constraint for payment transaction upserts
-- Date: 2026-03-24
-- Purpose: Ensure ON CONFLICT(provider, provider_payment_id) works reliably for checkout creation

BEGIN;

-- Keep one row per (provider, provider_payment_id) before adding strict uniqueness.
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY provider, provider_payment_id
            ORDER BY created_at DESC, id DESC
        ) AS rn
    FROM public.payment_transactions
    WHERE provider_payment_id IS NOT NULL
)
DELETE FROM public.payment_transactions t
USING ranked r
WHERE t.id = r.id
  AND r.rn > 1;

-- Remove old partial unique index that cannot satisfy plain ON CONFLICT target inference.
DROP INDEX IF EXISTS public.uq_payment_transactions_provider_payment_id;

-- Add a real unique table constraint (idempotent).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'payment_transactions_provider_provider_payment_id_key'
          AND conrelid = 'public.payment_transactions'::regclass
    ) THEN
        ALTER TABLE public.payment_transactions
            ADD CONSTRAINT payment_transactions_provider_provider_payment_id_key
            UNIQUE (provider, provider_payment_id);
    END IF;
END;
$$;

COMMIT;
