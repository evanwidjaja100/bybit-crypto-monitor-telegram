# Bybit Live Momentum Monitor → Telegram
## Final Production-Readiness Implementation Plan

**Document purpose:** This file is the authoritative final remediation and release-readiness plan for the current Bybit monitoring repository.

**Starting project state:** `REMEDIATED RELEASE CANDIDATE — NOT YET PRODUCTION READY`

**Primary objective:** Fix the remaining correctness and concurrency issues, validate the real production paths, complete a clean live staging cycle and a fresh ≥24-hour soak, then perform final acceptance before the bot may be declared production-ready.

---

# 1. Non-Negotiable Product Rules

The final production bot must preserve these rules exactly.

## 1.1 Market universe

Mandatory:

- Bybit Spot.
- Bybit Linear USDT-settled derivatives.
- Bybit Linear USDC-settled derivatives.
- Linear perpetuals.
- Linear futures.
- Linear `PreLaunch` discovery.
- Automatic new instrument discovery.
- Automatic monitoring of newly tradable instruments.

Out of scope for this release:

- Options.
- Trading.
- Orders.
- Positions.
- Balances.
- Private Bybit endpoints.

### Inverse contracts

The current release must choose **one** of the following:

```text
OPTION A — Recommended:
remove/disable public inverse support completely

OR

OPTION B:
implement inverse end-to-end and validate it fully
```

Do not leave a configuration flag that implies support when the full price and WebSocket paths do not exist.

For this production release, **Option A is preferred**.

---

## 1.2 Momentum qualification

The rule is:

```python
change_1h > 5.0
```

No rounding may happen before qualification.

Examples:

```text
+5.0000000000%  -> does NOT qualify
+5.0000000001%  -> qualifies
+7.5%           -> qualifies
```

Rounding is allowed only for:

```text
display
logs
Telegram formatting
```

Never for the business decision.

---

## 1.3 Unique-coin aggregation

All markets for the same `baseCoin` count once.

Example:

```text
BTCUSDT Spot
BTCUSDT Linear
BTCUSDC Linear
```

equals:

```text
BTC = 1 unique coin
```

---

## 1.4 Telegram range rule

```python
1 <= unique_qualifying_base_coins <= 3
```

| Count | Behavior |
|---:|---|
| 0 | no range alert |
| 1 | active |
| 2 | active |
| 3 | active |
| 4+ | suppress |

---

## 1.5 Debounce rule

A transition into:

```text
ACTIVE_RANGE
```

must survive the configured debounce period before a user-facing transition alert may be sent.

Default:

```text
20 seconds
```

No hourly or composition alert may bypass this debounce.

---

# 2. Final Known Issues to Resolve

The current repository has already repaired the original major audit findings. This plan targets the remaining issues.

Priority classification:

```text
P0 = production blocker
P1 = required before soak
P2 = required before final acceptance
```

---

## P0-1 — SQLite transaction ownership is not task-aware

Current design uses:

```python
self._in_transaction: bool
```

A global Boolean cannot distinguish:

```text
Task A owns the transaction
```

from:

```text
Task B observes that a transaction exists
```

This can allow another asyncio task to bypass the connection lock while the first task owns an open SQLite transaction.

### Required invariant

While Task A owns a transaction:

```text
no other task may issue SQL on the same connection
```

Only the actual transaction-owning task may perform reentrant SQL operations inside that transaction.

---

## P1-1 — Momentum calculation rounds before qualification

Current calculation may round the percentage before the strict threshold decision.

Required:

```python
raw_change = (current / reference - 1.0) * 100.0
qualifies = raw_change > threshold
```

Do not round `raw_change`.

---

## P1-2 — Inverse feature flag is incomplete

Current configuration exposes inverse support without a complete end-to-end implementation.

Recommended production action:

```text
remove inverse from public runtime configuration
document inverse as unsupported in this release
```

---

## P1-3 — Dispatcher worker lacks top-level supervision

