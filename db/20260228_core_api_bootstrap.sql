-- Core API bootstrap (idempotent) for fresh database initialization.
-- Safe to run on upgraded databases: uses IF NOT EXISTS / CREATE OR REPLACE only.

BEGIN;

CREATE TABLE IF NOT EXISTS public.strategies (
    strategy_id BIGSERIAL PRIMARY KEY,
    strategy_name VARCHAR(255),
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(16),
    confidence NUMERIC(5, 2),
    take_profit NUMERIC(18, 8),
    stop_loss NUMERIC(18, 8),
    risk_reward_ratio NUMERIC(12, 6),
    expiry_minutes INTEGER,
    expiry_time TIMESTAMPTZ,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detailed_analysis TEXT,
    entry_signal JSONB,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    trade_mode VARCHAR(32),
    execution_allowed BOOLEAN,
    risk_level VARCHAR(32),
    trade_recommended BOOLEAN NOT NULL DEFAULT true,
    summary TEXT,
    news_context TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT strategies_expiry_minutes_check
      CHECK (expiry_minutes IS NULL OR expiry_minutes = 0 OR (expiry_minutes >= 5 AND expiry_minutes <= 240))
);

CREATE INDEX IF NOT EXISTS idx_strategies_symbol_timestamp
    ON public.strategies (symbol, "timestamp" DESC);

CREATE INDEX IF NOT EXISTS idx_strategies_status_expiry
    ON public.strategies (status, expiry_time DESC);

CREATE TABLE IF NOT EXISTS public.email_news_analysis (
    email_id BIGSERIAL PRIMARY KEY,
    forexfactory_content_id TEXT,
    headline TEXT,
    original_email_content TEXT,
    ai_analysis_summary TEXT,
    forex_relevant BOOLEAN NOT NULL DEFAULT false,
    forex_instruments TEXT[] DEFAULT ARRAY[]::TEXT[],
    primary_instrument TEXT,
    us_political_related BOOLEAN NOT NULL DEFAULT false,
    forexfactory_category TEXT,
    trade_deal_related BOOLEAN NOT NULL DEFAULT false,
    central_bank_related BOOLEAN NOT NULL DEFAULT false,
    importance_score INTEGER,
    sentiment_score NUMERIC(8, 4),
    analysis_confidence NUMERIC(8, 4),
    news_category TEXT,
    entities_mentioned TEXT[] DEFAULT ARRAY[]::TEXT[],
    trading_sessions TEXT[] DEFAULT ARRAY[]::TEXT[],
    market_impact_prediction TEXT,
    impact_timeframe TEXT,
    volatility_expectation TEXT,
    similar_news_context TEXT,
    similar_news_ids TEXT[] DEFAULT ARRAY[]::TEXT[],
    human_takeaway TEXT,
    attention_score INTEGER,
    news_state TEXT,
    market_pressure TEXT,
    attention_window TEXT,
    confidence_label TEXT,
    expected_followups TEXT[] DEFAULT ARRAY[]::TEXT[],
    email_received_at TIMESTAMPTZ,
    forexfactory_urls TEXT[] DEFAULT ARRAY[]::TEXT[],
    breaking_news BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_news_received_at_desc
    ON public.email_news_analysis (email_received_at DESC);

CREATE INDEX IF NOT EXISTS idx_email_news_forex_filters
    ON public.email_news_analysis (forex_relevant, importance_score, email_received_at DESC);

CREATE INDEX IF NOT EXISTS idx_email_news_primary_instrument
    ON public.email_news_analysis (primary_instrument);

CREATE INDEX IF NOT EXISTS idx_email_news_forex_instruments_gin
    ON public.email_news_analysis USING GIN (forex_instruments);

CREATE INDEX IF NOT EXISTS idx_email_news_forexfactory_content_id
    ON public.email_news_analysis (forexfactory_content_id);

CREATE OR REPLACE FUNCTION public.get_active_strategies(pair TEXT)
RETURNS TABLE (
    strategy_id BIGINT,
    symbol VARCHAR,
    strategy_name VARCHAR,
    direction VARCHAR,
    entry_signal JSONB,
    take_profit NUMERIC,
    stop_loss NUMERIC,
    risk_reward_ratio NUMERIC,
    confidence NUMERIC,
    expiry_time TIMESTAMPTZ,
    detailed_analysis TEXT,
    strategy_timestamp TIMESTAMPTZ,
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
        s."timestamp" AS strategy_timestamp,
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

COMMIT;
