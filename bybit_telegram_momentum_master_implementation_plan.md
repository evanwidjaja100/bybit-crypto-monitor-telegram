# Bybit Live Momentum Monitor → Telegram
## Independent Audit Remediation Master Implementation Plan

**Document purpose:** This is the authoritative repair plan for the current Bybit monitoring repository after independent review of the repository ZIP and the AI-agent implementation sessions.

**Project state at the start of this plan:** `RELEASE CANDIDATE — NOT PRODUCTION READY`

**Primary objective:** Repair the confirmed production defects without changing the locked product behavior, prove the fixes with regression and recovery tests, then complete a clean 24-hour soak before the project may be marked `DONE`.

---

# 1. Critical Execution Rule

This is a **remediation plan**, not a feature-expansion plan.

Do **not**:

- Rewrite the repository from scratch.
- Change the core business rule.
- Add trading functionality.
- Add options monitoring.
- Add unrelated dashboards, databases, message brokers, or infrastructure.
- Refactor large areas merely for style.
- Mark a defect fixed without first creating a test that reproduces it.
- Mark Phase 12 or Phase 13 complete before the required elapsed 24-hour soak has actually completed.

The repair sequence must be:

```text
REPRODUCE DEFECT WITH A FAILING TEST
        ↓
IMPLEMENT MINIMUM CORRECT FIX
        ↓
RUN TARGETED TEST
        ↓
RUN RELATED TEST MODULES
        ↓
RUN FULL TEST SUITE
        ↓
INSPECT ACTUAL BEHAVIOR
        ↓
COMMIT
        ↓
ONLY THEN MOVE TO NEXT PHASE
```

If the new regression test unexpectedly passes before the fix:

```text
STOP
↓
verify the test is exercising the real production path
↓
do not weaken the assertion
↓
find why the defect is not reproduced
```

---

# 2. Locked Product Behavior

The following requirements remain unchanged.

## 2.1 Market universe

Mandatory:

- Bybit Spot.
- Bybit Linear USDT-settled derivatives.
- Bybit Linear USDC-settled derivatives.
- Linear perpetuals.
- Linear futures.
- Linear `PreLaunch` discovery.
- Automatic discovery of new instruments.
- Automatic monitoring of newly tradable instruments.

Default-disabled:

- Inverse contracts.

Out of scope:

- Options.
- Trading.
- Orders.
- Positions.
- Balances.
- Private Bybit endpoints.

---

## 2.2 Momentum threshold

The qualifying rule remains exactly:

```python
change_1h > 5.0
```

Examples:

```text
+4.999999%  -> does not qualify
+5.000000%  -> does not qualify
+5.000001%  -> qualifies
+9.000000%  -> qualifies
```

The implementation must not approximate the business rule by rounding the percentage before comparison.

---

## 2.3 Unique-coin rule

Alert decisions operate on unique `baseCoin`, not raw market symbols.

Example:

```text
BTCUSDT Spot
BTCUSDT Linear
BTCUSDC Linear
```

all count as:

```text
BTC = 1 unique coin
```

---

## 2.4 Alert range

```python
alert_active = 1 <= unique_qualifying_base_coins <= 3
```

Behavior:

| Unique qualifying coins | Alert state |
|---:|---|
| 0 | EMPTY / no alert |
| 1 | ACTIVE_RANGE |
| 2 | ACTIVE_RANGE |
| 3 | ACTIVE_RANGE |
| 4+ | OVER_RANGE / suppress |

---

## 2.5 Live transition debounce

Entering the active range from either:

```text
EMPTY -> ACTIVE_RANGE
```

or:

```text
OVER_RANGE -> ACTIVE_RANGE
```

must remain continuously valid for the configured debounce interval before the live transition alert is emitted.

Default:

```text
20 seconds
```

No other alert type may bypass this initial debounce.

---

## 2.6 Hourly active snapshot

An hourly snapshot may be sent only when:

```text
state == ACTIVE_RANGE
AND
the active state is already confirmed/debounced
AND
that hourly bucket has not already been represented by another alert
```

The first entry into `ACTIVE_RANGE` must not generate an immediate "hourly" message before the debounce completes.

---

# 3. Confirmed Audit Findings to Repair

Every item below must have a regression test and explicit completion evidence.

## P0 — Release blocking

### P0-1 — Listing formatter production wiring is broken

Current production path passes configuration as the formatter timestamp:

```python
format_listing_alert(event, self.config)
```

but the formatter expects:

```python
format_listing_alert(event, now: Optional[int] = None)
```

A real listing notification can therefore raise `TypeError`.

---

### P0-2 — Hourly alert bypasses the transition debounce

On initial entry into `ACTIVE_RANGE`, `hourly_update` can become true while `pending_since` is still active.

Consequences:

- Immediate Telegram message before 20-second debounce.
- Possible second live-transition message after debounce.
- Short-lived threshold spikes can alert even though they did not survive debounce.

---

### P0-3 — Alert state and outgoing notification are not atomic

Current sequence is effectively:

```text
persist alert state
↓
then enqueue/persist Telegram notification
```

A crash between those operations can permanently lose the notification while the persisted alert state says the transition already happened.

---

### P0-4 — Failed Telegram notifications are abandoned permanently

Current durable outbox supports startup requeue for `pending`, but messages marked `failed` are not scheduled for retry after Telegram recovers.

---

### P0-5 — Listing events are marked sent when only queued

A listing event is marked `telegram_sent = 1` immediately after the dispatcher accepts the message into the queue.

That is not proof of Telegram delivery.

If delivery then fails:

```text
listing event = sent
outgoing notification = failed
```

and the listing alert can be lost permanently.

---

## P1 — API and runtime correctness

### P1-1 — Announcement normalization does not match the real Bybit structure

Repair support for structured announcement fields including:

```text
type.key
type.title
tags
```

Do not depend exclusively on a title containing a complete `...USDT` or `...USDC` symbol.

Announcement signal is supplemental; instrument registry remains authoritative.

---

### P1-2 — Wrong prelisting field

Current normalizer reads:

```text
preList
```

Repair to use:

```text
isPreListing
```

while still recognizing `status == "PreLaunch"`.

---