An unexpected non-Telegram exception inside the worker loop can terminate the dispatcher task.

Required:

```text
dispatcher worker must survive unexpected operational errors
or fail visibly and force service health failure/restart
```

Preferred design:

```text
supervised retry loop
+
structured exception logging
+
health status
```

---

## P1-4 — Hourly-only configuration edge case

If:

```text
IMMEDIATE_TRANSITION_ALERTS=false
HOURLY_ACTIVE_ALERTS=true
```

the transition logic must not claim the hourly bucket without actually sending a transition message.

Hourly-only mode must still send the appropriate hourly snapshot.

---

## P2-1 — Docker/test Python mismatch

Production Docker Python version should match the validated runtime, or the final acceptance suite must explicitly validate the Docker runtime version.

---

## P2-2 — Telegram health error timestamp blind spot

Every failed Telegram delivery attempt should update health state, including:

```text
HTTP 429
permanent HTTP errors
network failures
retry-exhausted failures
```

Health must represent the latest actual Telegram state.

---

# 3. Final Target Architecture

The final system should retain:

```text
BYBIT REST
    ↓
instrument discovery
reconciliation
recovery snapshots

BYBIT WEBSOCKET
    ↓
live Spot + Linear ticker updates

        ↓
NORMALIZED MARKET STATE
        ↓
MOMENTUM ENGINE
        ↓
UNIQUE BASE-COIN AGGREGATION
        ↓
ALERT STATE MACHINE
        ↓
ATOMIC STATE + OUTBOX TRANSACTION
        ↓
DURABLE DISPATCHER
        ↓
TELEGRAM
        ↓
DELIVERY ACK
```

Database access must obey:

```text
ONE SQLite connection
        ↓
ONE task-aware connection ownership policy
        ↓
transaction owner gets reentrant access
all other tasks block
```

---

# 4. Mandatory AI-Agent Execution Loop

The coding agent must use this loop for **every phase**.

```text
READ PHASE
    ↓
WRITE/VERIFY FAILING REGRESSION TEST
    ↓
RUN TARGETED TEST
    ↓
CONFIRM FAILURE IS THE EXPECTED BUG
    ↓
IMPLEMENT MINIMUM CORRECT FIX
    ↓
RUN TARGETED TEST
    ↓
RUN RELATED MODULE TESTS
    ↓
RUN FULL SUITE
    ↓
RUN FAULT/CONCURRENCY TEST IF RELEVANT
    ↓
DOCUMENT RESULTS
    ↓
COMMIT
    ↓
ONLY THEN CONTINUE
```

Do not:

```text
modify code first
then write a test that already passes
```

unless the issue is purely documentation/configuration.

---

# 5. Phase F0 — Freeze the Current Remediated Candidate

## Goal

Create an auditable starting point.

## Required actions

1. Create branch:

```text
final-production-readiness
```

2. Record:

```text
current commit
current Python version
current Docker base image
current pinned dependency versions
current test count
git status
```

3. Run full test suite unchanged.

4. Save:

```text
artifacts/final-readiness-baseline.txt
```

5. Confirm:

```text
no .env tracked
no Telegram token
no chat ID
no private keys
```

## Exit gate

The baseline state is documented before any final fixes.

## Commit

No code commit required if only status documentation changes.

---

# 6. Phase F1 — Replace Global Transaction Boolean with Task Ownership

## Goal

Guarantee true SQLite cross-task transaction isolation.

This is the highest priority remaining fix.

---

## 6.1 Failing regression test first

Add a test similar to:

```text
Task A:
    open transaction
    execute statement
    signal transaction is open
    wait on event

Task B:
    attempt fetch/execute

Assert:
    Task B is still blocked

Release Task A

Assert:
    Task A commits
    Task B then proceeds
```

The test must exercise the actual `Database` wrapper.

Recommended name:

```text
test_other_task_cannot_execute_while_transaction_is_owned
```

Also add:

```text
test_transaction_owner_can_execute_reentrantly
```

