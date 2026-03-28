# SQL Operations Quick Reference

**Last Verified:** December 25, 2025  
**Database:** ai_trading_bot_data  
**Status:** ✅ All tables confirmed present (16 tables)

## Database Tables (Verified)

The following tables exist in the `ai_trading_bot_data` database:
- `analysis_batches` - Workflow run tracking
- `regime_data` - Market regime classifications
- `regime_vectors` - Vector embeddings for regimes
- `strategies` - AI-generated trading strategies
- `strategy_vectors` - Vector embeddings for strategies  
- `signals` - MT5 trade execution records
- `email_news_analysis` - News sentiment analysis
- `email_news_vectors` - News vector embeddings
- `candlesticks` - OHLCV price data (TimescaleDB hypertable)
- `technical_indicators` - Calculated indicators
- `market_structure` - Market structure analysis
- `data_metadata` - Data tracking metadata
- `api_cache_log` - API cache tracking
- `sentiment_data` - Sentiment analysis data
- `regime_classifications` - Historical regime classifications

**Note:** Supabase tables (profiles, subscriptions, payments) are in a separate database and accessed via Supabase API.

---

## Database Connection

```bash
# Connect to PostgreSQL
docker exec -it n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data

# Or execute single command
docker exec n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data -c "YOUR_SQL_HERE"
```

---

## Table Management

### List All Tables
```sql
\dt
-- Or
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;
```

### Describe Table Schema
```sql
\d table_name
-- Examples:
\d regime_data
\d strategies
\d signals
```

### Table Sizes
```sql
SELECT 
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
AND tablename IN ('regime_data', 'strategies', 'signals', 'regime_vectors', 'strategy_vectors')
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

---

## Common Queries

### 1. Get Latest Regime for All Pairs
```sql
SELECT DISTINCT ON (trading_pair)
    trading_pair,
    regime_type,
    confidence_score,
    analysis_timestamp,
    LEFT(regime_summary, 100) as summary_preview
FROM regime_data
WHERE archived = false
ORDER BY trading_pair, analysis_timestamp DESC;
```

### 2. Get Active Strategies
```sql
-- For all pairs
SELECT * FROM get_active_strategies(NULL);

-- For specific pair
SELECT * FROM get_active_strategies('XAUUSD');

-- Or manual query
SELECT 
    trading_pair,
    strategy_name,
    direction,
    confidence,
    take_profit,
    stop_loss,
    EXTRACT(EPOCH FROM (expiry_time - NOW()))/60 as minutes_remaining
FROM strategies
WHERE status = 'active'
AND expiry_time > NOW()
AND archived = false
ORDER BY confidence DESC, expiry_time ASC;
```

### 3. Get Open Positions
```sql
SELECT 
    mt5_ticket,
    trading_pair,
    direction,
    entry_price,
    take_profit,
    stop_loss,
    lot_size,
    entry_time,
    EXTRACT(EPOCH FROM (NOW() - entry_time))/60 as minutes_open
FROM signals
WHERE status = 'open'
ORDER BY entry_time DESC;
```

### 4. Today's Performance
```sql
SELECT 
    trading_pair,
    COUNT(*) as total_trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
    ROUND(SUM(pnl), 2) as total_pnl,
    ROUND(AVG(pnl), 2) as avg_pnl
FROM signals
WHERE entry_time::DATE = CURRENT_DATE
AND status LIKE 'closed%'
GROUP BY trading_pair;
```

### 5. Strategy Win Rates
```sql
SELECT 
    s.strategy_name,
    COUNT(*) as executions,
    ROUND(AVG(CASE WHEN sig.hit_tp THEN 100 ELSE 0 END), 2) as tp_rate,
    ROUND(AVG(CASE WHEN sig.hit_sl THEN 100 ELSE 0 END), 2) as sl_rate,
    ROUND(AVG(sig.pnl), 2) as avg_pnl
