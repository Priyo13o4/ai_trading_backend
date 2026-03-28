-- ==========================================================================
-- Referral System Phase 1 Schema Migration — PipFactor
-- Date: 2026-03-25
-- Scope: Add referral schema only (no payment flow changes)
-- Idempotent: Safe to re-run via IF NOT EXISTS / DO $$ guards
-- ==========================================================================

BEGIN;

-- --------------------------------------------------------------------------
-- 1. REFERRAL CODES
-- One code per user, globally unique code value.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.referral_codes (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    code        TEXT        NOT NULL,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT referral_codes_user_unique UNIQUE (user_id),
    CONSTRAINT referral_codes_code_unique UNIQUE (code),
    CONSTRAINT referral_codes_code_format_check CHECK (code ~ '^[A-Z0-9]{6,20}$')
);

-- Supports FK validation from referral_tracking(referral_code_id, referrer_id).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'referral_codes_id_user_unique'
          AND conrelid = 'public.referral_codes'::regclass
    ) THEN
        ALTER TABLE public.referral_codes
            ADD CONSTRAINT referral_codes_id_user_unique UNIQUE (id, user_id);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_referral_codes_code_active
    ON public.referral_codes(code)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_referral_codes_user_active
    ON public.referral_codes(user_id)
    WHERE is_active = TRUE;

ALTER TABLE public.referral_codes ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_codes'
          AND policyname = 'Users view own referral codes'
    ) THEN
        CREATE POLICY "Users view own referral codes"
            ON public.referral_codes FOR SELECT
            USING (auth.uid() = user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_codes'
          AND policyname = 'Service role manages referral codes'
    ) THEN
        CREATE POLICY "Service role manages referral codes"
            ON public.referral_codes FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.referral_codes FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.referral_codes TO authenticated;
GRANT ALL ON TABLE public.referral_codes TO service_role;

-- --------------------------------------------------------------------------
-- 2. REFERRAL TRACKING
-- Tracks attribution and lifecycle for a referred user signup/conversion.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.referral_tracking (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    referrer_id       UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    referred_id       UUID        NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    referral_code_id  UUID        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'pending',
    source            TEXT        NOT NULL DEFAULT 'code',
    fraud_reason      TEXT,
    registration_ip_prefix TEXT,
    registration_ua_hash   TEXT,
    attributed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    qualified_at      TIMESTAMPTZ,
    metadata          JSONB       DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT referral_tracking_referred_id_unique UNIQUE (referred_id),
    CONSTRAINT referral_tracking_no_self_referral_check CHECK (referrer_id <> referred_id),
    CONSTRAINT referral_tracking_status_check CHECK (status IN ('pending','qualified','rejected_fraud')),
    CONSTRAINT referral_tracking_source_check CHECK (source IN ('code','link')),
    CONSTRAINT referral_tracking_code_owner_fk
        FOREIGN KEY (referral_code_id, referrer_id)
        REFERENCES public.referral_codes(id, user_id)
        ON DELETE RESTRICT
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'referral_tracking'
          AND column_name = 'referrer_user_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'referral_tracking'
          AND column_name = 'referrer_id'
    ) THEN
        ALTER TABLE public.referral_tracking
            RENAME COLUMN referrer_user_id TO referrer_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'referral_tracking'
          AND column_name = 'referred_user_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'referral_tracking'
          AND column_name = 'referred_id'
    ) THEN
        ALTER TABLE public.referral_tracking
            RENAME COLUMN referred_user_id TO referred_id;
    END IF;
END;
$$;

ALTER TABLE public.referral_tracking
    ADD COLUMN IF NOT EXISTS source TEXT;

ALTER TABLE public.referral_tracking
    ADD COLUMN IF NOT EXISTS fraud_reason TEXT;

ALTER TABLE public.referral_tracking
    ADD COLUMN IF NOT EXISTS registration_ip_prefix TEXT;

ALTER TABLE public.referral_tracking
    ADD COLUMN IF NOT EXISTS registration_ua_hash TEXT;

ALTER TABLE public.referral_tracking
    ADD COLUMN IF NOT EXISTS referrer_id UUID;

ALTER TABLE public.referral_tracking
    ADD COLUMN IF NOT EXISTS referred_id UUID;

ALTER TABLE public.referral_tracking
    ALTER COLUMN referrer_id SET NOT NULL;

ALTER TABLE public.referral_tracking
    ALTER COLUMN referred_id SET NOT NULL;

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_referred_user_unique;

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_referred_id_unique;

ALTER TABLE public.referral_tracking
    ADD CONSTRAINT referral_tracking_referred_id_unique
    UNIQUE (referred_id);

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_no_self_referral_check;

