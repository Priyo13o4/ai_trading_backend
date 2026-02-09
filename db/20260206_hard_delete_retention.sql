-- Hard delete retention + remove soft-delete columns
-- Date: 2026-02-06
-- DB: ai_trading_bot_data

BEGIN;

-- 1) Drop soft-delete helper functions (they reference archived columns)
DROP FUNCTION IF EXISTS public.export_archived_data(text);
DROP FUNCTION IF EXISTS public.purge_archived_records();

-- 2) Remove soft-delete columns from strategies/signals
ALTER TABLE IF EXISTS public.strategies
  DROP COLUMN IF EXISTS archived_at,
  DROP COLUMN IF EXISTS archived;

ALTER TABLE IF EXISTS public.signals
  DROP COLUMN IF EXISTS archived_at,
  DROP COLUMN IF EXISTS archived;

-- 3) Drop indexes that depended on archived columns (if present)
DROP INDEX IF EXISTS public.idx_strategies_archived;
DROP INDEX IF EXISTS public.idx_signals_archived;

-- 4) Retention purge proc for 6-month hard delete
-- TimescaleDB background jobs call procedures with (job_id int, config jsonb)
CREATE OR REPLACE PROCEDURE public.purge_old_records(job_id INT, config JSONB)
LANGUAGE plpgsql
AS $$
DECLARE
  v_strategy_count INTEGER;
  v_signal_count INTEGER;
BEGIN
  -- Delete strategies older than 6 months which are no longer active
  DELETE FROM public.strategies
  WHERE created_at < NOW() - INTERVAL '6 months'
    AND (status IS DISTINCT FROM 'active' OR expiry_time < NOW());
  GET DIAGNOSTICS v_strategy_count = ROW_COUNT;

  -- Delete signals older than 6 months which are not open
  DELETE FROM public.signals
  WHERE entry_time < NOW() - INTERVAL '6 months'
    AND status IS DISTINCT FROM 'open';
  GET DIAGNOSTICS v_signal_count = ROW_COUNT;

  RAISE NOTICE 'purge_old_records: strategies=% signals=%', v_strategy_count, v_signal_count;
END;
$$;

-- 5) Ensure a daily scheduled job exists (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM timescaledb_information.jobs j
    WHERE j.proc_name = 'purge_old_records'
  ) THEN
    PERFORM public.add_job(
      'purge_old_records'::regproc,
      INTERVAL '1 day',
      '{}'::jsonb,
      NOW() + INTERVAL '10 minutes',
      true,
      NULL,
      false,
      'UTC',
      'purge_old_records_daily'
    );
  END IF;
END;
$$;

COMMIT;