### P1-3 — Spot instruments request sends pagination arguments

Spot discovery should not use Linear pagination semantics.

Spot request:

```text
category=spot
```

must not include `limit` or `cursor`.

Linear must continue to paginate correctly.

---

### P1-4 — Discovery health timestamp can remain `None`

A normal production call to:

```python
discover_once()
```

must set a real wall-clock success timestamp.

---

### P1-5 — Health monitor mixes monotonic and epoch clocks

WebSocket staleness/reconnect logic may use `time.monotonic()` internally.

Health reporting must not subtract a monotonic timestamp from `time.time()`.

Use separate fields or convert the health timestamp to wall-clock epoch.

---

### P1-6 — WebSocket top-level `ts` is discarded

Bybit WebSocket message timestamps must be propagated into normalized ticker state.

Do not expect the timestamp to live inside ticker `data`.

---

## P2 — Hardening and cleanup

### P2-1 — Spot anchor tolerance configuration is not fully wired

`spot_anchor_tolerance_seconds` must be passed into the actual Spot history/momentum implementation.

---

### P2-2 — WebSocket reconnect maximum attempts setting is unused

Either:

1. Implement it with safe behavior and clear semantics, or
2. Remove it from configuration if indefinite reconnect is intentionally required.

Do not leave dead configuration.

For a 24/7 monitor, preferred behavior is indefinite service recovery with capped backoff rather than permanently giving up after 20 attempts. If so, rename/remove the misleading setting.

---

### P2-3 — Subscription synchronization needs stricter filtering

WebSocket topics must reflect enabled market flags and supported settlement currencies.

Also reconcile removed/delisted instruments instead of leaving stale desired subscriptions indefinitely.

---

### P2-4 — Duplicate task registration

Review task creation in `main.py`.

If `_spawn()` already stores the returned task, callers must not append the same task to the tracking list again.

---

### P2-5 — Documentation drift

Repair README references that do not exist or use the wrong Compose service name.

---

### P2-6 — Dependency reproducibility

Current loose `>=` dependency declarations are not enough for a production release.

Create a reproducible tested dependency set.

---

# 4. Target Reliability Model

The corrected alert pipeline must become:

```text
MARKET SNAPSHOT
      ↓
MOMENTUM ENGINE
      ↓
UNIQUE BASE-COIN SET
      ↓
PURE ALERT DECISION
      ↓
ATOMIC DATABASE COMMIT
 ┌───────────────┬────────────────────┐
 │ alert state   │ outgoing outbox row│
 └───────────────┴────────────────────┘
      ↓
DURABLE DISPATCHER
      ↓
TELEGRAM
      ↓
SUCCESS ACK
      ↓
mark outbox sent
      ↓
if listing origin:
mark listing delivered
```

Temporary failure:

```text
Telegram fails
      ↓
outbox status = retry
attempt_count += 1
next_attempt_at = future time
      ↓
monitoring continues
      ↓
dispatcher retries later
      ↓
success
```

Crash:

```text
database transaction committed
      ↓
process crashes before Telegram delivery
      ↓
restart
      ↓
outbox row still pending/retry
      ↓
dispatcher requeues
```

---

# 5. Database Migration Strategy

Do not destroy the existing SQLite database.

Create a new migration version.

Recommended additions to `outgoing_notifications`:

```text
dedupe_key TEXT
attempt_count INTEGER NOT NULL DEFAULT 0
next_attempt_at INTEGER
last_attempt_at INTEGER
origin_type TEXT
origin_key TEXT
```

Existing fields may remain:

```text
id
message_tag
message
created_at
status
sent_at
error
```

Recommended statuses:

```text
pending
retry
sent
dead
```

Do not use `failed` as a terminal state for normal transient Telegram failures.

Create a unique index:

```sql
CREATE UNIQUE INDEX ... ON outgoing_notifications(dedupe_key)
WHERE dedupe_key IS NOT NULL;
```

If SQLite version compatibility makes a partial unique index undesirable, use a normal unique index and ensure non-deduplicated records receive unique keys.

---

## 5.1 Dedupe-key examples

Transition:

```text
transition:<state-version-or-event-id>:BTC,ETH
```

Hourly:

```text
hourly:<UTC-hour-bucket>:BTC,ETH
```

Composition:

```text
composition:<fingerprint>:<cooldown-bucket>
```

Listing:

```text
listing:<event_key>
```

The key must be deterministic for the logical notification.

---

## 5.2 Listing delivery linkage

Recommended:

```text
origin_type = "listing"
origin_key = listing_event.event_key
```

On successful Telegram delivery:

```text
outgoing_notifications.status = sent
listing_events.telegram_sent = 1
```

Prefer doing those success updates in one transaction.

---

# 6. Phase R0 — Freeze and Baseline the Current Release Candidate

## Goal

Establish a known starting point before repairs.

## Required actions

1. Create a repair branch:

```text
audit-remediation
```

2. Record the current commit hash.

3. Run the existing complete test suite unchanged.

4. Save the output:

```text
artifacts/baseline-test-results.txt
```

5. Record:

```text
Python version
dependency versions
OS
current test count
current git status
```

6. Confirm no real credentials are tracked.

7. Do not modify existing tests before the baseline run.

## Baseline report

Create:

```text
AUDIT_REMEDIATION_STATUS.md
```

with:

```text
BASELINE COMMIT:
BASELINE TEST COUNT:
BASELINE PASS/FAIL:
PYTHON:
DATE:
```

## Loop

```text
checkout repair branch
→ verify clean tree
→ run baseline
→ save output
→ inspect failures
→ if existing suite unexpectedly fails, investigate environment first
→ repeat until baseline is understood
```

## Exit gate

Do not proceed unless:

- Baseline result is recorded.
- Repository state is clean or all pre-existing differences are explained.
- No repair has been mixed into the baseline commit.

---

# 7. Phase R1 — Add Regression Tests for Every Confirmed Defect

## Goal

Create failing tests that prove the audit findings before fixing them.

These tests are the contract for the remediation.

Do not weaken them to accommodate the current code.

---

## 7.1 Required regression tests

Add tests with names equivalent to:

