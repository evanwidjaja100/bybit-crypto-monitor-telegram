# Bybit Live Momentum Monitor → Telegram
## Master Implementation Plan for AI Coding Agent

**Document purpose:** This file is the authoritative implementation blueprint for building, testing, validating, and deploying the Bybit crypto momentum monitoring bot.

**Execution rule:** Do not skip phases. Do not mark a phase complete until its exit gate passes.

---

# 1. Product Objective

Build a resilient 24/7 monitoring bot that:

- Monitors all active Bybit Spot markets.
- Monitors all active Bybit Linear derivatives settled in:
  - USDT
  - USDC
- Detects newly listed instruments automatically.
- Detects Linear `PreLaunch` instruments.
- Calculates 1-hour price momentum.
- Identifies coins whose price increase is strictly greater than 5% over 1 hour.
- Groups markets by unique `baseCoin`.
- Sends Telegram alerts only when the total number of unique qualifying coins is between 1 and 3 inclusive.
- Suppresses the group alert when 4 or more unique coins qualify.
- Sends hourly active-state alerts when the 1–3 coin condition remains active.
- Survives restarts, API failures, WebSocket disconnects, and Telegram failures without losing important state.

This is a monitoring and notification system only.

It must **not** place trades, manage positions, or access private Bybit account endpoints.

---

# 2. Locked Business Rules

These rules are authoritative.

If implementation details conflict with these rules, the business rules win.

## 2.1 Supported market universe

Mandatory:

- Bybit Spot
- Bybit Linear USDT-settled derivatives
- Bybit Linear USDC-settled derivatives
- Linear perpetuals
- Linear futures
- Linear `PreLaunch` instruments for discovery

Not part of the initial production release:

- Options
- Automated trading
- Account balances
- Open positions
- Order placement

Inverse contracts should be architecturally possible through a feature flag but disabled by default.

---

## 2.2 Definition of a qualifying coin

A market qualifies when:

```python
change_1h > 5.0
```

This is a **strictly greater than** comparison.

Examples:

```text
+4.999%  -> does not qualify
+5.000%  -> does not qualify
+5.001%  -> qualifies
+8.500%  -> qualifies
```

Do not implement:

```python
change_1h >= 5.0
```

---

## 2.3 Definition of a unique coin

Alerts operate on unique `baseCoin`, not raw symbols.

Example:

```text
BTCUSDT Spot
BTCUSDT Linear
BTCUSDC Linear
```

All represent:

```text
BTC
```

Therefore they count as **one qualifying coin**.

---

## 2.4 Telegram group-alert range

Let:

```text
N = number of unique qualifying base coins
```

Behavior:

| Qualifying unique coins | Group alert |
|---:|---|
| 0 | OFF |
| 1 | ON |
| 2 | ON |
| 3 | ON |
| 4+ | OFF / SUPPRESSED |

Exact expression:

```python
alert_active = 1 <= qualifying_coin_count <= 3
```

---

## 2.5 Alert cadence

The system should support two alert layers.

### Live transition alert

Send when the market state transitions into the active range:

```text
0 -> 1-3
```

or:

```text
4+ -> 1-3
```

Use a short debounce period before sending.

Recommended default:

```text
20 seconds
```

### Hourly active-state alert

Once per hour:

```text
if qualifying count is 1-3
    send active-state Telegram snapshot
else
    send nothing
```

Do not generate more than one hourly active-state message for the same hourly bucket.

---

## 2.6 New-listing behavior

The bot should detect new markets through:

1. Bybit instrument discovery.
2. Linear `PreLaunch` status.
3. Bybit listing announcements where relevant.

A new market must enter monitoring automatically.

No application restart should be necessary.

---

# 3. Engineering Principles

The implementation must follow these rules:

1. Separate ingestion from business logic.
2. Separate alert decisions from Telegram delivery.
3. Persist important state.
4. Treat REST and WebSocket as independent components.
5. Use REST as the source of truth for discovery and reconciliation.
6. Use WebSocket as the final primary source of live prices.
7. Prove correctness with REST polling before adding WebSocket complexity.
8. Never use raw contract count for alert decisions.
9. Never hard-code the list of coins.
10. Never store production secrets in source control.
11. Never mark a phase complete without tests.
12. Never implement multiple major phases at once without validating the previous phase.

---

# 4. Recommended Technology Stack

Recommended:

```text
Python 3.12+
asyncio
httpx
websockets
aiosqlite
pydantic-settings
pytest
pytest-asyncio
Docker
Docker Compose
SQLite
```

Optional later:

```text
PostgreSQL
Prometheus
Grafana
Redis
```

Do not introduce optional infrastructure until the SQLite version is stable.

---

# 5. Recommended Repository Structure

