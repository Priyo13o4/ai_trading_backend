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

# Write a gunicorn config that suppresses noisy health-check access log lines.
# Without this, every 60-second Docker healthcheck floods stdout with:
#   "GET /api/health HTTP/1.1" 200 - "-" "python-urllib/3.x"
cat > /tmp/gunicorn_conf.py << 'EOF'
import logging

class HealthCheckFilter(logging.Filter):
    """Drop access log records for GET /api/health 200 responses."""
    def filter(self, record):
        msg = record.getMessage()
        return not ("GET /api/health" in msg and '" 200 ' in msg)

# Attach filter to the gunicorn access logger at startup.
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "health_check": {"()": HealthCheckFilter},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["health_check"],
        }
    },
    "loggers": {
        "gunicorn.access": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        }
    },
}
EOF

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
	--config /tmp/gunicorn_conf.py \
	app.main:app
