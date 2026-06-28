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

## Portfolio model & dashboard (EA-01/04/08/16/18 mirror)

The engine mirrors the live EA's account-level behaviour via a chronological single-account
portfolio walk (`simulation/portfolio.py`), on by default:

- **EA-04** rejects a new entry when an opposite-direction position is open on the symbol.
- **EA-16** sizes off floating EQUITY and reduces lots by `drawdown_reduction_factor` in drawdown.
- Concurrency (`max_concurrent_trades`) and total-risk (`max_total_risk_percent`) caps; an equity
  curve, max drawdown and profit factor.
- **EA-01** (`max_entry_distance_atr`) and **EA-08** (range-breakout off `level`) act on entry detection.

It emits a decision-grade `dashboard_data.json` (Trust / Edge / Contamination / Portfolio) that the
`ai_trading_backtest_dashboard` app reads directly (no API / no `backtest_lab` needed):

```bash
# Run with the portfolio model and refresh the dashboard payload in one shot
python -m backtest_engine.cli run --portfolio \
  --dashboard-out ../ai_trading_backtest_dashboard/public/

# Offline dashboard demo data (no DB), reading the real EA version for the trust card
PYTHONPATH=. .venv/bin/python scripts/generate_sample_dashboard.py
```

Flags: `--portfolio/--no-portfolio` (default on), `--starting-balance` (default 500),
`--dashboard-out PATH`. Artifacts per run: `portfolio_summary.{md,json}`,
`raw/portfolio_equity_curve.csv`, `raw/portfolio_trades.csv`, `dashboard_data.json`.

> Parity note: full Layer-A bit-exact parity (VALIDATION_DESIGN/03) still needs an MT5
> Strategy-Tester EA decision trace; until then the dashboard trust card shows surfaces as
> *audited status*, not trace-measured. See `VALIDATION_DESIGN/07_ROADMAP.md` for status.

## Configuration

- **EA config**: YAML file (see `configs/ea_v3_00.yml`); `ea_version` pins it to the deployed EA build
- **Broker specs**: JSONL file with per-symbol execution parameters
- **DB connections**: All from environment variables (never hardcoded)
