FROM python:3.12-slim

WORKDIR /app

COPY docker-monitor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker-monitor/ .

CMD ["python", "-u", "system_monitor.py"]