```text
bybit-monitor/
|
|-- app/
|   |-- __init__.py
|   |-- main.py
|   |-- config.py
|   |
|   |-- bybit/
|   |   |-- __init__.py
|   |   |-- rest.py
|   |   |-- websocket.py
|   |   |-- models.py
|   |   `-- normalizer.py
|   |
|   |-- market/
|   |   |-- __init__.py
|   |   |-- discovery.py
|   |   |-- price_engine.py
|   |   |-- momentum.py
|   |   `-- deduplication.py
|   |
|   |-- alerts/
|   |   |-- __init__.py
|   |   |-- state_machine.py
|   |   |-- formatter.py
|   |   `-- dispatcher.py
|   |
|   |-- telegram/
|   |   |-- __init__.py
|   |   `-- client.py
|   |
|   |-- persistence/
|   |   |-- __init__.py
|   |   |-- database.py
|   |   |-- repository.py
|   |   `-- migrations.py
|   |
|   `-- health/
|       |-- __init__.py
|       `-- monitor.py
|
|-- tests/
|   |-- unit/
|   |-- integration/
|   |-- fixtures/
|   `-- test_alert_scenarios.py
|
|-- data/
|   `-- .gitkeep
|
|-- Dockerfile
|-- docker-compose.yml
|-- .env.example
|-- .gitignore
|-- requirements.txt
|-- README.md
`-- SPEC.md
```

---

# 6. Configuration Specification

All important runtime behavior must be configuration-driven.

Recommended environment variables:

```text
BYBIT_BASE_URL=https://api.bybit.com

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

ALERT_THRESHOLD_PERCENT=5.0

MIN_QUALIFYING_COINS=1
MAX_QUALIFYING_COINS=3

INSTRUMENT_REFRESH_SECONDS=300
ANNOUNCEMENT_REFRESH_SECONDS=300

REST_TICKER_POLL_SECONDS=10
SPOT_SAMPLE_SECONDS=60

IMMEDIATE_TRANSITION_ALERTS=true
HOURLY_ACTIVE_ALERTS=true

ALERT_DEBOUNCE_SECONDS=20
COMPOSITION_CHANGE_COOLDOWN_SECONDS=300

ENABLE_SPOT=true
ENABLE_LINEAR_USDT=true
ENABLE_LINEAR_USDC=true
ENABLE_INVERSE=false

DATABASE_PATH=/data/bybit_monitor.sqlite

LOG_LEVEL=INFO
```

Production secrets must never have hard-coded defaults.

---

# 7. Phase 1 — Repository and Application Foundation

## Goal

Create a clean, testable, asynchronous application skeleton.

## Required work

Implement:

- Project structure.
- Dependency management.
- Configuration loading.
- Logging initialization.
- Application startup.
- Graceful shutdown.
- Empty SQLite database initialization.
- Test framework.

## Required startup behavior

The application should:

```text
load config
-> initialize logging
-> initialize database
-> start application services
-> stay alive
```

## Required shutdown behavior

On:

```text
SIGINT
SIGTERM
```

perform:

```text
stop background loops
-> flush pending work
-> close HTTP sessions
-> close WebSockets
-> commit database state
-> close database
-> exit
```

## Implementation loop

```text
create skeleton
-> load configuration
-> start application
-> stop application
-> run tests
-> fix failures
-> repeat
```

## Required tests

- Valid environment loads successfully.
- Missing Telegram token fails clearly where appropriate.
- Invalid numeric configuration is rejected.
- Database initializes.
- SIGTERM exits cleanly.
- No secrets are logged.

## Exit gate

Do not proceed until:

- `pytest` passes.
- Application starts.
- Application shuts down cleanly.
- `.env` is ignored.
- No credentials exist in repository.

---

# 8. Phase 2 — Bybit REST Client

## Goal

Build a reliable REST interface before implementing market logic.

## Required methods

Implement methods equivalent to:

```python
get_spot_instruments()
get_linear_instruments(status="Trading")
get_linear_instruments(status="PreLaunch")
get_spot_tickers()
get_linear_tickers()
get_announcements()
get_server_time()
```

## REST requirements

Every request must handle:

- HTTP timeout.
- Connection errors.
- HTTP status errors.
- Bybit non-zero `retCode`.
- Invalid JSON.
- Missing fields.
- Schema changes.
- Retryable failures.
- Non-retryable failures.

## Retry strategy

Use:

```text
bounded retries
+
exponential backoff
+
jitter
```

Do not retry indefinitely.

## Linear pagination

Linear instrument discovery must paginate until no cursor remains.

Pseudo-loop:

```python
cursor = None
results = []

while True:
    page = fetch_linear_page(cursor=cursor)
    results.extend(page.items)

    if not page.next_cursor:
        break

    cursor = page.next_cursor
```

Never assume a single page contains all Linear contracts.

## Implementation loop

```text
request
-> validate HTTP response
-> validate Bybit response
-> parse
-> normalize
-> test pagination
-> simulate failure
-> verify retry behavior
-> repeat
```

## Required tests

