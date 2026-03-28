-- Scope 2: device-only trial eligibility registry.
-- Safe to rerun.

BEGIN;

CREATE TABLE IF NOT EXISTS public.device_trials (
    device_id_hash TEXT PRIMARY KEY,
    first_user_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    trial_used BOOLEAN NOT NULL DEFAULT TRUE,
    trial_first_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_device_trials_first_user_id
    ON public.device_trials(first_user_id);

ALTER TABLE public.device_trials ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'device_trials'
          AND policyname = 'Service role manages device trials'
    ) THEN
        CREATE POLICY "Service role manages device trials"
            ON public.device_trials FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.device_trials FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.device_trials TO service_role;

COMMIT;