---

## 6.2 Required design

Replace:

```python
self._in_transaction: bool
```

with an owner-aware model.

Recommended:

```python
self._lock = asyncio.Lock()
self._tx_owner: asyncio.Task | None = None
self._tx_depth: int = 0
```

### Guard logic

Conceptually:

```python
current = asyncio.current_task()

if self._tx_owner is current:
    # transaction owner may execute without re-acquiring
    yield
else:
    async with self._lock:
        yield
```

### Transaction logic

Conceptually:

```python
current = asyncio.current_task()

if self._tx_owner is current:
    # Decide whether nested transaction is supported.
    # Preferred: disallow accidental nested BEGIN clearly,
    # unless SAVEPOINT support is intentionally implemented.
    raise RuntimeError("nested transaction not supported")

await self._lock.acquire()
self._tx_owner = current

try:
    BEGIN
    yield
    COMMIT
except:
    ROLLBACK
    raise
finally:
    self._tx_owner = None
    self._lock.release()
```

---

## 6.3 Nested transaction policy

Choose one explicit policy.

### Recommended

Do not support nested top-level transactions.

If the same task calls `transaction()` while already owning one:

```python
raise RuntimeError(...)
```

Repository methods used inside an outer transaction should continue to use:

```text
commit=False
```

or `*_no_commit` methods.

This keeps transaction boundaries obvious.

---

## 6.4 Cancellation safety

Test cancellation during a transaction.

Expected:

```text
ROLLBACK
owner cleared
lock released
next task can use DB
```

Required test:

```text
test_cancelled_transaction_releases_database_lock
```

---

## 6.5 Exception safety

Test:

```text
exception inside transaction
→ rollback
→ owner cleared
→ other task proceeds
```

---

## 6.6 Concurrent production-path test

Run:

```text
AlertService.process()
+
AlertDispatcher worker
+
PriceSample writes
```

concurrently.

Assert:

```text
no nested transaction OperationalError
no uncommitted data
no deadlock
no cross-task execution during another transaction
```

---

## Loop

```text
write owner-isolation test
→ confirm current implementation fails
→ implement owner-aware lock
→ run database tests
→ run repository tests
→ run dispatcher/alert concurrency tests
→ run full suite
```

## Exit gate

All of these must hold:

- Another task blocks while a transaction is open.
- Owner task can execute SQL inside its transaction.
- Rollback releases ownership.
- Cancellation releases ownership.
- No `cannot start a transaction within a transaction`.
- No deadlock.

## Commit

```text
Phase F1: task-aware SQLite transaction ownership
```

---

# 7. Phase F2 — Remove Pre-Qualification Rounding

## Goal

Make the `> 5.0` business rule exact relative to the raw calculated float.

---

## 7.1 Required change

Linear:

```python
return ((last_price / prev_price_1h) - 1.0) * 100.0
```

Spot:

```python
return ((current_price / reference_price) - 1.0) * 100.0
```

Do not call `round()` in momentum calculation.

---

## 7.2 Display rounding

Keep formatting only when presenting data:

```python
f"{change_1h:+.2f}%"
```

---

## 7.3 Required tests

```text
100 → 105
= exactly 5%
→ false
```

```text
100 → 105.000000001
→ true
```

Test end-to-end:

```text
prices
→ change calculation
→ qualification
```

Do not test only:

```python
qualifies(5.0000001)
```

---

## Loop

```text
add failing calculation test
→ remove rounding
→ run momentum tests
→ run dedup/alert tests
→ full suite
```

## Exit gate

The threshold is never pre-rounded.

## Commit

```text
Phase F2: strict raw momentum qualification
```

---

# 8. Phase F3 — Resolve Inverse Support Truthfully

## Goal

Ensure configuration and documentation do not claim unsupported behavior.

### Recommended action for this release

Remove inverse as an active feature.

---

## 8.1 Required changes

Remove or deprecate:

```text
ENABLE_INVERSE
```

from public `.env.example`.

Remove incomplete runtime paths that imply inverse monitoring.

Document:

```text
Supported:
Spot
Linear USDT
Linear USDC

Not supported in this release:
Inverse
Options
```

---

## 8.2 Compatibility

If old `.env` contains:

```text
ENABLE_INVERSE=false
```

decide whether config parser:

```text
ignores deprecated field
```

or clearly documents removal.

Do not break normal startup unnecessarily.

---

## 8.3 Tests

Assert enabled market universe contains only:

```text
Spot
Linear USDT
Linear USDC
```

for production configuration.

---

## Exit gate

No public setting falsely implies working inverse monitoring.

## Commit

```text
Phase F3: remove incomplete inverse feature exposure
```

---

# 9. Phase F4 — Supervise the Telegram Dispatcher Worker

## Goal

Prevent silent dispatcher death.

---

## 9.1 Top-level worker protection

Recommended:

```python
async def _run(self):
    while not self._stop.is_set():
        try:
            await self._iteration()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event=telegram_dispatcher_loop_error")
            self.last_worker_error_at = int(time.time())
            await asyncio.sleep(self.config.dispatcher_error_backoff_seconds)
```

Separate:

```text
worker loop
```

from:

```text
single iteration
```

for testability.

---

## 9.2 Health fields

Track:

```text
worker_started_at
worker_last_iteration_at
worker_last_error_at
worker_error_count
worker_alive
```

Health output should surface:

```text
Telegram dispatcher healthy/unhealthy
```

---

## 9.3 Required tests

### Unexpected DB error

Inject:

```text
repo.expire_notifications raises once
```

Expected:

```text
worker logs error
worker remains alive
next iteration succeeds
```

### Unexpected due-notification query error

Same requirement.

### Cancellation

Cancellation must terminate cleanly.

---

## 9.4 Do not swallow permanent programming errors indefinitely

The goal is operational resilience, not hiding defects.

Recommended:

```text
count consecutive loop failures
```

If failures exceed a high threshold:

```text
health = unhealthy
```

The process may still continue so Docker healthcheck/restart policy can act.

---

## Loop

```text
inject worker exception
→ verify current worker dies
→ add supervision
→ verify recovery
→ health test
→ full suite
```

## Exit gate

One transient internal dispatcher exception cannot permanently stop Telegram delivery.

## Commit

```text
Phase F4: supervise dispatcher worker and health
```

---

# 10. Phase F5 — Fix Hourly-Only Alert Mode

## Goal

Make:

```text
IMMEDIATE_TRANSITION_ALERTS=false
HOURLY_ACTIVE_ALERTS=true
```

behave correctly.

---

## 10.1 Correct bucket semantics

Only claim:

```text
last_hourly_bucket
```

during transition completion when a transition notification was actually emitted.

Pseudo:

```python
if debounce_completed:
    if immediate_transition_alerts:
        decision.live_transition = True
        state.last_hourly_bucket = bucket
    else:
        # do not consume bucket
        pass
```

Then hourly logic may emit.

---

## 10.2 Tests

### Immediate false / hourly true

Expected:

```text
debounce survives
no transition message
hourly message allowed
```

### Both enabled

Expected:

```text
one transition message
no same-bucket hourly duplicate
```

### Both disabled

Expected:

```text
state still updates
no user-facing notification
```

---

## Exit gate

All configuration combinations behave deliberately.

## Commit

```text
Phase F5: correct hourly-only alert semantics
```

---

# 11. Phase F6 — Align Docker and Runtime Validation

## Goal

Make the tested Python runtime and Docker runtime consistent.

---

## 11.1 Choose canonical production Python

Recommended:

```text
Python 3.14.x
```

if that is the environment used for final pinned dependency validation.

Otherwise use:

```text
Python 3.13.x
```

and rerun all acceptance tests against that version.

Do not claim:

```text
tested on Python 3.14
```

