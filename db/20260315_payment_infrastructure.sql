-- ==========================================================================
-- Payment Infrastructure Migration — PipFactor
-- Version: 2.0 (Live Mode, Razorpay + Plisio subscriptions)
-- Apply via: Supabase SQL Editor (this project does NOT use supabase CLI migrations)
-- Idempotent: Safe to re-run. Uses IF NOT EXISTS / DO $$ guards throughout.
-- ==========================================================================

BEGIN;

-- --------------------------------------------------------------------------
-- 1. RENAME BETA PLAN TO CORE
-- --------------------------------------------------------------------------
-- The beta plan is now the "Core" paid plan at $5/month.
-- We retain starter, professional, elite plans unchanged.

UPDATE public.subscription_plans
SET
    name         = 'core',
    display_name = 'Core',
    price_usd    = 5.00,
    billing_period = 'monthly',
    description  = 'Core plan — AI-generated market signals for active traders.',
    is_active    = true,
    updated_at   = NOW()
WHERE name = 'beta';

-- --------------------------------------------------------------------------
-- 2. SCHEMA CLEANUP — Drop stale Stripe-specific columns
-- --------------------------------------------------------------------------
ALTER TABLE public.subscription_plans
    DROP COLUMN IF EXISTS stripe_price_id,
    DROP COLUMN IF EXISTS stripe_product_id;

-- profiles.stripe_customer_id does not exist in this deployment (already clean).
-- Adding a guard just for idempotency:
ALTER TABLE public.profiles
    DROP COLUMN IF EXISTS stripe_customer_id;

-- --------------------------------------------------------------------------
-- 3. UPDATE payment_provider CHECK CONSTRAINT
-- Adds: 'plisio', 'coinbase'. Retains: 'stripe', 'razorpay', 'manual'.
-- Removes stale: 'paypal'
-- --------------------------------------------------------------------------
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
        CHECK (payment_provider = ANY (
            ARRAY['razorpay','stripe','coinbase','plisio','manual']::text[]
        ));
END;
$$;

-- --------------------------------------------------------------------------
-- 4. ADDITIVE COLUMNS ON EXISTING TABLES
-- --------------------------------------------------------------------------
ALTER TABLE public.user_subscriptions
    ADD COLUMN IF NOT EXISTS plan_snapshot JSONB;

