# Historical Data System - Deployment Guide
=============================================

## Overview
Complete database-backed system for 3-year historical forex data with real-time updates.

## Architecture
```
[Twelve Data API] 
       ↓ (backfill: ~48 min one-time)
[TimescaleDB + PostgreSQL]
   ├─ candlesticks (3 years OHLCV) ~750 MB
   ├─ technical_indicators (last 1000 bars) ~15 MB
   └─ metadata (completeness tracking)
       ↓
[Redis Cache] (5 min TTL)
       ↓
[FastAPI Historical Endpoints] → [Frontend]
```

## 🚀 Quick Start

### 1. Update Docker Stack with TimescaleDB
```bash
cd ai_trading_bot

# Rebuild with TimescaleDB
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Wait 30 seconds for initialization
sleep 30
```

**What Changed:**
- PostgreSQL image: `pgvector/pgvector:pg18` → `timescale/timescaledb-ha:pg17`
- Volume mount: Added `/db/schema.sql` for auto-initialization
- Extension: TimescaleDB loaded on startup

### 2. Verify Database Schema
```bash
# Connect to PostgreSQL
docker exec -it n8n-postgres psql -U postgres -d trading_db

# Check tables
\dt

# Expected output:
# - candlesticks (hypertable)
# - technical_indicators (hypertable)
# - market_structure (hypertable)
# - data_metadata
# - api_cache_log
# - regime_classifications

# Check views
\dv

# Expected output:
# - data_freshness
# - data_coverage_summary
# - api_usage_last_24h

# Exit
\q
```

### 3. Run Historical Backfill (~48 minutes)
```bash
cd scripts

# Test mode first (1 API call only)
python backfill_historical_data.py --test

# If successful, run full backfill
python backfill_historical_data.py

# Or run in background
nohup python backfill_historical_data.py &

# Monitor progress
tail -f nohup.out
```

**What It Does:**
- Fetches 3 years of OHLCV data for 6 symbols × 5 timeframes
- Total: 384 API calls (64 per symbol)
- Rate limiting: 7.5s between calls (Free tier = 8 calls/min)
- Stores data in TimescaleDB with UPSERT (no duplicates)
- Updates metadata after each symbol/timeframe

**Resume After Interruption:**
```bash
python backfill_historical_data.py --resume
```

### 4. Calculate Recent Indicators (~5-10 minutes)
```bash
# After backfill completes, calculate indicators for last 1000 bars
python calculate_recent_indicators.py

# Or for specific symbol/timeframe
python calculate_recent_indicators.py --symbol XAUUSD --timeframe H1
```

**What It Does:**
- Fetches last 1000 bars from database
- Calculates all technical indicators (EMAs, RSI, MACD, ATR, BB, ADX, etc.)
- Stores in `technical_indicators` table
- Uses pandas_ta for calculations

### 5. Setup Real-Time Updater (Every 5 minutes)

**Option A: n8n Workflow** (Recommended)
```
1. Open n8n: http://localhost:5678
2. Create new workflow:
   - Schedule Trigger (every 5 minutes)
   - Execute Command node: python /app/scripts/realtime_updater.py
   - Optional: Notification on failure
3. Activate workflow
```

**Option B: Cron Job** (Alternative)
```bash
# Edit crontab
crontab -e

# Add this line (runs every 5 minutes)
*/5 * * * * cd /path/to/ai_trading_bot/scripts && python realtime_updater.py >> /tmp/realtime_updater.log 2>&1
```

**What It Does:**
- Fetches latest 2 candles per symbol/timeframe (current + previous for confirmation)
- Stores in database with UPSERT
- Updates metadata automatically via trigger
- Logs progress to console/file

### 6. Test Historical API Endpoints
```bash
# Test API endpoints
curl "http://localhost:8080/api/historical/XAUUSD/H1?limit=100"

# With date range
curl "http://localhost:8080/api/historical/EURUSD/D1?start_date=2023-01-01&end_date=2024-01-01"

# Without indicators (faster)
curl "http://localhost:8080/api/historical/GBPUSD/M5?limit=500&include_indicators=false"

# Check data coverage
curl "http://localhost:8080/api/historical/coverage/XAUUSD"

# Check data freshness
curl "http://localhost:8080/api/historical/freshness"

# Find gaps
curl "http://localhost:8080/api/historical/gaps/XAUUSD/H1?gap_threshold_hours=24"
```

## 📊 Available Endpoints

### GET /api/historical/{symbol}/{timeframe}
Fetch historical OHLCV data with optional indicators

**Query Parameters:**
- `start_date` (optional): YYYY-MM-DD
- `end_date` (optional): YYYY-MM-DD
- `limit` (default: 1000, max: 5000)
- `include_indicators` (default: true)

**Response:**
```json
{
  "symbol": "XAUUSD",
  "timeframe": "H1",
  "bars": 100,
  "data": [
    {
      "datetime": "2023-01-01T00:00:00+00:00",
      "open": 1820.5,
      "high": 1825.3,
      "low": 1818.2,
      "close": 1822.8,
      "volume": 0,
      "indicators": {
        "ema_9": 1821.4,
        "ema_21": 1819.7,
        "rsi": 55.3,
        "macd": {
          "main": 1.2,
          "signal": 0.8,
          "histogram": 0.4
        },
        "atr": 3.5,
        "bollinger_bands": {
          "upper": 1830.5,
          "middle": 1822.0,
          "lower": 1813.5
        },
        "adx": 25.3
      }
    }
  ],
  "metadata": {
    "start": "2023-01-01T00:00:00+00:00",
    "end": "2023-01-05T00:00:00+00:00"
  }
}
```

