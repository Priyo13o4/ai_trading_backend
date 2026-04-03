-- Harden used_trial_emails access after trial reclaim migration.
-- Table is internal-only and should be accessible via service role paths.

BEGIN;

ALTER TABLE public.used_trial_emails ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.used_trial_emails FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.used_trial_emails TO service_role;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'used_trial_emails'
          AND policyname = 'Service role manages used_trial_emails'
    ) THEN
        CREATE POLICY "Service role manages used_trial_emails"
            ON public.used_trial_emails
            FOR ALL
            USING (auth.role() = 'service_role')
            WITH CHECK (auth.role() = 'service_role');
    END IF;
END;
$$;

COMMIT;
