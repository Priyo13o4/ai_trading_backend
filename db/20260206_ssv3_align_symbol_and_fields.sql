-- Align DB schema with Strategy Selector V3 prompts/spec.
-- Changes:
--  - strategies.trading_pair -> strategies.symbol
--  - add strategies.trade_recommended, strategies.summary, strategies.news_context
--  - allow expiry_minutes = 0 for non-executable strategies
--  - update helper functions that reference strategies.trading_pair

BEGIN;

-- 1) Rename strategies.trading_pair -> strategies.symbol (spec uses "symbol")
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'strategies'
          AND column_name = 'trading_pair'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'strategies'
          AND column_name = 'symbol'
    ) THEN
        ALTER TABLE public.strategies RENAME COLUMN trading_pair TO symbol;
    END IF;
END $$;

-- 2) Add missing prompt-level fields as first-class columns
ALTER TABLE public.strategies
    ADD COLUMN IF NOT EXISTS trade_recommended BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS news_context TEXT;

-- Make summary required going forward (only if safe). We keep it nullable for now to avoid breaking existing rows.
-- You can tighten later once backfill is done.

-- 3) Allow expiry_minutes = 0 (informational-only output)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'strategies_expiry_minutes_check'
          AND conrelid = 'public.strategies'::regclass
    ) THEN
        ALTER TABLE public.strategies DROP CONSTRAINT strategies_expiry_minutes_check;
    END IF;
END $$;

ALTER TABLE public.strategies
    ADD CONSTRAINT strategies_expiry_minutes_check
    CHECK (expiry_minutes = 0 OR (expiry_minutes >= 5 AND expiry_minutes <= 240));

-- 4) Update helper functions that previously referenced strategies.trading_pair
-- NOTE: these functions are optional in your app path, but are used by API and/or tools.

CREATE OR REPLACE FUNCTION public.get_active_strategies(pair TEXT)
RETURNS TABLE (
    strategy_id INT,
    symbol VARCHAR,
    strategy_name VARCHAR,
    direction VARCHAR,
    entry_signal JSONB,
    take_profit NUMERIC,
    stop_loss NUMERIC,
    risk_reward_ratio NUMERIC,
    confidence VARCHAR,
    expiry_time TIMESTAMPTZ,
    detailed_analysis TEXT,
    timestamp TIMESTAMPTZ,
    status VARCHAR,
    trade_mode VARCHAR,
    execution_allowed BOOLEAN,
    risk_level VARCHAR,
    trade_recommended BOOLEAN,
    summary TEXT,
    news_context TEXT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        s.strategy_id,
        s.symbol,
        s.strategy_name,
        s.direction,
        s.entry_signal,
        s.take_profit,
        s.stop_loss,
        s.risk_reward_ratio,
        s.confidence,
        s.expiry_time,
        s.detailed_analysis,
        s."timestamp",
        s.status,
        s.trade_mode,
        s.execution_allowed,
        s.risk_level,
        s.trade_recommended,
        s.summary,
        s.news_context
    FROM public.strategies s
    WHERE s.symbol = UPPER(pair)
      AND s.status = 'active'
      AND s.expiry_time > NOW()
    ORDER BY s.confidence DESC, s."timestamp" DESC;
$$;

-- get_pair_performance does not depend on strategies.symbol (signals still use trading_pair)

COMMIT;
