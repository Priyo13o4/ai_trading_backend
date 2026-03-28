-- Referral pause/resume schema alignment (Scope E)
-- Ensures architecture-shift pause cycle columns and supporting indexes exist.
-- Safe to re-run in production.

BEGIN;

ALTER TABLE public.referral_reward_pause_cycles
    ADD COLUMN IF NOT EXISTS last_charge_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_charge_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pause_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS free_access_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pause_deferred_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cycle_duration_seconds INTEGER CHECK (cycle_duration_seconds IS NULL OR cycle_duration_seconds > 0);

-- Resume due scans: rows ready to transition from paused to resume flow.
CREATE INDEX IF NOT EXISTS idx_referral_pause_cycles_resume_due
    ON public.referral_reward_pause_cycles(status, pause_end_time)
    WHERE status IN ('paused', 'resume_pending');

-- Pending/deferred processing queue scans.
CREATE INDEX IF NOT EXISTS idx_referral_pause_cycles_pending_deferred
    ON public.referral_reward_pause_cycles(status, pause_deferred_until)
    WHERE status IN ('reward_pending', 'pause_pending', 'pause_failed');

-- Access gating checks for effective free access windows.
CREATE INDEX IF NOT EXISTS idx_referral_pause_cycles_access_gate
    ON public.referral_reward_pause_cycles(status, pause_confirmed, free_access_until)
    WHERE status = 'paused';

COMMIT;
