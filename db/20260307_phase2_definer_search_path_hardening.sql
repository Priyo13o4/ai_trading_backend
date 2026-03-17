-- Phase 2 security hardening: ensure remaining SECURITY DEFINER routines
-- use a fixed, safe search_path.
-- Date: 2026-03-07

DO $$
BEGIN
    IF to_regprocedure('public.expire_subscriptions()') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.expire_subscriptions() SET search_path = pg_catalog, public';
    END IF;

    IF to_regprocedure('public.get_expiring_subscriptions(integer)') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.get_expiring_subscriptions(INTEGER) SET search_path = pg_catalog, public';
    END IF;

    IF to_regprocedure('public.run_expire_subscriptions()') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.run_expire_subscriptions() SET search_path = pg_catalog, public';
    END IF;

    IF to_regprocedure('public.handle_new_user()') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.handle_new_user() SET search_path = pg_catalog, public';
    END IF;

    IF to_regprocedure('public.verify_and_delete_account(text)') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.verify_and_delete_account(TEXT) SET search_path = pg_catalog, public';
    END IF;

    -- Additional remaining definer routines present in live schema.
    IF to_regprocedure('public.can_access_pair(uuid,text)') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.can_access_pair(UUID, TEXT) SET search_path = pg_catalog, public';
    END IF;

    IF to_regprocedure('public.select_trading_pairs(uuid,text[])') IS NOT NULL THEN
        EXECUTE 'ALTER FUNCTION public.select_trading_pairs(UUID, TEXT[]) SET search_path = pg_catalog, public';
    END IF;
END
$$;