ALTER TABLE public.referral_tracking
    ADD CONSTRAINT referral_tracking_no_self_referral_check
    CHECK (referrer_id <> referred_id);

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_code_owner_fk;

ALTER TABLE public.referral_tracking
    ADD CONSTRAINT referral_tracking_code_owner_fk
    FOREIGN KEY (referral_code_id, referrer_id)
    REFERENCES public.referral_codes(id, user_id)
    ON DELETE RESTRICT;

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_id_referrer_unique;

ALTER TABLE public.referral_tracking
    ALTER COLUMN source SET DEFAULT 'code';

UPDATE public.referral_tracking
SET source = 'code'
WHERE source IS NULL;

ALTER TABLE public.referral_tracking
    ALTER COLUMN source SET NOT NULL;

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_status_check;

ALTER TABLE public.referral_tracking
    ADD CONSTRAINT referral_tracking_status_check
    CHECK (status IN ('pending','qualified','rejected_fraud'));

ALTER TABLE public.referral_tracking
    DROP CONSTRAINT IF EXISTS referral_tracking_source_check;

ALTER TABLE public.referral_tracking
    ADD CONSTRAINT referral_tracking_source_check
    CHECK (source IN ('code','link'));

-- Supports FK validation from referral_rewards(referral_id, user_id).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'referral_tracking_id_referrer_id_unique'
          AND conrelid = 'public.referral_tracking'::regclass
    ) THEN
        ALTER TABLE public.referral_tracking
            ADD CONSTRAINT referral_tracking_id_referrer_id_unique UNIQUE (id, referrer_id);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_referral_tracking_referrer_status
    ON public.referral_tracking(referrer_id, status);

CREATE INDEX IF NOT EXISTS idx_referral_tracking_referred
    ON public.referral_tracking(referred_id);

CREATE INDEX IF NOT EXISTS idx_referral_tracking_code_status
    ON public.referral_tracking(referral_code_id, status);

ALTER TABLE public.referral_tracking ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_tracking'
          AND policyname = 'Users view own referral tracking'
    ) THEN
        DROP POLICY "Users view own referral tracking" ON public.referral_tracking;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_tracking'
          AND policyname = 'Service role manages referral tracking'
    ) THEN
        CREATE POLICY "Service role manages referral tracking"
            ON public.referral_tracking FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.referral_tracking FROM PUBLIC, anon;
REVOKE SELECT ON TABLE public.referral_tracking FROM authenticated;
GRANT ALL ON TABLE public.referral_tracking TO service_role;

-- --------------------------------------------------------------------------
-- 3. REFERRAL REWARDS
-- One reward row per referral_tracking row.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.referral_rewards (
    referral_id       UUID         NOT NULL,
    user_id           UUID         NOT NULL,
    trigger_payment_id UUID        NOT NULL,
    status            TEXT         NOT NULL DEFAULT 'on_hold',
    hold_expires_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT referral_rewards_referral_id_unique UNIQUE (referral_id),
    CONSTRAINT referral_rewards_status_check CHECK (status IN ('on_hold','available','applied','revoked')),
    CONSTRAINT referral_rewards_referral_user_fk
        FOREIGN KEY (referral_id, user_id)
        REFERENCES public.referral_tracking(id, referrer_id)
        ON DELETE CASCADE
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'referral_rewards'
          AND column_name = 'referrer_user_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'referral_rewards'
          AND column_name = 'user_id'
    ) THEN
        ALTER TABLE public.referral_rewards
            RENAME COLUMN referrer_user_id TO user_id;
    END IF;
END;
$$;

ALTER TABLE public.referral_rewards
    ADD COLUMN IF NOT EXISTS user_id UUID;

ALTER TABLE public.referral_rewards
    ADD COLUMN IF NOT EXISTS trigger_payment_id UUID;

ALTER TABLE public.referral_rewards
    ADD COLUMN IF NOT EXISTS hold_expires_at TIMESTAMPTZ;

ALTER TABLE public.referral_rewards
    ALTER COLUMN status SET DEFAULT 'on_hold';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.referral_rewards
        WHERE user_id IS NULL
    ) THEN
        ALTER TABLE public.referral_rewards
            ALTER COLUMN user_id SET NOT NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.referral_rewards
        WHERE trigger_payment_id IS NULL
    ) THEN
        ALTER TABLE public.referral_rewards
            ALTER COLUMN trigger_payment_id SET NOT NULL;
    END IF;
