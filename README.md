# Bybit Live Momentum Monitor → Telegram

A resilient 24/7 monitor that watches all Bybit Spot + Linear (USDT/USDC)
markets, computes 1-hour price momentum, and alerts a Telegram channel
when **1–3 unique base coins** are up more than **5% in 1 hour**. When 4+
coins qualify, alerts are suppressed. It never trades, never accesses
private Bybit endpoints, and survives restarts, API outages, WebSocket
drops, and Telegram failures.

> Monitoring and notification only. No trading. No account data.

---

## Quick start (local)

```bash
# 1. Create a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
#    edit .env and set:
#    TELEGRAM_BOT_TOKEN=<bot token from @BotFather>
#    TELEGRAM_CHAT_ID=<chat/channel id>

# 4. Run the tests
pytest

# 5. Run the bot
python -m app.main
```

Monitoring-only mode (no Telegram credentials) is allowed when all alert
layers are disabled:

```
IMMEDIATE_TRANSITION_ALERTS=false
HOURLY_ACTIVE_ALERTS=false
LISTING_NOTIFICATIONS_ENABLED=false
```

---

## Docker deployment

```bash
docker compose up -d --build
docker compose logs -f monitor
```

- Persists state in the named volume `bybit_monitor_data` at `/data`.
- Runs as non-root user.
- Restart policy `unless-stopped`.
- Graceful shutdown on `SIGTERM`.
- Overrides `DATABASE_PATH=/data/bybit_monitor.sqlite` inside the
  container; configure Telegram via `docker-compose.yml` environment or a
  host `.env` file (Docker Compose reads `.env` automatically).

Healthcheck probes the process every 30 s after a 10 s start period.

---

## Configuration

All runtime behaviour is configuration-driven. See `.env.example` for the
full list and the `SPEC.md` for the specification. Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | Telegram bot token |
| `TELEGRAM_CHAT_ID` | (required) | Telegram chat/channel id |
| `ALERT_THRESHOLD_PERCENT` | `5.0` | 1-hour momentum threshold |
| `MIN/MAX_QUALIFYING_COINS` | `1` / `3` | group alert range |
| `REST_TICKER_POLL_SECONDS` | `10` | ticker polling interval |
| `SPOT_SAMPLE_SECONDS` | `60` | spot history sample interval |
| `ALERT_DEBOUNCE_SECONDS` | `20` | transition debounce |
| `ENABLE_SPOT` / `ENABLE_LINEAR_USDT` / `ENABLE_LINEAR_USDC` | `true` | market universe |
| `DATABASE_PATH` | `./data/bybit_monitor.sqlite` | SQLite location |
| `LOG_LEVEL` | `INFO` | logging verbosity |

Secrets never have hard-coded defaults and are redacted from logs.

---

## Project layout

```
app/
|-- main.py            application lifecycle
|-- config.py          pydantic-settings configuration
|-- logging_config.py  secret-redacting logging
|-- bybit/             REST + WebSocket ingestion, models, normalizer
|-- market/            discovery, price engine, momentum, deduplication
|-- alerts/            state machine, formatter, dispatcher
|-- telegram/          delivery client
|-- persistence/       SQLite database, migrations, repositories
`-- health/            health snapshot + observability

tests/
|-- unit/              unit tests
|-- integration/       mocked-Bybit integration tests
|-- fixtures/          shared test fixtures
`-- test_alert_scenarios.py  end-to-end alert scenarios

data/                  local SQLite store (gitignored)
```

---

## Architecture notes

1. REST is the source of truth for discovery and reconciliation.
2. WebSocket is the final primary source of live prices.
3. Alert decisions are based *only* on unique `baseCoin` counts.
4. Telegram delivery is decoupled via an async queue; a Telegram outage
   never stops market ingestion.
5. All important state (registry, spot history, alert state, listing
   events, notification records) is persisted in SQLite.

---

## Development

- Python 3.12+ (developed and tested on 3.14).
- `pytest` runs unit, integration, and scenario/chaos tests.
- Integration tests mock the Bybit HTTP API with `httpx` `MockTransport`
  and the WebSocket with `websockets`' `serve()`.
- Run a specific suite:
  ```bash
  pytest tests/unit -v
  pytest tests/integration -v
  pytest tests/test_alert_scenarios.py -v
  ```

---

## Soak test

`scripts/soak_test.py` can run a deterministic self-contained soak
scenario (fake data, accelerated time) for a confidence check. A full
24-hour production soak requires live credentials and a running stack;
see `docs/soak-test.md` for the runbook.

---

## Security

- Never commit `.env` (ignored by git).
- `.env.example` contains placeholders only.
- No secrets appear in logs (redaction filter).
- Container runs as non-root.

## License

Internal use. See the master implementation plan for the authoritative
specification (`bybit_telegram_momentum_master_implementation_plan.md`).