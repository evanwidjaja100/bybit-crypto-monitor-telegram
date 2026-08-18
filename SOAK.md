# 24-Hour Soak Test

## Status

Started live at `2026-08-18 03:17 UTC` inside `docker compose` (monitoring-only
mode, no Telegram credentials). Keep the stack running for 24h and inspect per
the procedure below before declaring production-ready.

## How to run

```bash
cp .env.example .env        # fill in real credentials for full alert delivery
docker compose up -d        # build + start
docker compose logs -f      # watch event=... lines
```

## How to inspect

```bash
docker compose logs --since=24h | grep -E "event=(ws_reconnect|ticker_poll_failed|telegram_retry|alert_decision|listing_event)"
docker compose logs --since=24h | grep "event=health_issues"    # any flagged subsystem
docker exec bybitcryptomonitortelegram-bybit-monitor-1 python -c "import sqlite3;print(sqlite3.connect('/data/bybit_monitor.sqlite').execute('select count(*) from price_samples').fetchone())"
docker stats --no-stream bybitcryptomonitortelegram-bybit-monitor-1
```

Health summary (logged every `HEALTH_SUMMARY_SECONDS`) shows instrument
counts, WS state, REST/Telegram health, qualifying coins, queue depth and
last-discovery age.

## In-session findings (first hour, 2026-08-18)

- Live universe: 1382 instruments (556 spot, 753 linear USDT, 68 linear USDC),
  no missing markets, pagination complete.
- Both WebSocket streams connected with full subscriptions (556 / 821 topics).
- REST fallback poll running every 10s; REST reconciliation executes on every
  WS reconnect (`event=ws_reconnect_rest_refresh`).
- 4 container restarts: registry/alert/listing/spot-history state all restored
  from the `/data` volume; `first_run=False events=0` - no false listing storm.
- 1 forced network interruption (60s disconnect): no crash; both streams
  reconnected and re-subscribed; no duplicate alerts after recovery.
- Live alert policy exercised: qualifying counts 2-5 observed;
  `state=OVER_RANGE` at 4+ (suppressed), `state=ACTIVE_RANGE` at 1-3, no
  duplicate transitions after restarts.
- Graceful shutdown verified: SIGTERM -> `telegram_dispatcher_stopped` ->
  `services_stopped` -> `shutdown_complete`.
- Health summary runs; `event=health_issues` correctly flags rest/telegram
  when stale (expected in monitoring-only mode: telegram has no credentials).

## Remaining interventions (if not yet done)

1. Telegram failure simulation (only relevant once credentials are set).
2. One restart while 1-3 coins are actively qualifying.
3. Spot-history quality check after 24h (anchor hit rate).

## Acceptance

After 24h: registry restores, spot history restores, alert state restores,
WebSockets reconnect, REST reconciliation executes, no false listing storm,
no duplicate alert storm. Record findings, then mark production-ready.