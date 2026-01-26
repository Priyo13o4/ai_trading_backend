-- ============================================================================
-- TimescaleDB Schema for AI Trading Bot
-- Database: Market Data Storage (OHLCV + Indicators)
-- Created: December 22, 2025
-- ============================================================================

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ============================================================================
-- TABLE: candlesticks (Raw OHLCV Data)
-- Purpose: Store all historical price data (3 years)
-- Size: ~750 MB for 3 years, 6 symbols, 5 timeframes
-- ============================================================================

CREATE TABLE IF NOT EXISTS candlesticks (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    open NUMERIC(18, 8) NOT NULL,
    high NUMERIC(18, 8) NOT NULL,
    low NUMERIC(18, 8) NOT NULL,
    close NUMERIC(18, 8) NOT NULL,
    volume BIGINT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Convert to hypertable (TimescaleDB magic for time-series optimization)
SELECT create_hypertable('candlesticks', 'time', 
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- Create composite unique index (prevents duplicates)
CREATE UNIQUE INDEX IF NOT EXISTS idx_candlesticks_unique 
    ON candlesticks (symbol, timeframe, time DESC);

-- Additional indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_candlesticks_symbol_time 
    ON candlesticks (symbol, time DESC);

CREATE INDEX IF NOT EXISTS idx_candlesticks_timeframe 
    ON candlesticks (timeframe, time DESC);

CREATE INDEX IF NOT EXISTS idx_candlesticks_symbol_timeframe 
    ON candlesticks (symbol, timeframe, time DESC);

-- ============================================================================
-- SYMBOL DISCOVERY (MT5 hot-add)
-- Purpose: deterministic first-ever symbol detection without polling.
-- Mechanism:
--   1) Maintain known_symbols(symbol PRIMARY KEY)
--   2) On INSERT into candlesticks, insert symbol into known_symbols (ON CONFLICT DO NOTHING)
--   3) If inserted, NOTIFY symbol_discovery with payload '<SYMBOL>'
-- Constraints:
--   - Trigger MUST NOT call HTTP.
--   - Trigger must fire on first-ever symbol regardless of timeframe.
-- ============================================================================