while shipping a Docker runtime that was never tested.

---

## 11.2 Docker requirements

- Pinned base version when practical.
- Non-root runtime.
- `/data` persistent volume.
- Healthcheck.
- `restart: unless-stopped`.
- No secrets baked into image.

---

## 11.3 Clean Docker test

Build from scratch:

```text
docker build --no-cache
```

Then run:

```text
container
→ migrations
→ service startup
→ health
→ graceful SIGTERM
→ restart
```

---

## 11.4 Test suite in Docker

Run the full test suite inside the production Python container or an equivalent test image.

Save:

```text
artifacts/docker-test-results.txt
```

---

## Exit gate

The exact production runtime has passed the full suite.

## Commit

```text
Phase F6: align Docker and validated Python runtime
```

---

# 12. Phase F7 — Make Telegram Health Reflect Every Failure

## Goal

Ensure observability matches actual delivery state.

---

## 12.1 Health updates

For every Telegram attempt:

### Success

```text
last_success_at = now
last_error_at unchanged or cleared according to design
```

### Failure

Update:

```text
last_error_at = now
last_error_type
```

including:

```text
429
400/403 permanent errors
timeout
connection error
5xx
retry exhaustion
```

---

## 12.2 Health interpretation

Health should consider:

```text
last successful send
last failed send
dispatcher worker alive
outbox retry count
outbox dead count
```

Example:

```text
Telegram API: DEGRADED
Dispatcher: HEALTHY
Retry queue: 2
Last failure: 31s ago
```

---

## Tests

- 429 updates health failure.
- Permanent error updates health failure.
- Successful later send restores healthy/degraded state according to defined policy.

---

## Exit gate

Telegram health never reports stale success while the most recent operation failed.

## Commit

```text
Phase F7: complete Telegram health tracking
```

---

# 13. Phase F8 — Final Concurrency and Recovery Test Expansion

## Goal

Prove that the final changes are safe under real asynchronous execution.

---

## Required concurrency tests

### Test A — DB transaction owner isolation

Mandatory.

### Test B — Alert service + dispatcher race

Run concurrently for many iterations.

### Test C — Listing success acknowledgement + alert write

Concurrent transaction activity must serialize.

### Test D — Spot history write during alert transaction

Must not corrupt or prematurely commit.

### Test E — Cancellation while database transaction open

Lock must recover.

---

## Required delivery tests

- Telegram timeout → retry.
- 429 → honors retry delay.
- Permanent error → dead.
- Retry survives restart.
- Listing retry survives restart.
- Sent row does not resend.

---

## Required market-state tests

```text
0 → 1 → debounce → alert
1 → 0 before debounce → no alert
3 → 4 → suppress
4 → 3 → debounce → alert
```

---

## Required dedup tests

```text
XYZ Spot +6%
XYZ USDT +9%
XYZ USDC +8%
```

Expected:

```text
unique count = 1
```

---

## Repetition gate

Run the complete suite:

```text
5 consecutive times
```

not 3.

Store:

```text
artifacts/final-suite-run1.txt
artifacts/final-suite-run2.txt
artifacts/final-suite-run3.txt
artifacts/final-suite-run4.txt
artifacts/final-suite-run5.txt
```

All must have identical success.

---

## Exit gate

No intermittent concurrency failure across 5 complete runs.

## Commit

```text
Phase F8: final concurrency and recovery acceptance
```

---

# 14. Phase F9 — Real Bybit Staging Validation

## Goal

Verify live public Bybit behavior after all final code changes.

This phase uses public market endpoints only.

---

## Required live checks

Record actual:

```text
Spot instrument count
Linear USDT instrument count
Linear USDC instrument count
Linear PreLaunch count
```

Verify:

- Spot discovery.
- Linear pagination.
- Settlement filtering.
- WebSocket Spot connection.
- WebSocket Linear connection.
- Dynamic subscriptions.
- Top-level `ts`.
- 1h derivative reference.
- Spot history accumulation.
- Health timestamps.
- Discovery refresh.

