-- Restore anon execute compatibility for frontend RPC calls.
-- Function body still enforces auth via auth.uid().

GRANT EXECUTE ON FUNCTION public.request_account_deletion() TO anon;
GRANT EXECUTE ON FUNCTION public.verify_and_delete_account(text) TO anon;
