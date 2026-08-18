# Soak testing

## Deterministic self-contained soak (fast, no credentials)

```bash
python scripts/soak_test.py
```

Simulates **26 hours** of market activity in about 30 seconds against the
real production pipeline (discovery -> price engine -> momentum ->
deduplication -> alert state machine -> SQLite outbox -> dispatcher ->
fake Telegram), then asserts:

- exactly one transition alert per qualifying episode (suppression at 4+);
- at most one hourly alert per UTC hour bucket;
- no duplicate notifications (dedupe keys);
- the outbox drains fully to delivered state.

Exit code 0 = pass. This is a confidence check only and does **not**
replace the mandatory 24-hour production soak below.

## Mandatory 24-hour production soak

Required before the project may be marked `PRODUCTION READY` (Phase R12
of the master implementation plan). See `SOAK.md` for the full runbook.

Prerequisites: a deployed stack (docker compose) with real Telegram
credentials in `.env` and live Bybit access.

Minimum viable check:

```bash
docker compose up -d --build
docker compose logs -f bybit-monitor        # watch HEALTH lines
```

1. Let it run 24 continuous hours; do not restart the stack.
2. Verify every hour contains a health summary with
   `REST: healthy`, `Spot WS: connected`, `Telegram: healthy`.
3. Verify at least one Telegram message was actually delivered
   (transition, hourly, or listing alert).
4. Confirm no `event=telegram_send_failed` messages older than the
   retry schedule, and no unexpected crashes in the logs.
5. Record results in `SOAK.md` per section 18 of the plan.