---

## New listing dry-run

Use a synthetic registry event through the real production callback.

Do not call formatter directly.

Path:

```text
registry/listing event
→ durable outbox
→ dispatcher
```

Telegram may be stubbed in this phase if real credentials are not yet configured.

---

## Exit gate

No API-contract mismatch on current Bybit public endpoints.

## Documentation

Create/update:

```text
STAGING_VALIDATION.md
```

---

# 15. Phase F10 — Real Telegram Staging Validation

## Goal

Exercise real Telegram delivery using the production path.

Real credentials must be provided only via environment/runtime secret.

Never read or print them.

Never commit them.

---

## 15.1 Basic production-path delivery

Generate a synthetic alert through:

```text
AlertService
→ state + durable outbox
→ dispatcher
→ real Telegram
```

Confirm:

```text
message received
outbox = sent
```

---

## 15.2 Listing production path

Generate synthetic listing:

```text
ListingTracker
→ outbox
→ real Telegram
→ listing telegram_sent = 1
```

---

## 15.3 Controlled failure

Use one of:

- temporarily invalid destination
- network isolation
- controlled Telegram client stub at dispatcher boundary

Preferred when possible:

```text
network interruption
```

Expected:

```text
outbox retry
monitor continues
```

Restore connectivity.

Expected:

```text
retry delivered
```

---

## 15.4 Restart with pending/retry

Create a durable pending/retry row.

Restart container.

Expected:

```text
dispatcher recovers it
message delivered
no duplicate outbox
```

---

## Exit gate

Real Telegram delivery and recovery are verified.

Update:

```text
STAGING_VALIDATION.md
```

with timestamps and results.

Do not include credentials.

---

# 16. Phase F11 — Final Security and Repository Audit

## Goal

Ensure release artifact is safe.

---

## Required scan

Verify no tracked:

```text
.env
Telegram bot token
Telegram chat ID
private keys
passwords
session cookies
runtime secrets
production database
```

Verify logs and artifacts do not contain credentials.

---

## Dependency scan

Record:

```text
pip freeze
```

or lock file hash.

Optionally run vulnerability scanning if available.

Do not add a new service dependency solely for this scan.

---

## Repository cleanliness

```text
git status --short
```

must be clean before soak start.

---

## Exit gate

Security sweep clean.

---

# 17. Phase F12 — Fresh 24-Hour Production Candidate Soak

## Goal

Run the **final exact candidate** continuously for at least 24 elapsed hours.

This is mandatory.

---

## 17.1 Soak reset rule

Any code/configuration change affecting runtime behavior after soak start means:

```text
SOAK INVALID
↓
restart clock from zero
```

Documentation-only edits that cannot affect runtime may be allowed, but record them.

Preferred:

```text
freeze all files during soak
```

---

## 17.2 Start record

Record:

```text
START UTC:
COMMIT:
DOCKER IMAGE ID:
PYTHON VERSION:
DEPENDENCY LOCK HASH:
DATABASE MIGRATION VERSION:
```

---

## 17.3 Monitoring

Track:

```text
process uptime
CPU
memory
DB size
price_samples count
outbox pending
outbox retry
outbox dead
Telegram sends
Telegram errors
dispatcher worker status
REST health
Spot WS health
Linear WS health
discovery age
ticker ages
instrument counts
qualifying coin count
listing events
```

---

## 17.4 Mandatory interventions

Perform during the soak:

1. Three container restarts.
2. One temporary network outage.
3. One forced Spot WS reconnect.
4. One forced Linear WS reconnect.
5. One Telegram delivery failure + recovery.
6. One restart with pending/retry outbox.
7. One restart with alert state in `ACTIVE_RANGE`.
8. One synthetic `OVER_RANGE → ACTIVE_RANGE` test.
9. Verify Spot history continuity after restart.
10. Verify dispatcher survives an injected transient repository error in staging mode if safely possible.

