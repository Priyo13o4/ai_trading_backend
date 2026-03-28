-- Referral Audit Fixes Phase 1
-- Applied: 2026-03-27
-- Fixes: C1 (PK), C2+M2 (hold anchor + clamping), H3 (fraud RPC), M1 (enum)
-- Safe to re-run: all operations are idempotent.

BEGIN;

-- ============================================================
-- C1: Add PRIMARY KEY to referral_rewards
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.referral_rewards'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.referral_rewards
            ADD CONSTRAINT referral_rewards_pkey PRIMARY KEY (referral_id);
    END IF;
END $$;

COMMIT;

-- ============================================================
-- M1: Align referral_pause_cycle_status enum to 8-state machine
-- NOTE: ALTER TYPE ADD VALUE cannot run inside a transaction block.
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'reward_pending'
        AND enumtypid = 'public.referral_pause_cycle_status'::regtype) THEN
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE 'reward_pending';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'pause_pending'
        AND enumtypid = 'public.referral_pause_cycle_status'::regtype) THEN
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE 'pause_pending';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'resume_pending'
        AND enumtypid = 'public.referral_pause_cycle_status'::regtype) THEN
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE 'resume_pending';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'pause_failed'
        AND enumtypid = 'public.referral_pause_cycle_status'::regtype) THEN
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE 'pause_failed';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'resume_failed'
        AND enumtypid = 'public.referral_pause_cycle_status'::regtype) THEN
        ALTER TYPE public.referral_pause_cycle_status ADD VALUE 'resume_failed';
    END IF;
END $$;

-- ============================================================
-- C2 + M2: Update qualify_referral_reward RPC
--   C2: anchor hold_expires_at to payment timestamp (not NOW())
--   M2: transparently clamp hold_days to max 7
-- ============================================================
BEGIN;

CREATE OR REPLACE FUNCTION public.qualify_referral_reward(
    referred_user_id UUID,
    trigger_payment_id UUID,
    hold_days INT DEFAULT 7,
    payment_success_at TIMESTAMPTZ DEFAULT NULL
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
    v_hold_days INT := LEAST(COALESCE(hold_days, 7), 7);
    v_hold_anchor TIMESTAMPTZ := COALESCE(payment_success_at, NOW());
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
        RETURN QUERY SELECT 'skip_no_pending_referral'::TEXT, NULL::UUID, FALSE, FALSE;
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
        RETURN QUERY SELECT 'skip_not_first_success'::TEXT, v_referral_id, FALSE, FALSE;
        RETURN;
    END IF;

    INSERT INTO public.referral_rewards (
        referral_id, user_id, trigger_payment_id, status, hold_expires_at
    )
    VALUES (
        v_referral_id, v_referrer_id, trigger_payment_id,
        'on_hold',
        v_hold_anchor + make_interval(days => v_hold_days)
    )
    ON CONFLICT (referral_id) DO NOTHING;

    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    v_reward_created := v_row_count > 0;

    IF NOT v_reward_created THEN
        PERFORM 1 FROM public.referral_rewards AS rr
        WHERE rr.referral_id = v_referral_id FOR UPDATE;
    END IF;

    IF v_reward_created OR EXISTS (
        SELECT 1 FROM public.referral_rewards AS rr WHERE rr.referral_id = v_referral_id
    ) THEN
        UPDATE public.referral_tracking AS rt
        SET status = 'qualified',
            qualified_at = COALESCE(rt.qualified_at, NOW()),
            metadata = COALESCE(rt.metadata, '{}'::jsonb)
                || jsonb_build_object(
                    'qualified_trigger_payment_id', trigger_payment_id,
                    'qualified_via', 'qualify_referral_reward'
                )
        WHERE rt.id = v_referral_id AND rt.status = 'pending';

        GET DIAGNOSTICS v_row_count = ROW_COUNT;
        v_qualified_updated := v_row_count > 0;
    END IF;

    v_result_code := CASE WHEN v_reward_created THEN 'success_reward_created'
                          ELSE 'success_already_rewarded_reconciled' END;
    RETURN QUERY SELECT v_result_code, v_referral_id, v_reward_created, v_qualified_updated;
END;
$$;

REVOKE ALL ON FUNCTION public.qualify_referral_reward(UUID, UUID, INT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.qualify_referral_reward(UUID, UUID, INT, TIMESTAMPTZ) TO service_role;

COMMIT;

-- ============================================================
-- H3: check_duplicate_payment_identity RPC
-- ============================================================
BEGIN;

CREATE OR REPLACE FUNCTION public.check_duplicate_payment_identity(
    p_referrer_id UUID,
    p_payment_identity_hash TEXT,
    p_exclude_user_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM public.referral_tracking AS rt
        JOIN public.payment_transactions AS pt ON pt.user_id = rt.referred_id
        WHERE rt.referrer_id = p_referrer_id
          AND rt.referred_id <> p_exclude_user_id
          AND pt.payment_identity_hash = p_payment_identity_hash
          AND pt.status = 'succeeded'
          AND pt.payment_type = 'subscription'
        LIMIT 1
    );
END;
$$;

REVOKE ALL ON FUNCTION public.check_duplicate_payment_identity(UUID, TEXT, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_duplicate_payment_identity(UUID, TEXT, UUID) TO service_role;

COMMIT;
