#!/bin/bash
# Startup script for the dedicated SSE service (api-sse container)
# Uses raw Uvicorn — no Gunicorn needed because the entire workload is I/O-bound async.
# A single Uvicorn process can hold 10,000+ concurrent SSE connections.

echo "================================"
echo "PipFactor SSE Service - Starting"
echo "================================"

# How many Uvicorn processes to run.
# Default: 1 (pure async I/O; scale up only when connection count demands it).
# Override via SSE_WORKERS env var in docker-compose.
SSE_WORKERS="${SSE_WORKERS:-1}"
# Uvicorn requires lowercase log level ('info', not 'INFO')
LOG_LEVEL_LOWER=$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')

echo "Starting SSE server with ${SSE_WORKERS} Uvicorn worker(s)..."

exec uvicorn app.sse_main:app \
    --host 0.0.0.0 \
    --port 8081 \
    --workers "$SSE_WORKERS" \
    --log-level "$LOG_LEVEL_LOWER" \
    --no-access-log
