# 24-Hour Soak Test

## Status

**STATUS: INVALIDATED**

reason:

- H1-H3 changed runtime behavior (subscription ACK correlation, socket
  lifecycle cleanup, container health semantics).

old soak start:

- `2026-08-19 01:01:37` UTC (commit `bb2410d`, image
  `sha256:9cab41a5b20f...`)
- historical only
- not valid for final release

The old soak clock does not count toward final acceptance.

## Required next soak sequence

```text
J1-J5 complete
↓
J-series evidence refreshed
↓
F10 real Telegram passed
↓
final image built
↓
fresh F12 soak starts from zero
```

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

Re-run in the fresh F12 soak, on the final image:

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

Interventions 5-8 and 10 require alert delivery enabled; they will run
after Phase F10 is unblocked.

## Acceptance

After 24 elapsed hours of the fresh F12 soak with no failure criteria
(§17.5): record results here, then mark production-ready.