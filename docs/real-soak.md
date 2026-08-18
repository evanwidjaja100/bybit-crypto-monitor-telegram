# R12 — Real 24-Hour Soak (Operator Runbook)

Requires at least 24 elapsed hours. Do not simulate completion. Do not mark
`SOAK.md` DONE early. Any code change during the soak restarts the clock.

## 0. Prerequisites (all must hold before start)

- All remediation tests pass: `python -m pytest tests -q` → 283 passed.
- R11 live staging validation recorded (public part done; Telegram items below).
- `.env` configured with real TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
- R11 operator tasks completed (see AUDIT_REMEDIATION_STATUS.md): 17.3 controlled
  Telegram delivery (transition + listing through the outbox path), 17.4 controlled
  outage + recovery, 17.5 container restart with pending outbox.
- Container built from the final candidate commit: `docker compose build`.

## 1. Start

```text
docker compose up -d
```

Record metadata (18.2) at the top of `SOAK.md`:

```text
START UTC:
COMMIT HASH:            (git rev-parse HEAD, must not change during soak)
IMAGE ID:               (docker images -q bybit-monitor)
PYTHON VERSION:         (docker exec bybit-monitor python --version)
DEPENDENCY LOCK HASH:   (sha256sum requirements.txt)
DATABASE PATH:          /data/bybit_monitor.sqlite (docker volume bybit-data)
```

## 2. Monitoring (18.3) — check at least twice, once after the forced interventions

```text
docker stats --no-stream bybit-monitor                                  # CPU / memory
docker exec bybit-monitor sh -c 'du -h /data/bybit_monitor.sqlite'      # db size
docker compose logs --since 2h bybit-monitor | grep -E "ERROR|reconnect"  # REST errors, WS reconnects
```

DB inspection (while container runs, via a second process; use `sqlite3` if
available, otherwise a short python script against the same DB):

```text
SELECT COUNT(*) FROM price_samples;                      # price sample count
SELECT status, COUNT(*) FROM outgoing_notifications GROUP BY status;  # pending/retry/dead
SELECT COUNT(*) FROM listing_events WHERE telegram_sent = 0;          # listing events undelivered
```

Health endpoint / logs: last ticker ages, discovery age, instrument counts,
qualifying unique coin count, duplicate alerts (message_tag counting).

## 3. Mandatory interventions (18.4) — do all, at least 3 restarts total

1. Controlled restart (normal operation): `docker compose restart bybit-monitor`
   → verify no duplicate transition, no duplicate hourly alert, no duplicate
   listing storm after restart.
2. Network interruption: stop the container's outbound traffic for 60–90s
   (e.g. `docker network disconnect`), verify monitoring continues, REST/WS
   recover on reconnect.
3. Forced Spot WS reconnect and 4. forced Linear WS reconnect: the app has no
   admin endpoint; simulate by restarting (counts as a restart) or by adding a
   temporary network filter at the proxy level. Verify stale watchdog +
   resubscription (see test_websocket stale/disconnect tests for the contract).
5. Controlled Telegram failure: set an invalid bot token, confirm:
   `monitoring continues / notification moves to retry / listing not marked sent`.
   Restore token, confirm `notification succeeds / listing delivery state repaired`.
6. Restart with a pending/retry outbox message: inject a synthetic notification
   (clearly labeled, e.g. message_tag `synthetic-soak`) that fails delivery
   (step 5 token trick), restart, confirm it retries and drains.
7. Restart while alert state is ACTIVE_RANGE: craft a qualifying state, restart,
   confirm state persisted (no duplicate transition on resume).
8. Verify Spot history survives restart: price_samples rows older than the
   restart still power find_anchor (WARMING_UP resolves after restart).

Synthetic events: label them (message_tag / log line) and exclude them from
real-market interpretation in SOAK.md.

## 4. Failure criteria (18.5) — abort on any

Process dies without recovery / DB corruption / unbounded memory / unbounded
outbox growth / duplicate alert storm / false listing storm / pending/retry
messages disappear / listing marked delivered when Telegram failed / WS stale
without recovery / REST discovery dead / nonsensical health ages / 4+ coins
produce range alerts / debounce bypassed.

## 5. End (18.6) — after ≥24h elapsed

Verify: service healthy, state persisted, no release-blocking defect, no
unexplained notification loss, no duplicate storm, Spot history valid, listing
system healthy, outbox drained/retrying correctly.

Fill in `SOAK.md` with actual findings and timestamps, then update
`AUDIT_REMEDIATION_STATUS.md`:

```text
R12 — 24-hour soak | COMPLETE | <commit> | 24-hour soak passes = TRUE
Project status: PRODUCTION READY (only after R12 COMPLETE + R13 final acceptance)
```
