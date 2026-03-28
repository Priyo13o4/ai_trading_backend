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
-- TABLE: market_structure (Swing Analysis, Pivot Points)
-- Purpose: Store complex market structure analysis results
-- Size: Minimal (~5 MB)
-- ============================================================================

CREATE TABLE IF NOT EXISTS market_structure (
    time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    
    -- Price ranges
    recent_high NUMERIC(18, 8),
    recent_low NUMERIC(18, 8),
    range_percent NUMERIC(18, 8),
    
    -- Swing analysis
    total_swing_highs INTEGER,
    total_swing_lows INTEGER,
    higher_highs INTEGER,
    lower_highs INTEGER,
    higher_lows INTEGER,
    lower_lows INTEGER,
    
    -- Pivot points (stored as JSONB for flexibility)
    pivot_points JSONB,
    
    -- Volume profile (limited for forex)
    volume_profile JSONB,
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Convert to hypertable
SELECT create_hypertable('market_structure', 'time',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- Unique index
CREATE UNIQUE INDEX IF NOT EXISTS idx_structure_unique 
    ON market_structure (symbol, timeframe, time DESC);

-- ============================================================================
-- TABLE: data_metadata (Track Data Completeness)
-- Purpose: Monitor data quality and coverage
-- Size: Minimal (~30 KB)
-- ============================================================================

CREATE TABLE IF NOT EXISTS data_metadata (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    earliest_timestamp TIMESTAMPTZ,
    latest_timestamp TIMESTAMPTZ,
    total_bars INTEGER,
    expected_bars INTEGER,
    data_completeness NUMERIC(5, 2), -- Percentage
    missing_bars INTEGER,
    last_gap_check TIMESTAMPTZ,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_metadata_symbol ON data_metadata(symbol);
CREATE INDEX IF NOT EXISTS idx_metadata_completeness ON data_metadata(data_completeness);

-- ============================================================================
-- TABLE: api_cache_log (Track API Call Usage)
-- Purpose: Monitor Twelve Data API usage for rate limiting
-- Size: ~1 MB per month
-- ============================================================================

CREATE TABLE IF NOT EXISTS api_cache_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    endpoint VARCHAR(100),
    symbol VARCHAR(20),
    timeframe VARCHAR(10),
    bars_requested INTEGER,
    api_calls_used INTEGER,
    response_time_ms INTEGER,
    success BOOLEAN,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_log_timestamp ON api_cache_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_api_log_symbol ON api_cache_log(symbol);

-- Retention: Keep only last 90 days of API logs
CREATE TABLE IF NOT EXISTS api_cache_log_cleanup_policy (
    last_cleanup TIMESTAMPTZ DEFAULT NOW()
);

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

-- Function to calculate data completeness percentage
CREATE OR REPLACE FUNCTION calculate_data_completeness(
    p_symbol VARCHAR,
    p_timeframe VARCHAR
) RETURNS NUMERIC AS $$
DECLARE
    v_total_bars INTEGER;
    v_expected_bars INTEGER;
    v_earliest TIMESTAMPTZ;
    v_latest TIMESTAMPTZ;
    v_time_diff INTERVAL;
BEGIN
    -- Get earliest and latest timestamps
    SELECT MIN(time), MAX(time), COUNT(*)
    INTO v_earliest, v_latest, v_total_bars
    FROM candlesticks
    WHERE symbol = p_symbol AND timeframe = p_timeframe;
    
    IF v_earliest IS NULL THEN
        RETURN 0;
    END IF;
    
    -- Calculate expected bars based on timeframe
    v_time_diff := v_latest - v_earliest;
    
    v_expected_bars := CASE p_timeframe
        WHEN 'M5' THEN EXTRACT(EPOCH FROM v_time_diff) / 300
        WHEN 'M15' THEN EXTRACT(EPOCH FROM v_time_diff) / 900
        WHEN 'H1' THEN EXTRACT(EPOCH FROM v_time_diff) / 3600
        WHEN 'H4' THEN EXTRACT(EPOCH FROM v_time_diff) / 14400
        WHEN 'D1' THEN EXTRACT(EPOCH FROM v_time_diff) / 86400
        ELSE 0
    END;
    
    -- Return percentage (accounting for weekends - multiply by 0.7)
    IF v_expected_bars > 0 THEN
        RETURN (v_total_bars::NUMERIC / (v_expected_bars * 0.7)) * 100;
    ELSE
        RETURN 0;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Function to update metadata after data insert
CREATE OR REPLACE FUNCTION update_data_metadata()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO data_metadata (
        symbol, timeframe, earliest_timestamp, latest_timestamp,
        total_bars, expected_bars, data_completeness, last_updated
    )
    SELECT
        NEW.symbol,
        NEW.timeframe,
        MIN(time),
        MAX(time),
        COUNT(*),
        0, -- Will be calculated by function
        calculate_data_completeness(NEW.symbol, NEW.timeframe),
        NOW()
    FROM candlesticks
    WHERE symbol = NEW.symbol AND timeframe = NEW.timeframe
    ON CONFLICT (symbol, timeframe) DO UPDATE SET
        earliest_timestamp = EXCLUDED.earliest_timestamp,
        latest_timestamp = EXCLUDED.latest_timestamp,
        total_bars = EXCLUDED.total_bars,
        data_completeness = EXCLUDED.data_completeness,
        last_updated = NOW();
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to auto-update metadata
CREATE TRIGGER trigger_update_metadata
AFTER INSERT OR UPDATE ON candlesticks
FOR EACH ROW
EXECUTE FUNCTION update_data_metadata();

-- ============================================================================
-- UTILITY VIEWS
-- ============================================================================

-- View: Latest data freshness per symbol/timeframe
CREATE OR REPLACE VIEW data_freshness AS
SELECT
    symbol,
    timeframe,
    MAX(time) AS latest_timestamp,
    EXTRACT(EPOCH FROM (NOW() - MAX(time))) / 60 AS minutes_old,
    COUNT(*) AS total_bars
FROM candlesticks
GROUP BY symbol, timeframe
ORDER BY symbol, 
    CASE timeframe
        WHEN 'M5' THEN 1
        WHEN 'M15' THEN 2
        WHEN 'H1' THEN 3
        WHEN 'H4' THEN 4
        WHEN 'D1' THEN 5
    END;

-- View: Data coverage summary
CREATE OR REPLACE VIEW data_coverage_summary AS
SELECT
    symbol,
    COUNT(DISTINCT timeframe) AS timeframes_count,
    SUM(total_bars) AS total_bars,
    AVG(data_completeness) AS avg_completeness,
    MIN(earliest_timestamp) AS oldest_data,
    MAX(latest_timestamp) AS newest_data
FROM data_metadata
GROUP BY symbol
ORDER BY symbol;

-- View: API usage statistics (last 24 hours)
CREATE OR REPLACE VIEW api_usage_last_24h AS
SELECT
    DATE_TRUNC('hour', timestamp) AS hour,
    COUNT(*) AS total_calls,
    SUM(api_calls_used) AS api_credits_used,
    AVG(response_time_ms) AS avg_response_time,
    SUM(CASE WHEN success THEN 1 ELSE 0 END) AS successful_calls,
    SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) AS failed_calls
FROM api_cache_log
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY hour
ORDER BY hour DESC;

-- ============================================================================
-- GRANTS (Security)
-- ============================================================================

-- Grant permissions to api user (create this user in docker-compose)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO api_user;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO api_user;

-- ============================================================================
-- INITIAL DATA QUALITY CHECK
-- ============================================================================

-- After backfill, run this to populate metadata
-- SELECT update_data_metadata() FROM candlesticks LIMIT 1;

-- ============================================================================
-- MAINTENANCE QUERIES
-- ============================================================================

-- Query to find data gaps
CREATE OR REPLACE FUNCTION find_data_gaps(
    p_symbol VARCHAR,
    p_timeframe VARCHAR,
    p_gap_threshold_hours INTEGER DEFAULT 4
) RETURNS TABLE (
    gap_start TIMESTAMPTZ,
    gap_end TIMESTAMPTZ,
    gap_duration_hours NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        time AS gap_start,
        LEAD(time) OVER (ORDER BY time) AS gap_end,
        EXTRACT(EPOCH FROM (LEAD(time) OVER (ORDER BY time) - time)) / 3600 AS gap_duration_hours
    FROM candlesticks
    WHERE symbol = p_symbol AND timeframe = p_timeframe
    HAVING EXTRACT(EPOCH FROM (LEAD(time) OVER (ORDER BY time) - time)) / 3600 > p_gap_threshold_hours;
END;
$$ LANGUAGE plpgsql;

-- Clean up old indicator data (keep only last 1000 bars)
CREATE OR REPLACE FUNCTION cleanup_old_indicators() RETURNS void AS $$
DECLARE
    v_symbol VARCHAR;
    v_timeframe VARCHAR;
    v_cutoff_time TIMESTAMPTZ;
BEGIN
    FOR v_symbol, v_timeframe IN
        SELECT DISTINCT symbol, timeframe FROM technical_indicators
    LOOP
        -- Get the 1000th most recent timestamp
        SELECT time INTO v_cutoff_time
        FROM technical_indicators
        WHERE symbol = v_symbol AND timeframe = v_timeframe
        ORDER BY time DESC
        OFFSET 1000 LIMIT 1;
        
        -- Delete older data
        IF v_cutoff_time IS NOT NULL THEN
            DELETE FROM technical_indicators
            WHERE symbol = v_symbol 
                AND timeframe = v_timeframe 
                AND time < v_cutoff_time;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Schedule cleanup (run daily)
-- Add to cron or n8n workflow: SELECT cleanup_old_indicators();

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