```text
test_real_application_listing_callback_formats_and_enqueues
test_initial_active_entry_does_not_emit_hourly_before_debounce
test_transient_active_range_shorter_than_debounce_emits_nothing
test_debounce_completion_emits_exactly_one_transition
test_alert_state_and_outbox_commit_atomically
test_crash_after_atomic_commit_requeues_notification_on_restart
test_failed_telegram_notification_is_retried_after_recovery
test_listing_is_not_marked_sent_when_only_enqueued
test_listing_is_marked_sent_after_successful_delivery
test_real_bybit_announcement_schema_new_crypto
test_is_pre_listing_uses_real_field
test_spot_instruments_request_has_no_limit_or_cursor
test_discovery_success_time_is_set_without_injected_now
test_health_does_not_mix_monotonic_and_epoch_time
test_websocket_top_level_ts_is_preserved
test_spot_anchor_tolerance_config_is_wired
test_threshold_does_not_round_before_strict_comparison
```

Add any additional tests needed for implementation safety.

---

## 7.2 Production-path testing rule

For wiring bugs, do not test only helper classes.

At least one test must exercise:

```text
Application construction/wiring
→ actual callback
→ actual formatter
→ actual dispatcher boundary
```

This is necessary because the listing bug survived module-level tests.

---

## 7.3 Realistic Bybit fixtures

Create sanitized fixtures based on actual response structure.

Examples should include:

### Instrument fixture

```json
{
  "symbol": "XYZUSDT",
  "status": "PreLaunch",
  "baseCoin": "XYZ",
  "quoteCoin": "USDT",
  "settleCoin": "USDT",
  "contractType": "LinearPerpetual",
  "isPreListing": true
}
```

### Announcement fixture

Include nested structured fields:

```json
{
  "id": "example-id",
  "title": "New Listing: Example (XYZ) on Bybit",
  "type": {
    "title": "New Listings",
    "key": "new_crypto"
  },
  "tags": ["Spot", "Spot Listings"],
  "dateTimestamp": "..."
}
```

The announcement test must not require `XYZUSDT` to appear literally in the title.

### WebSocket ticker fixture

Include:

```json
{
  "topic": "tickers.XYZUSDT",
  "type": "snapshot",
  "ts": 1700000000123,
  "data": {
    "symbol": "XYZUSDT",
    "lastPrice": "...",
    "prevPrice1h": "..."
  }
}
```

---

## Loop

```text
add one regression test
→ run only that test
→ confirm it FAILS for the expected reason
→ document failure
→ move to next regression test
```

## Exit gate

Before implementation fixes:

- Each confirmed bug has at least one reproducing test.
- The new tests fail because of the real defect, not because the test itself is malformed.
- Existing unrelated tests still pass.

Commit:

```text
Phase R1: audit regression tests
```

---

# 8. Phase R2 — Repair Listing Wiring and Debounce Semantics

## Goal

Fix the two immediate runtime defects without yet redesigning persistence.

---

## 8.1 Fix listing formatter production wiring

Correct the actual `Application` callback.

Expected behavior:

```text
listing event
→ format_listing_alert(event)
→ dispatcher.enqueue(...)
```

If a deterministic timestamp is required for tests, inject an integer timestamp explicitly.

Never pass `Settings` as the formatter's `now` argument.

---

## 8.2 Fix initial debounce

During:

```text
pending_since != None
```

all user-facing alert outputs must be false:

```python
decision.live_transition is False
decision.hourly_update is False
decision.composition_update is False
```

until the debounce completes.

Recommended state rule:

```text
ACTIVE_RANGE + pending debounce
    = provisional active state
    = no notification
```

Only after the pending state becomes confirmed may hourly/composition policies apply.

---

## 8.3 Bucket ownership

When the live transition fires:

```text
transition alert represents the current hourly snapshot
```

so:

```text
last_hourly_bucket = current bucket
```

must prevent an additional hourly notification in the same bucket.

---

## Required test scenarios

### Scenario A

```text
t=0
BTC +6%
```

Expected:

```text
no transition
no hourly
no composition
```

### Scenario B

```text
t=10
BTC +4.9%
```

Expected:

```text
no message ever sent
pending debounce cleared
```

### Scenario C

```text
t=0 BTC +6%
t=20 BTC +6%
```

Expected:

```text
exactly one transition message
no hourly message in same bucket
```

### Scenario D

```text
OVER_RANGE -> ACTIVE_RANGE
```

must follow the same debounce rule.

---

## Loop

```text
run failing wiring test
→ fix wiring
→ rerun

run debounce tests
→ modify state logic
→ rerun targeted state-machine tests
→ run alert-service tests
→ run integration pipeline tests
→ run full suite
```

## Exit gate

- Real listing callback no longer raises.
- Initial debounce cannot be bypassed by hourly/composition alerts.
- Debounced entry sends exactly one message.
- Existing 0/1/2/3/4+ behavior remains unchanged.

Commit:

```text
Phase R2: listing wiring and debounce correctness
```

---

# 9. Phase R3 — Make Alert Decision Persistence Atomic

## Goal

Eliminate the crash window between state transition persistence and outbox insertion.

This is the most important architectural repair.

---

## 9.1 Separate decision calculation from commit

Preferred design:

```text
AlertStateMachine.evaluate(...)
```

returns:

```text
next state
+
decision
```

without committing.

Then an orchestration layer performs one transaction:

```text
BEGIN
    save next alert state
    insert notification if required
COMMIT
```

Alternative designs are acceptable only if they provide the same atomic guarantee.

---

## 9.2 Do not call commit inside transaction-aware repository methods

Current repository methods commit independently.

Introduce transaction-compatible methods, for example:

```python
save_no_commit(...)
insert_outgoing_notification_no_commit(...)
```

or:

```python
save(..., commit=False)
insert_outgoing_notification(..., commit=False)
```

or a dedicated atomic repository/coordinator.

Preferred approach: keep public high-level repository APIs simple and add a dedicated method for the atomic business operation.

Example conceptual API:

```python
await alert_repo.persist_decision_and_outbox(
    next_state=...,
    notification=...,
)
```

---

## 9.3 Notification must be created before the state transition is considered durable

Atomic outcome must be either:

```text
A:
state changed
AND outbox row exists
```

