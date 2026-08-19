FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bybit_monitor.sqlite

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin monitor \
    && mkdir -p /data \
    && chown -R monitor:monitor /data

USER monitor

# Health = critical application health (persisted snapshot + heartbeat
# freshness), not SQLite file mtime. See scripts/container_healthcheck.py.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python scripts/container_healthcheck.py || exit 1

CMD ["python", "-m", "app.main"]