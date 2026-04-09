FROM ghcr.io/d4vinci/scrapling:latest

WORKDIR /app

RUN python3 -m pip install --no-cache-dir "scrapling[fetchers]==0.4.3" typing_extensions aiohttp

COPY escalation.py /srv/scrapling-api/escalation.py
COPY app.py /srv/scrapling-api/app.py
COPY app_async.py /srv/scrapling-api/app_async.py

ENV SCRAPLING_HOST=0.0.0.0
ENV SCRAPLING_PORT=8010

ENTRYPOINT []

CMD ["python3", "/srv/scrapling-api/app_async.py"]