FROM strategies s
LEFT JOIN signals sig ON s.strategy_id = sig.strategy_id
WHERE s.archived = false
GROUP BY s.strategy_name
ORDER BY avg_pnl DESC;
```

### 6. Recent Workflow Runs
```sql
SELECT 
    batch_id,
    batch_timestamp,
    triggered_by,
    symbols_analyzed,
    total_strategies_generated,
    processing_duration_ms
FROM analysis_batches
ORDER BY batch_timestamp DESC
LIMIT 10;
```

### 7. Regime History for Pair
```sql
SELECT 
    analysis_timestamp,
    regime_type,
    confidence_score,
    LEFT(regime_summary, 150) as summary
FROM regime_data
WHERE trading_pair = 'XAUUSD'
AND analysis_timestamp > NOW() - INTERVAL '24 hours'
AND archived = false
ORDER BY analysis_timestamp DESC;
```

---

## Helper Functions

### Get Latest Regime
```sql
SELECT * FROM get_latest_regime('XAUUSD');
```

### Get Active Strategies
```sql
SELECT * FROM get_active_strategies('EURUSD');
```

### Get Performance Metrics
```sql
SELECT * FROM get_pair_performance('XAUUSD');
```

### Rollback Workflow Run
```sql
-- View batch details first
SELECT * FROM analysis_batches WHERE batch_id = 123;

-- Then rollback
SELECT * FROM rollback_batch(123);
```

---

## Archival Operations

### 1. Archive Old Records (2+ years)
```sql
SELECT * FROM archive_old_records();
```

**Output:**
```
table_name     | archived_count
regime_data    | 150
strategies     | 300
signals        | 45
```

### 2. Export Archived Data
```sql
-- Export as JSONB
SELECT * FROM export_archived_data('regime_data');
SELECT * FROM export_archived_data('strategies');
SELECT * FROM export_archived_data('signals');
```

**Save to file (from terminal):**
```bash
# Export regime_data
docker exec n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data \
  -c "\copy (SELECT * FROM export_archived_data('regime_data')) TO '/tmp/regime_archive.jsonl'"

# Copy to host
docker cp n8n-postgres:/tmp/regime_archive.jsonl ./backups/regime_$(date +%Y%m%d).jsonl
```

### 3. View Archived Records
```sql
-- Count archived records
SELECT 
    'regime_data' as table_name,
    COUNT(*) as archived_count
FROM regime_data WHERE archived = true
UNION ALL
SELECT 
    'strategies',
    COUNT(*)
FROM strategies WHERE archived = true
UNION ALL
SELECT 
    'signals',
    COUNT(*)
FROM signals WHERE archived = true;
```

### 4. Purge Archived Data (CAUTION!)
```sql
-- ⚠️ Export first!
SELECT * FROM purge_archived_records();
```

---

## Data Insertion

### Insert Regime Data
```sql
-- 1. Create batch
INSERT INTO analysis_batches (triggered_by, symbols_analyzed, metadata)
VALUES (
    'schedule',
    ARRAY['XAUUSD', 'EURUSD'],
    '{"workflow": "Regime Classifier V2"}'::JSONB
)
RETURNING batch_id;

