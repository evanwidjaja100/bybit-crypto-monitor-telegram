# SPEC — Bybit Live Momentum Monitor → Telegram

Derived from `bybit_telegram_momentum_master_implementation_plan.md`
(the authoritative blueprint). This document captures the locked
specification of the product.

## 1. Objective

A resilient 24/7 monitoring bot that:

- Monitors all active Bybit Spot markets.
- Monitors all active Bybit Linear derivatives settled in USDT and USDC.
- Detects newly listed instruments automatically (including Linear
  `PreLaunch`).
- Calculates 1-hour price momentum per market.
- Emits Telegram alerts when the number of **unique qualifying base
  coins** is between 1 and 3 (inclusive) and each has at least one
  supported market with `change_1h > 5.0`.
- Suppresses the group alert when 4 or more unique coins qualify.
- Sends hourly active-state snapshots while the 1–3 condition holds.
- Survives restarts, API failures, WebSocket disconnects, and Telegram
  outages without losing important state.

It is a **monitoring and notification system only**. It never places
trades, manages positions, or accesses private Bybit endpoints.

## 2. Locked business rules

| Rule | Value |
|---|---|
| Qualifying market | `change_1h > 5.0` (strictly greater) |
| Unique coin | one per `baseCoin`, across Spot + Linear markets of that coin |
| Group alert range | `1 <= unique_qualifying_base_coins <= 3` |
| Suppression | 4+ unique qualifying coins ⇒ no group alert |
| Live transition alert | on `0 -> 1-3` or `4+ -> 1-3`, after debounce (20 s default) |
| Debounce | require state to persist `ALERT_DEBOUNCE_SECONDS` before sending |
| Hourly active-state alert | once per hourly bucket when count is 1–3 |
| Composition change | optional, cooldown-controlled (300 s default) |
| New listings | auto-discovered, no restart required |

### 2.1 Qualifying market — exact test vector

| from | to | change_1h | qualifies |
|---|---|---|---|
| 100 | 105.000 | +5.000% | no |
| 100 | 105.001 | +5.001% | yes |
| 100 | 110 | +10.000% | yes |
| 200 | 210 | +5.000% | no |
| 10 | 9 | -10.000% | no |

## 3. Market universe

Mandatory: Spot, Linear perpetuals, Linear futures, Linear USDT-settled,
Linear USDC-settled, Linear `PreLaunch` (discovery).

Inverse contracts are architecturally supported behind
`ENABLE_INVERSE=false` (disabled by default).

## 4. Momentum calculation

- **Linear:** `change_1h = ((last_price / prev_price_1h) - 1) * 100`
  only when `last_price > 0 and prev_price_1h > 0`, else unavailable.
- **Spot:** locally maintained, persisted history. A sample is recorded
  per symbol approximately every `SPOT_SAMPLE_SECONDS`. The anchor is the
  sample closest to `T - 3600` seconds within `+/- 90 s` tolerance. If no
  valid anchor exists the market is `WARMING_UP`; no result is fabricated.
- Spot history retention default is 120 minutes and survives restarts.

## 5. Representative market selection

Qualifying markets are grouped by `baseCoin`, then ranked by:

1. highest valid 1h increase
2. tie-break: USDT Linear → USDC Linear → stablecoin Spot → other Spot

All qualifying markets are retained internally; only the representative
is used for formatting.
## 6. Alert state machine

States: `EMPTY`, `ACTIVE_RANGE`, `OVER_RANGE` (plus an internal
`PENDING_ACTIVE` debounce state).

Transitions (with debounce applied on entry into the active range):

| from | to | action |
|---|---|---|
| EMPTY | ACTIVE_RANGE | live transition alert |
| ACTIVE_RANGE | ACTIVE_RANGE | no immediate duplicate |
| ACTIVE_RANGE | OVER_RANGE | suppress |
| OVER_RANGE | ACTIVE_RANGE | live transition alert |
| ACTIVE_RANGE | EMPTY | reset active state |
| OVER_RANGE | EMPTY | silent |

Persistence: state, fingerprint, debounce start, last hourly bucket,
last composition message — all stored in SQLite so restart does not
duplicate alerts.

## 7. Configuration

All runtime behaviour is configuration-driven (see `.env.example`):

```
BYBIT_BASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
ALERT_THRESHOLD_PERCENT, MIN_QUALIFYING_COINS, MAX_QUALIFYING_COINS,
INSTRUMENT_REFRESH_SECONDS, ANNOUNCEMENT_REFRESH_SECONDS,
REST_TICKER_POLL_SECONDS, SPOT_SAMPLE_SECONDS,
IMMEDIATE_TRANSITION_ALERTS, HOURLY_ACTIVE_ALERTS,
ALERT_DEBOUNCE_SECONDS, COMPOSITION_CHANGE_COOLDOWN_SECONDS,
ENABLE_SPOT, ENABLE_LINEAR_USDT, ENABLE_LINEAR_USDC, ENABLE_INVERSE,
DATABASE_PATH, LOG_LEVEL
```

Secrets never have hard-coded defaults. Start-up fails fast when an alert
layer is enabled without Telegram credentials.

## 8. Persistence (SQLite)

- `instruments` — authoritative registry, keyed `(category, symbol)`.
- `price_samples` — spot 1h history.
- `alert_state` — single-row alert machine state.
- `listing_events` — idempotent listing lifecycle events.
- `outgoing_notifications` — audit trail of messages.
- `kv` — generic key/value state.
- `schema_migrations` — migration versions.

SQLite runs with WAL, foreign keys, busy timeout, and transactions.

## 9. Resilience

- REST: bounded retries, exponential backoff + jitter, hard timeouts.
- WebSocket: separate Spot/Linear connections, batched subscriptions,
  heartbeat (20 s), stale-stream watchdog, bounded-backoff reconnect,
  REST reconciliation after reconnect, delta-field merging.
- Telegram: async queue decoupled from ingestion; bounded retries;
  failure never stops monitoring.
- Graceful SIGINT/SIGTERM shutdown with database commit + flush.

## 10. Security

- No production secrets in git.
- `.env` ignored; `.env.example` placeholders only.
- Secret-redacting log filter.
- Container runs as non-root with a persistent `/data` volume.

## 11. Definition of done

See the master plan §27: discovery, pagination, listings, momentum,
spot history persistence, unique-coin aggregation, 1–3 rule, 4+
suppression, decoupled Telegram delivery, WebSocket recovery, REST
fallback, restart-safe state, Docker persistence, automated + chaos
tests, 24-hour soak test, and the final acceptance checklist.