---

## 17.5 Failure criteria

Soak fails if any:

```text
unrecoverable process exit
dispatcher silently dies
SQLite nested transaction error
SQLite deadlock
DB corruption
lost pending notification
listing marked sent without delivery
duplicate alert storm
false listing storm
WebSocket remains stale
REST discovery remains dead
health clock nonsense
4+ coins produce range alert
debounce bypass
unbounded memory growth
unbounded retry/outbox growth
```

---

## 17.6 Success criteria

After ≥24 actual hours:

- Service healthy.
- No unexplained data loss.
- No duplicate notification storm.
- Outbox behaves correctly.
- SQLite remains healthy.
- Spot history remains usable.
- WebSockets recover.
- REST discovery continues.
- Telegram retry/delivery works.
- Alert rules remain correct.

Update:

```text
SOAK.md
```

with actual results.

---

## Exit gate

Only after ≥24 hours:

```text
24H SOAK = PASS
```

may be recorded.

---

# 18. Phase F13 — Final Acceptance Audit

## Goal

Perform the final pre-release review after soak completion.

---

## 18.1 Clean environment test

Create a clean environment from the committed dependency files.

Run full suite.

Save:

```text
artifacts/final-acceptance-tests.txt
```

---

## 18.2 Migration validation

Test:

### Fresh DB

```text
empty database
→ all migrations
→ application starts
```

### Previous release candidate DB

```text
old DB
→ new migrations
→ data preserved
→ application starts
```

---

## 18.3 Final checklist

### SQLite

- [ ] Task-aware transaction ownership.
- [ ] Cross-task SQL blocks during transaction.
- [ ] Owner task reentrant SQL works.
- [ ] Exception rollback releases lock.
- [ ] Cancellation releases lock.
- [ ] No nested transaction race.

### Momentum

- [ ] No pre-qualification rounding.
- [ ] Exactly 5.0 does not qualify.
- [ ] Any raw value >5.0 qualifies.

### Market support

- [ ] Spot supported.
- [ ] Linear USDT supported.
- [ ] Linear USDC supported.
- [ ] PreLaunch discovery supported.
- [ ] No false claim of inverse support.

### Alert semantics

- [ ] 0 = no range alert.
- [ ] 1 = active.
- [ ] 2 = active.
- [ ] 3 = active.
- [ ] 4+ = suppress.
- [ ] Debounce works.
- [ ] Hourly cannot bypass debounce.
- [ ] Hourly-only mode works.
- [ ] Same-bucket duplicate suppressed.

### Durable outbox

- [ ] State + outbox atomic.
- [ ] Crash recovery works.
- [ ] Retry survives restart.
- [ ] 429 honored.
- [ ] Listing ack tied to real delivery.
- [ ] Dedupe keys prevent duplicate creation.

### Dispatcher

- [ ] Worker supervised.
- [ ] Worker health visible.
- [ ] Transient DB error does not kill delivery permanently.

### API

- [ ] Spot no pagination args.
- [ ] Linear pagination complete.
- [ ] `isPreListing`.
- [ ] Announcement nested type/tags.
- [ ] WS top-level `ts`.
- [ ] Settlement filters correct.

### Runtime

- [ ] Production Docker Python validated.
- [ ] Non-root container.
- [ ] Persistent `/data`.
- [ ] Healthcheck works.
- [ ] Graceful shutdown.

### Security

- [ ] No secrets tracked.
- [ ] `.env` ignored.
- [ ] No credentials in logs/artifacts.
- [ ] Dependencies pinned/reproducible.

### Staging

- [ ] Live Bybit validation passed.
- [ ] Real Telegram delivery passed.
- [ ] Real Telegram retry/recovery passed.

### Soak

- [ ] Final candidate unchanged during soak.
- [ ] ≥24 elapsed hours.
- [ ] All mandatory interventions completed.
- [ ] Soak passed.

---

# 19. Production-Ready Decision Rule

The AI agent must not infer production readiness from:

