-- ============================================================================
-- TimescaleDB Continuous Aggregates for OHLCV (Derived Timeframes)
--
-- **IMPORTANT (v2.0 - DST Fix):**
-- - D1, W1, MN1 are NO LONGER aggregated here
-- - These timeframes are sourced directly from broker via MT5 EA
-- - Rationale: Forex session boundaries (DST, Sunday 22:00 UTC open) cannot be
--   expressed in fixed UTC aggregation without systematic drift
--
-- Goals:
-- - Aggregate M1 into fixed-duration timeframes: M5, M15, M30, H1, H4 ONLY
-- - Keep strict UTC bucket alignment for these timeframes
-- - Avoid exposing partial/open buckets: materialized_only=true + end_offset
--
-- Notes:
-- - Source data must be written only as timeframe='M1' into candlesticks
-- - D1/W1/MN1 written directly by MT5 EA (DST-aware, session-aligned)
-- - These caggs intentionally FILTER timeframe='M1'
-- - Created WITH NO DATA to avoid huge one-time aggregation during init
--   Populate via refresh_continuous_aggregate(...) as needed
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ------------------------------
-- M5
-- ------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS candlesticks_m5
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '5 minutes', time) AS time,
  first(open, time)  AS open,
  max(high)          AS high,
  min(low)           AS low,
  last(close, time)  AS close,
  sum(volume)        AS volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '5 minutes', time)
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_m5 SET (timescaledb.materialized_only = TRUE);
CREATE INDEX IF NOT EXISTS idx_candlesticks_m5_symbol_time ON candlesticks_m5 (symbol, time DESC);
SELECT add_continuous_aggregate_policy('candlesticks_m5',
  start_offset => INTERVAL '30 days',
  end_offset => INTERVAL '1 minute',
  schedule_interval => INTERVAL '1 minute',
  if_not_exists => TRUE
);

-- ------------------------------
-- M15
-- ------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS candlesticks_m15
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '15 minutes', time) AS time,
  first(open, time)  AS open,
  max(high)          AS high,
  min(low)           AS low,
  last(close, time)  AS close,
  sum(volume)        AS volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '15 minutes', time)
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_m15 SET (timescaledb.materialized_only = TRUE);
CREATE INDEX IF NOT EXISTS idx_candlesticks_m15_symbol_time ON candlesticks_m15 (symbol, time DESC);
SELECT add_continuous_aggregate_policy('candlesticks_m15',
  start_offset => INTERVAL '60 days',
  end_offset => INTERVAL '2 minutes',
  schedule_interval => INTERVAL '2 minutes',
  if_not_exists => TRUE
);

-- ------------------------------
-- M30
-- ------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS candlesticks_m30
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '30 minutes', time) AS time,
  first(open, time)  AS open,
  max(high)          AS high,
  min(low)           AS low,
  last(close, time)  AS close,
  sum(volume)        AS volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '30 minutes', time)
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_m30 SET (timescaledb.materialized_only = TRUE);
CREATE INDEX IF NOT EXISTS idx_candlesticks_m30_symbol_time ON candlesticks_m30 (symbol, time DESC);
SELECT add_continuous_aggregate_policy('candlesticks_m30',
  start_offset => INTERVAL '90 days',
  end_offset => INTERVAL '5 minutes',
  schedule_interval => INTERVAL '5 minutes',
  if_not_exists => TRUE
);

-- ------------------------------
-- H1
-- ------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS candlesticks_h1
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '1 hour', time) AS time,
  first(open, time)  AS open,
  max(high)          AS high,
  min(low)           AS low,
  last(close, time)  AS close,
  sum(volume)        AS volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '1 hour', time)
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_h1 SET (timescaledb.materialized_only = TRUE);
CREATE INDEX IF NOT EXISTS idx_candlesticks_h1_symbol_time ON candlesticks_h1 (symbol, time DESC);
SELECT add_continuous_aggregate_policy('candlesticks_h1',
  start_offset => INTERVAL '180 days',
  end_offset => INTERVAL '10 minutes',
  schedule_interval => INTERVAL '10 minutes',
  if_not_exists => TRUE
);

-- ------------------------------
-- H4
-- ------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS candlesticks_h4
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '4 hours', time) AS time,
  first(open, time)  AS open,
  max(high)          AS high,
  min(low)           AS low,
  last(close, time)  AS close,
  sum(volume)        AS volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '4 hours', time)
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_h4 SET (timescaledb.materialized_only = TRUE);
CREATE INDEX IF NOT EXISTS idx_candlesticks_h4_symbol_time ON candlesticks_h4 (symbol, time DESC);
SELECT add_continuous_aggregate_policy('candlesticks_h4',
  start_offset => INTERVAL '365 days',
  end_offset => INTERVAL '30 minutes',
  schedule_interval => INTERVAL '30 minutes',
  if_not_exists => TRUE
);

-- ============================================================================
-- D1, W1, MN1 REMOVED (v2.0)
-- ============================================================================
-- These timeframes are now sourced directly from broker via MT5 EA.
-- 
-- Why aggregation fails for D1/W1/MN1:
-- - Forex trading day starts Sunday ~22:00 UTC (shifts with DST)
-- - time_bucket() uses fixed UTC midnight origin → misaligns with broker sessions
-- - Weekly boundaries inherit daily misalignment → compounding error
-- - DST transitions cause silent 1-hour shift twice yearly
--
-- Broker-provided candles are the only correct source for these timeframes.
-- They already encode DST transitions, holidays, and session boundaries.
-- ============================================================================
