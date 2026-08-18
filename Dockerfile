FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bybit_monitor.sqlite

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin monitor \
    && mkdir -p /data \
    && chown -R monitor:monitor /data

USER monitor

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "exec('''import os, sqlite3, time\np = \"/data/bybit_monitor.sqlite\"\nlatest = 0.0\nfor f in (p, p + \"-wal\"):\n    if os.path.exists(f):\n        latest = max(latest, os.path.getmtime(f))\nconn = sqlite3.connect(p)\nconn.execute(\"SELECT 1\")\nconn.close()\nassert time.time() - latest < 600, \"database stale\"\n''')"

CMD ["python", "-m", "app.main"]