- Spot instrument parsing.
- Linear instrument parsing.
- USDT settlement detection.
- USDC settlement detection.
- `Trading` detection.
- `PreLaunch` detection.
- Multiple-page Linear pagination.
- Empty cursor termination.
- Timeout retry.
- Non-zero `retCode`.
- Malformed response handling.

## Exit gate

REST client tests must all pass before market logic is introduced.

---

# 9. Phase 3 — Instrument Registry

## Goal

Maintain a persistent authoritative registry of discovered markets.

## Internal instrument model

Use a structure equivalent to:

```python
Instrument(
    category="linear",
    symbol="BTCUSDT",
    base_coin="BTC",
    quote_coin="USDT",
    settle_coin="USDT",
    contract_type="LinearPerpetual",
    status="Trading",
    launch_time=None,
    delivery_time=None,
    is_pre_listing=False,
)
```

## Instrument identity

Use:

```python
(category, symbol)
```

Do not identify instruments by symbol alone.

## Database table

Recommended:

```sql
CREATE TABLE instruments (
    category TEXT NOT NULL,
    symbol TEXT NOT NULL,
    base_coin TEXT NOT NULL,
    quote_coin TEXT,
    settle_coin TEXT,
    contract_type TEXT,
    status TEXT NOT NULL,
    launch_time INTEGER,
    delivery_time INTEGER,
    is_pre_listing INTEGER NOT NULL DEFAULT 0,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (category, symbol)
);
```

## Discovery loop

Every configured interval:

```text
fetch Spot
+
fetch Linear Trading
+
fetch Linear PreLaunch
-> normalize
-> compare with registry
-> insert new
-> update existing
-> detect status transitions
-> persist
-> publish internal discovery events
```

## First-start rule

On an empty database:

```text
seed existing instruments silently
```

Do not send hundreds of "new listing" alerts.

Only instruments first observed after initialization should be considered newly discovered.

## Required tests

- First startup seeds silently.
- Restart does not rediscover all markets as new.
- Newly appearing market generates discovery event.
- `PreLaunch -> Trading` transition is detected.
- Removed market is handled without corrupting registry.

## Exit gate

Repeated restarts must not create false new-listing events.

---

# 10. Phase 4 — REST Price Engine MVP

## Goal

Prove complete market monitoring using REST polling before adding WebSockets.

## Polling scope

Poll:

```text
Spot tickers
Linear tickers
```

Recommended initial interval:

```text
10 seconds
```

## Normalized ticker model

Use a model equivalent to:

```python
Ticker(
    category="linear",
    symbol="BTCUSDT",
    last_price=0.0,
    mark_price=None,
    index_price=None,
    prev_price_1h=None,
    change_24h=None,
    turnover_24h=None,
    volume_24h=None,
    open_interest=None,
    funding_rate=None,
    timestamp=0,
)
```

## Required normalization rules

- Convert numeric strings safely.
- Reject non-positive prices for momentum calculations.
- Preserve `None` for unavailable fields.
- Never silently convert invalid values to zero.

## Implementation loop

```text
fetch
-> normalize
-> validate
-> update latest market state
-> count processed markets
-> log anomalies
-> repeat
```

## Required runtime metrics

At minimum log periodic counts for:

```text
Spot instruments
Linear USDT instruments
Linear USDC instruments
ticker rows received
ticker rows accepted
ticker rows rejected
```

## Exit gate

Observed counts must agree with REST responses and no supported settlement currency may be silently omitted.

---

# 11. Phase 5 — One-Hour Momentum Engine

## Goal

Calculate correct 1-hour percentage change for all supported markets.

---

## 11.1 Linear derivatives

For Linear derivatives:

```python
change_1h = ((last_price / prev_price_1h) - 1) * 100
```

Only calculate when:

```python
last_price > 0 and prev_price_1h > 0
```

Otherwise:

```text
change_1h = unavailable
```

---

## 11.2 Spot

Spot requires locally maintained history.

Persist approximately one sample per minute.

Recommended table:

```sql
CREATE TABLE price_samples (
    category TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (category, symbol, timestamp)
);
```

Recommended retention:

```text
90-120 minutes minimum
```

At time:

```text
T
```

target:

```text
T - 3600 seconds
```

Select the closest valid historical sample within a defined tolerance.

Recommended tolerance:

```text
+/- 90 seconds
```

If a valid 1-hour anchor does not exist:

```text
status = WARMING_UP
```

Do not fabricate a result.

---

## 11.3 Persistence

Spot history must survive restart.

The system should not require another full hour of runtime after every application restart if usable history is already persisted.

---

## Required mathematical tests

```text
100 -> 105.000 = +5.000%  -> does NOT qualify
100 -> 105.001 = +5.001%  -> qualifies
100 -> 110     = +10.000%
200 -> 210     = +5.000%   -> does NOT qualify
10  -> 9       = -10.000%
```

