-- ============================================================================
-- TimescaleDB Compression & Retention Policies
-- Applied: 2026-01-28
-- Purpose: Add compression to regime_data + retention policies for all tables
-- ============================================================================

-- ============================================================================
-- REGIME_DATA: Convert to Hypertable + Compression + Hard Delete (2 years)
-- ============================================================================

-- Step 1: Drop existing primary key (regime_id only)
ALTER TABLE regime_data DROP CONSTRAINT regime_data_pkey;

-- Step 2: Create composite primary key (regime_id + created_at)
-- Required for TimescaleDB hypertable partitioning
ALTER TABLE regime_data ADD PRIMARY KEY (regime_id, created_at);

-- Step 3: Convert regime_data to a TimescaleDB hypertable
SELECT create_hypertable(
    'regime_data',
    'created_at',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- Step 4: Enable compression on regime_data
ALTER TABLE regime_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'trading_pair, regime_type',
    timescaledb.compress_orderby = 'created_at DESC'
);

-- Step 5: Compress data older than 30 days (regime lookups are infrequent)
SELECT add_compression_policy('regime_data', INTERVAL '30 days', if_not_exists => TRUE);

-- Step 6: Hard delete data older than 2 years
SELECT add_retention_policy('regime_data', INTERVAL '2 years', if_not_exists => TRUE);

-- ============================================================================
-- TECHNICAL_INDICATORS: Hard Delete Retention (1 month)
-- ============================================================================
-- Currently: 69,016 rows (~1000 per symbol/timeframe)
-- Oldest data: MN1/W1 go back to 2020, D1 to 2022, intraday to recent months
-- Compression already enabled (3-day threshold)
-- ============================================================================

-- Hard delete technical indicators older than 1 month
-- Rationale: indicators are derived from candlesticks; keep only a short cache window
SELECT add_retention_policy('technical_indicators', INTERVAL '1 month', if_not_exists => TRUE);

-- ============================================================================
-- CANDLESTICKS: Already has 5-year retention (no change needed)
-- ============================================================================
-- Current policy: add_retention_policy('candlesticks', INTERVAL '5 years')
-- Compression: 7-day threshold
-- ============================================================================

-- ============================================================================
-- REMOVE SOFT-DELETE LOGIC: archive_old_records() function
-- ============================================================================
-- Since we're using TimescaleDB hard retention policies now,
-- the archive_old_records() function is redundant.
-- Drop it to avoid confusion.
-- ============================================================================

DROP FUNCTION IF EXISTS archive_old_records() CASCADE;

-- Also remove archived/archived_at columns from regime_data (no longer needed)
ALTER TABLE regime_data DROP COLUMN IF EXISTS archived CASCADE;
ALTER TABLE regime_data DROP COLUMN IF EXISTS archived_at CASCADE;

-- ============================================================================
-- VERIFICATION QUERIES (run after migration)
-- ============================================================================

-- Check compression policies:
-- SELECT * FROM timescaledb_information.compression_settings;

-- Check retention policies:
-- SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention';

-- Check current data ranges:
-- SELECT 
--     'regime_data' as table_name,
--     COUNT(*) as records,
--     MIN(created_at) as oldest,
--     MAX(created_at) as newest
-- FROM regime_data;

-- SELECT 
--     'technical_indicators' as table_name,
--     COUNT(*) as records,
--     MIN(time) as oldest,
--     MAX(time) as newest
-- FROM technical_indicators;
