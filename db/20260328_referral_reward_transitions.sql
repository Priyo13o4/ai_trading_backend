-- Referral Rewards Transition RPCs - Scope C Implementation
-- Date: 2026-03-28
-- Scope: Automated state transitions for referral rewards hold -> available -> applied
-- Idempotent: Safe to re-run; uses UPSERT patterns and deterministic logic

BEGIN;

-- --------------------------------------------------------------------------
-- 1) transition_rewards_on_hold_to_available()
--    Batch transition: on_hold -> available when hold_expires_at <= NOW (UTC)
--    Idempotent: Safe to call repeatedly; only updates rows that meet criteria
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.transition_rewards_on_hold_to_available()
RETURNS TABLE (
    transitioned_count BIGINT,
    result_code TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_count BIGINT := 0;
BEGIN
    -- Batch update all on_hold rewards where expiration has passed
    UPDATE public.referral_rewards AS rr
    SET
        status = 'available',
        updated_at = NOW()
    WHERE rr.status = 'on_hold'
      AND rr.hold_expires_at IS NOT NULL
      AND rr.hold_expires_at <= NOW();

    GET DIAGNOSTICS v_count = ROW_COUNT;

    RETURN QUERY SELECT
        v_count,
        'success'::TEXT;
END;
$$;

REVOKE ALL ON FUNCTION public.transition_rewards_on_hold_to_available() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.transition_rewards_on_hold_to_available() TO service_role;

-- --------------------------------------------------------------------------
-- 2) apply_available_rewards()
--    Batch transition: available -> applied
--    Idempotent: Safe to call repeatedly; only updates rows in 'available' status
--    Note: Does NOT validate payment provider state; assumes external readiness
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.apply_available_rewards()
RETURNS TABLE (
    applied_count BIGINT,
    result_code TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_count BIGINT := 0;
BEGIN
    -- Batch transition all available rewards to applied status
    -- This is a state machine: available -> applied
    -- Safe to call repeatedly (no-op if no available rewards)
    UPDATE public.referral_rewards AS rr
    SET
        status = 'applied',
        updated_at = NOW()
    WHERE rr.status = 'available';

    GET DIAGNOSTICS v_count = ROW_COUNT;

    RETURN QUERY SELECT
        v_count,
        'success'::TEXT;
END;
$$;

REVOKE ALL ON FUNCTION public.apply_available_rewards() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_available_rewards() TO service_role;

COMMIT;
