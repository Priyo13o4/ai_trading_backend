-- Referral pause/resume state vocabulary upgrade
-- Adds phase-specific pending/failed states and safely maps legacy rows.
-- Idempotent and compatible with both legacy and fresh installs.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public'
          AND t.typname = 'referral_pause_cycle_status'
    ) THEN
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE IF NOT EXISTS 'reward_pending';
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE IF NOT EXISTS 'pause_pending';
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE IF NOT EXISTS 'resume_pending';
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE IF NOT EXISTS 'pause_failed';
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE IF NOT EXISTS 'resume_failed';
    END IF;
END;
$$;

-- Legacy compatibility mapping:
-- pending -> pause_pending (already queued for provider pause)
-- failed -> phase-specific failed based on whether pause metadata exists
UPDATE public.referral_reward_pause_cycles
SET status = CASE
    WHEN status::text = 'pending' THEN 'pause_pending'::public.referral_pause_cycle_status
    WHEN status::text = 'failed' THEN CASE
        WHEN COALESCE(pause_confirmed, FALSE) = TRUE
          OR pause_end_time IS NOT NULL
          OR razorpay_pause_id IS NOT NULL
        THEN 'resume_failed'::public.referral_pause_cycle_status
        ELSE 'pause_failed'::public.referral_pause_cycle_status
    END
    ELSE status
END
WHERE status::text IN ('pending', 'failed');

ALTER TABLE public.referral_reward_pause_cycles
    ALTER COLUMN status SET DEFAULT 'reward_pending';

COMMIT;
