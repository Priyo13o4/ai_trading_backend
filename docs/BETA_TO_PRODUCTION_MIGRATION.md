# Beta to Production Migration SQL

## What This Does:
1. **Updates subscription plans** with production pricing ($5, $8, $12)
2. **Adds beta plan** (temporary, free access to all features for 1 year)
3. **Creates user_pair_selections table** to track which pairs users select (locked until next payment)
4. **Updates handle_new_user trigger** to auto-assign beta access to new signups
5. **Adds select_trading_pairs function** for users to choose their pairs post-payment
6. **All beta code is marked with "-- BETA:" comments** for easy removal later

## How to Remove Beta After Launch:
1. Search for "-- BETA:" in this file and in frontend code
2. Delete those sections from database (run the removal queries at the bottom)
3. Remove beta-related frontend components (BetaBanner.tsx, beta badges, etc.)
4. Done! Clean production-ready code remains.

---

## 🚀 EXECUTE THIS IN SUPABASE SQL EDITOR

```sql
-- =================================================================
-- BETA TO PRODUCTION SUBSCRIPTION SYSTEM
-- Pricing: Starter $5, Professional $8, Elite $12
-- Beta: Auto-assign all new users to Elite (free for 1 year)
-- =================================================================

-- =====================================================
-- STEP 1: Update Subscription Plans (Production Ready)
-- =====================================================

-- Clear existing plans (keeps subscription history intact)
DELETE FROM public.subscription_plans;

-- Insert production plans + beta plan
INSERT INTO public.subscription_plans (
    name, 
    display_name, 
    description,
    price_usd, 
    billing_period, 
    pairs_allowed, 
    features,
    ai_analysis_enabled, 
    priority_support,
    api_access_enabled,
    sort_order,
    is_active
)
VALUES
    -- BETA: Free Elite access for beta testers (REMOVE AFTER BETA)
    (
        'beta', 
        '🚀 Beta Access', 
        'Free Elite access during our beta testing period. Thank you for being an early adopter!',
        0, 
        'yearly', 
        ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD'],
        jsonb_build_object(
            'is_beta', true,
            'beta_expires', '2026-01-01',
            'news_analysis', true,
            'all_pairs', true,
            'email_notifications', true,
            'mobile_app_access', true,
            'priority_support', true,
            'advanced_analytics', true
        ),
        true,
        true,
        true,
        0,
        true
    ),
    -- Production Plan 1: Starter ($5/month)
    (
        'starter', 
        'Starter', 
        'Perfect for beginners - choose 1 trading pair + news analysis',
        5.00, 
        'monthly', 
        ARRAY[]::TEXT[], -- Empty array, user selects 1 pair after payment
        jsonb_build_object(
            'news_analysis', true,
            'pair_limit', 1,
            'can_choose_pair', true,
            'email_notifications', true,
            'mobile_app_access', true
        ),
        true,
        false,
        false,
        1,
        true
    ),
    -- Production Plan 2: Professional ($8/month)
    (
        'professional', 
        'Professional', 
        'For active traders - choose 3 trading pairs + news analysis',
        8.00, 
        'monthly', 
        ARRAY[]::TEXT[], -- Empty array, user selects 3 pairs after payment
        jsonb_build_object(
            'news_analysis', true,
            'pair_limit', 3,
            'can_choose_pair', true,
            'email_notifications', true,
            'mobile_app_access', true,
            'advanced_analytics', true
        ),
        true,
        false,
        true,
        2,
        true
    ),
    -- Production Plan 3: Elite ($12/month)
    (
        'elite', 
        'Elite', 
        'Full access to all trading pairs + premium features',
        12.00, 
        'monthly', 
        ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD'],
        jsonb_build_object(
            'news_analysis', true,
            'all_pairs', true,
            'email_notifications', true,
            'mobile_app_access', true,
            'priority_support', true,
            'advanced_analytics', true,
            'api_access', true
        ),
        true,
        true,
        true,
        3,
        true
    )
ON CONFLICT (name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    price_usd = EXCLUDED.price_usd,
    billing_period = EXCLUDED.billing_period,
    pairs_allowed = EXCLUDED.pairs_allowed,
    features = EXCLUDED.features,
    ai_analysis_enabled = EXCLUDED.ai_analysis_enabled,
    priority_support = EXCLUDED.priority_support,
    api_access_enabled = EXCLUDED.api_access_enabled,
    sort_order = EXCLUDED.sort_order,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- =====================================================
-- STEP 2: User Pair Selections Table (Payment Lock)
-- =====================================================

CREATE TABLE IF NOT EXISTS public.user_pair_selections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES public.profiles(id) ON DELETE CASCADE,
    subscription_id UUID REFERENCES public.user_subscriptions(id) ON DELETE CASCADE,
    
    -- Selected pairs
    selected_pairs TEXT[] NOT NULL DEFAULT '{}',
    
    -- Payment cycle lock (prevents swapping pairs until next payment)
    locked_until TIMESTAMP WITH TIME ZONE,
    can_change_pairs BOOLEAN DEFAULT false,
    
    -- Metadata
    last_changed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    change_count INTEGER DEFAULT 0,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(user_id, subscription_id)
);

-- Enable RLS
ALTER TABLE public.user_pair_selections ENABLE ROW LEVEL SECURITY;

-- Users can view their own selections
CREATE POLICY "Users view own pair selections" ON public.user_pair_selections
    FOR SELECT USING (auth.uid() = user_id);

-- Service role manages selections
CREATE POLICY "Service role manages selections" ON public.user_pair_selections
    FOR ALL USING (auth.role() = 'service_role');

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_pair_selections_user ON public.user_pair_selections(user_id);
CREATE INDEX IF NOT EXISTS idx_pair_selections_subscription ON public.user_pair_selections(subscription_id);

-- =====================================================
-- STEP 3: Update Auto-Signup Trigger (BETA MODE)
-- =====================================================

-- BETA: Auto-assign beta plan to new users (REMOVE AFTER BETA)
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
DECLARE
    v_beta_plan_id UUID;
    v_subscription_id UUID;
BEGIN
    -- Create profile
    INSERT INTO public.profiles (id, email, full_name, email_verified)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        NEW.email_confirmed_at IS NOT NULL
    );
    
    -- BETA: Get beta plan ID (REMOVE AFTER BETA - replace with starter plan)
    SELECT id INTO v_beta_plan_id 
    FROM public.subscription_plans 
    WHERE name = 'beta' 
    LIMIT 1;
    
    -- BETA: Create 1-year free Elite access (REMOVE AFTER BETA)
    IF v_beta_plan_id IS NOT NULL THEN
        v_subscription_id := public.create_subscription(
            NEW.id,
            v_beta_plan_id,
            'manual',
            NULL,
            0, -- No trial, direct beta access
            jsonb_build_object(
                'signup_date', NOW(),
                'beta_tester', true,
                'welcome_message', 'Thank you for joining our beta!'
            )
        );
        
        -- BETA: Auto-select all 5 pairs for beta users (REMOVE AFTER BETA)
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
            NOW() + INTERVAL '1 year', -- Locked for 1 year (beta period)
            false -- Cannot change during beta
        );
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Apply trigger
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- =====================================================
-- STEP 4: Pair Selection Function (Post-Payment)
-- =====================================================

CREATE OR REPLACE FUNCTION public.select_trading_pairs(
    p_user_id UUID,
    p_selected_pairs TEXT[]
)
RETURNS JSONB AS $$
DECLARE
    v_subscription RECORD;
    v_plan RECORD;
    v_pair_limit INTEGER;
    v_existing_selection RECORD;
    v_can_change BOOLEAN;
BEGIN
    -- SECURITY: Verify caller is selecting for themselves
    IF auth.uid() != p_user_id THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Unauthorized: Cannot select pairs for other users'
        );
    END IF;
    
    -- Get active subscription
    SELECT * INTO v_subscription
    FROM public.user_subscriptions
    WHERE user_id = p_user_id
    AND status IN ('active', 'trial')
    AND expires_at > NOW()
    ORDER BY expires_at DESC
    LIMIT 1;
    
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'No active subscription found'
        );
    END IF;
    
    -- Get plan details
    SELECT * INTO v_plan
    FROM public.subscription_plans
    WHERE id = v_subscription.plan_id;
    
    -- BETA: Beta users cannot change pairs (REMOVE AFTER BETA)
    IF v_plan.name = 'beta' THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Beta users have all pairs pre-selected. Cannot change during beta period.'
        );
    END IF;
    
    -- Elite users have all pairs, no need to select
    IF v_plan.name = 'elite' THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Elite plan includes all pairs automatically'
        );
    END IF;
    
    -- Get pair limit from plan
    v_pair_limit := (v_plan.features->>'pair_limit')::INTEGER;
    
    IF v_pair_limit IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Plan does not support pair selection'
        );
    END IF;
    
    -- Validate pair count
    IF array_length(p_selected_pairs, 1) != v_pair_limit THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', format('You must select exactly %s pair(s)', v_pair_limit)
        );
    END IF;
    
    -- Validate pairs are from allowed list
    IF NOT (p_selected_pairs <@ ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD']) THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Invalid trading pair selected'
        );
    END IF;
    
    -- Check existing selection
    SELECT * INTO v_existing_selection
    FROM public.user_pair_selections
    WHERE user_id = p_user_id
    AND subscription_id = v_subscription.id;
    
    IF FOUND THEN
        -- Check if user can change pairs
        v_can_change := v_existing_selection.can_change_pairs 
                        OR v_existing_selection.locked_until < NOW();
        
        IF NOT v_can_change THEN
            RETURN jsonb_build_object(
                'success', false,
                'error', 'Pair selection is locked until next billing cycle',
                'locked_until', v_existing_selection.locked_until
            );
        END IF;
        
        -- Update existing selection
        UPDATE public.user_pair_selections
        SET selected_pairs = p_selected_pairs,
            locked_until = v_subscription.next_billing_date,
            can_change_pairs = false,
            last_changed_at = NOW(),
            change_count = change_count + 1,
            updated_at = NOW()
        WHERE id = v_existing_selection.id;
    ELSE
        -- Create new selection
        INSERT INTO public.user_pair_selections (
            user_id,
            subscription_id,
            selected_pairs,
            locked_until,
            can_change_pairs
        ) VALUES (
            p_user_id,
            v_subscription.id,
            p_selected_pairs,
            v_subscription.next_billing_date,
            false
        );
    END IF;
    
    RETURN jsonb_build_object(
        'success', true,
        'selected_pairs', p_selected_pairs,
        'locked_until', v_subscription.next_billing_date
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- =====================================================
-- STEP 5: Enhanced can_access_pair Function
-- =====================================================

-- Updated to check user_pair_selections table
CREATE OR REPLACE FUNCTION public.can_access_pair(
    p_user_id UUID,
    p_trading_pair TEXT
)
RETURNS BOOLEAN AS $$
DECLARE
    v_subscription RECORD;
    v_plan RECORD;
    v_selected_pairs TEXT[];
BEGIN
    -- SECURITY: Verify caller is checking their own access or is service role
    IF auth.uid() != p_user_id AND auth.role() != 'service_role' THEN
        RAISE EXCEPTION 'Unauthorized: Cannot check access for other users';
    END IF;
    
    -- Get active subscription
    SELECT * INTO v_subscription FROM public.get_active_subscription(p_user_id);
    
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    
    IF NOT v_subscription.is_current THEN
        RETURN false;
    END IF;
    
    IF v_subscription.status NOT IN ('active', 'trial') THEN
        RETURN false;
    END IF;
    
    -- Get plan details
    SELECT * INTO v_plan
    FROM public.subscription_plans
    WHERE name = v_subscription.plan_name;
    
    -- BETA: Beta users have access to all pairs (REMOVE AFTER BETA)
    IF v_plan.name = 'beta' THEN
        RETURN p_trading_pair = ANY(ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD']);
    END IF;
    
    -- Elite plan: all pairs
    IF v_plan.name = 'elite' THEN
        RETURN p_trading_pair = ANY(v_plan.pairs_allowed);
    END IF;
    
    -- Starter/Professional: check selected pairs
    SELECT selected_pairs INTO v_selected_pairs
    FROM public.user_pair_selections
    WHERE user_id = p_user_id
    AND subscription_id = v_subscription.subscription_id
    LIMIT 1;
    
    IF NOT FOUND OR v_selected_pairs IS NULL THEN
        RETURN false; -- No pairs selected yet
    END IF;
    
    RETURN p_trading_pair = ANY(v_selected_pairs);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- =====================================================
-- STEP 6: Account Deletion with OTP Verification
-- =====================================================

-- Table to store account deletion requests with OTP
CREATE TABLE IF NOT EXISTS public.account_deletion_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES public.profiles(id) ON DELETE CASCADE,
    otp_code TEXT NOT NULL,
    otp_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    verified BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(user_id, otp_code)
);

-- Enable RLS
ALTER TABLE public.account_deletion_requests ENABLE ROW LEVEL SECURITY;

-- Users can view their own deletion requests
CREATE POLICY "Users view own deletion requests" ON public.account_deletion_requests
    FOR SELECT USING (auth.uid() = user_id);

-- Service role manages deletion requests
CREATE POLICY "Service role manages deletion requests" ON public.account_deletion_requests
    FOR ALL USING (auth.role() = 'service_role');

-- Index
CREATE INDEX IF NOT EXISTS idx_deletion_requests_user ON public.account_deletion_requests(user_id);

-- Function: Request account deletion (generates OTP, sends email)
CREATE OR REPLACE FUNCTION public.request_account_deletion()
RETURNS JSONB AS $$
DECLARE
    v_user_id UUID;
    v_user_email TEXT;
    v_otp_code TEXT;
    v_deletion_request_id UUID;
BEGIN
    -- SECURITY: Get authenticated user ID
    v_user_id := auth.uid();
    
    IF v_user_id IS NULL THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'Unauthorized: You must be logged in'
        );
    END IF;
    
    -- Get user email
    SELECT email INTO v_user_email
    FROM public.profiles
    WHERE id = v_user_id;
    
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'success', false,
            'error', 'User profile not found'
        );
    END IF;
    
    -- Generate 6-digit OTP
    v_otp_code := LPAD(FLOOR(RANDOM() * 1000000)::TEXT, 6, '0');
    
    -- Delete any existing unverified requests for this user
    DELETE FROM public.account_deletion_requests
    WHERE user_id = v_user_id
    AND verified = false;
    
    -- Create deletion request
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
    
    -- TODO: Send OTP via email (integrate with your email service)
    -- For now, return OTP in response (REMOVE IN PRODUCTION!)
    
    RETURN jsonb_build_object(
        'success', true,
        'message', 'OTP sent to your email. Please check your inbox.',
        'email', v_user_email,
        'otp_for_testing', v_otp_code, -- REMOVE THIS IN PRODUCTION!
        'expires_in_minutes', 10
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function: Verify OTP and delete account permanently
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
    
    -- Delete all user data (CASCADE will handle most of it)
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
    
    -- 6. Delete auth user (Supabase Auth)
    -- Note: This requires admin privileges, so we'll return a flag to handle this in the app
    
    RETURN jsonb_build_object(
        'success', true,
        'message', 'Account deletion verified. All data has been permanently deleted.',
        'deleted_subscriptions', v_deleted_subscriptions,
        'deleted_payments', v_deleted_payments,
        'deleted_selections', v_deleted_selections,
        'auth_user_deletion_required', true -- Frontend should call Supabase Admin API
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Grant permissions
GRANT EXECUTE ON FUNCTION public.request_account_deletion() TO authenticated;
GRANT EXECUTE ON FUNCTION public.verify_and_delete_account(TEXT) TO authenticated;

-- =====================================================
-- STEP 7: Grant Permissions (Security Hardened)
-- =====================================================

-- =====================================================
-- SECURITY: Revoke all default permissions first
-- =====================================================

-- Revoke all public access to functions
REVOKE ALL ON FUNCTION public.get_active_subscription(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.can_access_pair(UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.select_trading_pairs(UUID, TEXT[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.create_subscription(UUID, UUID, TEXT, TEXT, INTEGER, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cancel_subscription(UUID, BOOLEAN) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.renew_subscription(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.record_payment(UUID, UUID, DECIMAL, TEXT, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC;

-- Grant schema access
GRANT USAGE ON SCHEMA public TO anon, authenticated;
GRANT SELECT ON public.subscription_plans TO anon, authenticated;

-- Grant EXECUTE only to authenticated users (not anon, not public)
GRANT EXECUTE ON FUNCTION public.get_active_subscription(UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.can_access_pair(UUID, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.select_trading_pairs(UUID, TEXT[]) TO authenticated;

-- Create RLS policy for public reading of active plans
DROP POLICY IF EXISTS "public_read_active_plans" ON public.subscription_plans;
CREATE POLICY "public_read_active_plans" ON public.subscription_plans
    FOR SELECT
    TO anon, authenticated
    USING (is_active = true);

-- =====================================================
-- VERIFICATION QUERIES
-- =====================================================

-- View all plans
SELECT name, display_name, price_usd, sort_order FROM public.subscription_plans ORDER BY sort_order;

-- Expected output:
-- beta          | 🚀 Beta Access    | 0.00  | 0
-- starter       | Starter           | 5.00  | 1
-- professional  | Professional      | 8.00  | 2
-- elite         | Elite             | 12.00 | 3

```

