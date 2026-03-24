-- Migration: Create partial unique index for checkout double-click protection
-- Date: 2026-03-23
-- Purpose: Prevent users from having multiple active checkouts (double-charge protection)
--
-- Apply via Supabase SQL Editor (production direct per user decision)
-- IMPORTANT: Backup database before applying

-- Create partial unique index to enforce one active checkout per user
-- This prevents the scenario where a user clicks "pay" multiple times
-- and creates multiple pending payment transactions
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_payment_per_user
    ON public.payment_transactions (user_id)
    WHERE status IN ('pending', 'processing');

-- Add comment for documentation
COMMENT ON INDEX public.uq_active_payment_per_user IS
'Partial unique index that enforces at most one active checkout per user.
Prevents double-click scenarios where multiple PENDING transactions could
lead to duplicate charges. Covers statuses: pending, processing.
Does not affect completed/failed/cancelled transactions.';
