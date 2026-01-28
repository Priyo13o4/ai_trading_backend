#!/bin/bash
# Startup script for trading bot API (web only)

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

# Start the API server
echo "Starting FastAPI server (web only)..."
GUNICORN_LOG_LEVEL="${LOG_LEVEL:-info}"
if [ -z "$GUNICORN_WORKERS" ]; then
	if [ "$MT5_INGEST_ENABLE" = "true" ]; then
		GUNICORN_WORKERS=1
	else
		GUNICORN_WORKERS=2
	fi
fi

exec gunicorn -w "$GUNICORN_WORKERS" -k uvicorn.workers.UvicornWorker \
	--bind 0.0.0.0:8080 \
	--log-level "$GUNICORN_LOG_LEVEL" \
	--access-logfile - \
	--error-logfile - \
	app.main:app
