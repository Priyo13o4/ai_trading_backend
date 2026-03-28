-- Post-audit hardening deltas for referral tables only.
-- Safe to rerun on already-live environments.

BEGIN;

-- --------------------------------------------------------------------------
-- 1) Add audit metadata columns to referral_tracking (idempotent)
-- --------------------------------------------------------------------------
ALTER TABLE IF EXISTS public.referral_tracking
    ADD COLUMN IF NOT EXISTS registration_ip_prefix TEXT;

ALTER TABLE IF EXISTS public.referral_tracking
    ADD COLUMN IF NOT EXISTS registration_ua_hash TEXT;

-- --------------------------------------------------------------------------
-- 2) Remove end-user SELECT exposure on referral_tracking
--    Keep service_role policy/grants intact.
-- --------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'referral_tracking'
          AND policyname = 'Users view own referral tracking'
    ) THEN
        DROP POLICY "Users view own referral tracking" ON public.referral_tracking;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'referral_tracking'
          AND policyname = 'Service role manages referral tracking'
    ) THEN
        CREATE POLICY "Service role manages referral tracking"
            ON public.referral_tracking FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE SELECT ON TABLE public.referral_tracking FROM authenticated;
GRANT ALL ON TABLE public.referral_tracking TO service_role;

-- --------------------------------------------------------------------------
-- 3) Ensure updated_at trigger functions/triggers exist for referral tables
--    Recreate triggers to guarantee correct binding and behavior.
-- --------------------------------------------------------------------------
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

DROP TRIGGER IF EXISTS set_referral_tracking_updated_at ON public.referral_tracking;
CREATE TRIGGER set_referral_tracking_updated_at
    BEFORE UPDATE ON public.referral_tracking
    FOR EACH ROW EXECUTE FUNCTION public.set_referral_tracking_updated_at();

DROP TRIGGER IF EXISTS set_referral_codes_updated_at ON public.referral_codes;
CREATE TRIGGER set_referral_codes_updated_at
    BEFORE UPDATE ON public.referral_codes
    FOR EACH ROW EXECUTE FUNCTION public.set_referral_codes_updated_at();

COMMIT;
