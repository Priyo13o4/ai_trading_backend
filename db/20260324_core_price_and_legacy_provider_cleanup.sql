-- Migration: Enforce core pricing source-of-truth and clean legacy provider values
-- Date: 2026-03-24

BEGIN;

UPDATE public.subscription_plans
SET price_usd = 5.00,
    updated_at = NOW()
WHERE lower(name) = 'core'
  AND price_usd <> 5.00;

UPDATE public.user_subscriptions
SET payment_provider = 'manual'
WHERE payment_provider NOT IN ('razorpay','stripe','coinbase','plisio','manual');

UPDATE public.payment_transactions
SET provider = 'manual'
WHERE provider NOT IN ('razorpay','stripe','coinbase','plisio','manual');

COMMIT;