END;
$$;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_referral_unique;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_referral_id_unique;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_referral_id_unique
    UNIQUE (referral_id);

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_amount_nonnegative_check;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_currency_check;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_type_check;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_referral_owner_fk;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_referral_user_fk;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_user_id_fk;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_trigger_payment_id_fk;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_user_id_fk
    FOREIGN KEY (user_id)
    REFERENCES public.profiles(id)
    ON DELETE CASCADE;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_trigger_payment_id_fk
    FOREIGN KEY (trigger_payment_id)
    REFERENCES public.payment_transactions(id)
    ON DELETE RESTRICT;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_referral_user_fk
    FOREIGN KEY (referral_id, user_id)
    REFERENCES public.referral_tracking(id, referrer_id)
    ON DELETE CASCADE;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_pkey;

ALTER TABLE public.referral_rewards
    DROP COLUMN IF EXISTS id;

ALTER TABLE public.referral_rewards
    DROP COLUMN IF EXISTS reward_type;

ALTER TABLE public.referral_rewards
    DROP COLUMN IF EXISTS amount_usd;

ALTER TABLE public.referral_rewards
    DROP COLUMN IF EXISTS currency;

ALTER TABLE public.referral_rewards
    DROP COLUMN IF EXISTS issued_at;

ALTER TABLE public.referral_rewards
    DROP COLUMN IF EXISTS metadata;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_status_check;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_status_check
    CHECK (status IN ('on_hold','available','applied','revoked'));

CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer_status
    ON public.referral_rewards(user_id, status);

CREATE INDEX IF NOT EXISTS idx_referral_rewards_status_created
    ON public.referral_rewards(status, created_at DESC);

ALTER TABLE public.referral_rewards ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_rewards'
          AND policyname = 'Users view own referral rewards'
    ) THEN
        CREATE POLICY "Users view own referral rewards"
            ON public.referral_rewards FOR SELECT
            USING (auth.uid() = user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_rewards'
          AND policyname = 'Service role manages referral rewards'
    ) THEN
        CREATE POLICY "Service role manages referral rewards"
            ON public.referral_rewards FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.referral_rewards FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.referral_rewards TO authenticated;
GRANT ALL ON TABLE public.referral_rewards TO service_role;

-- --------------------------------------------------------------------------
-- 4. UPDATED_AT HANDLING FOR REFERRAL REWARDS
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.set_referral_rewards_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.set_referral_tracking_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.set_referral_codes_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'set_referral_tracking_updated_at'
          AND tgrelid = 'public.referral_tracking'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER set_referral_tracking_updated_at
            BEFORE UPDATE ON public.referral_tracking
            FOR EACH ROW EXECUTE FUNCTION public.set_referral_tracking_updated_at();
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'set_referral_codes_updated_at'
          AND tgrelid = 'public.referral_codes'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER set_referral_codes_updated_at
            BEFORE UPDATE ON public.referral_codes
            FOR EACH ROW EXECUTE FUNCTION public.set_referral_codes_updated_at();
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'set_referral_rewards_updated_at'
          AND tgrelid = 'public.referral_rewards'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER set_referral_rewards_updated_at
            BEFORE UPDATE ON public.referral_rewards
            FOR EACH ROW EXECUTE FUNCTION public.set_referral_rewards_updated_at();
    END IF;
END;
$$;

-- --------------------------------------------------------------------------
-- 5. AUTO-CREATE REFERRAL CODE ON PROFILE INSERT
-- Collision-safe generation with bounded retries.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.create_referral_code_for_profile()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_code TEXT;
    i INT;
BEGIN
    -- Respect existing row in case of retry/replay or manual seed.
    IF EXISTS (SELECT 1 FROM public.referral_codes WHERE user_id = NEW.id) THEN
        RETURN NEW;
    END IF;

    FOR i IN 1..25 LOOP
        v_code := 'PF' || UPPER(SUBSTRING(REPLACE(gen_random_uuid()::text, '-', '') FROM 1 FOR 10));

        BEGIN
            INSERT INTO public.referral_codes (user_id, code, is_active)
            VALUES (NEW.id, v_code, TRUE);
            RETURN NEW;
        EXCEPTION
            WHEN unique_violation THEN
                -- If another transaction already inserted for this user, treat as success.
                IF EXISTS (SELECT 1 FROM public.referral_codes WHERE user_id = NEW.id) THEN
                    RETURN NEW;
                END IF;
                -- Otherwise retry on possible code collision.
        END;
    END LOOP;

    RAISE EXCEPTION 'Unable to generate unique referral code after % attempts for user %', 25, NEW.id;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'on_profile_created_create_referral_code'
          AND tgrelid = 'public.profiles'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER on_profile_created_create_referral_code
            AFTER INSERT ON public.profiles
            FOR EACH ROW EXECUTE FUNCTION public.create_referral_code_for_profile();
    END IF;
END;
$$;

COMMIT;
