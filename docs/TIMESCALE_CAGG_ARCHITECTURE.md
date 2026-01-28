# TimescaleDB Continuous Aggregates Architecture
## Post-Migration Summary (Jan 2026)

---

## Quick Reference (Read This First)

### What we store where (hybrid truth model)

- `candlesticks` hypertable stores **broker truth**:
  - `M1` (source for derived timeframes)
  - `D1/W1/MN1` (broker-provided, DST/session aligned)
- Timescale continuous aggregates store **derived truth** (fixed UTC buckets):
  - `candlesticks_m5`, `candlesticks_m15`, `candlesticks_m30`, `candlesticks_h1`, `candlesticks_h4`

**Rule:** The API must never read derived TFs from `candlesticks`, and must never read D1/W1/MN1 from CAGGs.

### End-to-end flow

1. **Ingest**
  - MT5 EA writes `M1/D1/W1/MN1` into `candlesticks`.
  - Tick importer writes **M1 only** into `candlesticks` (derived TFs come from CAGGs).
2. **Materialize derived TFs**
  - CAGGs compute M5–H4 from `candlesticks WHERE timeframe='M1'` and store results in internal Timescale hypertables.
3. **Serve API**
  - `GET /api/historical/{symbol}/{tf}`:
    - If `tf` is `M5..H4` → query CAGG view (requires `USE_TIMESCALE_CAGGS=true`).
    - If `tf` is `M1/D1/W1/MN1` → query `candlesticks`.
4. **Indicators**
  - Indicators are joined by `(symbol, timeframe, time)`; for derived TFs, `timeframe` is supplied as a parameter because CAGGs don’t have a `timeframe` column.

### Why `WITH NO DATA` and why manual refresh exists

- `WITH NO DATA` means: **create the CAGG definition now, do not backfill history automatically**.
- Policies keep a *rolling window* up-to-date (e.g., last 30/60/90/180/365 days).
- Manual refresh is used to:
  - backfill older history outside the policy window, or
  - populate a newly-created CAGG when historical M1 already exists.

### Operational gotcha (existing volumes)

⚠️ SQL files mounted into `docker-entrypoint-initdb.d/` only run on first init of an empty PGDATA volume. If you reused `./volumes/pgdata` and CAGGs are missing, apply `ai_trading_bot/db/continuous_aggregates.sql` manually, then refresh.

---

## Day-to-day ops (most common)

- After importing/backfilling M1 history, backfill CAGGs for the same range:
  - `CALL refresh_continuous_aggregate('candlesticks_m5',  '2025-01-01', '2026-01-01');`
  - Repeat for `candlesticks_m15/m30/h1/h4`.
- After that, Timescale policies keep the *recent rolling window* current automatically.

## Verify it’s working

- CAGGs exist + are continuous:
  - `SELECT view_name, materialization_hypertable_name FROM timescaledb_information.continuous_aggregates;`
  - `SELECT application_name, schedule_interval, last_run_status FROM timescaledb_information.job_stats JOIN timescaledb_information.jobs USING (job_id) WHERE application_name LIKE 'Refresh Continuous Aggregate Policy%';`
- Check for bad timestamps (example bounds):
  - `SELECT symbol, timeframe, COUNT(*) FROM candlesticks WHERE time < '2000-01-01' OR time > (NOW() + INTERVAL '1 day') GROUP BY 1,2;`

## Notes on compression

- Base hypertables like `candlesticks` and `technical_indicators` can be compressed (your DB has compression enabled and a compression policy job).
- CAGG materializations are stored in internal hypertables; compressing those is optional/advanced.
  - If needed later, we can enable compression on the `_timescaledb_internal._materialized_hypertable_*` tables and add policies.

---

<!-- Archived: older verbose notes kept for reference.

(Hidden to keep this doc short and scannable. If you need it, remove the HTML comment markers.)


<details>
<summary>Detailed background (optional)</summary>

## Overview

We migrated from **Python-side candle aggregation** (storing all TFs in one table) to **TimescaleDB continuous aggregates** to eliminate CPU spikes for fixed-duration timeframes.

