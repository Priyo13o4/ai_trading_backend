-- Referral pause/resume v2 window fields
-- Adds provider-derived billing window metadata and pause confirmation/access fields.
-- Idempotent and forward-only.

BEGIN;

ALTER TABLE public.referral_reward_pause_cycles
    ADD COLUMN IF NOT EXISTS last_charge_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_charge_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cycle_duration_seconds INTEGER CHECK (cycle_duration_seconds IS NULL OR cycle_duration_seconds > 0),
    ADD COLUMN IF NOT EXISTS pause_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS free_access_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pause_deferred_until TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_referral_pause_cycles_pending_deferred
    ON public.referral_reward_pause_cycles(status, pause_deferred_until)
    WHERE status IN ('reward_pending', 'pause_pending', 'pause_failed');

CREATE INDEX IF NOT EXISTS idx_referral_pause_cycles_access_window
    ON public.referral_reward_pause_cycles(status, free_access_until)
    WHERE status = 'paused';

COMMIT;
