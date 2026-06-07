FROM python:3.11-slim

# System deps for asyncpg / psycopg binary wheels
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq-dev gcc postgresql-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install trading_common first (changes less often → better layer cache) ──
COPY common /tmp/common
RUN pip install --no-cache-dir /tmp/common && rm -rf /tmp/common

# ── Install backtest engine ──
COPY backtest_engine /app
RUN pip install --no-cache-dir .

# Default: show help. Override in docker-compose command.
ENTRYPOINT ["backtest-engine"]
CMD ["--help"]
