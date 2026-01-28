#!/bin/bash
# Startup script for trading bot worker
# Runs MT5 ingest TCP server (no HTTP server)

set -euo pipefail

echo "================================"
echo "AI Trading Bot Worker - Starting"
echo "================================"

# Start MT5 ingest TCP server (port 9001)
echo "Starting MT5 ingest server (TCP port 9001)..."
exec python -u /app/scripts/mt5_ingest_server.py