CREATE TABLE IF NOT EXISTS known_symbols (
    symbol VARCHAR(20) PRIMARY KEY,
    first_seen_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION notify_symbol_discovery() RETURNS trigger AS $$
BEGIN
    IF NEW.symbol IS NULL OR NEW.symbol = '' THEN
        RETURN NEW;
    END IF;

    INSERT INTO known_symbols(symbol) VALUES (NEW.symbol)
    ON CONFLICT (symbol) DO NOTHING;

    IF FOUND THEN
        PERFORM pg_notify('symbol_discovery', NEW.symbol);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_candlesticks_symbol_discovery ON candlesticks;
CREATE TRIGGER trg_candlesticks_symbol_discovery
AFTER INSERT ON candlesticks
FOR EACH ROW
EXECUTE FUNCTION notify_symbol_discovery();

-- Enable automatic compression (reduces storage by 90%+)
ALTER TABLE candlesticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, timeframe',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress data older than 7 days
SELECT add_compression_policy('candlesticks', INTERVAL '7 days', if_not_exists => TRUE);

-- Retention policy: Drop data older than 5 years
SELECT add_retention_policy('candlesticks', INTERVAL '5 years', if_not_exists => TRUE);

-- ============================================================================
-- TABLE: technical_indicators (Pre-calculated Indicators)
-- Purpose: Store indicators for RECENT data only (last 1000 bars per symbol/TF)
-- Size: ~15 MB for recent data
-- Strategy: Rolling window - delete old, keep new
-- ============================================================================

CREATE TABLE IF NOT EXISTS technical_indicators (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    
    -- EMAs (Trend indicators)
    ema_9 NUMERIC(18, 8),
    ema_21 NUMERIC(18, 8),
    ema_50 NUMERIC(18, 8),
    ema_100 NUMERIC(18, 8),
    ema_200 NUMERIC(18, 8),
    ema_momentum_slope NUMERIC(18, 8),
    
    -- Momentum indicators
    rsi NUMERIC(18, 8),
    macd_main NUMERIC(18, 8),
    macd_signal NUMERIC(18, 8),
    macd_histogram NUMERIC(18, 8),
    roc_percent NUMERIC(18, 8),
    
    -- Volatility indicators
    atr NUMERIC(18, 8),
    atr_percentile NUMERIC(18, 8),
    bb_upper NUMERIC(18, 8),
    bb_middle NUMERIC(18, 8),
    bb_lower NUMERIC(18, 8),
    bb_squeeze_ratio NUMERIC(18, 8),
    bb_width_percentile NUMERIC(18, 8),
    
    -- Directional indicators
    adx NUMERIC(18, 8),
    dmp NUMERIC(18, 8),
    dmn NUMERIC(18, 8),
    
    -- Volume (forex = 0)
    obv_slope NUMERIC(18, 8),
    
    -- Flexible storage for additional indicators (future-proof)
    indicators_json JSONB,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Convert to hypertable
SELECT create_hypertable('technical_indicators', 'time',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- Create composite unique index
CREATE UNIQUE INDEX IF NOT EXISTS idx_indicators_unique 
    ON technical_indicators (symbol, timeframe, time DESC);

-- Additional indexes
CREATE INDEX IF NOT EXISTS idx_indicators_symbol_time 
    ON technical_indicators (symbol, time DESC);

CREATE INDEX IF NOT EXISTS idx_indicators_timeframe 
    ON technical_indicators (timeframe, time DESC);

-- JSONB GIN index for flexible querying
CREATE INDEX IF NOT EXISTS idx_indicators_json 
    ON technical_indicators USING GIN (indicators_json);

-- Enable compression for indicators
ALTER TABLE technical_indicators SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, timeframe',
    timescaledb.compress_orderby = 'time DESC'
);

-- Compress data older than 3 days
SELECT add_compression_policy('technical_indicators', INTERVAL '3 days', if_not_exists => TRUE);

-- ============================================================================
-- TABLE: regime_classifications (AI/LLM Results)
-- Purpose: Store Gemini LLM regime classification results
-- Size: ~10 MB per year
-- ============================================================================

CREATE TABLE IF NOT EXISTS regime_classifications (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10),
    
    -- Classification results
    regime_type VARCHAR(50), -- "Trending Bullish", "Ranging", etc.
    confidence NUMERIC(5, 2), -- Percentage
    timeframe_agreement VARCHAR(20), -- "4/5 bullish"
    regime_strength INTEGER, -- 1-10 scale
    
    -- Evidence
    key_evidence JSONB, -- Array of supporting indicators
    conflicting_signals JSONB,
    
    -- Predictions
    expected_duration VARCHAR(50), -- "3-5 days"
    transition_signals JSONB,
    
    -- Trading implications
    strategy VARCHAR(100),
    risk_level VARCHAR(20),
    position_sizing VARCHAR(20),
    
    -- LLM metadata
    model_used VARCHAR(50), -- "gemini-2.0-flash-exp"
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    processing_time_ms INTEGER,
    
    -- Raw output
    raw_response JSONB
);

CREATE INDEX IF NOT EXISTS idx_regime_timestamp ON regime_classifications(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_regime_symbol ON regime_classifications(symbol);
CREATE INDEX IF NOT EXISTS idx_regime_type ON regime_classifications(regime_type);

-- ============================================================================
-- CONTINUOUS AGGREGATES (TimescaleDB Feature)
-- Purpose: Pre-computed aggregations for faster queries
-- ============================================================================

-- Daily OHLCV aggregation (from M5 data)
CREATE MATERIALIZED VIEW IF NOT EXISTS daily_candlesticks
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS day,
    symbol,
    FIRST(open, time) AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    LAST(close, time) AS close,
    SUM(volume) AS volume
FROM candlesticks
WHERE timeframe = 'M5'
GROUP BY day, symbol
WITH NO DATA;

-- Refresh policy: Update every hour
SELECT add_continuous_aggregate_policy('daily_candlesticks',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================
-- NOTE: Removed unused functions (calculate_data_completeness, update_data_metadata,
--       find_data_gaps, cleanup_old_indicators) in v2.0 cleanup

-- ============================================================================
-- UTILITY VIEWS
-- ============================================================================
-- NOTE: Removed unused views (data_freshness, data_coverage_summary, api_usage_last_24h)
--       in v2.0 cleanup. These relied on deleted tables (data_metadata, api_cache_log)

-- ============================================================================
-- GRANTS (Security)
-- ============================================================================

-- Grant permissions to api user (create this user in docker-compose)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO api_user;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO api_user;

-- ============================================================================
-- MAINTENANCE NOTES
-- ============================================================================
-- 1. Indicators are calculated post-backfill by scripts/calculate_recent_indicators_v2.py
-- 2. Compression runs automatically (TimescaleDB background jobs)
-- 3. Use scripts/validate_htf_migration.py to verify D1/W1/MN1 broker data
-- 4. CAGGs were removed in v2.0 (DST fix) - D1/W1/MN1 now from broker via MT5 EA
-- 5. Removed unused tables: market_structure, data_metadata, api_cache_log (Jan 2026)

-- ============================================================================
-- PERFORMANCE OPTIMIZATION NOTES
-- ============================================================================

-- 1. TimescaleDB automatically partitions data by time (chunks)
-- 2. Compression reduces storage by 90%+ for old data
-- 3. Indexes on (symbol, timeframe, time) enable fast queries
-- 4. JSONB allows flexible indicator storage without schema changes
-- 5. Continuous aggregates pre-compute common queries
-- 6. Retention policies auto-delete very old data (5+ years)

-- ============================================================================
-- END OF SCHEMA
-- ============================================================================