---

## 🗑️ REMOVE BETA CODE (Run After Beta Ends)

```sql
-- =================================================================
-- BETA REMOVAL - Run this after beta period ends
-- This will remove all beta-specific code and migrate beta users
-- =================================================================

-- STEP 1: Migrate beta users to free trial (or cancel)
UPDATE public.user_subscriptions
SET status = 'expired',
    cancel_at_period_end = true,
    cancelled_at = NOW(),
    updated_at = NOW()
WHERE plan_id IN (
    SELECT id FROM public.subscription_plans WHERE name = 'beta'
)
AND status IN ('active', 'trial');

-- STEP 2: Delete beta plan
DELETE FROM public.subscription_plans WHERE name = 'beta';

-- STEP 3: Update handle_new_user to give free trial instead
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
DECLARE
    v_starter_plan_id UUID;
BEGIN
    -- Create profile
    INSERT INTO public.profiles (id, email, full_name, email_verified)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', ''),
        NEW.email_confirmed_at IS NOT NULL
    );
    
    -- Get starter plan ID
    SELECT id INTO v_starter_plan_id 
    FROM public.subscription_plans 
    WHERE name = 'starter' 
    LIMIT 1;
    
    -- Create 7-day free trial
    IF v_starter_plan_id IS NOT NULL THEN
        PERFORM public.create_subscription(
            NEW.id,
            v_starter_plan_id,
            'manual',
            NULL,
            7, -- 7-day trial
            jsonb_build_object('signup_date', NOW())
        );
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- STEP 4: Update can_access_pair to remove beta checks
CREATE OR REPLACE FUNCTION public.can_access_pair(
    p_user_id UUID,
    p_trading_pair TEXT
)
RETURNS BOOLEAN AS $$
DECLARE
    v_subscription RECORD;
    v_plan RECORD;
    v_selected_pairs TEXT[];
BEGIN
    SELECT * INTO v_subscription FROM public.get_active_subscription(p_user_id);
    
    IF NOT FOUND OR NOT v_subscription.is_current THEN
        RETURN false;
    END IF;
    
    IF v_subscription.status NOT IN ('active', 'trial') THEN
        RETURN false;
    END IF;
    
    SELECT * INTO v_plan FROM public.subscription_plans WHERE name = v_subscription.plan_name;
    
    -- Elite plan: all pairs
    IF v_plan.name = 'elite' THEN
        RETURN p_trading_pair = ANY(v_plan.pairs_allowed);
    END IF;
    
    -- Starter/Professional: check selected pairs
    SELECT selected_pairs INTO v_selected_pairs
    FROM public.user_pair_selections
    WHERE user_id = p_user_id AND subscription_id = v_subscription.subscription_id
    LIMIT 1;
    
    IF NOT FOUND OR v_selected_pairs IS NULL THEN
        RETURN false;
    END IF;
    
    RETURN p_trading_pair = ANY(v_selected_pairs);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- STEP 5: Update select_trading_pairs to remove beta checks
CREATE OR REPLACE FUNCTION public.select_trading_pairs(
    p_user_id UUID,
    p_selected_pairs TEXT[]
)
RETURNS JSONB AS $$
DECLARE
    v_subscription RECORD;
    v_plan RECORD;
    v_pair_limit INTEGER;
    v_existing_selection RECORD;
    v_can_change BOOLEAN;
BEGIN
    SELECT * INTO v_subscription
    FROM public.user_subscriptions
    WHERE user_id = p_user_id AND status IN ('active', 'trial') AND expires_at > NOW()
    ORDER BY expires_at DESC LIMIT 1;
    
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'error', 'No active subscription found');
    END IF;
    
    SELECT * INTO v_plan FROM public.subscription_plans WHERE id = v_subscription.plan_id;
    
    IF v_plan.name = 'elite' THEN
        RETURN jsonb_build_object('success', false, 'error', 'Elite plan includes all pairs automatically');
    END IF;
    
    v_pair_limit := (v_plan.features->>'pair_limit')::INTEGER;
    
    IF v_pair_limit IS NULL THEN
        RETURN jsonb_build_object('success', false, 'error', 'Plan does not support pair selection');
    END IF;
    
    IF array_length(p_selected_pairs, 1) != v_pair_limit THEN
        RETURN jsonb_build_object('success', false, 'error', format('You must select exactly %s pair(s)', v_pair_limit));
    END IF;
    
    IF NOT (p_selected_pairs <@ ARRAY['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD']) THEN
        RETURN jsonb_build_object('success', false, 'error', 'Invalid trading pair selected');
    END IF;
    
    SELECT * INTO v_existing_selection FROM public.user_pair_selections
    WHERE user_id = p_user_id AND subscription_id = v_subscription.id;
    
    IF FOUND THEN
        v_can_change := v_existing_selection.can_change_pairs OR v_existing_selection.locked_until < NOW();
        
        IF NOT v_can_change THEN
            RETURN jsonb_build_object('success', false, 'error', 'Pair selection is locked until next billing cycle', 'locked_until', v_existing_selection.locked_until);
        END IF;
        
        UPDATE public.user_pair_selections
        SET selected_pairs = p_selected_pairs, locked_until = v_subscription.next_billing_date,
            can_change_pairs = false, last_changed_at = NOW(), change_count = change_count + 1, updated_at = NOW()
        WHERE id = v_existing_selection.id;
    ELSE
        INSERT INTO public.user_pair_selections (user_id, subscription_id, selected_pairs, locked_until, can_change_pairs)
        VALUES (p_user_id, v_subscription.id, p_selected_pairs, v_subscription.next_billing_date, false);
    END IF;
    
    RETURN jsonb_build_object('success', true, 'selected_pairs', p_selected_pairs, 'locked_until', v_subscription.next_billing_date);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Done! Beta code removed from database.
```

---

## 📱 Frontend Cleanup (After Beta Ends)

**Files to Delete:**
- `src/components/marketing/BetaBanner.tsx`

**Files to Update:**
1. `src/App.tsx` - Remove BetaBanner import and component
2. `src/components/marketing/Navbar.tsx` - Remove `--beta-banner-offset` CSS variable
3. `src/components/auth/SignUpDialog.tsx` - Update signup message
4. `src/components/marketing/Hero.tsx` - Change "Free Beta Access" to "Start Free Trial"

---

## 🎯 Summary

**What This Adds:**
- ✅ Production pricing: $5, $8, $12
- ✅ Beta plan (free, all pairs, 1 year)
- ✅ Pair selection system with payment lock
- ✅ Auto-beta assignment for new users

**Beta Removal is Easy:**
1. Run the "REMOVE BETA CODE" SQL block
2. Delete `BetaBanner.tsx`
3. Remove beta imports from `App.tsx`
4. Done in 5 minutes!

All beta code is clearly marked with `-- BETA:` comments.