## Floating-point rule

Threshold logic should use the calculated numeric percentage directly.

If necessary, use `Decimal` or another deterministic strategy in tests to avoid boundary ambiguity.

## Implementation loop

```text
receive price
-> persist/downsample if needed
-> find 1h reference
-> calculate change
-> validate
-> compare expected result
-> repeat
```

## Exit gate

All boundary tests must pass.

Exactly +5.000% must never qualify.

---

# 12. Phase 6 — Unique-Coin Aggregation

## Goal

Convert qualifying instruments into qualifying unique base coins.

## Example

Input:

```text
XYZUSDT Spot     +5.9%
XYZUSDT Linear   +8.4%
XYZUSDC Linear   +8.1%
ABCUSDT Linear   +6.2%
```

Expected unique coins:

```text
XYZ
ABC
```

Expected count:

```text
2
```

Not:

```text
4
```

## Representative-market selection

Keep all qualifying markets internally.

Select one representative market per base coin.

Primary rule:

```text
highest valid 1h increase
```

Tie-break preference:

```text
USDT Linear
-> USDC Linear
-> stablecoin Spot
-> other Spot
```

## Example representative result

```text
XYZ
representative: XYZUSDT Linear +8.4%
other qualifying markets:
- XYZUSDC Linear +8.1%
- XYZ Spot +5.9%
```

## Implementation loop

```text
qualifying instruments
-> group by baseCoin
-> deduplicate
-> rank markets
-> select representative
-> calculate unique count
-> repeat
```

## Required tests

```text
BTCUSDT +6%, BTCUSDC +7% -> unique count 1
BTC + ETH                -> unique count 2
BTC + ETH + SOL          -> unique count 3
BTC + ETH + SOL + DOGE   -> unique count 4
```

## Exit gate

Alert decisions must consume only the unique-coin result.

Raw contract count must never reach the alert policy.

---

# 13. Phase 7 — Alert State Machine

## Goal

Implement the 1-to-3 qualifying coin rule exactly.

## States

```text
EMPTY
ACTIVE_RANGE
OVER_RANGE
```

Definitions:

```text
EMPTY        = 0 qualifying coins
ACTIVE_RANGE = 1-3 qualifying coins
OVER_RANGE   = 4+ qualifying coins
```

## Basic transitions

```text
EMPTY -> ACTIVE_RANGE
    send live transition alert

ACTIVE_RANGE -> ACTIVE_RANGE
    do not send immediate duplicate by default

ACTIVE_RANGE -> OVER_RANGE
    suppress range alerts

OVER_RANGE -> ACTIVE_RANGE
    send live transition alert

ACTIVE_RANGE -> EMPTY
    reset active state

OVER_RANGE -> EMPTY
    remain silent
```

---

## 13.1 Debounce

Before sending a transition into `ACTIVE_RANGE`, require the state to remain valid for the configured debounce period.

Recommended:

```text
20 seconds
```

Example noisy behavior:

```text
5.01%
4.99%
5.02%
4.98%
```

should not create repeated Telegram messages.

---

## 13.2 Composition changes

Track a fingerprint:

```python
tuple(sorted(unique_base_coins))
```

Example:

```text
BTC + ETH
```

changes to:

```text
BTC + SOL
```

even though count stays at 2.

Composition-change messages should be optional and cooldown-controlled.

Recommended cooldown:

```text
5 minutes
```

Do not allow composition churn to generate Telegram spam.

---

## 13.3 Hourly active-state logic

Once per hourly bucket:

```text
calculate unique qualifying set
-> if count is 1-3
   -> send hourly snapshot
-> otherwise
   -> send nothing
```

Persist the last hourly bucket that generated a message.

Restarting the bot must not duplicate the same hourly message.

---

## Required state-machine tests

```text
0 -> no alert
1 -> alert
2 -> alert
3 -> alert
4 -> no alert
10 -> no alert
```

Transitions:

```text
0 -> 1  = alert
1 -> 2  = no immediate duplicate unless configured composition update
2 -> 3  = no immediate duplicate unless configured composition update
3 -> 4  = suppress
4 -> 3  = alert
3 -> 0  = reset
```

## Exit gate

This phase is mandatory green before Telegram integration.

---

# 14. Phase 8 — Telegram Integration

## Goal

Deliver reliable, readable notifications without allowing Telegram failures to affect market ingestion.

## Required client behavior

Implement:

```python
TelegramClient.send_message()
```

with:

- timeout
- retry
- bounded backoff
- structured error handling
- safe message splitting
- no secret logging

## Alert-delivery architecture

Do not send Telegram messages directly from the market calculation loop.

Use:

```text
market event
-> alert decision
-> persistent/outgoing alert record
-> Telegram queue
-> Telegram dispatcher
```

A Telegram outage must not stop Bybit monitoring.

---

## Recommended alert format

Example:

```text
🚨 BYBIT 1H MOMENTUM ALERT

2 / 3 qualifying coins

🔥 XYZ
USDT Perpetual
1H: +9.42%
Price: $0.08421
24H: +18.30%
Mark: $0.08410
Funding: +0.018%
24H Turnover: $42.8M

🔥 ABC
USDC Perpetual
1H: +6.17%
Price: $1.284
24H: +8.92%

Rule:
1-3 unique coins > +5% / 1H

Updated: YYYY-MM-DD HH:MM UTC
```

For one coin:

```text
1 / 3 qualifying coins
```

For three:

```text
3 / 3 qualifying coins
```

Never produce:

```text
4 / 3
```

because 4+ is suppressed.

---

## Telegram test requirements

Test:

- One qualifying coin.
- Two qualifying coins.
- Three qualifying coins.
- Long message splitting.
- Retry after transient failure.
- Telegram failure while monitoring continues.
- Message formatting on a real Telegram client.

## Security rules

`.env.example` may contain:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

No real credentials may appear in:

- Git
- README
- Dockerfile
- docker-compose.yml
- logs
- test fixtures
- screenshots

## Exit gate

Controlled test notifications must succeed and monitoring must remain alive during simulated Telegram failure.

---

# 15. Phase 9 — New Listing Detection

## Goal

Automatically detect and onboard new Bybit markets.

## Signals

Use three independent signals.

### Signal A — Instrument registry

Authoritative for currently monitorable markets.

### Signal B — Linear PreLaunch

Detect upcoming derivatives before normal trading begins.

### Signal C — Bybit announcements

Poll announcements periodically and identify listing-related events.

---

## Listing state machine

Recommended states:

```text
UNKNOWN
ANNOUNCED
PRELAUNCH
TRADING
MONITORED
```

Not every listing will pass through all states.

The implementation must support:

```text
UNKNOWN -> TRADING
```

directly.

---

## Listing event persistence

Recommended table:

```sql
CREATE TABLE listing_events (
    event_key TEXT PRIMARY KEY,
    category TEXT,
    symbol TEXT,
    event_type TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    telegram_sent INTEGER NOT NULL DEFAULT 0
);
```

## Idempotency

A listing event must not resend because of restart.

## Discovery loop

```text
poll announcements
+
poll instruments
-> compare with previous state
-> detect transition
-> persist event
-> notify once if configured
-> reconcile monitoring registry
-> repeat
```

## Required tests

- New `PreLaunch` market.
- `PreLaunch -> Trading`.
- Direct `UNKNOWN -> Trading`.
- Restart after event.
- Duplicate API result.
- Delisted/removed market.

## Exit gate

No duplicate listing storm may occur after restart.

---

# 16. Phase 10 — WebSocket Live Upgrade

## Goal

Replace REST polling as the primary live price feed without changing the already-tested business logic.

## Final data architecture

```text
                 REST
                  |
       discovery/reconciliation
                  |
                  v
             Market State
                  ^
                  |
              WebSocket
              live tickers
```

REST remains required.

WebSocket does not replace discovery or recovery logic.

---

## Required WebSocket connections

Maintain separate public connections for:

```text
Spot
Linear
```

## Subscription generation

Generate ticker topics from the current instrument registry.

Example:

```text
tickers.BTCUSDT
tickers.ETHUSDT
...
```

Batch subscription requests according to Bybit limits.

When a new instrument becomes tradable:

```text
registry detects market
-> subscription manager updates
-> new ticker topic is subscribed
-> monitoring starts
```

No restart allowed.

---

## Delta-handling requirement

If a derivatives ticker update is a delta:

```python
latest = previous.copy()
latest.update(delta)
```

Do not replace the full ticker snapshot with a partial delta.

---

## Heartbeat

Maintain heartbeat/ping logic.

Recommended interval:

```text
approximately 20 seconds
```

## Reconnection loop

```text
connected
-> process data
-> detect disconnect/staleness
-> mark connection unhealthy
-> reconnect with bounded backoff
-> rebuild subscriptions
-> run REST reconciliation
-> continue
```

## Stale-stream watchdog

Track:

```text
last_spot_message_at
last_linear_message_at
```

If a stream becomes unexpectedly stale:

```text
mark unhealthy
-> activate REST fallback
-> reconnect WebSocket
```

## Exit gate

Manually break the WebSocket/network.

The system must:

- recover automatically
- restore subscriptions
- preserve state
- avoid duplicate alerts caused by reconnection

---

# 17. Phase 11 — Reliability and Persistence

## Goal

Ensure the bot can run continuously.

## SQLite configuration

Recommended:

```text
WAL mode
foreign keys ON
transactions
indexes
busy timeout
periodic cleanup
```

## Persist at minimum

- Instrument registry.
- Spot price history.
- Alert state.
- Alert fingerprint.
- Last hourly alert bucket.
- Listing events.
- Sent notification records.
- Important health timestamps.