or:

```text
B:
neither change exists
```

Never:

```text
state changed
BUT no outbox row
```

---

## 9.4 Deterministic dedupe key

Generate the logical notification key before insertion.

The unique constraint protects against:

```text
retry after timeout
process restart
duplicate service invocation
```

---

## Required fault-injection tests

Simulate failure:

```text
after state SQL
before outbox SQL
```

Expected:

```text
transaction rollback
old state remains
no outbox row
```

Simulate:

```text
after outbox SQL
before commit
```

Expected:

```text
transaction rollback
old state remains
no outbox row
```

Successful commit:

```text
next state exists
exactly one outbox row exists
```

Restart:

```text
pending outbox row remains recoverable
```

Duplicate processing:

```text
same dedupe key
→ exactly one logical notification row
```

---

## Loop

```text
design migration/API
→ write transactional test
→ implement transaction-safe repository behavior
→ inject failure
→ prove rollback
→ prove successful atomic commit
→ run all state/repository/dispatcher tests
→ run full suite
```

## Exit gate

No code path may persist a send-producing alert state without also durably creating its outgoing notification.

Commit:

```text
Phase R3: atomic alert state and durable outbox
```

---

# 10. Phase R4 — Durable Telegram Retry State Machine

## Goal

Ensure transient Telegram failures do not permanently abandon notifications.

---

## 10.1 Status semantics

Use:

```text
pending
retry
sent
dead
```

Definitions:

### pending

Ready for immediate first attempt.

### retry

Temporary failure occurred; schedule another attempt.

### sent

Telegram delivery confirmed.

### dead

Terminal failure after configured policy, used only when continued retry is clearly inappropriate.

For this bot, normal network errors, 429s, and 5xx responses are retryable.

---

## 10.2 Persist retry metadata

Track:

```text
attempt_count
last_attempt_at
next_attempt_at
error
```

---

## 10.3 Retry scheduling

Recommended default policy:

```text
attempt 1 → immediate
attempt 2 → +10 sec
attempt 3 → +30 sec
attempt 4 → +60 sec
attempt 5 → +5 min
then capped exponential backoff
```

A 24/7 monitoring bot should generally retain retryable messages instead of discarding them after a few minutes.

Set a reasonable maximum message age if required.

Example:

```text
transition/hourly alerts:
expire after configurable age if stale

listing alerts:
retain longer because listing notification remains relevant
```

If expiration is implemented, test it explicitly.

---

## 10.4 Telegram 429 handling

If Telegram supplies a `retry_after` delay:

```text
use retry_after as the minimum next-attempt delay
```

Do not hammer Telegram with generic retries during flood control.

---

## 10.5 Startup recovery

On startup, load:

```text
pending
+
retry where next_attempt_at <= now
```

Messages with a future retry time must be scheduled without busy-looping.

---

## 10.6 Idempotency

Dispatcher should assume:

```text
delivery attempt may be repeated
```

and use the outbox status/dedupe key to prevent duplicate queue creation inside the application.

Exact-once delivery cannot be guaranteed across an external network boundary, but the system must minimize duplicates and never intentionally discard transiently failed records.

---

## Required tests

- Network timeout -> retry state.
- Telegram 5xx -> retry state.
- Telegram 429 with `retry_after`.
- Restart while notification is `retry`.
- Notification eventually succeeds.
- Success sets `sent_at`.
- Successful row is not sent again on restart.
- Retry rows do not spin continuously.
- Outbox dedupe key prevents duplicate creation.

---

## Loop

```text
fail Telegram stub
→ inspect persisted retry state
→ advance fake clock
→ run dispatcher
→ verify retry
→ restore Telegram
→ verify success
→ restart
→ verify no duplicate resend
```

## Exit gate

A temporary Telegram outage followed by recovery must lead to eventual delivery of eligible persisted notifications without stopping market monitoring.

Commit:

```text
Phase R4: durable Telegram retry scheduling
```

---

# 11. Phase R5 — Correct Listing Delivery Acknowledgement

## Goal

Make `listing_events.telegram_sent` mean actual confirmed Telegram delivery.

---

## 11.1 Change listing flow

Old conceptual flow:

```text
listing event
→ enqueue
→ mark listing sent   ❌
```

New flow:

```text
listing event
→ create durable outbox notification
   origin_type = listing
   origin_key = event_key
→ leave telegram_sent = 0
→ dispatcher sends
→ Telegram success
→ in transaction:
      mark outbox sent
      mark listing event sent
```

---

## 11.2 `reconcile_unsent()` behavior

On startup:

```text
listing event unsent
```

must be reconciled with the outbox.

Do not blindly create duplicate notifications.

Pseudo-rule:

```text
if listing telegram_sent == 0:
    if outbox exists and is pending/retry:
        do nothing; dispatcher owns it
    elif outbox exists and sent:
        repair listing telegram_sent = 1
    elif no outbox exists:
        create one with deterministic listing dedupe key
```

This creates self-healing persistence.

---

## 11.3 Failure semantics

If Telegram fails:

```text
listing telegram_sent stays 0
outbox becomes retry
```

If eventually delivered:

```text
outbox = sent
listing telegram_sent = 1
```

---

## Required tests

1. Enqueue does not mark listing sent.
2. Failed delivery does not mark listing sent.
3. Successful delivery marks both records.
4. Restart with retry row creates no duplicate outbox row.
5. Restart with unsent listing but missing outbox repairs the missing outbox.
6. Restart with sent outbox but stale listing flag repairs the listing flag.
7. Real `Application._notify_listing` production path works.

---

## Loop

```text
create listing
→ inspect listing + outbox
→ fail Telegram
→ inspect states
→ restart
→ recover
→ succeed
→ inspect both states
→ repeat for prelaunch/trading/announcement
```

## Exit gate

Listing delivery status must accurately reflect Telegram success.

Commit:

```text
Phase R5: listing delivery acknowledgement and recovery
```

---

# 12. Phase R6 — Bybit API Contract Corrections

## Goal

Align the normalizers and request construction with the actual API shapes used by the application.

Do not change business behavior.

---

## 12.1 Spot instruments request

Refactor:

```python
get_spot_instruments()
```

so it requests:

