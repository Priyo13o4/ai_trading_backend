CREATE OR REPLACE FUNCTION public.verify_and_delete_account(
    p_otp_code TEXT
)
RETURNS JSONB AS $$
DECLARE
    v_user_id UUID;
    v_deletion_request RECORD;
    v_deleted_subscriptions INTEGER;
    v_deleted_payments INTEGER;
    v_deleted_selections INTEGER;
BEGIN
    -- SECURITY: Get authenticated user ID
    v_user_id := auth.uid();
    
    IF v_user_id IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Unauthorized: You must be logged in'
        );
    END IF;
    
    -- Find deletion request
    SELECT * INTO v_deletion_request
    FROM public.account_deletion_requests
    WHERE user_id = v_user_id
    AND otp_code = p_otp_code
    AND verified = false
    AND otp_expires_at > NOW()
    LIMIT 1;
    
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Invalid or expired OTP. Please request a new one.'
        );
    END IF;
    
    -- Mark request as verified
    UPDATE public.account_deletion_requests
    SET verified = true
    WHERE id = v_deletion_request.id;
    
    -- 1. Cancel all subscriptions first
    UPDATE public.user_subscriptions
    SET status = 'cancelled',
        cancelled_at = NOW(),
        auto_renew = false
    WHERE user_id = v_user_id;
    GET DIAGNOSTICS v_deleted_subscriptions = ROW_COUNT;
    
    -- 2. Delete pair selections
    DELETE FROM public.user_pair_selections WHERE user_id = v_user_id;
    GET DIAGNOSTICS v_deleted_selections = ROW_COUNT;
    
    -- 3. Delete payment history
    DELETE FROM public.payment_history WHERE user_id = v_user_id;
    GET DIAGNOSTICS v_deleted_payments = ROW_COUNT;
    
    -- 4. Delete deletion requests
    DELETE FROM public.account_deletion_requests WHERE user_id = v_user_id;
    
    -- 5. Delete profile (this will CASCADE delete subscriptions due to FK)
    DELETE FROM public.profiles WHERE id = v_user_id;
    
    -- 6. Actually delete auth user from Supabase (Requires SECURITY DEFINER)
    DELETE FROM auth.users WHERE id = v_user_id;
    
    RETURN jsonb_build_object(
        'success', true,
        'message', 'Account deletion verified. All data has been permanently deleted.',
        'deleted_subscriptions', v_deleted_subscriptions,
        'deleted_payments', v_deleted_payments,
        'deleted_selections', v_deleted_selections
    );
EXCEPTION WHEN OTHERS THEN
    RETURN jsonb_build_object('success', false, 'error', SQLERRM);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE EXECUTE ON FUNCTION public.verify_and_delete_account(text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.verify_and_delete_account(text) TO authenticated, service_role;
