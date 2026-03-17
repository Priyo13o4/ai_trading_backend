-- Phase 1 security hardening
-- Date: 2026-03-07

-- =====================================================
-- FUNCTION EXECUTE PERMISSIONS HARDENING
-- =====================================================

-- Remove execute permissions from broad roles for sensitive subscription functions.
REVOKE EXECUTE ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INTEGER, JSONB) FROM anon;
REVOKE EXECUTE ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INTEGER, JSONB) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INTEGER, JSONB) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.renew_subscription(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.renew_subscription(UUID, UUID) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.renew_subscription(UUID, UUID) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.cancel_subscription(UUID, BOOLEAN) FROM anon;
REVOKE EXECUTE ON FUNCTION public.cancel_subscription(UUID, BOOLEAN) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.cancel_subscription(UUID, BOOLEAN) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.record_payment(UUID, UUID, DECIMAL, TEXT, TEXT, TEXT, TEXT, JSONB) FROM anon;
REVOKE EXECUTE ON FUNCTION public.record_payment(UUID, UUID, DECIMAL, TEXT, TEXT, TEXT, TEXT, JSONB) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.record_payment(UUID, UUID, DECIMAL, TEXT, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.get_active_subscription(UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_active_subscription(UUID) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.get_active_subscription(UUID) FROM PUBLIC;

-- Mutation functions are service-role only.
GRANT EXECUTE ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INTEGER, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.renew_subscription(UUID, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.cancel_subscription(UUID, BOOLEAN) TO service_role;
GRANT EXECUTE ON FUNCTION public.record_payment(UUID, UUID, DECIMAL, TEXT, TEXT, TEXT, TEXT, JSONB) TO service_role;

-- Read function can be called by authenticated users and service role (never anon).
GRANT EXECUTE ON FUNCTION public.get_active_subscription(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_active_subscription(UUID) TO service_role;

-- =====================================================
-- FUNCTION BODY HARDENING (OWNERSHIP CHECKS)
-- =====================================================

CREATE OR REPLACE FUNCTION public.get_active_subscription(p_user_id UUID)
RETURNS TABLE (
    subscription_id UUID,
    plan_name TEXT,
    display_name TEXT,
    status TEXT,
    started_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE,
    days_remaining INTEGER,
    is_trial BOOLEAN,
    is_current BOOLEAN,
    pairs_allowed TEXT[],
    features JSONB,
    auto_renew BOOLEAN,
    cancel_at_period_end BOOLEAN
) AS $$
BEGIN
    -- Only the owner or service role can query this user's subscription.
    IF auth.role() <> 'service_role' AND auth.uid() <> p_user_id THEN
        RAISE EXCEPTION 'Unauthorized: Cannot get subscription for another user';
    END IF;

    RETURN QUERY
    SELECT
        us.id,
        sp.name,
        sp.display_name,
        us.status,
        us.started_at,
        us.expires_at,
        (EXTRACT(EPOCH FROM (us.expires_at - NOW())) / 86400)::INTEGER AS days_remaining,
        (us.status = 'trial') AS is_trial,
        (us.expires_at > NOW()) AS is_current,
        sp.pairs_allowed,
        sp.features,
        us.auto_renew,
        us.cancel_at_period_end
    FROM public.user_subscriptions us
    LEFT JOIN public.subscription_plans sp ON us.plan_id = sp.id
    WHERE us.user_id = p_user_id
    ORDER BY us.expires_at DESC NULLS LAST
    LIMIT 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION public.create_subscription(
    p_user_id UUID,
    p_plan_id UUID,
    p_payment_provider TEXT DEFAULT 'manual',
    p_external_id TEXT DEFAULT NULL,
    p_trial_days INTEGER DEFAULT 0,
    p_metadata JSONB DEFAULT '{}'
)
RETURNS UUID AS $$
DECLARE
    v_subscription_id UUID;
    v_plan RECORD;
    v_expires_at TIMESTAMP WITH TIME ZONE;
    v_trial_ends_at TIMESTAMP WITH TIME ZONE;
BEGIN
    -- Only the owner or service role can create subscriptions.
    IF auth.role() <> 'service_role' AND auth.uid() <> p_user_id THEN
        RAISE EXCEPTION 'Unauthorized: Cannot create subscription for another user';
    END IF;

    SELECT * INTO v_plan FROM public.subscription_plans WHERE id = p_plan_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Plan not found';
    END IF;

    UPDATE public.user_subscriptions
    SET status = 'cancelled',
        cancel_at_period_end = true,
        cancelled_at = NOW(),
        updated_at = NOW()
    WHERE user_id = p_user_id
      AND status IN ('active', 'trial');

    IF p_trial_days > 0 THEN
        v_trial_ends_at := NOW() + (p_trial_days || ' days')::INTERVAL;
        v_expires_at := v_trial_ends_at;
    ELSE
        v_expires_at := CASE v_plan.billing_period
            WHEN 'monthly' THEN NOW() + INTERVAL '30 days'
            WHEN 'yearly' THEN NOW() + INTERVAL '365 days'
            WHEN 'lifetime' THEN NOW() + INTERVAL '100 years'
            ELSE NOW() + INTERVAL '30 days'
        END;
    END IF;

    INSERT INTO public.user_subscriptions (
        user_id,
        plan_id,
        status,
        expires_at,
        trial_ends_at,
        payment_provider,
        external_subscription_id,
        next_billing_date,
        metadata
    )
    VALUES (
        p_user_id,
        p_plan_id,
        CASE WHEN p_trial_days > 0 THEN 'trial' ELSE 'active' END,
        v_expires_at,
        v_trial_ends_at,
        p_payment_provider,
        p_external_id,
        v_expires_at,
        p_metadata
    )
    RETURNING id INTO v_subscription_id;

    RETURN v_subscription_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION public.renew_subscription(
    p_subscription_id UUID,
    p_payment_id UUID DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
    v_subscription RECORD;
    v_plan RECORD;
    v_new_expires_at TIMESTAMP WITH TIME ZONE;
BEGIN
    SELECT * INTO v_subscription
    FROM public.user_subscriptions
    WHERE id = p_subscription_id;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    -- Only the owner of the subscription or service role can renew.
    IF auth.role() <> 'service_role' AND v_subscription.user_id <> auth.uid() THEN
        RAISE EXCEPTION 'Unauthorized: Cannot renew another user''s subscription';
    END IF;

    SELECT * INTO v_plan FROM public.subscription_plans WHERE id = v_subscription.plan_id;

    v_new_expires_at := CASE v_plan.billing_period
        WHEN 'monthly' THEN GREATEST(NOW(), v_subscription.expires_at) + INTERVAL '30 days'
        WHEN 'yearly' THEN GREATEST(NOW(), v_subscription.expires_at) + INTERVAL '365 days'
        WHEN 'lifetime' THEN NOW() + INTERVAL '100 years'
        ELSE GREATEST(NOW(), v_subscription.expires_at) + INTERVAL '30 days'
    END;

    UPDATE public.user_subscriptions
    SET status = 'active',
        expires_at = v_new_expires_at,
        next_billing_date = v_new_expires_at,
        last_payment_date = NOW(),
        updated_at = NOW()
    WHERE id = p_subscription_id;

    RETURN true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION public.cancel_subscription(
    p_subscription_id UUID,
    p_immediate BOOLEAN DEFAULT false
)
RETURNS BOOLEAN AS $$
DECLARE
    v_subscription_user_id UUID;
BEGIN
    SELECT user_id INTO v_subscription_user_id
    FROM public.user_subscriptions
    WHERE id = p_subscription_id;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    -- Only the owner of the subscription or service role can cancel.
    IF auth.role() <> 'service_role' AND v_subscription_user_id <> auth.uid() THEN
        RAISE EXCEPTION 'Unauthorized: Cannot cancel another user''s subscription';
    END IF;

    IF p_immediate THEN
        UPDATE public.user_subscriptions
        SET status = 'cancelled',
            cancelled_at = NOW(),
            auto_renew = false,
            updated_at = NOW()
        WHERE id = p_subscription_id;
    ELSE
        UPDATE public.user_subscriptions
        SET cancel_at_period_end = true,
            auto_renew = false,
            updated_at = NOW()
        WHERE id = p_subscription_id;
    END IF;

    RETURN true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION public.record_payment(
    p_user_id UUID,
    p_subscription_id UUID,
    p_amount DECIMAL,
    p_currency TEXT,
    p_provider TEXT,
    p_external_payment_id TEXT,
    p_status TEXT DEFAULT 'succeeded',
    p_metadata JSONB DEFAULT '{}'
)
RETURNS UUID AS $$
DECLARE
    v_payment_id UUID;
    v_subscription_user_id UUID;
BEGIN
    SELECT user_id INTO v_subscription_user_id
    FROM public.user_subscriptions
    WHERE id = p_subscription_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Subscription not found';
    END IF;

    -- For non-service callers, payment can only be recorded for caller-owned subscriptions.
    IF auth.role() <> 'service_role' AND v_subscription_user_id <> auth.uid() THEN
        RAISE EXCEPTION 'Unauthorized: Cannot record payment for another user''s subscription';
    END IF;

    -- For non-service callers, the provided user_id must match the subscription owner.
    IF auth.role() <> 'service_role' AND p_user_id <> v_subscription_user_id THEN
        RAISE EXCEPTION 'Unauthorized: user_id does not match subscription owner';
    END IF;

    INSERT INTO public.payment_history (
        user_id,
        subscription_id,
        amount,
        currency,
        status,
        provider,
        external_payment_id,
        metadata
    )
    VALUES (
        p_user_id,
        p_subscription_id,
        p_amount,
        p_currency,
        p_status,
        p_provider,
        p_external_payment_id,
        p_metadata
    )
    RETURNING id INTO v_payment_id;

    IF p_status = 'succeeded' THEN
        UPDATE public.user_subscriptions
        SET last_payment_amount = p_amount,
            last_payment_date = NOW(),
            updated_at = NOW()
        WHERE id = p_subscription_id;
    END IF;

    RETURN v_payment_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

-- =====================================================
-- ACCOUNT DELETION RESPONSE HARDENING
-- =====================================================

CREATE OR REPLACE FUNCTION public.request_account_deletion()
RETURNS JSONB AS $$
DECLARE
    v_user_id UUID;
    v_user_email TEXT;
    v_otp_code TEXT;
    v_deletion_request_id UUID;
BEGIN
    v_user_id := auth.uid();

    IF v_user_id IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Unauthorized: You must be logged in'
        );
    END IF;

    SELECT email INTO v_user_email
    FROM public.profiles
    WHERE id = v_user_id;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'User profile not found'
        );
    END IF;

    v_otp_code := LPAD(FLOOR(RANDOM() * 1000000)::TEXT, 6, '0');

    DELETE FROM public.account_deletion_requests
    WHERE user_id = v_user_id
      AND verified = false;

    INSERT INTO public.account_deletion_requests (
        user_id,
        otp_code,
        otp_expires_at,
        verified
    ) VALUES (
        v_user_id,
        v_otp_code,
        NOW() + INTERVAL '10 minutes',
        false
    )
    RETURNING id INTO v_deletion_request_id;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'OTP sent to your email. Please check your inbox.',
        'email', v_user_email,
        'expires_in_minutes', 10
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;
