-- Referral Pause/Resume Tracking - Scope E
-- Date: 2026-03-29
-- Purpose: Track Razorpay pause/resume state machine for referral free-month rewards.
-- Idempotent: Safe to re-run.

BEGIN;

-- Ensure Scope D compatibility: referral reward state machine includes `claimed`.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'referral_rewards_status_check'
          AND conrelid = 'public.referral_rewards'::regclass
    ) THEN
        ALTER TABLE public.referral_rewards
            DROP CONSTRAINT referral_rewards_status_check;
    END IF;

    ALTER TABLE public.referral_rewards
        ADD CONSTRAINT referral_rewards_status_check
        CHECK (status IN ('on_hold','available','applied','revoked','claimed'));
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type
        WHERE typname = 'referral_pause_cycle_status'
    ) THEN
        CREATE TYPE public.referral_pause_cycle_status AS ENUM (
            'reward_pending',
            'pause_pending',
            'paused',
            'resume_pending',
            'resumed',
            'pause_failed',
            'resume_failed'
        );
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS public.referral_reward_pause_cycles (
    reward_id UUID NOT NULL
        REFERENCES public.referral_rewards(referral_id) ON DELETE CASCADE,
    cycle_number INTEGER NOT NULL CHECK (cycle_number > 0),
    status public.referral_pause_cycle_status NOT NULL DEFAULT 'reward_pending',
    razorpay_pause_id TEXT,
    razorpay_subscription_id TEXT NOT NULL,
    pause_start_time TIMESTAMPTZ,
    pause_end_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT referral_reward_pause_cycles_pk PRIMARY KEY (reward_id, cycle_number)
);

CREATE INDEX IF NOT EXISTS idx_referral_reward_pause_cycles_status
    ON public.referral_reward_pause_cycles(status, pause_end_time);

CREATE INDEX IF NOT EXISTS idx_referral_reward_pause_cycles_subscription
    ON public.referral_reward_pause_cycles(razorpay_subscription_id, status);

ALTER TABLE public.referral_reward_pause_cycles ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_reward_pause_cycles'
          AND policyname = 'Users view own referral pause cycles'
    ) THEN
        CREATE POLICY "Users view own referral pause cycles"
            ON public.referral_reward_pause_cycles FOR SELECT
            USING (
                EXISTS (
                    SELECT 1
                    FROM public.referral_rewards rr
                    WHERE rr.referral_id = reward_id
                      AND rr.user_id = auth.uid()
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_reward_pause_cycles'
          AND policyname = 'Service role manages referral pause cycles'
    ) THEN
        CREATE POLICY "Service role manages referral pause cycles"
            ON public.referral_reward_pause_cycles FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_proc
        WHERE proname = 'update_updated_at'
          AND pg_function_is_visible(oid)
    ) THEN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_trigger
            WHERE tgname = 'set_referral_reward_pause_cycles_updated_at'
              AND tgrelid = 'public.referral_reward_pause_cycles'::regclass
              AND NOT tgisinternal
        ) THEN
            CREATE TRIGGER set_referral_reward_pause_cycles_updated_at
                BEFORE UPDATE ON public.referral_reward_pause_cycles
                FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
        END IF;
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.referral_reward_pause_cycles FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.referral_reward_pause_cycles TO authenticated;
GRANT ALL ON TABLE public.referral_reward_pause_cycles TO service_role;

COMMIT;