### GET /api/historical/coverage/{symbol}
Get data coverage summary per timeframe

**Response:**
```json
{
  "symbol": "XAUUSD",
  "timeframes": [
    {
      "timeframe": "M5",
      "total_bars": 315360,
      "earliest": "2022-01-01T00:00:00",
      "latest": "2024-12-22T14:30:00",
      "completeness_pct": 98.5
    },
    {
      "timeframe": "H1",
      "total_bars": 26280,
      "completeness_pct": 99.2
    }
  ]
}
```

### GET /api/historical/gaps/{symbol}/{timeframe}
Find gaps in data

**Query Parameters:**
- `gap_threshold_hours` (default: 24)

### GET /api/historical/freshness
Check how fresh data is across all symbols

## 🔧 Maintenance

### View Database Status
```bash
docker exec -it n8n-postgres psql -U postgres -d trading_db

-- Check data freshness
SELECT * FROM data_freshness;

-- Check coverage summary
SELECT * FROM data_coverage_summary;

-- Check API usage (last 24h)
SELECT * FROM api_usage_last_24h;

-- Find gaps
SELECT * FROM find_data_gaps('XAUUSD', 'H1', 24);

-- Calculate completeness
SELECT calculate_data_completeness('XAUUSD', 'H1');
```

### Clean Old Indicators (Keep Only Last 1000 Bars)
```sql
-- TimescaleDB compression automatically handles this
-- But you can manually run:
SELECT cleanup_old_indicators();
```

### Re-Backfill Single Symbol/Timeframe
```bash
python backfill_historical_data.py --symbol XAUUSD --timeframe H1
```

### Re-Calculate Indicators
```bash
python calculate_recent_indicators.py --symbol XAUUSD --timeframe H1
```

## 📈 Storage & Performance

### Storage Usage
- OHLCV data (3 years): ~750 MB
- Technical indicators (1000 bars × 30): ~15 MB
- Metadata: ~30 KB
- Total: **~865 MB**

### Compression (Automatic)
- TimescaleDB compresses data >7 days old
- Compression ratio: ~90%
- Final storage: **~100 MB** (after compression kicks in)

### Retention Policy
- Automatic deletion of data >5 years old
- Configurable in schema.sql

### Cache Performance
- Redis caches queries for 5 minutes
- Cache hit rate: Expected >80% for common queries
- Cache key format: `historical:{symbol}:{timeframe}:{params}`

## ⚠️ Troubleshooting

### Issue: Schema Not Loaded
```bash
# Manually load schema
docker exec -i n8n-postgres psql -U postgres -d trading_db < db/schema.sql
```

### Issue: Backfill API Rate Limit Exceeded
```bash
# Increase delay between calls (edit backfill_historical_data.py)
RATE_LIMIT_DELAY = 10  # Change from 7.5 to 10 seconds
```

### Issue: Real-Time Updater Not Running
```bash
# Check logs
tail -f /tmp/realtime_updater.log

# Test manually
python realtime_updater.py
```

### Issue: Missing Indicators
```bash
# Re-run indicator calculation
python calculate_recent_indicators.py
```

### Issue: Database Connection Failed
```bash
# Check PostgreSQL is running
docker ps | grep n8n-postgres

# Check connection string in scripts
export DATABASE_URL="postgresql://postgres:yourpassword@localhost:5432/trading_db"
```

## 🎯 Next Steps

### 1. Update Frontend Signals Page
Replace existing API calls with new historical endpoints:

**Before:**
```javascript
// Old real-time API (no history)
const data = await fetch(`/api/market-data/${symbol}/${timeframe}`);
```

**After:**
```javascript
// New historical API (3 years of data)
const data = await fetch(
  `/api/historical/${symbol}/${timeframe}?limit=1000&include_indicators=true`
);
```

### 2. Add Date Range Picker to Dashboard
Allow users to select custom date ranges:
```javascript
const startDate = '2023-01-01';
const endDate = '2024-01-01';
const data = await fetch(
  `/api/historical/${symbol}/${timeframe}?start_date=${startDate}&end_date=${endDate}`
);
```

### 3. Display Data Coverage Indicator
Show users what data is available:
```javascript
const coverage = await fetch(`/api/historical/coverage/${symbol}`);
// Display: "XAUUSD: 98.5% complete (3 years)"
```

### 4. Create Gap Healing Script
Automatically fill gaps detected by `find_data_gaps()`:
```bash
# Future enhancement
python heal_data_gaps.py --auto
```

### 5. Add Enhanced Indicators (Optional)
Currently skipped per user request. Add later:
- Stochastic Oscillator
- CCI (Commodity Channel Index)
- Supertrend
- Ichimoku Cloud

## 📝 Files Created

1. `/ai_trading_bot/db/schema.sql` - Complete TimescaleDB schema
2. `/ai_trading_bot/scripts/backfill_historical_data.py` - Historical data download
3. `/ai_trading_bot/scripts/calculate_recent_indicators.py` - Indicator calculation
4. `/ai_trading_bot/scripts/realtime_updater.py` - Real-time 5-min updates
5. `/ai_trading_bot/api/app/routes/historical.py` - Historical API endpoints
6. `/ai_trading_bot/docker-compose.yml` - Updated with TimescaleDB

## 🎉 Summary

You now have:
- ✅ 3 years of historical OHLCV data (~750 MB)
- ✅ Technical indicators for recent 1000 bars (~15 MB)
- ✅ Real-time updates every 5 minutes
- ✅ Smart API endpoints with Redis caching
- ✅ Database compression (90%+ reduction)
- ✅ Automatic retention policies (5 years)
- ✅ Gap detection and metadata tracking

**Ready to integrate with frontend dashboard!**
