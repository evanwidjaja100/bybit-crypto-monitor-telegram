# 24-Hour Soak Test

## Status

**F12 soak in progress** — final production candidate (Phase F8 code).

- START UTC: `2026-08-19 01:01:37`
- COMMIT: `bb2410d` (F8; F9/F11 added only scripts/docs/artifacts, no runtime change)
- DOCKER IMAGE ID: `sha256:9cab41a5b20f7c0a8e253c8346d38319b267d6e1edf71d5cb821bb7c278f3cd3`
- PYTHON VERSION: 3.14.7 (`python:3.14-slim`)
- DEPENDENCY LOCK HASH: pip freeze `SHA256 DC8F0A8B5687161B0204F4440C3939A588F0CDF3D60E7D642436A82394F39EF8` (`artifacts/pip-freeze.txt`)
- DATABASE MIGRATION VERSION: 3 (fresh volume `bybit-monitor-soak-data`)
- Container: `bybit-monitor-soak` (`--restart unless-stopped`), monitoring-only
  mode (no Telegram credentials — delivery paths pending Phase F10).

The earlier compose stack (started 2026-08-18, pre-F8 code) is superseded and
stopped; it does not count toward the F12 clock.

## How to inspect

```bash
docker logs bybit-monitor-soak --since=24h | grep -E "event=(ws_reconnect|ticker_poll_failed|telegram_|alert_decision|listing_event)"
docker logs bybit-monitor-soak --since=24h | grep "event=health_issues"
docker exec bybit-monitor-soak python -c "import sqlite3;print(sqlite3.connect('/data/bybit_monitor.sqlite').execute('select count(*) from price_samples').fetchone())"
docker stats --no-stream bybit-monitor-soak
```

Health summary is logged every `HEALTH_SUMMARY_SECONDS` (300): instrument
counts, WS state, REST/Telegram health, qualifying coins, outbox depth,
discovery age.

## Mandatory interventions (plan §17.4)

1. Three container restarts.
2. One temporary network outage.
3. One forced Spot WS reconnect.
4. One forced Linear WS reconnect.
5. One Telegram delivery failure + recovery — pending real credentials (F10).
6. One restart with pending/retry outbox — pending F10.
7. One restart with alert state in `ACTIVE_RANGE` — pending F10 (alerts
   disabled in monitoring-only mode).
8. One synthetic `OVER_RANGE -> ACTIVE_RANGE` test — pending F10.
9. Verify Spot history continuity after restart.
10. Dispatcher survives injected transient repository error — pending F10.

Interventions 5-8 and 10 require alert delivery enabled; they will be run
after Phase F10 is unblocked, and the soak clock restarts from zero if any
runtime code/configuration changes at that point.

## Acceptance

After ≥24 elapsed hours with no failure criteria (§17.5): record results
here, then mark production-ready.