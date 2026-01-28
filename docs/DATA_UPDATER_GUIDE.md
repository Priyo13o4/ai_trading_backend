# Data Updater System - Quick Reference

## Overview
Automated data updates run on server startup and every 5 minutes to keep market data current.

## How It Works

### Architecture
- **Worker Startup Script**: `/app/worker_start.sh` runs in `api-worker`
- **Scheduler**: `data_updater_scheduler.py` manages update timing
- **Indicator Runner**: `calculate_recent_indicators_v2.py` updates recent technical indicators

### Update Cycle
1. **On Startup**: Runs immediately when container starts
2. **Every 5 Minutes**: Automatically fetches new candles for all pairs/timeframes
3. **Smart Detection**: Only fetches data if gap > 1 hour (skips if up to date)

## Fixed Issues

### ✅ D1 Candle Errors
**Problem**: Daily candles returned date-only format `'2025-12-22'` instead of `'2025-12-22 HH:MM:SS'`

**Solution**: Updated datetime parsing to handle both formats:
```python
try:
    dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
except ValueError:
    dt = datetime.strptime(dt_str, '%Y-%m-%d')  # D1 format
```

### ✅ December 23rd Bug (Future Timestamps)
**Problem**: Chart showed Dec 23rd when current date was Dec 22nd

**Root Cause**: Twelve Data API returns timestamps in GMT+11 (Sydney/Forex timezone), but we stored them as UTC without conversion

**Solution**: Convert from GMT+11 to UTC:
```python
dt_gmt11 = dt.replace(tzinfo=timezone.utc)  # Treat as GMT+11
dt_utc = dt_gmt11 - timedelta(hours=11)     # Convert to UTC
```

## Current Status

### Database
- **Total Rows**: 11,667,724 candlesticks
- **Date Range**: 2021-12-01 to 2025-12-22 (today)
- **Latest Data**: Up to 17:05 UTC (current time: ~17:12 UTC)
- **All Pairs**: XAUUSD, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD
- **All Timeframes**: M5, M15, H1, H4, D1 (M1 from MT5 import)

### Example Latest Times (Correct!)
```
EURUSD M5:  2025-12-22 17:05:00+00:00  ✓
EURUSD M15: 2025-12-22 17:00:00+00:00  ✓
EURUSD H1:  2025-12-22 17:00:00+00:00  ✓
EURUSD H4:  2025-12-22 16:00:00+00:00  ✓
EURUSD D1:  2025-12-22 00:00:00+00:00  ✓
```

## Monitoring

### Check Scheduler Status
```bash
docker logs tradingbot-api-worker 2>&1 | grep -E "(SCHEDULER|💤|GAP FILLING)" | tail -20
```

### Check Latest Update Time
```bash
docker exec tradingbot-api-worker python -c "
import psycopg
from datetime import datetime, timezone
conn = psycopg.connect('postgresql://Priyo13o4:priyodip13o4@n8n-postgres:5432/ai_trading_bot_data')
cur = conn.cursor()
cur.execute('SELECT symbol, timeframe, MAX(time) FROM candlesticks WHERE timeframe='\''M5'\'' GROUP BY symbol, timeframe ORDER BY symbol')
for r in cur.fetchall():
    print(f'{r[0]}: {r[2]}')
print(f'\\nCurrent UTC: {datetime.now(timezone.utc)}')
conn.close()
"
```

### Manual Update (if needed)
```bash
docker exec tradingbot-api-worker python /app/scripts/calculate_recent_indicators_v2.py
```

## Files Modified

### Created
- `/api-worker/scripts/worker/data_updater_scheduler.py` - 5-minute update scheduler (worker)
- `/api-worker/scripts/data_updater_scheduler.py` - Compatibility shim
- `/api-worker/worker_start.sh` - Worker startup script

### Updated
- `/api-worker/scripts/calculate_recent_indicators_v2.py` - Indicator calculation (v2)
- `/api-web/Dockerfile` - Web container build
- `/api-worker/Dockerfile` - Worker container build
- `/docker-compose.yml` - Split web/worker services

## Technical Details

### Timezone Handling
- **Database**: Stores all timestamps in UTC (TIMESTAMPTZ)
- **Twelve Data API**: Returns GMT+11 (Sydney timezone)
- **Conversion**: Subtract 11 hours from API timestamps
- **Chart Display**: Shows UTC timestamps correctly

### Rate Limiting
- **Twelve Data Free Plan**: 8 API calls per minute
- **Sleep Between Calls**: 7.5 seconds
- **Total Pairs × Timeframes**: 6 × 5 = 30 calls per update
- **Update Duration**: ~4 minutes per cycle

### Next Update
Check logs to see when next update will run:
```bash
docker logs tradingbot-api-worker 2>&1 | grep "💤" | tail -1
```

## Troubleshooting

### Scheduler Not Running
```bash
# Check if process is running
docker exec tradingbot-api-worker ps aux | grep data_updater

# Restart container
docker compose restart api-worker
```

### Wrong Timestamps
```bash
# Delete incorrect data (if needed)
docker exec tradingbot-api-worker python -c "
import psycopg
conn = psycopg.connect('postgresql://Priyo13o4:priyodip13o4@n8n-postgres:5432/ai_trading_bot_data')
cur = conn.cursor()
cur.execute('DELETE FROM candlesticks WHERE time > NOW()')
conn.commit()
print(f'Deleted {cur.rowcount} future rows')
conn.close()
"

# Recalculate indicators
docker exec tradingbot-api-worker python /app/scripts/calculate_recent_indicators_v2.py
```

### API Rate Limit Hit
If you see rate limit errors, the scheduler will automatically continue on the next cycle (5 minutes later).

---

Last Updated: 2025-12-22 17:12 UTC
