-- Migration: Add lease/retry columns to webhook_events for worker claiming
-- Date: 2026-03-23
-- Purpose: Enable claim_ready_webhooks RPC with SKIP LOCKED and lease reclaim
--
-- Apply via Supabase SQL Editor (production direct per user decision)
-- IMPORTANT: Backup database before applying

-- Add lease and retry tracking columns to webhook_events
ALTER TABLE public.webhook_events
    ADD COLUMN IF NOT EXISTS processing BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS last_error TEXT;

-- Create index for efficient claim queries
-- This index supports the claim_ready_webhooks RPC's WHERE clause
CREATE INDEX IF NOT EXISTS idx_webhook_events_ready_queue
    ON public.webhook_events (processed, processing, next_retry_at, received_at)
    WHERE processed = FALSE;

-- Add comment for documentation
COMMENT ON COLUMN public.webhook_events.processing IS 'True when a worker has claimed this event for processing';
COMMENT ON COLUMN public.webhook_events.processing_started_at IS 'Timestamp when processing started - used for lease expiry';
COMMENT ON COLUMN public.webhook_events.retry_count IS 'Number of processing attempts - dead-letter after 5';
COMMENT ON COLUMN public.webhook_events.next_retry_at IS 'When this event can be retried (exponential backoff)';
COMMENT ON COLUMN public.webhook_events.last_error IS 'Error message from last failed processing attempt';