## Important rule

Do not rely only on volatile memory such as:

```python
last_alert = {}
```

for behavior that must survive restart.

## Graceful shutdown

On termination:

```text
stop loops
-> stop accepting new queue work
-> flush outgoing alerts
-> commit database
-> close WebSockets
-> close HTTP clients
-> exit
```

## Required recovery tests

- Restart after alert.
- Restart while active range is 1-3.
- Restart during 4+ suppressed state.
- Restart after new-listing notification.
- Restart with Spot price history.
- Database busy/lock simulation.

## Exit gate

Restart must not create duplicate alerts or lose required state.

---

# 18. Phase 12 — Health Monitoring and Observability

## Goal

Make unattended operation diagnosable.

## Structured logging

Examples:

```text
event=ticker_update
category=linear
symbol=BTCUSDT
```

```text
event=alert_decision
qualifying_count=2
state=ACTIVE_RANGE
action=SEND
```

```text
event=ws_reconnect
stream=linear
attempt=3
```

Do not log every high-frequency WebSocket tick in production.

---

## Health state

Track:

```text
Bybit REST healthy?
Spot WebSocket healthy?
Linear WebSocket healthy?
Telegram healthy?
Database healthy?
Last Spot ticker age
Last Linear ticker age
Last discovery age
Active Spot instrument count
Active Linear USDT count
Active Linear USDC count
Qualifying unique coin count
Telegram queue depth
```

## Periodic health summary

Example:

```text
HEALTH
------
Spot instruments: X
Linear USDT: Y
Linear USDC: Z
Spot WS: connected
Linear WS: connected
REST: healthy
Telegram: healthy
Qualifying coins: 2
Telegram queue: 0
Last discovery: 48s ago
```

## Implementation loop

```text
run bot
-> inspect logs
-> create controlled failure
-> verify logs explain failure
-> improve diagnostics
-> repeat
```

## Exit gate

A developer reading logs should be able to identify:

- which subsystem failed
- when it failed
- whether fallback activated
- whether it recovered

---

# 19. Phase 13 — Comprehensive Automated Testing

## Goal

Prove business rules and resilience before deployment.

---

## 19.1 Unit tests

Mandatory unit coverage:

- Percentage calculation.
- Strict 5% threshold.
- Spot 1-hour anchor selection.
- Linear `prevPrice1h` calculation.
- Unique `baseCoin` grouping.
- Representative-market selection.
- 1-3 range logic.
- 4+ suppression.
- State-machine transitions.
- Debounce.
- Cooldowns.
- Pagination.
- Config validation.
- Telegram formatting.
- Listing event idempotency.

---

## 19.2 Integration tests

Mock the Bybit API.

Test:

```text
REST
-> normalizer
-> momentum engine
-> unique coin aggregator
-> state machine
-> Telegram queue
```

Also test:

```text
WebSocket
-> market state
-> momentum engine
-> alert decision
```

---

## 19.3 Critical alert scenario

Input:

```text
BTC +6%
ETH +7%
SOL +8%
```

Expected:

```text
qualifying count = 3
alert = YES
```

Add:

```text
DOGE +5.5%
```

Expected:

```text
qualifying count = 4
alert = NO
state = OVER_RANGE
```

Drop ETH below threshold.

Remaining:

```text
BTC +6%
SOL +8%
DOGE +5.5%
```

Expected:

```text
qualifying count = 3
state returns to ACTIVE_RANGE
alert = YES
```

---

## 19.4 Cross-market duplicate scenario

Input:

```text
XYZ Spot   +6%
XYZ USDT   +9%
XYZ USDC   +8%
```

Expected:

```text
unique coins = 1
representative = XYZ USDT +9%
```

---

## 19.5 Restart scenario

1. Trigger alert.
2. Stop bot.
3. Restart bot.
4. Feed unchanged market state.

Expected:

```text
no duplicate live transition alert caused only by restart
```

---

## 19.6 Listing scenario

Inject:

```text
ABCUSDT PreLaunch
```

Then:

```text
ABCUSDT Trading
```

Expected:

- One prelaunch event.
- One trading-live event.
- Automatic monitoring.
- No duplicate event after restart.

---

## 19.7 Chaos scenarios

Simulate:

```text
Bybit HTTP timeout
Bybit HTTP 5xx
Bybit non-zero retCode
invalid JSON
WebSocket disconnect
WebSocket stale stream
Telegram timeout
Telegram HTTP 429
SQLite busy/lock
application restart
```

The application must recover or fail safely.

## Exit gate

All mandatory unit, integration, state-machine, restart, and chaos tests must pass.

---

# 20. Phase 14 — Docker and Deployment

## Goal

Make deployment reproducible and persistent.

## Required files

```text
Dockerfile
docker-compose.yml
.env.example
```

## Container requirements

- Run as non-root.
- Persistent `/data` volume.
- Graceful SIGTERM.
- Restart policy.
- Healthcheck.
- No secrets baked into image.