```text
tests pass
```

alone.

Production readiness requires:

```text
ALL FINAL FIXES
+
ALL REGRESSION TESTS
+
5 CONSECUTIVE FULL SUITE PASSES
+
LIVE BYBIT STAGING
+
REAL TELEGRAM STAGING
+
SECURITY SWEEP
+
FRESH 24H SOAK
+
FINAL CLEAN ACCEPTANCE TEST
```

Only then may the status become:

```text
STATUS: DONE
PRODUCTION READY: YES
```

Before that:

```text
PRODUCTION READY: NO
```

---

# 20. Mandatory Phase Report Format

After each phase the agent must output:

```text
PHASE:
STATUS: COMPLETE | IN PROGRESS | BLOCKED

BASE COMMIT:
NEW COMMIT:

FILES CREATED:
- ...

FILES CHANGED:
- ...

ISSUES ADDRESSED:
- ...

REGRESSION TESTS:
- ...

TEST COMMANDS:
- ...

TEST RESULTS:
- ...

CONCURRENCY/FAULT TESTS:
- ...

LIVE VALIDATION:
- ...

SECURITY IMPACT:
- ...

KNOWN ISSUES:
- ...

REMAINING RISKS:
- ...

NEXT PHASE:
- ...
```

---

# 21. Commit Discipline

Recommended commits:

```text
Phase F1: task-aware SQLite transaction ownership
Phase F2: strict raw momentum qualification
Phase F3: remove incomplete inverse feature exposure
Phase F4: supervise dispatcher worker and health
Phase F5: correct hourly-only alert semantics
Phase F6: align Docker and validated Python runtime
Phase F7: complete Telegram health tracking
Phase F8: final concurrency and recovery acceptance
Phase F9: live Bybit staging validation
Phase F10: real Telegram staging validation
Phase F11: final security audit
Phase F12: completed final 24-hour soak
Phase F13: production readiness acceptance
```

Never commit:

```text
.env
real Telegram credentials
runtime secrets
virtualenv
temporary cache
private logs
production DB
```

---

# 22. Stop Conditions

The agent must stop and report `BLOCKED` if:

- A regression test cannot reproduce an audited bug.
- SQLite owner isolation cannot be demonstrated.
- Full suite becomes flaky.
- Live Bybit API response contradicts assumptions.
- Real Telegram test cannot be completed due to missing credentials.
- Docker runtime cannot reproduce tests.
- 24-hour soak has not actually elapsed.
- Any code changes after soak begins.

Do not work around these by weakening acceptance criteria.

---

# 23. Final Handoff Package

After production readiness is achieved, prepare:

```text
bybit-monitor-production-ready.zip
final-agent-session.md or .json
AUDIT_REMEDIATION_STATUS.md
STAGING_VALIDATION.md
SOAK.md
artifacts/final-acceptance-tests.txt
artifacts/docker-test-results.txt
README.md
requirements.txt / lock file
```

Exclude:

```text
.env
credentials
private data
runtime cache
virtualenv
unnecessary DB files
```

---

# 24. Final Instruction to the AI Agent

Do not rewrite working subsystems.

Do not add unrelated features.

The remaining work is narrow:

```text
transaction ownership correctness
→ exact momentum qualification
→ truthful market support
→ dispatcher supervision
→ alert configuration correctness
→ runtime alignment
→ health accuracy
→ concurrency validation
→ live staging
→ real Telegram
→ 24h soak
→ final acceptance
```

The most important invariant is:

```text
if one asyncio task owns a SQLite transaction,
no other task can execute SQL on that same connection
until the transaction commits or rolls back.
```

The most important product invariant remains:

```python
1 <= unique_qualifying_base_coins <= 3
```

with each qualifying base coin satisfying:

```python
change_1h > 5.0
```

without pre-qualification rounding.

The bot is production-ready only after the **final unchanged candidate** passes the complete live staging and actual ≥24-hour soak requirements.
