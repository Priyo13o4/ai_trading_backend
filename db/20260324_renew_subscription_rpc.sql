-- Migration: Add renew_subscription RPC for webhook renewal flow
-- Date: 2026-03-24
-- Purpose: Provide renewal RPC compatible with current caller usage

CREATE OR REPLACE FUNCTION public.renew_subscription(
    p_subscription_id UUID,
    p_payment_id UUID DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_subscription RECORD;
    v_plan RECORD;
    v_new_expires_at TIMESTAMP WITH TIME ZONE;
BEGIN
    SELECT * INTO v_subscription
    FROM public.user_subscriptions
    WHERE id = p_subscription_id;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    IF auth.role() <> 'service_role' AND v_subscription.user_id <> auth.uid() THEN
        RAISE EXCEPTION 'Unauthorized: Cannot renew another user''s subscription';
    END IF;

    SELECT * INTO v_plan
    FROM public.subscription_plans
    WHERE id = v_subscription.plan_id;

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
        updated_at = NOW()
    WHERE id = p_subscription_id;

    RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.renew_subscription(UUID, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.renew_subscription(UUID, UUID) TO service_role;
