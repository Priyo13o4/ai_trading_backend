# Utility Scripts

This folder contains one-time setup and data import utilities that may be needed for future operations.

## Import/Reimport Scripts

### `import_historical_csv.py`
Imports MT5 CSV exports and aggregates M1 data into all required timeframes (M1, M5, M15, M30, H1, H4, D1, W1, MN1).

**Usage:**
```bash
docker compose exec api-worker python /app/scripts/utils/import_historical_csv.py
```

**When to use:**
- Adding a new symbol to the database
- Reimporting historical data after corrections
- Initial database population

**Requirements:**
- CSV files mounted at `/app/csv_data/`
- Format: Tab-delimited MT5 exports with columns: DATE, TIME, OPEN, HIGH, LOW, CLOSE, TICKVOL, VOL, SPREAD
- Timezone: UTC

---

### `pre_import_cleanup.py`
Prepares database for clean import by removing old data and disabling TimescaleDB compression.

**Usage:**
```bash
docker compose exec api-worker python /app/scripts/utils/pre_import_cleanup.py
```

**What it does:**
- Truncates candlesticks, technical_indicators, and market_structure tables
- Removes compression policy
- Decompresses all chunks
- Resets metadata

**⚠️ WARNING:** Deletes all candlestick and indicator data. Use with caution.

---

### `post_import_restoration.py`
Re-enables database optimizations and recalculates dependent data after import.

**Usage:**
```bash
docker compose exec api-worker python /app/scripts/utils/post_import_restoration.py
```

**What it does:**
- Re-enables TimescaleDB compression (7-day threshold)
- Compresses existing chunks
- Refreshes materialized views (if exist)
- Updates data_metadata table
- Recalculates technical indicators for last 1000 bars

---

### `master_reimport.sh`
Orchestrates full reimport process with confirmations.

**Usage:**
```bash
cd ai_trading_bot
./api-worker/scripts/utils/master_reimport.sh
```

**Process:**
1. Runs pre_import_cleanup.py
2. Runs import_historical_csv.py
3. Runs post_import_restoration.py
4. Prompts for confirmation between steps

**Use this when:** Performing complete database reset and reimport.

---

### `import_mt5_csv.py`
*(Legacy script - may have different implementation)*

Check contents before use. Likely superseded by `import_historical_csv.py`.

---

## Adding a New Symbol

To add a new symbol (e.g., NZDUSD):

1. **Export CSV from MT5:**
   - Format: Tab-delimited, M1 timeframe
   - Date range: Match existing symbols (2016-11-24 onwards)
   - Save to: `Mt5-chartdata-export/NZDUSD_M1_*.csv`

2. **Update symbol lists:**
   ```bash
   # In api-web/app/market_data.py
   SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"]
   ```

3. **Import data:**
   ```bash
   # Option A: Import only new symbol (modify import_historical_csv.py to filter)
   docker compose exec api-worker python /app/scripts/utils/import_historical_csv.py
   
   # Option B: Full reimport (if data issues exist)
   ./api-worker/scripts/utils/master_reimport.sh
   ```

4. **Verify:**
   ```bash
   docker compose exec -T postgres psql -U Priyo13o4 -d ai_trading_bot_data -c \
     "SELECT symbol, timeframe, COUNT(*) FROM candlesticks WHERE symbol='NZDUSD' GROUP BY symbol, timeframe;"
   ```

5. **Calculate indicators:**
   ```bash
   docker compose exec api-worker python /app/scripts/calculate_recent_indicators_v2.py
   ```

6. **Clear cache:**
   ```bash
   docker compose exec redis redis-cli -a "priyodip13o4" FLUSHALL
   ```

---

## Notes

- **Batch size:** Import uses 100K row batches for optimal performance
- **Performance:** Achieved ~35K rows/sec during last import (15.5M rows in 7min 22sec)
- **Compression:** TimescaleDB compresses data ~90%+ after 7 days
- **Indicators:** Calculated for last 1000 bars per symbol/timeframe