-- --------------------------------------------------------------------------
-- 5. PROVIDER CUSTOMERS TABLE
-- Maps users to provider-specific customer IDs (Razorpay, Plisio, etc.)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.provider_customers (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    provider             TEXT        NOT NULL,
    provider_customer_id TEXT        NOT NULL,
    metadata             JSONB       DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT provider_customers_provider_customer_unique
        UNIQUE (provider, provider_customer_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_customers_user_provider
    ON public.provider_customers(user_id, provider);

ALTER TABLE public.provider_customers ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'provider_customers'
          AND policyname = 'Service role manages provider customers'
    ) THEN
        CREATE POLICY "Service role manages provider customers"
            ON public.provider_customers FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.provider_customers FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.provider_customers TO service_role;

-- --------------------------------------------------------------------------
-- 6. PROVIDER PRICES TABLE
-- Maps subscription_plans to provider-specific plan/price IDs.
-- Seeded with Plisio Core plan ID.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.provider_prices (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id           UUID        NOT NULL REFERENCES public.subscription_plans(id) ON DELETE CASCADE,
    provider          TEXT        NOT NULL,
    provider_price_id TEXT        NOT NULL,  -- Razorpay plan_id or Plisio subscription identifier
    metadata          JSONB       DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT provider_prices_provider_price_unique
        UNIQUE (provider, provider_price_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_prices_plan_provider
    ON public.provider_prices(plan_id, provider);

ALTER TABLE public.provider_prices ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'provider_prices'
          AND policyname = 'Service role manages provider prices'
    ) THEN
        CREATE POLICY "Service role manages provider prices"
            ON public.provider_prices FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.provider_prices FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.provider_prices TO service_role;

-- Seed: Plisio "Core" subscription plan
-- This INSERT is idempotent via the UNIQUE constraint + ON CONFLICT DO NOTHING.
INSERT INTO public.provider_prices (plan_id, provider, provider_price_id, metadata)
SELECT
    sp.id,
    'plisio',
    '2082535887',
    jsonb_build_object(
        'plan_name', 'Core',
        'cost_per_period', '5 USD',
        'period', '30 Days',
        'seeded_at', NOW()
    )
FROM public.subscription_plans sp
WHERE sp.name = 'core'
ON CONFLICT (provider, provider_price_id) DO NOTHING;

-- --------------------------------------------------------------------------
-- 7. PAYMENT TRANSACTIONS TABLE
-- Unified state machine — source of truth for all payment attempts.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.payment_transactions (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    subscription_id             UUID        REFERENCES public.user_subscriptions(id) ON DELETE SET NULL,

    provider                    TEXT        NOT NULL,   -- 'razorpay', 'plisio', 'stripe'
    provider_payment_id         TEXT,                   -- Razorpay payment_id / NP invoice ID
    provider_checkout_session_id TEXT,                  -- stripe checkout session (reserved)
    provider_subscription_id    TEXT,                   -- Razorpay sub_xxx / NP subscription ID

    amount                      NUMERIC(12,2) NOT NULL,
    currency                    TEXT          NOT NULL DEFAULT 'USD',

    status                      TEXT          NOT NULL DEFAULT 'pending',

    last_provider_event_time    TIMESTAMPTZ,            -- prevents out-of-order webhook regressions

    payment_type                TEXT          NOT NULL DEFAULT 'subscription',

    metadata                    JSONB         DEFAULT '{}',

    created_at                  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_transactions_status_check'
          AND conrelid = 'public.payment_transactions'::regclass
    ) THEN
        ALTER TABLE public.payment_transactions
            ADD CONSTRAINT payment_transactions_status_check
            CHECK (status IN ('pending','processing','succeeded','failed','refunded','cancelled','expired'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_transactions_payment_type_check'
          AND conrelid = 'public.payment_transactions'::regclass
    ) THEN
        ALTER TABLE public.payment_transactions
            ADD CONSTRAINT payment_transactions_payment_type_check
            CHECK (payment_type IN ('subscription','one_time'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_transactions_provider_check'
          AND conrelid = 'public.payment_transactions'::regclass
    ) THEN
        ALTER TABLE public.payment_transactions
            ADD CONSTRAINT payment_transactions_provider_check
            CHECK (provider IN ('razorpay','plisio','stripe','coinbase','manual'));
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_payment_transactions_user_id
    ON public.payment_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider_payment_id
    ON public.payment_transactions(provider, provider_payment_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_transactions_provider_payment_id
    ON public.payment_transactions(provider, provider_payment_id)
    WHERE provider_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payment_transactions_status
    ON public.payment_transactions(status)
    WHERE status IN ('pending','processing');
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider_subscription_id
    ON public.payment_transactions(provider_subscription_id)
    WHERE provider_subscription_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_payment_transactions_updated_at'
          AND tgrelid = 'public.payment_transactions'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER set_payment_transactions_updated_at
            BEFORE UPDATE ON public.payment_transactions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;

ALTER TABLE public.payment_transactions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'payment_transactions'
          AND policyname = 'Users view own payment transactions'
    ) THEN
        CREATE POLICY "Users view own payment transactions"
            ON public.payment_transactions FOR SELECT
            USING (auth.uid() = user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'payment_transactions'
          AND policyname = 'Service role manages payment transactions'
    ) THEN
        CREATE POLICY "Service role manages payment transactions"
            ON public.payment_transactions FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.payment_transactions FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.payment_transactions TO authenticated;
GRANT ALL ON TABLE public.payment_transactions TO service_role;

-- --------------------------------------------------------------------------
-- 8. WEBHOOK EVENTS TABLE
-- Idempotent event store — insert before processing, mark processed after.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.webhook_events (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider         TEXT        NOT NULL,   -- 'razorpay', 'plisio', 'stripe'
    event_id         TEXT        NOT NULL,   -- provider's unique event/IPN ID
    event_type       TEXT        NOT NULL,   -- 'subscription.charged', 'payment_status', etc.

    payload          JSONB       NOT NULL,

    processed        BOOLEAN     NOT NULL DEFAULT FALSE,
    processing_error TEXT,

    received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at     TIMESTAMPTZ,

    CONSTRAINT webhook_events_provider_event_unique UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_unprocessed
    ON public.webhook_events(provider, processed)
    WHERE processed = FALSE;

ALTER TABLE public.webhook_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'webhook_events'
          AND policyname = 'Service role manages webhook events'
    ) THEN
        CREATE POLICY "Service role manages webhook events"
            ON public.webhook_events FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.webhook_events FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.webhook_events TO service_role;

-- --------------------------------------------------------------------------
-- 9. CRYPTO INVOICES TABLE
-- Blockchain-specific state machine. Status reflects confirmation progress.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.crypto_invoices (
    id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    transaction_id         UUID        REFERENCES public.payment_transactions(id) ON DELETE SET NULL,

    provider               TEXT        NOT NULL,     -- 'plisio'
    provider_payment_id    TEXT        NOT NULL,     -- Plisio payment/invoice ID

    hosted_url             TEXT,                     -- Plisio hosted invoice page URL
    pay_address            TEXT,                     -- blockchain address
    pay_currency           TEXT,                     -- BTC, ETH, USDT, USDC
    pay_amount             NUMERIC(24, 8),            -- crypto denomination amount
    usd_amount             NUMERIC(12, 2) NOT NULL,   -- original USD price

    confirmations          INT         DEFAULT 0,
    required_confirmations INT         DEFAULT 1,

    -- waiting → confirming → confirmed → failed/expired
    status                 TEXT        NOT NULL DEFAULT 'waiting',

    expires_at             TIMESTAMPTZ,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'crypto_invoices_status_check'
          AND conrelid = 'public.crypto_invoices'::regclass
    ) THEN
        ALTER TABLE public.crypto_invoices
            ADD CONSTRAINT crypto_invoices_status_check
            CHECK (status IN ('waiting','confirming','confirmed','failed','expired'));
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_crypto_invoices_user_id
    ON public.crypto_invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_crypto_invoices_provider_payment_id
    ON public.crypto_invoices(provider, provider_payment_id);
CREATE INDEX IF NOT EXISTS idx_crypto_invoices_status
    ON public.crypto_invoices(status)
    WHERE status IN ('waiting','confirming');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_crypto_invoices_updated_at'
          AND tgrelid = 'public.crypto_invoices'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER set_crypto_invoices_updated_at
            BEFORE UPDATE ON public.crypto_invoices
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;

ALTER TABLE public.crypto_invoices ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'crypto_invoices'
          AND policyname = 'Users view own crypto invoices'
    ) THEN
        CREATE POLICY "Users view own crypto invoices"
            ON public.crypto_invoices FOR SELECT
            USING (auth.uid() = user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'crypto_invoices'
          AND policyname = 'Service role manages crypto invoices'
    ) THEN
        CREATE POLICY "Service role manages crypto invoices"
            ON public.crypto_invoices FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.crypto_invoices FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.crypto_invoices TO authenticated;
GRANT ALL ON TABLE public.crypto_invoices TO service_role;

-- --------------------------------------------------------------------------
-- 10. PAYMENT AUDIT LOGS TABLE
-- Immutable ledger of all payment state transitions.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.payment_audit_logs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID        REFERENCES public.payment_transactions(id) ON DELETE SET NULL,
    entity_type     TEXT        NOT NULL,  -- 'payment_transaction','crypto_invoice','user_subscription'
    entity_id       UUID        NOT NULL,

    previous_state  TEXT,
    new_state       TEXT        NOT NULL,

    trigger_source  TEXT        NOT NULL,  -- 'razorpay_webhook','plisio_webhook','admin','cron'
    trigger_event_id TEXT,                 -- webhook_events.event_id that caused this change

    reason          TEXT,
    metadata        JSONB       DEFAULT '{}',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_audit_logs_transaction_id
    ON public.payment_audit_logs(transaction_id);
CREATE INDEX IF NOT EXISTS idx_payment_audit_logs_entity
    ON public.payment_audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_payment_audit_logs_created_at
    ON public.payment_audit_logs(created_at);

ALTER TABLE public.payment_audit_logs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'payment_audit_logs'
          AND policyname = 'Service role manages audit logs'
    ) THEN
        CREATE POLICY "Service role manages audit logs"
            ON public.payment_audit_logs FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.payment_audit_logs FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.payment_audit_logs TO service_role;

-- --------------------------------------------------------------------------
-- 11. UPDATE create_subscription RPC
-- Adds plan_snapshot population. Keeps all existing behavior intact.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.create_subscription(
    p_user_id   UUID,
    p_plan_id   UUID,
    p_payment_provider TEXT,
    p_external_id TEXT,
    p_trial_days INT DEFAULT 0,
    p_metadata   JSONB DEFAULT '{}'
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_subscription_id UUID;
    v_plan            RECORD;
    v_expires_at      TIMESTAMP WITH TIME ZONE;
    v_trial_ends_at   TIMESTAMP WITH TIME ZONE;
    v_plan_snapshot   JSONB;
BEGIN
    -- Only the owner or service role can create subscriptions.
    IF auth.role() <> 'service_role' AND auth.uid() <> p_user_id THEN
        RAISE EXCEPTION 'Unauthorized: Cannot create subscription for another user';
    END IF;

    SELECT * INTO v_plan FROM public.subscription_plans WHERE id = p_plan_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Plan not found';
    END IF;

    -- Build plan snapshot for historical pricing preservation.
    v_plan_snapshot := jsonb_build_object(
        'plan_name',      v_plan.name,
        'display_name',   v_plan.display_name,
        'price_usd',      v_plan.price_usd,
        'billing_period', v_plan.billing_period,
        'features',       COALESCE(v_plan.features, '{}'),
        'pairs_allowed',  COALESCE(to_jsonb(v_plan.pairs_allowed), '[]'),
        'snapshot_at',    NOW()
    );

    -- Cancel any existing active/trial subscriptions.
    UPDATE public.user_subscriptions
    SET status             = 'cancelled',
        cancel_at_period_end = true,
        cancelled_at       = NOW(),
        updated_at         = NOW()
    WHERE user_id = p_user_id
      AND status IN ('active', 'trial');

    IF p_trial_days > 0 THEN
        v_trial_ends_at := NOW() + (p_trial_days || ' days')::INTERVAL;
        v_expires_at    := v_trial_ends_at;
    ELSE
        v_expires_at := CASE v_plan.billing_period
            WHEN 'monthly'  THEN NOW() + INTERVAL '30 days'
            WHEN 'yearly'   THEN NOW() + INTERVAL '365 days'
            WHEN 'lifetime' THEN NOW() + INTERVAL '100 years'
            ELSE NOW() + INTERVAL '30 days'
        END;
    END IF;

    INSERT INTO public.user_subscriptions (
        user_id,
        plan_id,
        status,
        expires_at,
        trial_ends_at,
        payment_provider,
        external_subscription_id,
        next_billing_date,
        plan_snapshot,
        metadata
    )
    VALUES (
        p_user_id,
        p_plan_id,
        CASE WHEN p_trial_days > 0 THEN 'trial' ELSE 'active' END,
        v_expires_at,
        v_trial_ends_at,
        p_payment_provider,
        p_external_id,
        v_expires_at,
        v_plan_snapshot,
        p_metadata
    )
    RETURNING id INTO v_subscription_id;

    RETURN v_subscription_id;
END;
$$;

REVOKE ALL ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INT, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INT, JSONB) TO service_role;

-- --------------------------------------------------------------------------
-- 12. UPDATE handle_new_user TRIGGER
-- Replaces the old beta-plan logic with a 7-day trial on the 'core' plan.
-- All new signups automatically get a 7-day trial subscription.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_core_plan_id    UUID;
    v_subscription_id UUID;
BEGIN
    -- Create the user profile row.
    INSERT INTO public.profiles (id, email, full_name, email_verified)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        NEW.email_confirmed_at IS NOT NULL
    );

    -- Look up the 'core' plan for the 7-day trial.
    SELECT id INTO v_core_plan_id
    FROM public.subscription_plans
    WHERE name = 'core' AND is_active = true
    LIMIT 1;

    IF v_core_plan_id IS NOT NULL THEN
        -- Create a 7-day trial subscription on the core plan.
        v_subscription_id := public.create_subscription(
            NEW.id,
            v_core_plan_id,
            'manual',   -- no payment provider during trial
            NULL,       -- no external subscription ID yet
            7,          -- 7-day trial
            jsonb_build_object(
                'signup_date',   NOW(),
                'trial_source',  'signup',
                'welcome_message', 'Welcome to PipFactor! Your 7-day trial is now active.'
            )
        );

        -- Give trial users access to all 5 major pairs.
        INSERT INTO public.user_pair_selections (
            user_id,
            subscription_id,
            selected_pairs,
            locked_until,
            can_change_pairs
        ) VALUES (
            NEW.id,
            v_subscription_id,
            ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD'],
            NOW() + INTERVAL '7 days',
            false
        )
        ON CONFLICT DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;

-- The trigger itself is already created on auth.users; we only replace the function body.
-- But if somehow missing, ensure trigger is attached:
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'on_auth_user_created'
    ) THEN
        CREATE TRIGGER on_auth_user_created
            AFTER INSERT ON auth.users
            FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
    END IF;
END;
$$;

-- --------------------------------------------------------------------------
-- 13. SCHEDULE expire_subscriptions() VIA pg_cron
-- Runs every hour to mark expired trial and paid subscriptions.
-- Requires pg_cron extension to be enabled in Supabase dashboard.
-- --------------------------------------------------------------------------
DO $$
BEGIN
    -- Enable pg_cron if not already enabled (safe no-op if already enabled)
    IF EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'pg_cron'
    ) THEN
        CREATE EXTENSION IF NOT EXISTS pg_cron;
    END IF;
END;
$$;

-- Schedule the expiry job (idempotent — unschedule first, then reschedule)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        -- Remove existing schedule if present to avoid duplicates
        PERFORM cron.unschedule('expire-subscriptions')
        WHERE EXISTS (
            SELECT 1 FROM cron.job WHERE jobname = 'expire-subscriptions'
        );

        PERFORM cron.schedule(
            'expire-subscriptions',
            '0 * * * *',   -- every hour at minute 0
            $$SELECT public.expire_subscriptions()$$
        );
    END IF;
END;
$$;

COMMIT;