---

## Data Storage Architecture

### Before (Legacy - Removed)
```
candlesticks table
├── M1 rows (timeframe='M1')
├── M5 rows (timeframe='M5')  ❌ DELETED
├── M15 rows                   ❌ DELETED
├── W1 rows (Monday-based)     ❌ DELETED (wrong alignment)
└── ...all TFs mixed
```

### Now (Current - Hybrid Truth Model)
```
candlesticks hypertable
├── M1 (timeframe='M1')       ← source of truth for derived CAGGs
└── D1/W1/MN1 (timeframe in {'D1','W1','MN1'}) ← source of truth (broker-provided)

candlesticks_m5  ← MATERIALIZED VIEW (continuous aggregate)
candlesticks_m15 ← MATERIALIZED VIEW
candlesticks_m30 ← MATERIALIZED VIEW
candlesticks_h1  ← MATERIALIZED VIEW
candlesticks_h4  ← MATERIALIZED VIEW

NOTE:
- D1/W1/MN1 continuous aggregates were intentionally removed due to DST/session alignment.
- D1/W1/MN1 must never be queried from CAGGs.
```

---

## How Continuous Aggregates Work

### 1. Definition (SQL)
Located in: `ai_trading_bot/db/continuous_aggregates.sql`

Each cagg is defined as:
```sql
CREATE MATERIALIZED VIEW candlesticks_h1
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '1 hour', time, 'UTC') AS time,
  first(open, time)  AS open,   -- First open in bucket
  max(high)          AS high,   -- Max high
  min(low)           AS low,    -- Min low
  last(close, time)  AS close,  -- Last close
  sum(volume)        AS volume  -- Total volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '1 hour', time, 'UTC')
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_h1 SET (timescaledb.materialized_only = TRUE);
```

