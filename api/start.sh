#!/bin/bash
# Startup script for trading bot API
# Starts data updater in background and then starts the API

echo "================================"
echo "AI Trading Bot API - Starting"
echo "================================"

# Default to MT5-only ingestion unless explicitly overridden.
if [ -z "$DATA_SOURCE" ]; then
	export DATA_SOURCE="MT5"
fi
if [ -z "$MT5_INGEST_ENABLE" ]; then
	export MT5_INGEST_ENABLE="true"
fi

# Start data updater scheduler in background
echo "Starting data updater scheduler..."
python -u /app/scripts/data_updater_scheduler.py &
SCHEDULER_PID=$!
echo "✓ Scheduler started (PID: $SCHEDULER_PID)"

# Give scheduler a moment to start initial update
sleep 2

# Start the API server
echo "Starting FastAPI server..."
GUNICORN_LOG_LEVEL="${LOG_LEVEL:-info}"
exec gunicorn -w 2 -k uvicorn.workers.UvicornWorker \
	--bind 0.0.0.0:8080 \
	--log-level "$GUNICORN_LOG_LEVEL" \
	--access-logfile - \
	--error-logfile - \
	app.main:app