```text
category=spot
```

without:

```text
limit
cursor
```

Linear remains paginated.

Do not share a helper that forces pagination params onto all categories unless the helper supports category-specific behavior cleanly.

---

## 12.2 `isPreListing`

Normalize:

```python
is_pre_listing = (
    status == "PreLaunch"
    or boolean_value(raw.get("isPreListing"))
)
```

Do not use `preList`.

Implement robust boolean parsing if the API may return boolean-like strings.

---

## 12.3 Announcement model

The domain model should explicitly represent structured announcement data.

Recommended:

```python
Announcement(
    id=...,
    title=...,
    description=...,
    type_key=...,
    type_title=...,
    tags=(...),
    timestamp=...,
    metadata=...
)
```

Do not store a nested object into a field typed as a string.

---

## 12.4 Announcement classification

Use structured fields first:

```text
type_key == "new_crypto"
listing-related tags
```

Then use title/description as supplemental evidence.

The goal is:

```text
identify that this is a listing-related announcement
```

Do not require perfect symbol extraction for the bot's monitoring correctness.

If only base ticker can be extracted:

```text
XYZ
```

record an announcement event using a safe announcement identity.

Do not fabricate:

```text
XYZUSDT
```

unless the source actually supports that market pair.

Instrument discovery remains authoritative for actual market onboarding.

---

## 12.5 WebSocket `ts`

Preserve the top-level WebSocket timestamp.

Expected conversion:

```text
message.ts milliseconds
→ ticker.timestamp epoch seconds
```

For delta messages:

```text
merge data fields
+
update timestamp from current envelope ts
```

---

## 12.6 REST/WS fixture parity

Tests should contain examples close to actual payload shapes.

Avoid convenient invented fields that the real API does not send.

---

## Required tests

- Spot request query parameters exact.
- Two-page Linear pagination still works.
- `isPreListing=true`.
- `status=PreLaunch` without flag.
- Nested announcement type parsing.
- Tags parsing.
- Listing classification with title that does not contain `USDT`.
- WS snapshot top-level `ts`.
- WS delta top-level `ts`.

---

## Loop

```text
update fixtures first
→ run normalizer tests
→ modify models/normalizer
→ run REST tests
→ modify request construction
→ run listing tests
→ run WS tests
→ run full suite
```

## Exit gate

No supported path depends on fields known to be artifacts of old test fixtures.

Commit:

```text
Phase R6: Bybit API contract alignment
```

---

# 13. Phase R7 — Repair Health and Clock Semantics

## Goal

Make health output trustworthy.

---

## 13.1 Discovery success timestamp

In:

```python
discover_once(now=None)
```

resolve:

```python
effective_now = int(time.time())
```

once at the beginning.

Pass that same value into:

```text
registry reconciliation
last_success_at
events where appropriate
```

Do not let one component generate a hidden timestamp while another stores `None`.

---

## 13.2 Separate clocks

Use:

```text
monotonic clock
```

for:

- connection timeout
- staleness watchdog
- elapsed reconnect logic

Use:

```text
epoch wall clock
```

for:

- health summaries
- human timestamps
- persisted event timestamps

Recommended WebSocket client fields:

```text
last_message_monotonic
last_message_at
```

Where:

```text
last_message_monotonic = time.monotonic()
last_message_at = int(time.time())
```

on each received message.

HealthMonitor uses only:

```text
last_message_at
```

Reconnect/stale watchdog uses only:

```text
last_message_monotonic
```

---

## 13.3 Negative-age guard

Health age calculation should not silently display negative values from clock anomalies.

If:

```text
last_seen > now
```

either:

```text
clamp to zero
```

or add an explicit health note.

Preferred:

```text
max(0, now - last_seen)
```

plus tests.

---

## Required tests

- `discover_once()` without `now`.
- Discovery health becomes healthy.
- WebSocket health age is realistic.
- Stale watchdog still uses monotonic clock.
- Fake wall-clock jump does not break reconnect logic.
- Health summary contains sane ages.

---

## Loop

```text
write clock tests
→ split timestamp fields
→ run WS tests
→ run health tests
→ run discovery tests
→ run full suite
```

## Exit gate

A health summary must never compare timestamps from different clock domains.

Commit:

```text
Phase R7: health and clock correctness
```

---

# 14. Phase R8 — Configuration and WebSocket Lifecycle Cleanup

## Goal

Remove configuration drift and subscription inconsistencies.

---

## 14.1 Spot anchor tolerance

Pass:

```text
config.spot_anchor_tolerance_seconds
```

into the actual Spot history anchor query/component.

Add a test that sets an unusual value, such as:

```text
7 seconds
```

and proves runtime behavior changes accordingly.

---

## 14.2 Threshold strictness

Remove qualification-time rounding.

Use:

```python
change_1h > threshold
```

directly.

Calculation functions may still round for display only.

Tests:

```text
5.0000000 -> false
5.0000001 -> true
```

---

## 14.3 Reconnect configuration

Decide explicitly:

### Preferred policy

24/7 monitoring should keep trying forever.

Use:

```text
bounded/capped backoff
```

but no terminal reconnect-attempt count.

If choosing this policy:

- remove `ws_reconnect_max_attempts` from configuration and docs, or
- rename it if it serves another purpose.

Do not retain a setting that is not enforced.

---

## 14.4 Subscription filtering

For desired Linear subscriptions:

```text
status == Trading
AND
settleCoin matches enabled flags
```

Examples:

```text
enable_linear_usdt = true
enable_linear_usdc = false
→ do not subscribe USDC linear symbols
```

Spot topics require:

```text
enable_spot = true
```

---

## 14.5 Subscription removal

When a market is no longer desired:

```text
registry reconciliation
→ desired set changes
```

the WebSocket manager must reconcile actual/desired subscriptions.

Acceptable strategies:

1. Send unsubscribe requests for removed topics.
2. Reconnect the category connection with the new desired subscription set.

Do not allow stale subscriptions to accumulate forever.

---

## 14.6 Task tracking

Audit all `_spawn()` call sites.

Invariant:

```text
each running task appears once in task registry
```

Add a lifecycle test if practical.

---

## Loop

