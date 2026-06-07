# PipFactor Backtest Engine

Offline strategy replay engine for PipFactor. Reads strategies and candlestick data
from the production `ai_trading_bot_data` database (read-only) and writes results to
a separate `backtest_lab` database.

## Quick start

```bash
# 1. Create the backtest_lab database
docker exec -it n8n-postgres psql -U "$POSTGRES_USER" -c "CREATE DATABASE backtest_lab;"

# 2. Run migrations
docker compose -f docker-compose.yml -f docker-compose.backtest.yml \
  --profile backtest run --rm backtest-engine db upgrade

# 3. Inspect source data
docker compose -f docker-compose.yml -f docker-compose.backtest.yml \
  --profile backtest run --rm backtest-engine inspect-source

# 4. Run a backtest
docker compose -f docker-compose.yml -f docker-compose.backtest.yml \
  --profile backtest run --rm backtest-engine run \
    --profile configs/ea_v3_00.yml \
    --universe expired \
    --report-dir /app/reports
```

## Local development (no Docker)

```bash
cd ai_trading_bot/backtest_engine
pip install -e ../common -e .

# Set env vars (or source .env.local)
export BACKTEST_DB_HOST=localhost BACKTEST_DB_PORT=5432 ...
export SOURCE_DB_HOST=localhost  SOURCE_DB_PORT=5432 ...

backtest-engine --help
```

## CLI commands

| Command            | Description                                      |
|--------------------|--------------------------------------------------|
| `run`              | Execute a backtest against the source database   |
| `db upgrade`       | Run Alembic migrations on `backtest_lab`         |
| `db dump`          | `pg_dump` the backtest_lab database              |
| `db restore`       | `pg_restore` a dump into backtest_lab            |
| `inspect-source`   | Print source DB stats (strategies, candles, etc.)|

## Configuration

- **EA config**: YAML file (see `configs/ea_v3_00.yml`)
- **Broker specs**: JSONL file with per-symbol execution parameters
- **DB connections**: All from environment variables (never hardcoded)