**Key points:**
- Source: `candlesticks WHERE timeframe='M1'`
- Output: **No `timeframe` column** (it's implicit: `candlesticks_h1` = H1 data)
- Bucket alignment: `time_bucket()` ensures correct UTC boundaries
- W1 special: Uses explicit origin `2000-01-02 22:00:00+00` (Sunday 22:00 UTC) for forex trading week

### 2. Materialization Policies
```sql
SELECT add_continuous_aggregate_policy('candlesticks_h1',
  start_offset => INTERVAL '5 years',
  end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour',
  if_not_exists => TRUE
);
```

**What this does:**
- Background job runs every `schedule_interval` (1 hour for H1)
- Materializes buckets in window: `[now - 5 years, now - 1 hour]`
- As new M1s arrive, Timescale auto-materializes new buckets
- **Zero Python aggregation code runs**

### 3. Manual Refresh (when needed)

⚠️ **Important (existing volumes):** The SQL files mounted into `docker-entrypoint-initdb.d/` only run on the *first* initialization of an empty PGDATA volume. If you reused an existing `./volumes/pgdata` (or restored a DB dump) and the `candlesticks_m5/m15/m30/h1/h4` relations don’t exist, apply `ai_trading_bot/db/continuous_aggregates.sql` manually, then refresh.

```sql
-- Full refresh (all history)
CALL refresh_continuous_aggregate('candlesticks_h1', NULL, NULL);

-- Partial refresh (specific range)
CALL refresh_continuous_aggregate('candlesticks_h1', '2025-01-01', '2026-01-01');
```

We ran full refreshes once after migration. After that, policies keep views up-to-date automatically.

---

## Backend (API) Changes

### Code Location
- `api/app/routes/historical.py` (historical candle endpoint)
- `api/app/db.py` (regime data queries)
- `api-worker/scripts/calculate_recent_indicators_v2.py` (indicator calculation)

### Query Pattern (ALREADY IMPLEMENTED ✅)

```python
def _use_timescale_caggs() -> bool:
   return os.getenv("USE_TIMESCALE_CAGGS") == "true"

def _cagg_relation_for_timeframe(timeframe: str) -> str:
   tf = timeframe.upper()
   if tf == "M1":
      return "candlesticks"
   mapping = {
      "M5": "candlesticks_m5",
      "M15": "candlesticks_m15",
      # ... etc
   }
   return mapping[tf]

# In query builder:
if _use_timescale_caggs() and timeframe != "M1":
   rel = _cagg_relation_for_timeframe(timeframe)
   # Query: SELECT * FROM candlesticks_h1 WHERE symbol = 'XAUUSD'
   # NO timeframe column in WHERE or JOIN
else:
   # Query: SELECT * FROM candlesticks WHERE symbol='X' AND timeframe='H1'
```

**Critical difference:**
- **Old:** `WHERE timeframe = 'H1'`
- **New:** Query `candlesticks_h1` directly (no `timeframe` column)

---

## Frontend Changes

### Current State: ✅ NO CHANGES NEEDED

Frontend calls:
```typescript
apiService.getHistoricalData('XAUUSD', 'H1', 1000)
// → GET /api/historical/XAUUSD/H1?limit=1000
```

Backend (`historical.py`) **already handles** the routing:
- If `USE_TIMESCALE_CAGGS=true` and timeframe != M1 → queries cagg view
- Otherwise → queries `candlesticks` table

**Result:** Frontend is **already compatible** (no code changes required).

---

## Caching Layer

### Current State: ✅ COMPATIBLE

Cache key pattern:
```python
candles_key(symbol, timeframe) → "candles:XAUUSD:H1"
```

**This still works** because:
1. Cache keys are **logical** (symbol + timeframe)
2. Backend abstracts storage (cagg vs table)
3. Cache doesn't know/care about the DB schema

### What we did:
- Cleared all `candles:*` keys after migration (fresh start)
- Cleared `forming:bucket:*` (Redis forming-state buckets)

### Going forward:
- Cache invalidation (on new M1 writes) works as before
- SSE candle updates publish with `{symbol, timeframe, ...}` (unchanged)

---

## Forming Candles (Live Updates)

### Before (Legacy - CPU Spike Issue)
```
On each forming M1 tick:
1. Query DB: SELECT * FROM candlesticks WHERE timeframe='M1' AND time >= bucket_start
2. Aggregate in Python
3. Publish via Redis/SSE
→ Heavy DB reads every second
```

### Now (Redis State - Zero DB Reads)
```
On closed M1 bar:
1. Update Redis bucket state (incremental OHLCV per TF)
  Key: "forming:bucket:XAUUSD:H1:1706025600"
  Hash: {open, high, low, close, volume, last_ts}

On forming M1 tick:
1. Read Redis bucket state (HGETALL)
2. Overlay forming M1 (update high/low/close/volume)
3. Publish via Redis/SSE
→ Zero DB queries
```

**Location:** `api/app/mt5_ingest.py`
- `_update_forming_state_from_closed_m1()` maintains Redis state
- `_compute_forming_candle_sync()` uses Redis state (not DB)

---

## Technical Indicators

### Storage
- Table: `technical_indicators`
- Columns: `symbol, timeframe, time, ema_9, rsi, macd_main, ...`
- Still has `timeframe` column (unchanged)

### Calculation
Script: `api-worker/scripts/calculate_recent_indicators_v2.py`

**Already updated** to:
1. Query correct relation for candles:
  ```python
  relation = _ohlcv_relation_for_timeframe('H1')  # → candlesticks_h1
  ```
2. Check latest candle time from correct view
3. Compute indicators on cagg data
4. Write to `technical_indicators` with `timeframe` column

### What we need to do (cleanup):
- Delete legacy Monday-based W1 indicator rows
- Recompute all indicators for all symbols/TFs to align with new cagg buckets

---

## Cleanup Tasks (TODO)

### 1. Delete Legacy W1 Indicators (Monday-based)
```sql
-- Find misaligned indicators (time not in cagg buckets)
DELETE FROM technical_indicators ti
WHERE ti.timeframe = 'W1'
  AND NOT EXISTS (
   SELECT 1 FROM candlesticks c
   WHERE c.symbol = ti.symbol AND c.time = ti.time AND c.timeframe = 'W1'
  );
```

### 2. Recompute All Indicators
```bash
docke...
```

### 3. Verify Alignment
```sql
-- Check for any orphaned indicators (candles exist but indicators missing)
SELECT c.symbol, COUNT(*) as missing_indicators
FROM candlesticks_h1 c
LEFT JOIN technical_indicators ti ON c.symbol=ti.symbol AND c.time=ti.time AND ti.timeframe='H1'
WHERE ti.time IS NULL
GROUP BY c.symbol;
```

---

## Verification Queries

### Check Row Counts
```sql
-- M1 source
SELECT COUNT(*) FROM candlesticks WHERE symbol='XAUUSD' AND timeframe='M1';

-- Aggregated TFs
SELECT COUNT(*) FROM candlesticks_h1 WHERE symbol='XAUUSD';

-- Broker-provided HTFs
SELECT COUNT(*) FROM candlesticks WHERE symbol='XAUUSD' AND timeframe='W1';
```

### Check Latest Timestamps
```sql
-- Latest M1
SELECT MAX(time) FROM candlesticks WHERE symbol='XAUUSD' AND timeframe='M1';

-- Latest H1 (should be close to latest M1, within 1 hour)
SELECT MAX(time) FROM candlesticks_h1 WHERE symbol='XAUUSD';
```

### Check Weekly Alignment (Forex Week)
```sql
-- All W1 buckets should be Sunday 22:00:00+00
SELECT time, EXTRACT(DOW FROM time) as day_of_week, TO_CHAR(time, 'Dy HH24:MI:SS') as formatted
FROM candlesticks
WHERE symbol='XAUUSD' AND timeframe='W1'
ORDER BY time DESC
LIMIT 10;
-- Expected: day_of_week=0 (Sunday), formatted='Sun 22:00:00'
```

---

## Monitoring

### Materialization Progress
```sql
-- Check last materialized range per cagg
SELECT view_name,
     range_start,
     range_end
FROM timescaledb_information.continuous_aggregate_stats
ORDER BY view_name;
```

### Policy Status
```sql
SELECT application_name, schedule_interval, config
FROM timescaledb_information.jobs
WHERE application_name LIKE '%continuous_aggregate%';
```

---

## Performance Benefits

### Before (Python Aggregation)
- CPU: 200% every 5 min (scheduler running Python aggregation)
- DB writes: 8 TFs × N symbols × new bars every interval
- Indicator calc: Re-queried aggregated data

### After (Timescale Caggs)
- CPU: ~10% baseline (only M1 writes + Redis state updates)
- DB writes: Only M1 (1 TF)
- Materialization: Background, incremental, efficient
- Indicator calc: Reads from indexed cagg views
- Forming candles: Zero DB reads (Redis-only)

---

## Summary for Frontend Team

**Good news: NO frontend changes required!**

1. **API contract unchanged:**
  - Still call `GET /api/historical/{symbol}/{timeframe}`
  - Response format identical

2. **Caching transparent:**
  - Keys still `candles:{symbol}:{timeframe}`
  - SSE updates unchanged

3. **What changed (backend only):**
  - Storage: M1 in base table, higher TFs in cagg views
  - Queries: Backend routes to correct relation
  - Performance: Much lower CPU/DB load

4. **If you query DB directly (not via API):**
  - ❌ Old: `SELECT * FROM candlesticks WHERE timeframe='H1'`
  - ✅ New: `SELECT * FROM candlesticks_h1` (no timeframe column)

---

## Next Steps

1. ✅ Migrate schema (done)
2. ✅ Refresh caggs (done)
3. ✅ Update backend code (done)
4. ✅ Clear cache (done)
5. ⏳ Delete legacy W1 indicators
6. ⏳ Recompute all indicators
7. ⏳ Monitor for 24h
8. ✅ Document (this file)
```
</details>

## Overview

We migrated from **Python-side candle aggregation** (storing all TFs in one table) to **TimescaleDB continuous aggregates** to eliminate CPU spikes for fixed-duration timeframes.

---

## Data Storage Architecture

### Before (Legacy - Removed)
```
candlesticks table
├── M1 rows (timeframe='M1')
├── M5 rows (timeframe='M5')  ❌ DELETED
├── M15 rows                   ❌ DELETED
├── W1 rows (Monday-based)     ❌ DELETED (wrong alignment)
└── ...all TFs mixed
```

### Now (Current - Hybrid Truth Model)
```
candlesticks hypertable
├── M1 (timeframe='M1')       ← source of truth for derived CAGGs
└── D1/W1/MN1 (timeframe in {'D1','W1','MN1'}) ← source of truth (broker-provided)

candlesticks_m5  ← MATERIALIZED VIEW (continuous aggregate)
candlesticks_m15 ← MATERIALIZED VIEW
candlesticks_m30 ← MATERIALIZED VIEW
candlesticks_h1  ← MATERIALIZED VIEW
candlesticks_h4  ← MATERIALIZED VIEW

NOTE:
- D1/W1/MN1 continuous aggregates were intentionally removed due to DST/session alignment.
- D1/W1/MN1 must never be queried from CAGGs.
```

---

## How Continuous Aggregates Work

### 1. Definition (SQL)
Located in: `ai_trading_bot/db/continuous_aggregates.sql`

Each cagg is defined as:
```sql
CREATE MATERIALIZED VIEW candlesticks_h1
WITH (timescaledb.continuous)
AS
SELECT
  symbol,
  time_bucket(INTERVAL '1 hour', time, 'UTC') AS time,
  first(open, time)  AS open,   -- First open in bucket
  max(high)          AS high,   -- Max high
  min(low)           AS low,    -- Min low
  last(close, time)  AS close,  -- Last close
  sum(volume)        AS volume  -- Total volume
FROM candlesticks
WHERE timeframe = 'M1'
GROUP BY symbol, time_bucket(INTERVAL '1 hour', time, 'UTC')
WITH NO DATA;

ALTER MATERIALIZED VIEW candlesticks_h1 SET (timescaledb.materialized_only = TRUE);
```

**Key points:**
- Source: `candlesticks WHERE timeframe='M1'`
- Output: **No `timeframe` column** (it's implicit: `candlesticks_h1` = H1 data)
- Bucket alignment: `time_bucket()` ensures correct UTC boundaries
- W1 special: Uses explicit origin `2000-01-02 22:00:00+00` (Sunday 22:00 UTC) for forex trading week

### 2. Materialization Policies
```sql
SELECT add_continuous_aggregate_policy('candlesticks_h1',
  start_offset => INTERVAL '5 years',
  end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour',
  if_not_exists => TRUE
);
```

**What this does:**
- Background job runs every `schedule_interval` (1 hour for H1)
- Materializes buckets in window: `[now - 5 years, now - 1 hour]`
- As new M1s arrive, Timescale auto-materializes new buckets
- **Zero Python aggregation code runs**

### 3. Manual Refresh (when needed)

⚠️ **Important (existing volumes):** The SQL files mounted into `docker-entrypoint-initdb.d/` only run on the *first* initialization of an empty PGDATA volume. If you reused an existing `./volumes/pgdata` (or restored a DB dump) and the `candlesticks_m5/m15/m30/h1/h4` relations don’t exist, apply `ai_trading_bot/db/continuous_aggregates.sql` manually, then refresh.

```sql
-- Full refresh (all history)
CALL refresh_continuous_aggregate('candlesticks_h1', NULL, NULL);

-- Partial refresh (specific range)
CALL refresh_continuous_aggregate('candlesticks_h1', '2025-01-01', '2026-01-01');
```

We ran full refreshes once after migration. After that, policies keep views up-to-date automatically.

---

## Backend (API) Changes

### Code Location
- `api/app/routes/historical.py` (historical candle endpoint)
- `api/app/db.py` (regime data queries)
- `api-worker/scripts/calculate_recent_indicators_v2.py` (indicator calculation)

### Query Pattern (ALREADY IMPLEMENTED ✅)

```python
def _use_timescale_caggs() -> bool:
    return os.getenv("USE_TIMESCALE_CAGGS") == "true"

def _cagg_relation_for_timeframe(timeframe: str) -> str:
    tf = timeframe.upper()
    if tf == "M1":
        return "candlesticks"
    mapping = {
        "M5": "candlesticks_m5",
        "M15": "candlesticks_m15",
        # ... etc
    }
    return mapping[tf]

# In query builder:
if _use_timescale_caggs() and timeframe != "M1":
    rel = _cagg_relation_for_timeframe(timeframe)
    # Query: SELECT * FROM candlesticks_h1 WHERE symbol = 'XAUUSD'
    # NO timeframe column in WHERE or JOIN
else:
    # Query: SELECT * FROM candlesticks WHERE symbol='X' AND timeframe='H1'
```

**Critical difference:**
- **Old:** `WHERE timeframe = 'H1'`
- **New:** Query `candlesticks_h1` directly (no `timeframe` column)

---

## Frontend Changes

### Current State: ✅ NO CHANGES NEEDED

Frontend calls:
```typescript
apiService.getHistoricalData('XAUUSD', 'H1', 1000)
// → GET /api/historical/XAUUSD/H1?limit=1000
```

Backend (`historical.py`) **already handles** the routing:
- If `USE_TIMESCALE_CAGGS=true` and timeframe != M1 → queries cagg view
- Otherwise → queries `candlesticks` table

**Result:** Frontend is **already compatible** (no code changes required).

---

## Caching Layer

### Current State: ✅ COMPATIBLE

Cache key pattern:
```python
candles_key(symbol, timeframe) → "candles:XAUUSD:H1"
```

**This still works** because:
1. Cache keys are **logical** (symbol + timeframe)
2. Backend abstracts storage (cagg vs table)
3. Cache doesn't know/care about the DB schema

### What we did:
- Cleared all `candles:*` keys after migration (fresh start)
- Cleared `forming:bucket:*` (Redis forming-state buckets)

### Going forward:
- Cache invalidation (on new M1 writes) works as before
- SSE candle updates publish with `{symbol, timeframe, ...}` (unchanged)

---

## Forming Candles (Live Updates)

### Before (Legacy - CPU Spike Issue)
```
On each forming M1 tick:
1. Query DB: SELECT * FROM candlesticks WHERE timeframe='M1' AND time >= bucket_start
2. Aggregate in Python
3. Publish via Redis/SSE
→ Heavy DB reads every second
```

### Now (Redis State - Zero DB Reads)
```
On closed M1 bar:
1. Update Redis bucket state (incremental OHLCV per TF)
   Key: "forming:bucket:XAUUSD:H1:1706025600"
   Hash: {open, high, low, close, volume, last_ts}

On forming M1 tick:
1. Read Redis bucket state (HGETALL)
2. Overlay forming M1 (update high/low/close/volume)
3. Publish via Redis/SSE
→ Zero DB queries
```

**Location:** `api/app/mt5_ingest.py`
- `_update_forming_state_from_closed_m1()` maintains Redis state
- `_compute_forming_candle_sync()` uses Redis state (not DB)

---

## Technical Indicators

### Storage
- Table: `technical_indicators`
- Columns: `symbol, timeframe, time, ema_9, rsi, macd_main, ...`
- Still has `timeframe` column (unchanged)

### Calculation
Script: `api-worker/scripts/calculate_recent_indicators_v2.py`

**Already updated** to:
1. Query correct relation for candles:
   ```python
   relation = _ohlcv_relation_for_timeframe('H1')  # → candlesticks_h1
   ```
2. Check latest candle time from correct view
3. Compute indicators on cagg data
4. Write to `technical_indicators` with `timeframe` column

### What we need to do (cleanup):
- Delete legacy Monday-based W1 indicator rows
- Recompute all indicators for all symbols/TFs to align with new cagg buckets

---

## Cleanup Tasks (TODO)

### 1. Delete Legacy W1 Indicators (Monday-based)
```sql
-- Find misaligned indicators (time not in cagg buckets)
DELETE FROM technical_indicators ti
WHERE ti.timeframe = 'W1'
  AND NOT EXISTS (
    SELECT 1 FROM candlesticks c
    WHERE c.symbol = ti.symbol AND c.time = ti.time AND c.timeframe = 'W1'
  );
```

### 2. Recompute All Indicators
```bash
docker exec tradingbot-api-worker python /app/scripts/calculate_recent_indicators_v2.py
# This will:
# - Read from correct cagg views
# - Upsert indicators aligned to new bucket timestamps
# - Report progress per symbol/TF
```

### 3. Verify Alignment
```sql
-- Check for any orphaned indicators (candles exist but indicators missing)
SELECT c.symbol, COUNT(*) as missing_indicators
FROM candlesticks_h1 c
LEFT JOIN technical_indicators ti ON c.symbol=ti.symbol AND c.time=ti.time AND ti.timeframe='H1'
WHERE ti.time IS NULL
GROUP BY c.symbol;
```

---

## Verification Queries

### Check Row Counts
```sql
-- M1 source
SELECT COUNT(*) FROM candlesticks WHERE symbol='XAUUSD' AND timeframe='M1';

-- Aggregated TFs
SELECT COUNT(*) FROM candlesticks_h1 WHERE symbol='XAUUSD';

-- Broker-provided HTFs
SELECT COUNT(*) FROM candlesticks WHERE symbol='XAUUSD' AND timeframe='W1';
```

### Check Latest Timestamps
```sql
-- Latest M1
SELECT MAX(time) FROM candlesticks WHERE symbol='XAUUSD' AND timeframe='M1';

-- Latest H1 (should be close to latest M1, within 1 hour)
SELECT MAX(time) FROM candlesticks_h1 WHERE symbol='XAUUSD';
```

### Check Weekly Alignment (Forex Week)
```sql
-- All W1 buckets should be Sunday 22:00:00+00
SELECT time, EXTRACT(DOW FROM time) as day_of_week, TO_CHAR(time, 'Dy HH24:MI:SS') as formatted
FROM candlesticks
WHERE symbol='XAUUSD' AND timeframe='W1'
ORDER BY time DESC
LIMIT 10;
-- Expected: day_of_week=0 (Sunday), formatted='Sun 22:00:00'
```

---

## Monitoring

### Materialization Progress
```sql
-- Check last materialized range per cagg
SELECT view_name,
       range_start,
       range_end
FROM timescaledb_information.continuous_aggregate_stats
ORDER BY view_name;
```

### Policy Status
```sql
SELECT application_name, schedule_interval, config
FROM timescaledb_information.jobs
WHERE application_name LIKE '%continuous_aggregate%';
```

---

## Performance Benefits

### Before (Python Aggregation)
- CPU: 200% every 5 min (scheduler running Python aggregation)
- DB writes: 8 TFs × N symbols × new bars every interval
- Indicator calc: Re-queried aggregated data

### After (Timescale Caggs)
- CPU: ~10% baseline (only M1 writes + Redis state updates)
- DB writes: Only M1 (1 TF)
- Materialization: Background, incremental, efficient
- Indicator calc: Reads from indexed cagg views
- Forming candles: Zero DB reads (Redis-only)

---

## Summary for Frontend Team

**Good news: NO frontend changes required!**

1. **API contract unchanged:**
   - Still call `GET /api/historical/{symbol}/{timeframe}`
   - Response format identical

2. **Caching transparent:**
   - Keys still `candles:{symbol}:{timeframe}`
   - SSE updates unchanged

3. **What changed (backend only):**
   - Storage: M1 in base table, higher TFs in cagg views
   - Queries: Backend routes to correct relation
   - Performance: Much lower CPU/DB load

4. **If you query DB directly (not via API):**
   - ❌ Old: `SELECT * FROM candlesticks WHERE timeframe='H1'`
   - ✅ New: `SELECT * FROM candlesticks_h1` (no timeframe column)

---

## Next Steps

1. ✅ Migrate schema (done)
2. ✅ Refresh caggs (done)
3. ✅ Update backend code (done)
4. ✅ Clear cache (done)
5. ⏳ Delete legacy W1 indicators
6. ⏳ Recompute all indicators
7. ⏳ Monitor for 24h
8. ✅ Document (this file)

-->
