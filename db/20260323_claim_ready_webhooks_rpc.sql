-- Migration: Create claim_ready_webhooks RPC function
-- Date: 2026-03-23
-- Purpose: Atomic webhook claiming with SKIP LOCKED for safe multi-worker processing
--
-- Apply via Supabase SQL Editor (production direct per user decision)
-- IMPORTANT: Backup database before applying
-- IMPORTANT: Run 20260323_webhook_lease_columns.sql FIRST

-- Create RPC function for atomic webhook claiming
-- Uses SKIP LOCKED to prevent workers from blocking on each other
-- Uses lease semantics for crash recovery (stranded events can be reclaimed)
CREATE OR REPLACE FUNCTION public.claim_ready_webhooks(
    batch_size INTEGER DEFAULT 50,
    lease_seconds INTEGER DEFAULT 300
)
RETURNS SETOF public.webhook_events
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    WITH claimable AS (
        SELECT we.id
        FROM public.webhook_events we
        WHERE we.processed = FALSE
            AND we.next_retry_at <= NOW()
            AND (
                -- Not currently being processed
                we.processing = FALSE
                -- OR lease expired (crash recovery)
                OR we.processing_started_at IS NULL
                OR we.processing_started_at < NOW() - make_interval(secs => lease_seconds)
            )
        ORDER BY we.received_at ASC
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE public.webhook_events we
    SET processing = TRUE,
        processing_started_at = NOW()
    WHERE we.id IN (SELECT id FROM claimable)
    RETURNING we.*;
END;
$$;

-- CRITICAL SECURITY: Restrict RPC access to service_role only
-- This prevents external attackers from locking the queue
REVOKE ALL ON FUNCTION public.claim_ready_webhooks(INTEGER, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.claim_ready_webhooks(INTEGER, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.claim_ready_webhooks(INTEGER, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.claim_ready_webhooks(INTEGER, INTEGER) TO service_role;

-- Add comment for documentation
COMMENT ON FUNCTION public.claim_ready_webhooks IS
'Atomically claims a batch of webhook events for processing.
Uses SKIP LOCKED to prevent workers from blocking each other.
Supports lease-based crash recovery: events stuck in processing state
longer than lease_seconds are automatically reclaimed.

Args:
  batch_size: Max number of events to claim (default 50)
  lease_seconds: How long before stuck events can be reclaimed (default 300)

Returns:
  Set of claimed webhook_events rows

Security:
  SECURITY DEFINER with locked search_path
  Only executable by service_role';
