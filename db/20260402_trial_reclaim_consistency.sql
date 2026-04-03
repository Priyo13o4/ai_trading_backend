-- Trial/Fraud consistency hardening:
-- 1) Preserve trial history on verified account deletion (email-hash keyed)
-- 2) On same-email re-signup:
--    - Resume remaining trial if previous trial has not expired
--    - Do not grant a fresh trial if previous trial is exhausted
-- 3) Keep repeat-device anti-abuse intact for non-reclaim flows

BEGIN;

CREATE TABLE IF NOT EXISTS public.used_trial_emails (
    email_hash TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ DEFAULT NOW(),
    trial_started_at TIMESTAMPTZ,
    trial_ends_at TIMESTAMPTZ,
    last_deleted_user_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.used_trial_emails
    ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_deleted_user_id UUID,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_used_trial_emails_trial_ends_at
    ON public.used_trial_emails(trial_ends_at);

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
    v_core_plan_id UUID;
    v_subscription_id UUID;
    v_email_hash TEXT;
    v_has_trial_history BOOLEAN := FALSE;
    v_historical_trial_started_at TIMESTAMPTZ;
    v_historical_trial_ends_at TIMESTAMPTZ;
    v_target_trial_ends_at TIMESTAMPTZ;
BEGIN
    INSERT INTO public.profiles (id, email, full_name, email_verified)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        NEW.email_confirmed_at IS NOT NULL
    )
    ON CONFLICT (id) DO UPDATE SET
        email = EXCLUDED.email,
        full_name = EXCLUDED.full_name,
        email_verified = EXCLUDED.email_verified;

    IF COALESCE(BTRIM(NEW.email), '') <> '' THEN
        v_email_hash := md5(LOWER(BTRIM(NEW.email)));

        SELECT
            TRUE,
            ute.trial_started_at,
            ute.trial_ends_at
        INTO
            v_has_trial_history,
            v_historical_trial_started_at,
            v_historical_trial_ends_at
        FROM public.used_trial_emails ute
        WHERE ute.email_hash = v_email_hash
        LIMIT 1;

        IF NOT FOUND THEN
            v_has_trial_history := FALSE;
        END IF;
    END IF;

    SELECT id INTO v_core_plan_id
    FROM public.subscription_plans
    WHERE name = 'core' AND is_active = TRUE
    LIMIT 1;

    IF v_core_plan_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF v_has_trial_history THEN
        IF v_historical_trial_ends_at IS NULL OR v_historical_trial_ends_at <= NOW() THEN
            -- Previous trial exists but is exhausted: no new trial is issued.
            RETURN NEW;
        END IF;

        v_target_trial_ends_at := v_historical_trial_ends_at;

        -- Create as trial and then pin exact expiry to the historical end time.
        v_subscription_id := public.create_subscription(
            NEW.id,
            v_core_plan_id,
            'manual',
            NULL,
            1,
            jsonb_build_object(
                'signup_date', NOW(),
                'trial_source', 'same_email_reclaim_resume',
                'historical_trial_started_at', v_historical_trial_started_at,
                'historical_trial_ends_at', v_historical_trial_ends_at,
                'reclaimed_email_hash', v_email_hash
            )
        );

        UPDATE public.user_subscriptions
        SET status = 'trial',
            expires_at = v_target_trial_ends_at,
            trial_ends_at = v_target_trial_ends_at,
            next_billing_date = v_target_trial_ends_at,
            metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'trial_source', 'same_email_reclaim_resume',
                'trial_resumed', TRUE,
                'historical_trial_started_at', v_historical_trial_started_at,
                'historical_trial_ends_at', v_historical_trial_ends_at,
                'reclaimed_email_hash', v_email_hash
            ),
            updated_at = NOW()
        WHERE id = v_subscription_id;
    ELSE
        v_target_trial_ends_at := NOW() + INTERVAL '7 days';

        v_subscription_id := public.create_subscription(
            NEW.id,
            v_core_plan_id,
            'manual',
            NULL,
            7,
            jsonb_build_object(
                'signup_date', NOW(),
                'trial_source', 'signup',
                'welcome_message', 'Welcome to PipFactor! Your 7-day trial is now active.'
            )
        );
    END IF;

    INSERT INTO public.user_pair_selections (
        user_id,
        subscription_id,
        selected_pairs,
        locked_until,
        can_change_pairs
    ) VALUES (
        NEW.id,
        v_subscription_id,
        ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD'],
        v_target_trial_ends_at,
        FALSE
    )
    ON CONFLICT DO NOTHING;

    RETURN NEW;