Recommended:

```yaml
restart: unless-stopped
```

## Runtime layout

```text
container
|
|-- application
`-- /data
    `-- bybit_monitor.sqlite
```

## Deployment test

Perform:

```text
docker compose up
-> verify monitoring
-> restart container
-> verify state persistence
-> stop/start stack
-> verify no duplicate listing or momentum storm
```

## Exit gate

Docker restart must preserve:

- database
- Spot history
- alert state
- listing state

---

# 21. Phase 15 — 24-Hour Soak Test

## Goal

Validate stability under realistic continuous operation.

Run continuously for at least 24 hours.

Monitor:

```text
instrument counts
memory usage
CPU usage
database size
database cleanup
WebSocket reconnects
REST errors
Telegram retries
duplicate alerts
Spot history quality
new-listing reconciliation
alert-state transitions
```

## Required interventions during soak test

Perform at least:

- Several manual container restarts.
- One temporary network interruption.
- One forced WebSocket reconnect.
- One Telegram failure simulation if possible.
- One application restart during an active 1-3 state.

## Acceptance behavior

After restart or reconnect:

```text
instrument registry restores
Spot history restores
alert state restores
WebSockets reconnect
subscriptions restore
REST reconciliation executes
no false new-listing storm
no duplicate Telegram storm
```

## Exit gate

Document soak-test findings before marking production-ready.

---

# 22. Phase 16 — Final Acceptance Checklist

The AI agent may mark the project `DONE` only when every applicable item is verified.

## Market coverage

- [ ] All active Bybit Spot markets are discovered.
- [ ] All Linear USDT-settled contracts are discovered.
- [ ] All Linear USDC-settled contracts are discovered.
- [ ] Linear pagination works across all pages.
- [ ] Linear `PreLaunch` instruments are detected.
- [ ] Newly trading instruments are automatically monitored.
- [ ] No restart is required for newly listed instruments.

## Momentum logic

- [ ] Linear 1-hour percentage is calculated correctly.
- [ ] Spot 1-hour percentage uses persisted history.
- [ ] Spot warm-up state is handled safely.
- [ ] Exactly +5.000% does not qualify.
- [ ] Any value greater than +5.000% qualifies.

## Unique coin logic

- [ ] Duplicate markets for one `baseCoin` count once.
- [ ] Representative market selection works.
- [ ] Raw contract count is never used for the range rule.

## Alert rules

- [ ] 0 unique qualifying coins = no group alert.
- [ ] 1 unique qualifying coin = alert.
- [ ] 2 unique qualifying coins = alert.
- [ ] 3 unique qualifying coins = alert.
- [ ] 4+ unique qualifying coins = suppressed.
- [ ] 4+ returning to 1-3 can reactivate alerts.
- [ ] Debounce works.
- [ ] Cooldown works.
- [ ] Hourly active-state messages work.
- [ ] Hourly state survives restart.

## Telegram

- [ ] Telegram messages are readable.
- [ ] Long messages split safely.
- [ ] Telegram outage does not stop monitoring.
- [ ] Telegram retries are bounded.
- [ ] Secrets never appear in logs.

## Reliability

- [ ] REST failures do not crash the bot.
- [ ] WebSocket disconnects recover automatically.
- [ ] Stale WebSocket detection works.
- [ ] REST fallback/reconciliation works.
- [ ] Alert state survives restart.
- [ ] Listing state survives restart.
- [ ] Spot history survives restart.
- [ ] SQLite persistence works.
- [ ] Docker persistence works.

## Security

- [ ] No production secrets exist in Git.
- [ ] `.env` is ignored.
- [ ] `.env.example` contains placeholders only.
- [ ] Container runs as non-root.
- [ ] No secrets appear in documentation or screenshots.

## Testing

- [ ] Unit tests pass.
- [ ] Integration tests pass.
- [ ] State-machine tests pass.
- [ ] Pagination tests pass.
- [ ] Restart tests pass.
- [ ] Listing tests pass.
- [ ] Chaos/recovery tests pass.
- [ ] 24-hour soak test passes.

---

# 23. Mandatory Agent Execution Loop

The coding agent must follow this loop for every phase.

```text
READ CURRENT PHASE
        |
        v
IMPLEMENT SMALLEST WORKING SLICE
        |
        v
RUN TESTS
        |
        v
INSPECT ACTUAL RESULTS
        |
        v
IDENTIFY FAILURES
        |
        v
FIX ROOT CAUSE
        |
        v
RUN TESTS AGAIN
        |
        v
DOCUMENT RESULT
        |
        v
ONLY THEN MOVE TO NEXT PHASE
```

Do not:

```text
implement several phases
-> run one superficial test
-> claim completion
```

---

# 24. Mandatory Phase Completion Report

After every phase, the AI agent must output:

```text
PHASE:
STATUS:

FILES CREATED:
- ...

FILES CHANGED:
- ...

WHAT WAS IMPLEMENTED:
- ...

TESTS RUN:
- ...

TEST RESULTS:
- ...

MANUAL VALIDATION:
- ...

KNOWN ISSUES:
- ...

ASSUMPTIONS:
- ...

TECHNICAL DEBT:
- ...

NEXT PHASE:
- ...
```

If tests fail:

```text
STATUS: BLOCKED
```

Do not proceed until the blocking failure is resolved unless explicitly authorized.

---

# 25. Final Runtime Architecture

The final production architecture should resemble:

```text
                         BYBIT
                           |
             +-------------+-------------+
             |                           |
           REST                      WEBSOCKET
             |                           |
     +-------+--------+          +-------+-------+
     |       |        |          |               |
 Instruments Tickers Listings   Spot           Linear
     |                          ticker WS       ticker WS
     |                                           |
     +----------------+--------------------------+
                      |
                      v
               NORMALIZED MARKET
                    STATE
                      |
          +-----------+------------+
          |                        |
    SPOT 1H HISTORY        DERIVATIVE 1H
          |                prevPrice1h
          +-----------+------------+
                      |
                      v
                MOMENTUM ENGINE
                      |
                 change > 5%
                      |
                      v
              GROUP BY baseCoin
                      |
                      v
            UNIQUE QUALIFYING COINS
                      |
          +-----------+-----------+
          |           |           |
          0          1-3          4+
          |           |           |
       SILENT       ACTIVE      SUPPRESS
                      |
                      v
                STATE MACHINE
                      |
          +-----------+-----------+
          |                       |
      live transition        hourly active
          |                       |
          +-----------+-----------+
                      |
                      v
               TELEGRAM QUEUE
                      |
                      v
                  TELEGRAM
```

---

# 26. Architectural Boundaries

The final system must keep these concerns separate.

## Bybit ingestion

Responsible only for:

- REST requests.
- WebSocket connections.
- Reconnection.
- Parsing.
- Normalization.

It must not decide whether Telegram should alert.

## Instrument registry

Responsible for:

- Instrument identity.
- Status.
- Settlement currency.
- Listing lifecycle.
- Discovery.

## Price engine

Responsible for:

- Latest price state.
- Spot history.
- Derivative reference prices.
- 1-hour calculations.

## Momentum engine

Responsible for:

```text
change_1h > threshold
```

only.

## Deduplication engine

Responsible for:

```text
instrument -> unique baseCoin
```

## Alert state machine

Responsible for:

```text
0
1-3
4+
```

and transition/cooldown behavior.

## Telegram dispatcher

Responsible only for delivery.

This separation is mandatory because it makes the system testable and prevents API/network failures from contaminating alert logic.

---

# 27. Definition of Done

The bot is not considered complete because:

```text
it runs
```

or because:

```text
a Telegram message was received
```

It is complete only when:

1. Market discovery is comprehensive.
2. Pagination is correct.
3. New listings are automatic.
4. Momentum calculations are mathematically correct.
5. Spot history survives restart.
6. Unique-coin aggregation is correct.
7. The 1-3 range rule passes all tests.
8. 4+ suppression works.
9. Telegram delivery is decoupled.
10. WebSocket recovery works.
11. REST fallback works.
12. Persistent state prevents duplicate alerts.
13. Docker deployment survives restart.
14. Automated tests pass.
15. Chaos tests pass.
16. The 24-hour soak test passes.
17. Final acceptance checklist is complete.

---

# 28. Handoff Requirements

When implementation is complete, prepare a review package containing:

```text
project ZIP
+
full AI-agent implementation session/transcript
+
final test output
+
24-hour soak-test notes
+
README deployment instructions
```

The ZIP should not include:

```text
.env
Telegram token
Telegram chat ID
private credentials
temporary cache files
unnecessary virtual environments
```

Recommended review package:

```text
bybit-monitor.zip
agent-session.txt or agent-session.md
test-results.txt
soak-test.md
```

This package will be used for an independent code and implementation audit.

---

# 29. Final Instruction to the Coding Agent

**Accuracy is more important than speed.**

Do not optimize early.

First prove:

```text
discovery correctness
-> momentum correctness
-> unique-coin correctness
-> alert-state correctness
-> persistence correctness
```

Then add:

```text
WebSocket performance
-> deployment
-> operational hardening
```

If any implementation decision is uncertain, prefer the design that is:

1. Easier to test.
2. Easier to recover.
3. Easier to inspect.
4. Less likely to produce duplicate alerts.
5. Less likely to silently miss newly listed markets.

The authoritative Telegram alert condition remains:

```python
1 <= unique_qualifying_base_coins <= 3
```

where each qualifying base coin has at least one supported Bybit market with:

```python
change_1h > 5.0
```

This requirement must never be weakened, approximated, or replaced by raw symbol count.
