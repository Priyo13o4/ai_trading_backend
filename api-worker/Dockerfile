FROM python:3.13-slim
WORKDIR /app

RUN apt-get update && apt-get install -y build-essential libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# Install common package first
COPY common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

COPY api-worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Worker app package (MT5 ingest, indicators, market status)
COPY api-worker/app/ /app/app/

# Worker scripts
COPY api-worker/scripts/ /app/scripts/
COPY api-worker/worker_start.sh /app/worker_start.sh

RUN chmod +x /app/worker_start.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/worker_start.sh"]
