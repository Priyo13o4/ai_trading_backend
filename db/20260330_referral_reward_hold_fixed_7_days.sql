-- Pin referral reward hold window to 7 days in RPC logic.
-- Safe to rerun.

BEGIN;

CREATE OR REPLACE FUNCTION public.qualify_referral_reward(
    referred_user_id UUID,
    trigger_payment_id UUID,
    hold_days INT DEFAULT 7
)
RETURNS TABLE (
    result_code TEXT,
    referral_id UUID,
    reward_created BOOLEAN,
    qualified_updated BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_referral_id UUID;
    v_referrer_id UUID;
    v_first_success_payment_id UUID;
    v_reward_created BOOLEAN := FALSE;
    v_qualified_updated BOOLEAN := FALSE;
    v_result_code TEXT;
    v_hold_days INT := 7;
    v_row_count BIGINT := 0;
BEGIN
    SELECT rt.id, rt.referrer_id
    INTO v_referral_id, v_referrer_id
    FROM public.referral_tracking AS rt
    WHERE rt.referred_id = referred_user_id
      AND rt.status = 'pending'
    ORDER BY rt.attributed_at DESC NULLS LAST, rt.created_at DESC, rt.id DESC
    LIMIT 1
    FOR UPDATE;

    IF v_referral_id IS NULL THEN
        RETURN QUERY SELECT
            'skip_no_pending_referral'::TEXT,
            NULL::UUID,
            FALSE,
            FALSE;
        RETURN;
    END IF;

    SELECT pt.id
    INTO v_first_success_payment_id
    FROM public.payment_transactions AS pt
    WHERE pt.user_id = referred_user_id
      AND pt.status = 'succeeded'
      AND pt.payment_type = 'subscription'
    ORDER BY pt.created_at ASC, pt.id ASC
    LIMIT 1
    FOR UPDATE;

    IF v_first_success_payment_id IS NULL OR v_first_success_payment_id <> trigger_payment_id THEN
        RETURN QUERY SELECT
            'skip_not_first_success'::TEXT,
            v_referral_id,
            FALSE,
            FALSE;
        RETURN;
    END IF;

    INSERT INTO public.referral_rewards (
        referral_id,
        user_id,
        trigger_payment_id,
        status,
        hold_expires_at
    )
    VALUES (
        v_referral_id,
        v_referrer_id,
        trigger_payment_id,
        'on_hold',
        NOW() + make_interval(days => v_hold_days)
    )
    ON CONFLICT (referral_id) DO NOTHING;

    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    v_reward_created := v_row_count > 0;

    IF NOT v_reward_created THEN
        PERFORM 1
        FROM public.referral_rewards AS rr
        WHERE rr.referral_id = v_referral_id
        FOR UPDATE;
    END IF;

    IF v_reward_created OR EXISTS (
        SELECT 1
        FROM public.referral_rewards AS rr
        WHERE rr.referral_id = v_referral_id
    ) THEN
        UPDATE public.referral_tracking AS rt
        SET
            status = 'qualified',
            qualified_at = COALESCE(rt.qualified_at, NOW()),
            metadata = COALESCE(rt.metadata, '{}'::jsonb)
                || jsonb_build_object(
                    'qualified_trigger_payment_id', trigger_payment_id,
                    'qualified_via', 'qualify_referral_reward'
                )
        WHERE rt.id = v_referral_id
          AND rt.status = 'pending';

        GET DIAGNOSTICS v_row_count = ROW_COUNT;
        v_qualified_updated := v_row_count > 0;
    END IF;

    IF v_reward_created THEN
        v_result_code := 'success_reward_created';
    ELSE
        v_result_code := 'success_already_rewarded_reconciled';
    END IF;

    RETURN QUERY SELECT
        v_result_code,
        v_referral_id,
        v_reward_created,
        v_qualified_updated;
END;
$$;

REVOKE ALL ON FUNCTION public.qualify_referral_reward(UUID, UUID, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.qualify_referral_reward(UUID, UUID, INT) TO service_role;

COMMIT;
