-- Referral manual activation threshold (Scope D)
-- Date: 2026-03-29
-- Adds claimed state support and atomic manual activation RPC.

BEGIN;

ALTER TABLE public.referral_rewards
    ADD COLUMN IF NOT EXISTS activation_date TIMESTAMPTZ;

ALTER TABLE public.referral_rewards
    ADD COLUMN IF NOT EXISTS activated_by_user_id UUID;

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_status_check;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_status_check
    CHECK (status IN ('on_hold', 'available', 'applied', 'claimed', 'revoked'));

ALTER TABLE public.referral_rewards
    DROP CONSTRAINT IF EXISTS referral_rewards_activated_by_user_fk;

ALTER TABLE public.referral_rewards
    ADD CONSTRAINT referral_rewards_activated_by_user_fk
    FOREIGN KEY (activated_by_user_id)
    REFERENCES public.profiles(id)
    ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS public.referral_reward_activation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    qualified_count INTEGER NOT NULL,
    activated_months INTEGER NOT NULL,
    next_threshold INTEGER NOT NULL,
    remaining_referrals_for_next INTEGER NOT NULL,
    claimed_reward_ids UUID[] NOT NULL DEFAULT '{}',
    requested_referral_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_activation_events_user_created
    ON public.referral_reward_activation_events(user_id, created_at DESC);

ALTER TABLE public.referral_reward_activation_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'referral_reward_activation_events'
          AND policyname = 'Service role manages referral reward activation events'
    ) THEN
        CREATE POLICY "Service role manages referral reward activation events"
            ON public.referral_reward_activation_events FOR ALL
            USING (auth.role() = 'service_role');
    END IF;
END;
$$;

REVOKE ALL ON TABLE public.referral_reward_activation_events FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.referral_reward_activation_events TO service_role;

CREATE OR REPLACE FUNCTION public.activate_referral_reward_manual(
    p_user_id UUID,
    p_referral_code TEXT DEFAULT NULL
)
RETURNS TABLE (
    result_code TEXT,
    activated_months INTEGER,
    qualified_count INTEGER,
    next_threshold INTEGER,
    remaining_referrals_for_next INTEGER,
    claimed_reward_ids UUID[]
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_qualified_count INTEGER := 0;
    v_total_qualified_count INTEGER := 0;
    v_rewards_to_claim INTEGER := 0;
    v_activated_months INTEGER := 0;
    v_next_threshold INTEGER := 5;
    v_remaining_referrals_for_next INTEGER := 5;
    v_claimed_reward_ids UUID[] := '{}';
    v_claimed_count INTEGER := 0;
BEGIN
    SELECT COUNT(*)::INTEGER
      INTO v_qualified_count
      FROM public.referral_rewards
     WHERE user_id = p_user_id
       AND status IN ('available', 'applied');

    SELECT COUNT(*)::INTEGER
      INTO v_total_qualified_count
      FROM public.referral_rewards
     WHERE user_id = p_user_id
       AND status IN ('available', 'applied', 'claimed');

    v_next_threshold := ((v_total_qualified_count / 5) + 1) * 5;
    v_remaining_referrals_for_next := v_next_threshold - v_total_qualified_count;

    IF v_qualified_count < 5 THEN
        IF v_qualified_count = 0 AND v_total_qualified_count >= 5 THEN
            RETURN QUERY SELECT
                'already_claimed_all'::TEXT,
                0,
                v_qualified_count,
                v_next_threshold,
                v_remaining_referrals_for_next,
                v_claimed_reward_ids;
            RETURN;
        END IF;

        RETURN QUERY SELECT
            'insufficient_referrals'::TEXT,
            0,
            v_qualified_count,
            v_next_threshold,
            (5 - (v_qualified_count % 5))::INTEGER,
            v_claimed_reward_ids;
        RETURN;
    END IF;

    v_rewards_to_claim := (v_qualified_count / 5) * 5;

    WITH candidates AS (
        SELECT rr.referral_id
          FROM public.referral_rewards AS rr
         WHERE rr.user_id = p_user_id
           AND rr.status IN ('available', 'applied')
         ORDER BY rr.created_at ASC, rr.referral_id ASC
         LIMIT v_rewards_to_claim
         FOR UPDATE
    ), updated AS (
        UPDATE public.referral_rewards AS rr
           SET status = 'claimed',
               activation_date = NOW(),
               activated_by_user_id = p_user_id,
               updated_at = NOW()
          FROM candidates c
         WHERE rr.referral_id = c.referral_id
           AND rr.status IN ('available', 'applied')
        RETURNING rr.referral_id
    )
    SELECT COALESCE(array_agg(u.referral_id), '{}')
      INTO v_claimed_reward_ids
      FROM updated u;

    v_claimed_count := COALESCE(array_length(v_claimed_reward_ids, 1), 0);
    v_activated_months := v_claimed_count / 5;

    IF v_claimed_count = 0 THEN
        RETURN QUERY SELECT
            'already_claimed_all'::TEXT,
            0,
            v_qualified_count,
            v_next_threshold,
            v_remaining_referrals_for_next,
            v_claimed_reward_ids;
        RETURN;
    END IF;

    INSERT INTO public.referral_reward_activation_events (
        user_id,
        qualified_count,
        activated_months,
        next_threshold,
        remaining_referrals_for_next,
        claimed_reward_ids,
        requested_referral_code
    ) VALUES (
        p_user_id,
        v_qualified_count,
        v_activated_months,
        v_next_threshold,
        v_remaining_referrals_for_next,
        v_claimed_reward_ids,
        p_referral_code
    );

    RETURN QUERY SELECT
        'success'::TEXT,
        v_activated_months,
        v_qualified_count,
        v_next_threshold,
        v_remaining_referrals_for_next,
        v_claimed_reward_ids;
END;
$$;

REVOKE ALL ON FUNCTION public.activate_referral_reward_manual(UUID, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.activate_referral_reward_manual(UUID, TEXT) TO service_role;

COMMIT;