```text
wire tolerance
→ strict-threshold test
→ clean config
→ subscription-filter tests
→ removal test
→ task-registration test
→ full suite
```

## Exit gate

- No meaningful runtime setting is silently ignored.
- Enabled market flags control actual WebSocket subscriptions.
- Removed markets are eventually unsubscribed.
- Task tracking contains no duplicate references.

Commit:

```text
Phase R8: configuration and websocket lifecycle hardening
```

---

# 15. Phase R9 — Documentation and Reproducible Environment

## Goal

Make the repository accurately describe how it runs.

---

## 15.1 README corrections

Verify every command/file reference.

Known items to check:

- Correct Docker Compose service name.
- Remove or create referenced `scripts/soak_test.py`.
- Remove or create referenced `docs/soak-test.md`.
- Correct test-file references.
- Document durable retry behavior.
- Document that 24-hour soak is mandatory before `DONE`.

No README command may refer to a path that does not exist.

---

## 15.2 `.env.example`

Ensure it contains every intended user-facing configuration value.

Do not include internal/deprecated/dead settings.

No credentials.

---

## 15.3 Dependencies

Create a reproducible dependency artifact.

Acceptable:

```text
requirements.txt = pinned tested versions
```

or:

```text
requirements.in
requirements.lock / compiled requirements.txt
```

Record the exact versions used for final acceptance.

Do not upgrade unrelated packages during remediation unless required.

---

## 15.4 Status documentation

Update:

```text
SOAK.md
AUDIT_REMEDIATION_STATUS.md
```

The project must remain:

```text
NOT PRODUCTION READY
```

until Phase R12 succeeds.

---

## Loop

```text
read README line by line
→ verify every command/path against repo
→ fix drift
→ create clean venv/container from dependency file
→ install
→ run tests
```

## Exit gate

A clean machine/container can follow README and reproduce the test environment.

Commit:

```text
Phase R9: documentation and dependency reproducibility
```

---

# 16. Phase R10 — Full Recovery and Chaos Validation

## Goal

Prove the repaired reliability model under controlled failures.

---

## 16.1 Required automated recovery scenarios

### Alert atomicity

```text
transition decided
→ injected DB failure
→ no half-committed state
```

### Crash recovery

```text
outbox committed
→ process terminated before send
→ restart
→ notification delivered
```

### Telegram temporary failure

```text
notification pending
→ Telegram unavailable
→ retry persisted
→ Telegram restored
→ delivery succeeds
```

### Listing failure

```text
new listing
→ Telegram unavailable
→ listing remains unsent
→ restart
→ no duplicate outbox
→ Telegram restored
→ delivered
→ listing marked sent
```

### 429

```text
Telegram returns retry_after
→ next_attempt_at honors delay
```

### Duplicate event

```text
same alert/listing processed twice
→ one logical outbox row
```

### Database busy/lock

Retain existing busy-lock tests and make sure new transactional logic does not create deadlocks.

### WebSocket disconnect

```text
disconnect
→ REST remains available
→ WS reconnects
→ subscriptions restored
→ no duplicate alert caused by reconnect
```

### Stale stream

```text
connection appears open
→ no messages
→ watchdog detects stale
→ reconnect/reconcile
```

---

## 16.2 Critical market-state sequence

Run:

```text
0 qualifying
↓
BTC +6%
↓
wait less than debounce
↓
0 again
```

Expected:

```text
zero messages
```

Then:

```text
0
↓
BTC +6%
↓
20 sec stable
```

Expected:

```text
1 transition
```

Then:

```text
BTC + ETH + SOL = 3
```

No immediate duplicate unless composition policy explicitly allows it after cooldown.

Then:

```text
BTC + ETH + SOL + DOGE = 4
```

Expected:

```text
suppressed
```

Then:

```text
ETH drops
→ 3 remain
→ debounce
```

Expected:

```text
one re-entry alert
```

---

## 16.3 Cross-market dedup

Input:

```text
XYZ Spot +6%
XYZ USDT +9%
XYZ USDC +8%
```

Expected:

```text
1 unique coin
representative = strongest valid market
```

---

## Loop

```text
run one fault scenario
→ inspect DB rows
→ inspect logs
→ inspect emitted messages
→ restart
→ re-check rows
→ fix
→ rerun
```

## Exit gate

All automated unit/integration/recovery/chaos tests pass repeatedly.

Run full suite at least:

```text
3 consecutive times
```

with identical success.

Store outputs:

```text
artifacts/remediation-full-tests-run1.txt
artifacts/remediation-full-tests-run2.txt
artifacts/remediation-full-tests-run3.txt
```

Commit:

```text
Phase R10: recovery and chaos acceptance
```

---

# 17. Phase R11 — Live Staging Validation

## Goal

Validate the corrected code against live public Bybit data and a controlled Telegram destination before the long soak.

Use real Telegram credentials only through local/runtime secrets.

Never commit them.

---

## 17.1 Pre-flight

Verify:

```text
git status clean
no .env tracked
all tests pass
Docker image builds
container runs non-root
persistent data volume exists
```

---

## 17.2 Live Bybit validation

Record actual runtime counts:

```text
Spot Trading instruments
Linear USDT Trading
Linear USDC Trading
Linear PreLaunch
```

Confirm:

- Spot request works without pagination args.
- Linear pagination completes.
- WebSocket Spot connects.
- WebSocket Linear connects.
- Ticker timestamps are non-zero and sane.
- REST discovery health is healthy.
- WS ticker ages are sane.
- Automatic subscription sync is working.

---

## 17.3 Controlled Telegram delivery

Send:

1. Manual test notification.
2. Synthetic transition alert through normal application path.
3. Synthetic listing event through normal application path.

Do not call the Telegram client directly for all tests; at least one must exercise:

```text
business event
→ outbox
→ dispatcher
→ Telegram
→ persisted success
```

Inspect DB afterward.

---

## 17.4 Controlled Telegram outage

Temporarily use a controlled failure mechanism.

Expected:

```text
monitoring continues
notification moves to retry
listing not marked sent
```

Restore Telegram.

Expected:

```text
notification succeeds
listing delivery state repaired
```

---

## 17.5 Container restart

Restart while:

```text
outbox has pending/retry work
```