-- 2. Insert regime (use returned batch_id)
INSERT INTO regime_data (
    batch_id,
    trading_pair,
    regime_type,
    regime_summary,
    market_data,
    collection_info,
    analysis_timestamp
) VALUES (
    1,  -- batch_id from step 1
    'XAUUSD',
    'Trending Bull',
    'Strong uptrend with momentum...',
    '{"M5": {}, "M15": {}}'::JSONB,
    '{"bars": 250}'::JSONB,
    NOW()
);
```

### Insert Strategy
```sql
INSERT INTO strategies (
    batch_id,
    strategy_name,
    trading_pair,
    direction,
    entry_signal,
    take_profit,
    stop_loss,
    confidence,
    expiry_minutes,
    timestamp,
    expiry_time,
    detailed_analysis
) VALUES (
    1,
    'Breakout Strategy',
    'XAUUSD',
    'long',
    '{"condition_type": "breakout_close", "level": 2650.5}'::JSONB,
    2655.0,
    2645.0,
    'High',
    30,
    NOW(),
    NOW() + INTERVAL '30 minutes',
    'Strong bullish momentum...'
)
RETURNING strategy_id;
```

### Insert Signal (MT5 Trade)
```sql
INSERT INTO signals (
    strategy_id,
    mt5_ticket,
    trading_pair,
    direction,
    entry_price,
    take_profit,
    stop_loss,
    lot_size,
    entry_time,
    status
) VALUES (
    1,
    123456789,
    'XAUUSD',
    'long',
    2650.5,
    2655.0,
    2645.0,
    0.01,
    NOW(),
    'open'
)
RETURNING signal_id;
```

---

## Data Updates

### Mark Strategy as Executed
```sql
UPDATE strategies
SET status = 'executed', executed_at = NOW()
WHERE strategy_id = 1;
```

### Expire Old Strategies
```sql
UPDATE strategies
SET status = 'expired'
WHERE expiry_time < NOW()
AND status = 'active';
```

### Close Signal with Outcome
```sql
UPDATE signals
SET 
    exit_price = 2655.0,
    exit_time = NOW(),
    status = 'closed_tp',
    pnl = 45.50,
    pnl_pips = 4.5,
    hit_tp = true,
    hit_sl = false
WHERE mt5_ticket = 123456789
RETURNING signal_id;
```

---

## Maintenance

### Vacuum Tables
```sql
VACUUM ANALYZE regime_data;
VACUUM ANALYZE strategies;
VACUUM ANALYZE signals;
```

### Rebuild Vector Indexes (if needed)
```sql
-- Drop old index
DROP INDEX IF EXISTS idx_regime_vectors_embedding;

-- Recreate with more data
CREATE INDEX idx_regime_vectors_embedding 
ON regime_vectors USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 100);

-- Same for strategy_vectors
DROP INDEX IF EXISTS idx_strategy_vectors_embedding;
CREATE INDEX idx_strategy_vectors_embedding 
ON strategy_vectors USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 100);
```

### Check Index Usage
```sql
SELECT 
    schemaname,
    tablename,
    indexname,
    idx_scan as times_used,
    idx_tup_read as tuples_read,
    idx_tup_fetch as tuples_fetched
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
AND tablename IN ('regime_data', 'strategies', 'signals')
ORDER BY times_used DESC;
```

---

## Debugging

### Count Records Per Table
```sql
SELECT 
    (SELECT COUNT(*) FROM analysis_batches) as batches,
    (SELECT COUNT(*) FROM regime_data WHERE archived = false) as regimes,
    (SELECT COUNT(*) FROM regime_vectors) as regime_vectors,
    (SELECT COUNT(*) FROM strategies WHERE archived = false) as strategies,
    (SELECT COUNT(*) FROM strategy_vectors) as strategy_vectors,
    (SELECT COUNT(*) FROM signals WHERE archived = false) as signals;
```

### Find Missing Vectors
```sql
-- Regimes without vectors
SELECT r.regime_id, r.trading_pair, r.analysis_timestamp
FROM regime_data r
LEFT JOIN regime_vectors rv ON r.regime_id = rv.regime_id
WHERE rv.id IS NULL
AND r.archived = false;

-- Strategies without vectors
SELECT s.strategy_id, s.strategy_name, s.trading_pair
FROM strategies s
LEFT JOIN strategy_vectors sv ON s.strategy_id = sv.strategy_id
WHERE sv.id IS NULL
AND s.archived = false;
```

### Check for Orphaned Records
```sql
-- Signals without strategies (expected if strategy deleted)
SELECT COUNT(*) 
FROM signals 
WHERE strategy_id IS NULL;