END;
$function$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'on_auth_user_created'
    ) THEN
        CREATE TRIGGER on_auth_user_created
            AFTER INSERT ON auth.users
            FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.verify_and_delete_account(
    p_otp_code TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
    v_user_id UUID;
    v_deletion_request RECORD;
    v_deleted_subscriptions INTEGER;
    v_deleted_payments INTEGER;
    v_deleted_selections INTEGER;
    v_user_email TEXT;
    v_email_hash TEXT;
    v_trial_started_at TIMESTAMPTZ;
    v_trial_ends_at TIMESTAMPTZ;
BEGIN
    v_user_id := auth.uid();

    IF v_user_id IS NULL THEN
        RETURN jsonb_build_object(
            'success', FALSE,
            'error', 'Unauthorized: You must be logged in'
        );
    END IF;

    SELECT * INTO v_deletion_request
    FROM public.account_deletion_requests
    WHERE user_id = v_user_id
      AND otp_code = p_otp_code
      AND verified = FALSE
      AND otp_expires_at > NOW()
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'success', FALSE,
            'error', 'Invalid or expired OTP. Please request a new one.'
        );
    END IF;

    SELECT COALESCE(au.email, p.email)
    INTO v_user_email
    FROM auth.users au
    LEFT JOIN public.profiles p ON p.id = au.id
    WHERE au.id = v_user_id;

    SELECT
        MIN(us.started_at),
        MAX(COALESCE(us.trial_ends_at, us.expires_at))
    INTO
        v_trial_started_at,
        v_trial_ends_at
    FROM public.user_subscriptions us
    WHERE us.user_id = v_user_id
      AND (us.trial_ends_at IS NOT NULL OR us.status = 'trial');

    IF COALESCE(BTRIM(v_user_email), '') <> '' AND v_trial_ends_at IS NOT NULL THEN
        v_email_hash := md5(LOWER(BTRIM(v_user_email)));

        INSERT INTO public.used_trial_emails (
            email_hash,
            deleted_at,
            trial_started_at,
            trial_ends_at,
            last_deleted_user_id,
            updated_at
        ) VALUES (
            v_email_hash,
            NOW(),
            v_trial_started_at,
            v_trial_ends_at,
            v_user_id,
            NOW()
        )
        ON CONFLICT (email_hash) DO UPDATE
        SET deleted_at = GREATEST(
                COALESCE(public.used_trial_emails.deleted_at, EXCLUDED.deleted_at),
                EXCLUDED.deleted_at
            ),
            trial_started_at = CASE
                WHEN public.used_trial_emails.trial_started_at IS NULL THEN EXCLUDED.trial_started_at
                WHEN EXCLUDED.trial_started_at IS NULL THEN public.used_trial_emails.trial_started_at
                ELSE LEAST(public.used_trial_emails.trial_started_at, EXCLUDED.trial_started_at)
            END,
            trial_ends_at = CASE
                WHEN public.used_trial_emails.trial_ends_at IS NULL THEN EXCLUDED.trial_ends_at
                WHEN EXCLUDED.trial_ends_at IS NULL THEN public.used_trial_emails.trial_ends_at
                ELSE GREATEST(public.used_trial_emails.trial_ends_at, EXCLUDED.trial_ends_at)
            END,
            last_deleted_user_id = EXCLUDED.last_deleted_user_id,
            updated_at = NOW();
    END IF;

    UPDATE public.account_deletion_requests
    SET verified = TRUE
    WHERE id = v_deletion_request.id;

    UPDATE public.user_subscriptions
    SET status = 'cancelled',
        cancelled_at = NOW(),
        auto_renew = FALSE
    WHERE user_id = v_user_id;
    GET DIAGNOSTICS v_deleted_subscriptions = ROW_COUNT;

    DELETE FROM public.user_pair_selections WHERE user_id = v_user_id;
    GET DIAGNOSTICS v_deleted_selections = ROW_COUNT;

    DELETE FROM public.payment_history WHERE user_id = v_user_id;
    GET DIAGNOSTICS v_deleted_payments = ROW_COUNT;

    DELETE FROM public.account_deletion_requests WHERE user_id = v_user_id;

    DELETE FROM public.profiles WHERE id = v_user_id;

    DELETE FROM auth.users WHERE id = v_user_id;

    RETURN jsonb_build_object(
        'success', TRUE,
        'message', 'Account deletion verified. All data has been permanently deleted.',
        'deleted_subscriptions', v_deleted_subscriptions,
        'deleted_payments', v_deleted_payments,
        'deleted_selections', v_deleted_selections
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN jsonb_build_object('success', FALSE, 'error', SQLERRM);
END;
$function$;

REVOKE EXECUTE ON FUNCTION public.verify_and_delete_account(text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.verify_and_delete_account(text) TO authenticated, service_role;

COMMIT;