Verify recovery.

Also restart during normal operation and inspect for:

```text
duplicate transition
duplicate hourly alert
duplicate listing storm
```

---

## Exit gate

Create:

```text
STAGING_VALIDATION.md
```

containing evidence and timestamps.

Do not proceed to 24-hour soak if any P0/P1 behavior fails.

Commit documentation only if code is unchanged.

---

# 18. Phase R12 — Clean 24-Hour Soak Test

## Goal

Actually satisfy the previously incomplete acceptance requirement.

**This phase requires at least 24 elapsed hours.**

Do not simulate completion.

Do not mark `DONE` early.

---

## 18.1 Start conditions

The 24-hour clock starts only after:

- All remediation tests pass.
- Live staging validation passes.
- Container is running the final candidate commit.
- No code changes occur after the soak starts.

If code changes during the soak:

```text
restart the 24-hour soak clock
```

---

## 18.2 Record metadata

At soak start record:

```text
START UTC:
COMMIT HASH:
IMAGE ID:
PYTHON VERSION:
DEPENDENCY LOCK HASH:
DATABASE PATH:
```

---

## 18.3 Monitor

At minimum:

```text
CPU
memory
database size
price sample count
outbox pending count
outbox retry count
outbox dead count
Telegram sends
Telegram retry events
REST errors
WS reconnects
last ticker ages
discovery age
instrument counts
qualifying unique coin count
listing events
duplicate alerts
```

---

## 18.4 Mandatory interventions

Perform during the 24-hour window:

1. At least 3 controlled container restarts.
2. One temporary network interruption.
3. One forced Spot WS reconnect.
4. One forced Linear WS reconnect.
5. One controlled Telegram failure followed by recovery.
6. One restart while at least one durable outbox message is pending/retry, using a synthetic message if the market does not naturally provide the state.
7. One restart while the alert state is simulated as `ACTIVE_RANGE`.
8. Verify Spot history remains useful after restart.

Synthetic test events must be clearly labeled and must not contaminate real market interpretation.

---

## 18.5 Failure criteria

The soak fails if any of these occur:

```text
process dies and does not recover
database corruption
unbounded memory growth
large unbounded outbox growth
duplicate alert storm
false new-listing storm
pending/retry messages disappear
listing marked delivered when Telegram failed
WS remains stale without recovery
REST discovery stops permanently
health ages become nonsensical
4+ qualifying coins produce range alerts
debounce is bypassed
```

---

## 18.6 Success criteria

After at least 24 elapsed hours:

```text
service still healthy
state persisted across interventions
no release-blocking defect reproduced
no unexplained notification loss
no duplicate storm
Spot history valid
new listing system healthy
outbox drained/retrying correctly
```

Update:

```text
SOAK.md
```

with actual findings.

## Exit gate

Only now may:

```text
24-hour soak test passes = TRUE
```

be checked.

---

# 19. Phase R13 — Final Acceptance and Release Candidate Handoff

## Goal

Perform a complete post-remediation audit before marking `DONE`.

---

## 19.1 Final test command

Run the complete suite in a clean environment.

Save:

```text
artifacts/final-test-results.txt
```

---

## 19.2 Security sweep

Verify:

```text
no .env tracked
no Telegram token
no chat ID
no private keys
no accidental runtime DB with secrets
no logs containing secrets
container non-root
```

---

## 19.3 Database migration test

Test:

```text
fresh database
→ all migrations
```

and:

```text
database created by old release candidate
→ new migrations
→ application starts
→ data preserved
```

This is mandatory.

---

## 19.4 Final acceptance checklist

### P0 defects

- [ ] Listing production formatter wiring fixed.
- [ ] Debounce cannot be bypassed by hourly alert.
- [ ] Alert state + outbox commit atomically.
- [ ] Retryable Telegram failures persist and retry.
- [ ] Listing is marked sent only after delivery.

### P1 correctness

- [ ] Real announcement shape supported.
- [ ] `isPreListing` supported.
- [ ] Spot instruments request sends no pagination args.
- [ ] Linear pagination still complete.
- [ ] Discovery health timestamp correct.
- [ ] Health uses consistent clock domains.
- [ ] WebSocket top-level `ts` preserved.

### P2 hardening

- [ ] Spot anchor tolerance configuration wired.
- [ ] Strict `>5.0` comparison does not pre-round.
- [ ] Reconnect configuration has real semantics.
- [ ] WebSocket subscriptions respect market flags.
- [ ] Removed subscriptions reconcile.
- [ ] No duplicate task tracking.
- [ ] README references are valid.
- [ ] Dependencies reproducible.

### Reliability

- [ ] Crash after outbox commit recovers.
- [ ] No half-committed state/outbox.
- [ ] Retry rows survive restart.
- [ ] Listing retries survive restart.
- [ ] 429 delay honored.
- [ ] WebSocket disconnect recovery works.
- [ ] Stale-stream recovery works.
- [ ] SQLite lock tests pass.

### Business behavior

- [ ] Exactly +5.0% does not qualify.
- [ ] >+5.0% qualifies.
- [ ] Markets deduplicate by `baseCoin`.
- [ ] 0 unique coins = no alert.
- [ ] 1 unique coin = active.
- [ ] 2 unique coins = active.
- [ ] 3 unique coins = active.
- [ ] 4+ unique coins = suppressed.
- [ ] 4+ → 1–3 re-entry is debounced.
- [ ] Hourly alert does not bypass debounce.

### Deployment

- [ ] Docker build reproducible.
- [ ] Container non-root.
- [ ] Persistent database works.
- [ ] Live staging passed.
- [ ] Clean 24-hour soak actually elapsed and passed.

---

## 19.5 Definition of `DONE`

The agent may output:

```text
STATUS: DONE
PRODUCTION READY: YES
```

only when every mandatory checklist item is verified.

If the 24-hour soak has not elapsed:

```text
STATUS: SOAK IN PROGRESS
PRODUCTION READY: NO
```

No exception.

---

# 20. Mandatory Agent Phase Report

After every phase, output exactly this structure:

