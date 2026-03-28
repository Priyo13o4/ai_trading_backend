-- Enforce a minimum delay between account deletion OTP requests.
-- This complements frontend cooldown and prevents client-side bypass.

CREATE OR REPLACE FUNCTION public.request_account_deletion()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
    v_user_id UUID;
    v_user_email TEXT;
    v_otp_code TEXT;
    v_last_requested_at TIMESTAMPTZ;
    v_retry_after_seconds INTEGER;
BEGIN
    v_user_id := auth.uid();

    IF v_user_id IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Unauthorized: You must be logged in'
        );
    END IF;

    SELECT email INTO v_user_email
    FROM auth.users
    WHERE id = v_user_id;

    IF v_user_email IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'User not found'
        );
    END IF;

    SELECT
        COALESCE(
            (to_jsonb(adr)->>'requested_at')::timestamptz,
            (to_jsonb(adr)->>'created_at')::timestamptz
        )
    INTO v_last_requested_at
    FROM public.account_deletion_requests adr
    WHERE adr.user_id = v_user_id
      AND adr.verified = false
    ORDER BY COALESCE(
        (to_jsonb(adr)->>'requested_at')::timestamptz,
        (to_jsonb(adr)->>'created_at')::timestamptz
    ) DESC
    LIMIT 1;

    IF v_last_requested_at IS NOT NULL AND NOW() < (v_last_requested_at + INTERVAL '60 seconds') THEN
        v_retry_after_seconds := GREATEST(
            1,
            CEIL(EXTRACT(EPOCH FROM ((v_last_requested_at + INTERVAL '60 seconds') - NOW())))::INTEGER
        );

        RETURN jsonb_build_object(
            'success', false,
            'error', 'Please wait before requesting another code.',
            'retry_after_seconds', v_retry_after_seconds
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
    );

    RETURN jsonb_build_object(
        'success', true,
        'message', 'OTP sent to your email. Please check your inbox.',
        'email', v_user_email,
        'expires_in_minutes', 10
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', SQLERRM
        );
END;
$function$;

GRANT EXECUTE ON FUNCTION public.request_account_deletion() TO authenticated;
