-- Metadata table for per-(symbol,timeframe) indicator update watermarks.
-- Used by the worker indicator updater to run incremental, bounded cycles.

CREATE TABLE IF NOT EXISTS indicator_update_watermarks (
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    watermark_time TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_indicator_update_watermarks_updated_at
    ON indicator_update_watermarks (updated_at DESC);