```text
PHASE:
STATUS: COMPLETE | BLOCKED | IN PROGRESS

BASE COMMIT:
NEW COMMIT:

FILES CREATED:
- ...

FILES CHANGED:
- ...

DEFECTS ADDRESSED:
- ...

REGRESSION TESTS ADDED:
- ...

TESTS RUN:
- command
- command

TEST RESULTS:
- ...

FAULT INJECTION PERFORMED:
- ...

DATABASE MIGRATION IMPACT:
- ...

MANUAL/LIVE VALIDATION:
- ...

KNOWN ISSUES:
- ...

ASSUMPTIONS:
- ...

REMAINING RISKS:
- ...

NEXT PHASE:
- ...
```

If a phase is blocked:

```text
STATUS: BLOCKED
```

and explain exactly what prevents completion.

Do not silently move forward.

---

# 21. Mandatory Commit Discipline

Use one commit per successfully completed remediation phase.

Recommended messages:

```text
Phase R1: audit regression tests
Phase R2: listing wiring and debounce correctness
Phase R3: atomic alert state and durable outbox
Phase R4: durable Telegram retry scheduling
Phase R5: listing delivery acknowledgement and recovery
Phase R6: Bybit API contract alignment
Phase R7: health and clock correctness
Phase R8: configuration and websocket lifecycle hardening
Phase R9: documentation and dependency reproducibility
Phase R10: recovery and chaos acceptance
Phase R11: live staging validation evidence
Phase R12: completed 24-hour soak evidence
Phase R13: final acceptance
```

Never commit:

```text
.env
live Telegram credentials
runtime secrets
temporary virtual environments
large transient logs
production SQLite DB unless explicitly intended
```

---

# 22. Recommended Implementation Details for the Atomic Outbox

This section is guidance for the difficult P0 persistence repair.

The exact implementation may differ, but all invariants must hold.

---

## 22.1 Pure decision object

A decision should contain enough information to persist later:

```python
AlertDecision(
    previous_state=...,
    next_state=...,
    qualifying_count=...,
    fingerprint=...,
    kind=...,
    should_notify=...,
    transition_reason=...,
    hourly_bucket=...,
    ...
)
```

Do not mutate durable state inside the calculation stage.

---

## 22.2 Atomic coordinator

Conceptual flow:

```python
decision = state_machine.evaluate(current_state, qualifying, now)

async with db.transaction():
    await alert_state_repo.save_no_commit(decision.next_state)

    if decision.should_notify:
        await notification_repo.insert_no_commit(
            dedupe_key=...,
            tag=decision.kind,
            message=...,
            status="pending",
            ...
        )
```

After commit:

```text
dispatcher wakeup / queue scheduling
```

The queue is an optimization.

The database outbox is the source of truth.

---

## 22.3 Queue-loss safety

If:

```text
DB commit succeeds
```

but:

```text
process crashes before asyncio.Queue.put(...)
```

the record must still be delivered after restart.

Therefore the dispatcher must periodically or on wakeup query the durable outbox.

Do not rely solely on one-time startup requeue.

Recommended:

```text
dispatcher poll due outbox every 1–5 seconds
```

or a wake-event + periodic safety poll.

This also naturally handles delayed retries.

---

## 22.4 Dispatcher claim semantics

With one process, a simple design is enough.

Optional safe fields:

```text
status = pending/retry
```

Select due rows.

Before sending, optionally update:

```text
last_attempt_at
attempt_count
```

After success:

```text
sent
```

After retryable failure:

```text
retry
next_attempt_at = ...
```

Because only one dispatcher worker exists, complex distributed leases are unnecessary.

Do not add Redis just for this.

---

# 23. Recommended Test Matrix for the Outbox

| Scenario | Expected DB state |
|---|---|
| Alert decision has no notification | state changed only |
| Alert decision sends | state + pending outbox atomically |
| Failure before transaction commit | neither persists |
| Crash after commit | pending survives |
| Retryable Telegram failure | retry + attempt count |
| Restart before retry time | no premature send |
| Retry time reached | message attempted |
| Telegram success | sent |
| Restart after success | no resend |
| Duplicate decision | no duplicate logical outbox |
| Listing enqueue | listing unsent + outbox pending |
| Listing failure | listing unsent + outbox retry |
| Listing success | listing sent + outbox sent |

---

# 24. Explicit Non-Goals During Remediation

Do not add:

- Price charts.
- Telegram command interface.
- Web UI.
- Trading execution.
- AI market commentary.
- Portfolio management.
- Redis.
- Kubernetes.
- PostgreSQL migration.
- Options.
- Additional exchanges.

The only objective is:

```text
make the existing bot correct, durable, recoverable, testable, and production-ready
```

---

# 25. Final Handoff Package

After Phase R13, prepare:

```text
bybit-monitor-remediated.zip
agent-remediation-session.md or .json
AUDIT_REMEDIATION_STATUS.md
STAGING_VALIDATION.md
SOAK.md
artifacts/final-test-results.txt
README.md
```

The ZIP must exclude:

```text
.env
Telegram credentials
private secrets
runtime cache
virtualenv
temporary logs
unnecessary databases
```

If a sample SQLite database is included for migration testing, it must contain no secrets and be clearly named as a fixture.

---

# 26. Instructions for the Next Independent Audit

The next reviewer should specifically verify:

1. The original five P0 defects cannot be reproduced.
2. The regression tests truly exercise production paths.
3. Atomic transaction boundaries are real.
4. The durable outbox, not `asyncio.Queue`, is the source of truth.
5. Failed Telegram notifications recover after restart.
6. Listing success is tied to actual delivery.
7. Bybit fixtures match real shapes.
8. Health timestamps are sane.
9. No business-rule drift occurred.
10. The 24-hour soak actually elapsed after the final code change.

---

# 27. Final Instruction to the AI Coding Agent

Do not optimize for the number of passing tests.

Optimize for:

```text
correct invariants
+
real production paths
+
failure recovery
+
persistence correctness
```

The old repository already had a large green test suite while several production defects remained.

Therefore:

```text
"tests pass"
```

is necessary but not sufficient.

Every repaired invariant must be demonstrated by:

```text
regression test
+
implementation
+
fault/restart validation where relevant
```

The project is not `DONE` until the full remediation checklist and clean 24-hour soak are both complete.
