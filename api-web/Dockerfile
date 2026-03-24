FROM python:3.13-slim
WORKDIR /app

RUN apt-get update && apt-get install -y build-essential libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# Install common package first
COPY common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

COPY api-web/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api-web/ /app/

# Make startup scripts executable
RUN chmod +x /app/start.sh /app/sse_start.sh /app/docker_entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

EXPOSE 8080

ENTRYPOINT ["/app/docker_entrypoint.sh"]
CMD ["/app/start.sh"]