-- Strategies without batch (shouldn't happen)
SELECT COUNT(*) 
FROM strategies 
WHERE batch_id IS NULL;
```

---

## Backup & Restore

### Backup Specific Tables
```bash
# Backup regime_data
docker exec n8n-postgres pg_dump -U Priyo13o4 -d ai_trading_bot_data \
  -t regime_data -t regime_vectors > regime_backup_$(date +%Y%m%d).sql

# Backup strategies
docker exec n8n-postgres pg_dump -U Priyo13o4 -d ai_trading_bot_data \
  -t strategies -t strategy_vectors > strategies_backup_$(date +%Y%m%d).sql

# Backup signals
docker exec n8n-postgres pg_dump -U Priyo13o4 -d ai_trading_bot_data \
  -t signals > signals_backup_$(date +%Y%m%d).sql
```

### Restore from Backup
```bash
# Restore
docker exec -i n8n-postgres psql -U Priyo13o4 -d ai_trading_bot_data < regime_backup_20251024.sql
```

---

## Useful Queries for Analytics

### Regime Distribution
```sql
SELECT 
    regime_type,
    COUNT(*) as occurrences,
    ROUND(AVG(confidence_score), 2) as avg_confidence
FROM regime_data
WHERE archived = false
AND analysis_timestamp > NOW() - INTERVAL '7 days'
GROUP BY regime_type
ORDER BY occurrences DESC;
```

### Best Performing Strategies
```sql
SELECT 
    s.strategy_name,
    s.trading_pair,
    COUNT(sig.signal_id) as total_executions,
    SUM(CASE WHEN sig.hit_tp THEN 1 ELSE 0 END) as tp_hits,
    SUM(CASE WHEN sig.hit_sl THEN 1 ELSE 0 END) as sl_hits,
    ROUND(SUM(sig.pnl), 2) as total_profit
FROM strategies s
LEFT JOIN signals sig ON s.strategy_id = sig.strategy_id
WHERE s.archived = false
AND sig.status LIKE 'closed%'
GROUP BY s.strategy_name, s.trading_pair
HAVING COUNT(sig.signal_id) >= 3
ORDER BY total_profit DESC;
```

### Hourly Performance
```sql
SELECT 
    EXTRACT(HOUR FROM entry_time) as hour,
    COUNT(*) as trades,
    ROUND(AVG(pnl), 2) as avg_pnl,
    SUM(CASE WHEN hit_tp THEN 1 ELSE 0 END) as tp_count
FROM signals
WHERE status LIKE 'closed%'
AND archived = false
GROUP BY EXTRACT(HOUR FROM entry_time)
ORDER BY hour;
```

---

## Quick Checks

```sql
-- Are workflows running?
SELECT COUNT(*) as batches_today 
FROM analysis_batches 
WHERE batch_timestamp::DATE = CURRENT_DATE;

-- Are strategies being generated?
SELECT COUNT(*) as strategies_today 
FROM strategies 
WHERE created_at::DATE = CURRENT_DATE;

-- Are trades being executed?
SELECT COUNT(*) as trades_today 
FROM signals 
WHERE entry_time::DATE = CURRENT_DATE;

-- Current active strategies
SELECT COUNT(*) as active_strategies 
FROM strategies 
WHERE status = 'active' AND expiry_time > NOW();

-- Open positions
SELECT COUNT(*) as open_positions 
FROM signals 
WHERE status = 'open';
```

---

## Emergency Operations

### Clear All Active Strategies (Reset)
```sql
-- Mark all as expired
UPDATE strategies
SET status = 'expired'
WHERE status = 'active';
```

### Delete Today's Bad Batch
```sql
-- Find batch ID
SELECT batch_id FROM analysis_batches 
WHERE batch_timestamp::DATE = CURRENT_DATE 
ORDER BY batch_timestamp DESC 
LIMIT 1;

-- Rollback
SELECT * FROM rollback_batch(123);  -- Replace 123 with actual batch_id
```

### Reset Signals Table (Testing Only!)
```sql
-- ⚠️ CAUTION: Deletes all signals
TRUNCATE signals RESTART IDENTITY CASCADE;
```

---

**Quick Reference Version:** 1.0  
**Last Updated:** October 24, 2025
