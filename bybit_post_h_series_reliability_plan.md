# Bybit Live Momentum Monitor → Telegram
## Post-H-Series WebSocket Reliability Remediation Plan

**Purpose:** This document is the authoritative implementation plan for the remaining WebSocket and health-layer issues identified after the H1–H5 hardening series.

**Starting state:**  
The repository has completed the H1–H5 hardening series and currently has strong automated coverage, refreshed Docker/staging evidence, and live public Bybit ACK validation. However, several edge cases remain that can still cause silent partial monitoring or misleading health reporting.

**Production status at start of this plan:**

```text
STATUS: IN PROGRESS
PRODUCTION READY: NO
```

**This plan is intentionally narrow.**  
Do not rewrite the bot, do not change the product rules, and do not add unrelated features.

---

# 1. Scope

This remediation covers exactly these seven issues:

1. **Subscription ACK race:** a Bybit ACK can arrive before the client records the batch as pending.
2. **ACK timeout watchdog gap:** pending ACK timeout is only checked when `ws.recv()` times out, so continuous market traffic can suppress timeout recovery.
3. **Ticker freshness vs heartbeat freshness:** pong/control traffic currently refreshes the same timestamp used for market ticker freshness.
4. **Container heartbeat configuration mismatch:** `HEALTH_HEARTBEAT_STALE_SECONDS` is exposed in configuration but the container healthcheck uses a hard-coded value.
5. **Short reconnect health semantics:** a temporarily unhealthy critical subsystem can still report `overall=healthy` while it is inside the grace window.
6. **Stale `SOAK.md`:** the old soak is invalidated but the document may still imply it is active/current.
7. **Broken README plan reference:** README references a plan file that no longer exists in the repository.

---

# 2. Locked Product Invariants

Do not change these behaviors.

## 2.1 Momentum threshold

```python
change_1h > 5.0
```

Exactly `5.0%` does not qualify.

---

## 2.2 Unique coin rule

Count by:

```text
baseCoin
```

not by market symbol.

Example:

```text
BTCUSDT Spot
BTCUSDT Linear
BTCUSDC Linear
```

must equal:

```text
BTC = 1 unique qualifying coin
```

---

## 2.3 Telegram range rule

```python
1 <= unique_qualifying_base_coins <= 3
```

| Qualifying unique coins | Result |
|---:|---|
| 0 | no range alert |
| 1 | active |
| 2 | active |
| 3 | active |
| 4+ | suppress |

---

## 2.4 Supported market universe

Supported:

```text
Spot
Linear USDT
Linear USDC
Linear PreLaunch discovery
```

Unsupported:

```text
Inverse
Options
Trading/order execution
```

---

# 3. Mandatory Agent Workflow

Every runtime fix must use this loop:

```text
READ CURRENT PRODUCTION PATH
        ↓
WRITE A REGRESSION TEST
        ↓
RUN TEST AGAINST CURRENT CODE
        ↓
CONFIRM IT FAILS FOR THE EXPECTED REASON
        ↓
IMPLEMENT MINIMUM CORRECT FIX
        ↓
RUN TARGETED TEST
        ↓
RUN RELATED TEST MODULES
        ↓
RUN COMPLETE SUITE
        ↓
RUN LIVE/STAGING VALIDATION IF RELEVANT
        ↓
REVIEW DIFF
        ↓
COMMIT
```

Do **not**:

```text
change code first
then add a test that already passes
```

unless the change is documentation-only.

---

# 4. Phase J0 — Freeze the Current Master Baseline

## Goal

Create an auditable starting point before this final WebSocket reliability repair.

## Required actions

1. Create branch:

```text
final-ws-reliability
```

2. Record:

```text
BASE COMMIT
CURRENT BRANCH
CURRENT TEST COUNT
PYTHON VERSION
DOCKER IMAGE ID
GIT STATUS
```

3. Run:

```bash
python -m pytest tests -q
```

4. Save:

```text
artifacts/ws-reliability-baseline.txt
```

5. Verify:

```text
.env is not tracked
no Telegram credentials are tracked
no runtime database is committed
```

## Exit gate

Do not start J1 until the baseline is documented.

---

# 5. Phase J1 — Eliminate the Subscription ACK Registration Race

## Goal

Guarantee that a Bybit subscription ACK can never arrive before the client has a pending record for the request.

This is the highest-priority remaining fix.

---

## 5.1 Current race

Unsafe ordering:

```text
generate req_id
        ↓
await ws.send(subscribe request)
        ↓
create _pending_subscriptions[req_id]
```

Possible runtime interleaving:

```text
Task A:
await ws.send(...)

Task B:
receives ACK before Task A resumes
        ↓
_handle_subscribe_ack(req_id)
        ↓
req_id not found
        ↓
ACK classified unknown
        ↓
ACK discarded

Task A resumes
        ↓
pending row created
```

Final incorrect state:

```text
Bybit = subscribed
bot = still pending
```

---

## 5.2 Required invariant

Before any subscribe frame becomes visible to the network:

```text
_pending_subscriptions[req_id]
```

must already exist.

---

## 5.3 Required ordering

Use:

```python
pending = PendingSubscription(
    req_id=req_id,
    symbols=...,
    topics=...,
    sent_at=time.monotonic(),
    attempt=...,
)

self._pending_subscriptions[req_id] = pending

try:
    await self._ws.send(json.dumps(payload))
except Exception:
    self._pending_subscriptions.pop(req_id, None)
    raise
```

Never:

```python
await send()
pending[...] = ...
```

---

## 5.4 Subscription-operation serialization

Add a dedicated lock:

```python
self._subscription_lock = asyncio.Lock()
```

Use it around subscription state mutation and send batching.

Required invariant:

```text
only one task may mutate
desired/pending/confirmed subscription state
during a subscribe synchronization operation
```

This prevents:

```text
startup resubscribe
+
dynamic listing sync
+
manual/internal resync
```

from creating overlapping batches for the same symbol.

---

## 5.5 Lock scope

The lock must protect:

```text
calculate missing symbols
create batches
register pending entries
send batches
```

Do not hold it while waiting for ACKs.

ACK handling must remain able to run concurrently.

---

## 5.6 Send failure rollback

If:

```text
pending registered
↓
send fails
```

then:

```text
remove pending batch
propagate/handle error
allow reconnect/resubscribe
```

Do not leave false pending state.

---

## 5.7 Required tests

Add:

```text
test_ack_arriving_during_send_is_not_lost
test_pending_exists_before_subscribe_frame_is_observable
test_send_failure_removes_pending_batch
test_concurrent_resubscribe_calls_do_not_duplicate_batches
test_dynamic_listing_and_startup_resubscribe_do_not_duplicate_symbol
```

### Critical race test

Create a fake WS whose `send()` does:

```text
parse req_id
immediately call client's ACK handler
only then return from send()
```

Expected:

```text
ACK finds pending row
pending removed
symbols confirmed
```

This test must fail on the current implementation before the fix.

---

## 5.8 State invariants after fix

At all times a desired symbol must be in at most one of:

```text
pending
confirmed
```

Never both.

And:

```text
confirmed ⊆ desired
```

unless the symbol was just removed and unsubscribe/reconnect reconciliation is still in progress.

---

## Loop

```text
write race test
→ confirm failure
→ register pending before send
→ add subscription lock
→ test send rollback
→ test concurrent sync
→ run WS tests
→ run full suite
```

## Exit gate

No ACK can be lost because the pending record did not yet exist.

## Commit

```text
Phase J1: eliminate WebSocket ACK registration race
```

---

# 6. Phase J2 — Make ACK Timeout Independent of Receive Silence

## Goal

Ensure a missing subscription ACK is detected even while other market traffic is continuously arriving.

---

## 6.1 Current weakness

Current conceptual implementation:

```python
try:
    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)

except asyncio.TimeoutError:
    check_pending_ack_timeouts()
```

This means timeout checks depend on:

```text
receiving no WebSocket data for 0.5 sec
```

That is unsafe on an active market stream.

---

## 6.2 Failure scenario

```text
500+ symbols already subscribed
        ↓
new XYZUSDT subscription sent
        ↓
XYZ ACK lost
        ↓
other ticker messages continue arriving continuously
        ↓
ws.recv() never raises asyncio.TimeoutError
        ↓
ACK watchdog never runs
        ↓
XYZ remains pending indefinitely
```

---

## 6.3 Required design

ACK expiry must be checked independently of receive inactivity.

### Preferred implementation

Create:

```python
def _expired_pending_subscriptions(
    self,
    now_monotonic: float | None = None,
) -> list[PendingSubscription]:
    ...
```

Then call it:

```text
after each received WS frame
AND
after recv timeout
```

This is the smallest safe change.

---

## 6.4 Alternative implementation

A separate periodic watchdog task is also acceptable:

```text
every 250–500 ms
→ inspect pending ACK ages
→ trigger recovery
```

But do not add an extra task if the simpler per-frame check is sufficient.

---

## 6.5 Recovery semantics

When any subscription batch exceeds:

```text
WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS
```

then:

```text
log exact req_id(s)
mark connection/subscription state unhealthy
trigger reconnect
finally clears connection state
reconnect clears pending/confirmed
desired remains
resubscribe desired universe
```

---

## 6.6 Do not silently delete timed-out pending state and continue

The safer recovery is:

```text
timeout
→ reconnect
```

because the server-side acceptance state is unknown.

---

## 6.7 Required tests

Add:

```text
test_ack_timeout_fires_while_other_market_messages_are_flowing
test_ack_timeout_reconnects_even_with_continuous_pong_frames
test_ack_timeout_reconnects_even_with_continuous_ticker_frames
test_ack_timeout_logs_expired_req_id
```

### Critical test

Configure:

```text
ACK timeout = 0.10 sec
server sends another valid WS frame every 0.03 sec
server never ACKs subscription
```

Expected before:

```text
connection remains alive
pending remains forever
```

Expected after fix:

```text
connection exits/reconnects after ~ACK timeout
pending does not remain indefinitely
```

---

## Loop

```text
write continuous-traffic timeout test
→ confirm failure
→ extract timeout checker
→ call after every frame + receive timeout
→ run WS hardening tests
→ run reconnect tests
→ full suite
```

## Exit gate

No amount of unrelated WS traffic can suppress the subscription ACK watchdog.

## Commit

```text
Phase J2: enforce ACK timeout during continuous WebSocket traffic
```

---

# 7. Phase J3 — Separate Connection Heartbeat From Ticker Freshness

## Goal

Ensure ping/pong control traffic cannot make a dead market ticker feed appear healthy.

---

## 7.1 Current semantic problem

The same timestamps are being used for:

```text
any WS message
and
ticker freshness
```

A pong updates those timestamps.

That means:

```text
ticker stream dead
but pongs continue
→ ticker age appears fresh
```

---

## 7.2 Required timestamp model

Add separate fields:

```python
self._last_any_message_at: float | None
self._last_any_message_monotonic: float | None

self._last_ticker_at: float | None
self._last_ticker_monotonic: float | None
```

---

## 7.3 Update rules

### Any valid incoming frame

Update:

```text
_last_any_message_*
```

This includes:

```text
pong
subscription ACK
ticker
other recognized control messages
```

### Ticker frame only

Update:

```text
_last_ticker_*
```

---

## 7.4 Market-feed stale watchdog

Use:

```text
_last_ticker_monotonic
```

for:

```text
ticker stale detection
market health
```

Do not use pong/control traffic.

---

## 7.5 Connection heartbeat

Use:

```text
_last_any_message_monotonic
```

for connection-level diagnostics only.

If desired, expose separate health fields:

```text
last_ws_message_age
last_ticker_age
```

Do not collapse them back together.

---

## 7.6 Startup semantics

On connection establishment:

```text
_last_any_message_monotonic = now
_last_ticker_monotonic = None
```

A connection that has never received a ticker should not look like it has fresh ticker data.

---

## 7.7 REST fallback

Do not remove the existing REST fallback.

The desired behavior remains:

```text
WS primary
REST fallback/safety
```

The purpose of this fix is to make WS feed degradation visible and recoverable.

---

## 7.8 Required tests

Add:

```text
test_pong_updates_connection_freshness_not_ticker_freshness
test_subscription_ack_updates_any_message_not_ticker_timestamp
test_ticker_updates_both_any_message_and_ticker_freshness
test_continuous_pong_without_ticker_eventually_triggers_ticker_stale_reconnect
test_health_reports_ticker_age_from_ticker_timestamp_only
```

### Critical test

Server behavior:

```text
connect
ACK subscription
never send ticker
reply/send pong every 10–20 sec equivalent in accelerated test
```

Expected:

```text
connection control traffic remains fresh
ticker freshness becomes stale
client reconnects
```

---

## Loop

```text
write pong-vs-ticker regression
→ confirm failure
→ add separate timestamps
→ update parser/handlers
→ update stale watchdog
→ update HealthMonitor accessors
→ run WS + health tests
→ full suite
```

## Exit gate

Pong/control traffic can never reset market ticker freshness.

## Commit

```text
Phase J3: separate WebSocket heartbeat and ticker freshness
```

---

# 8. Phase J4 — Make Container Health Respect Configured Heartbeat Threshold

## Goal

Ensure:

```text
HEALTH_HEARTBEAT_STALE_SECONDS
```

actually controls the standalone container healthcheck.

---

## 8.1 Current mismatch

Application configuration exposes:

```text
HEALTH_HEARTBEAT_STALE_SECONDS
```

but the healthcheck script currently uses a constant.

This creates two sources of truth.

---

## 8.2 Required fix

In:

```text
scripts/container_healthcheck.py
```

use:

```python
def heartbeat_stale_seconds() -> float:
    raw = os.environ.get(
        "HEALTH_HEARTBEAT_STALE_SECONDS",
        "120",
    )
    value = float(raw)

    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            "HEALTH_HEARTBEAT_STALE_SECONDS must be finite and > 0"
        )

    return value
```

Then use:

```python
threshold = heartbeat_stale_seconds()
```

instead of a hard-coded constant.

---

## 8.3 Docker environment

No Dockerfile hard-code is required if the application/container already receives normal environment variables.

Default remains:

```text
120 seconds
```

---

## 8.4 Fail-safe behavior

Invalid health threshold:

```text
NaN
0
-1
non-number
```

should cause:

```text
healthcheck exit 1
```

not silently fall back to another value.

---

## 8.5 Required tests

Add:

```text
test_healthcheck_uses_default_heartbeat_threshold
test_healthcheck_respects_custom_heartbeat_threshold
test_healthcheck_rejects_zero_threshold
test_healthcheck_rejects_negative_threshold
test_healthcheck_rejects_nan_threshold
```

### Required vector

Snapshot age:

```text
200 seconds
```

Case A:

```text
HEALTH_HEARTBEAT_STALE_SECONDS=300
→ healthy
```

Case B:

```text
HEALTH_HEARTBEAT_STALE_SECONDS=120
→ unhealthy
```

---

## Loop

```text
write custom-threshold test
→ confirm failure
→ read env
→ validate value
→ run healthcheck tests
→ Docker script test
→ full suite
```

## Exit gate

Runtime health configuration and Docker healthcheck use the same threshold.

## Commit

```text
Phase J4: honor configured health heartbeat threshold
```

---

# 9. Phase J5 — Make Grace-Window Critical Failures Report Degraded

## Goal

Ensure observability accurately distinguishes:

```text
healthy
degraded
unhealthy
```

during temporary critical-subsystem failures.

---

## 9.1 Desired semantics

### Healthy

No current critical or non-critical issue.

### Degraded

At least one issue exists, but no critical issue has exceeded its failure grace.

Examples:

```text
Spot WS reconnecting for 20 sec
Linear WS reconnecting for 30 sec
REST temporarily stale inside grace
Telegram retry queue non-empty
temporary Telegram error
```

### Unhealthy

Critical issue persisted beyond:

```text
CRITICAL_HEALTH_FAILURE_SECONDS
```

or database is inaccessible.

---

## 9.2 Current gap

A WS issue inside grace can currently produce:

```text
overall = healthy
```

because it is not yet persisted and is not added to the degraded list.

That is misleading.

---

## 9.3 Required classification

Conceptually:

```python
current_critical = {...}
persisted_critical = {...}

if database_failed:
    overall = "unhealthy"

elif persisted_critical:
    overall = "unhealthy"

elif current_critical or degraded_noncritical:
    overall = "degraded"

else:
    overall = "healthy"
```

---

## 9.4 Health snapshot detail

Persist:

```text
critical_issues
pending_critical_issues
degraded_issues
```

Suggested:

```python
"critical_issues": [...],
"pending_critical_issues": [...],
"degraded_issues": [...],
```

This helps operators distinguish:

```text
currently reconnecting
vs
persistently broken
```

---

## 9.5 Docker semantics

Container healthcheck should still:

```text
exit 0 for healthy
exit 0 for degraded
exit 1 for unhealthy
```

Do not restart the service for every short reconnect.

---

## 9.6 Required tests

Add/update:

```text
test_short_spot_disconnect_is_degraded
test_short_linear_disconnect_is_degraded
test_short_rest_failure_is_degraded
test_persisted_spot_failure_becomes_unhealthy
test_recovery_returns_healthy_and_clears_pending_critical
test_degraded_container_healthcheck_still_exits_zero
```

Remove/update any old test asserting:

```text
short critical failure == healthy
```

because the desired semantics are now:

```text
degraded
```

---

## Loop

```text
update failing semantic test
→ classify current vs persisted critical
→ persist both fields
→ run health tests
→ run container health tests
→ full suite
```

## Exit gate

A subsystem that is currently broken never reports overall healthy.

## Commit

```text
Phase J5: report grace-window critical failures as degraded
```

---

# 10. Phase J6 — Correct Release Documentation

## Goal

Make operator-facing documents reflect the actual current release state.

This phase is documentation-only.

---

## 10.1 `SOAK.md`

The previous soak is invalid after H-series runtime changes.

Update:

```text
STATUS: INVALIDATED
```

Document:

```text
reason:
H1-H3 changed runtime behavior

old soak start:
historical only
not valid for final release
```

Required next soak sequence:

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

Do not preserve wording that implies the old clock is still running for final acceptance.

---

## 10.2 README broken plan reference

Find the missing reference to:

```text
bybit_telegram_momentum_master_implementation_plan.md
```

Replace it with an existing authoritative source.

Preferred:

```text
SPEC.md
```

for product behavior.

And optionally:

```text
bybit_ws_ops_hardening_plan.md
```

as historical implementation-plan evidence.

Do not refer to nonexistent files.

---

## 10.3 README status

Ensure:

```text
PRODUCTION READY: NO
```

until F10 + new soak pass.

---

## 10.4 Documentation consistency scan

Verify:

```text
README.md
SPEC.md
SOAK.md
STAGING_VALIDATION.md
AUDIT_REMEDIATION_STATUS.md
```

do not contradict each other on:

```text
production status
soak status
supported markets
test count
health semantics
```

---

## Loop

```text
update SOAK status
→ fix README reference
→ grep old plan filename
→ grep old soak start wording
→ verify all referenced paths exist
→ review docs together
```

## Exit gate

No operator-facing document suggests the invalidated soak is still valid.

## Commit

```text
Phase J6: sync soak and release documentation
```

---

# 11. Phase J7 — Final Regression Expansion and Evidence Refresh

## Goal

Freeze the repaired runtime and regenerate evidence from the actual final J-series code.

---

## 11.1 Required new regression coverage

At minimum the final suite must contain tests for:

```text
ACK during send race
concurrent subscription synchronization
send-failure pending rollback
ACK timeout under continuous ticker traffic
ACK timeout under continuous pong/control traffic
ticker freshness unaffected by pong
ticker stale reconnect while pongs continue
custom health heartbeat threshold
invalid health heartbeat threshold
short WS failure = degraded
persisted WS failure = unhealthy
```

---

## 11.2 Five consecutive test runs

Run the entire suite five consecutive times:

```bash
python -m pytest tests -q
```

Save:

```text
artifacts/j-final-suite-run1.txt
artifacts/j-final-suite-run2.txt
artifacts/j-final-suite-run3.txt
artifacts/j-final-suite-run4.txt
artifacts/j-final-suite-run5.txt
artifacts/j-final-suite-gate.txt
```

If any run fails:

```text
gate fails
investigate
restart from run 1
```

---

## 11.3 Docker rebuild

Build:

```bash
docker build --no-cache -t bybit-monitor:final-j .
```

Record:

```text
commit
image ID
Python version
dependency versions
test count
```

Run the complete suite inside the image.

Save:

```text
artifacts/j-docker-test-results.txt
```

---

## 11.4 Live Bybit staging

Extend `scripts/staging_validation.py`.

Required validations:

```text
Spot subscribe request
Spot ACK pending→confirmed

Linear subscribe request
Linear ACK pending→confirmed

dynamic subscribe race-safe path
ticker freshness separate from heartbeat freshness

discovery stable
new listing dry-run
outbox/listing delivery using stub
```

The live public staging cannot force Bybit to reject an ACK reliably, so failed-ACK behavior remains regression-test/fault-injection coverage.

---

## 11.5 Evidence consistency

Update:

```text
STAGING_VALIDATION.md
AUDIT_REMEDIATION_STATUS.md
```

Add J-series phase table.

Do not claim:

```text
PRODUCTION READY: YES
```

---

## Exit gate

The final J-series code passes:

```text
5 consecutive local/full suite runs
+
Docker suite
+
live public Bybit staging
```

## Commit

```text
Phase J7: refresh post-H-series validation evidence
```

---

# 12. Phase J8 — Independent Pre-F10 Code Review

## Goal

Perform one final static review before introducing real Telegram credentials.

Review these exact invariants.

---

## 12.1 Subscription state

```text
pending registered before send
ACK cannot beat pending registration
failed send removes pending
failed ACK never confirms
timeout independent of receive silence
reconnect preserves desired
reconnect clears pending/confirmed
```

---

## 12.2 Freshness state

```text
pong ≠ ticker
ACK ≠ ticker
ticker updates ticker freshness
health reads ticker freshness
ticker stale detection uses ticker timestamp
```

---

## 12.3 Health

```text
healthy = no current issue
degraded = temporary/current issue inside grace
unhealthy = persisted critical issue or DB failure
```

---

## 12.4 Documentation

```text
old soak invalidated
README links valid
production ready remains NO
```

---

## Exit gate

No unresolved P0/P1 issue remains before real Telegram staging.

---

# 13. F10 — Real Telegram Staging

After J1–J8 are complete, proceed with real Telegram staging.

Credentials must be provided only through runtime secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Never:

```text
print them
commit them
write them to artifacts
```

---

## 13.1 Required real tests

### Transition

```text
synthetic/controlled qualifying state
→ atomic outbox
→ dispatcher
→ real Telegram
→ outbox sent
```

### Listing

```text
synthetic listing
→ durable outbox
→ real Telegram
→ listing telegram_sent=1
```

### Temporary failure

```text
delivery failure
→ retry persisted
→ monitoring continues
→ recovery
→ delivery succeeds
```

### Restart recovery

```text
retry/pending exists
→ restart
→ dispatcher recovers
→ no duplicate logical notification
```

---

## Exit gate

F10 must be:

```text
PASS
```

before final soak.

---

# 14. Final Fresh 24-Hour Soak

After F10:

```text
build exact final image
freeze runtime code/config
start new soak from zero
```

Record:

```text
START UTC
FINAL COMMIT
IMAGE ID
PYTHON VERSION
DEPENDENCY LOCK HASH
DATABASE MIGRATION VERSION
```

---

## 14.1 Mandatory interventions

During the ≥24-hour soak:

1. Three container restarts.
2. One temporary network outage.
3. One forced Spot WS reconnect.
4. One forced Linear WS reconnect.
5. One Telegram failure + recovery.
6. Restart with pending/retry outbox.
7. Restart in `ACTIVE_RANGE`.
8. Synthetic `OVER_RANGE → ACTIVE_RANGE`.
9. Verify Spot history continuity.
10. Dispatcher transient DB failure/recovery.
11. Failed subscription ACK fault-injection recovery.
12. ACK timeout recovery while other ticker traffic continues.
13. Pong-only / no-ticker stale-feed recovery.
14. Sustained critical health failure → container `unhealthy`.
15. Recovery → container health returns `healthy`.

---

## 14.2 Soak failure criteria

Fail if any:

```text
ACK lost because pending was not registered
pending ACK stuck indefinitely under continuous traffic
pong prevents ticker stale detection
dead market feed stays healthy
subscription batch falsely confirmed
Docker heartbeat threshold ignores configuration
temporary critical failure reports healthy
notification lost
duplicate alert storm
SQLite deadlock
dispatcher silently dies
4+ qualifying coins produce range alert
debounce bypass
memory/outbox grows without bound
```

---

# 15. Final Production-Ready Decision

Only after:

```text
J1-J8 COMPLETE
+
F10 REAL TELEGRAM PASS
+
FRESH >=24H SOAK PASS
+
FINAL CLEAN TEST SUITE PASS
```

may the agent set:

```text
STATUS: DONE
PRODUCTION READY: YES
```

Otherwise:

```text
PRODUCTION READY: NO
```

---

# 16. Mandatory Agent Phase Report

After every J phase:

```text
PHASE:
STATUS: COMPLETE | IN PROGRESS | BLOCKED

BASE COMMIT:
NEW COMMIT:

GOAL:
- ...

FILES CREATED:
- ...

FILES CHANGED:
- ...

REGRESSION TESTS ADDED:
- ...

TEST COMMANDS:
- ...

TEST RESULTS:
- ...

FAULT/CONCURRENCY TESTS:
- ...

LIVE/STAGING VALIDATION:
- ...

KNOWN ISSUES:
- ...

REMAINING RISKS:
- ...

NEXT PHASE:
- ...
```

---

# 17. Commit Discipline

Use separate commits:

```text
Phase J1: eliminate WebSocket ACK registration race
Phase J2: enforce ACK timeout during continuous WebSocket traffic
Phase J3: separate WebSocket heartbeat and ticker freshness
Phase J4: honor configured health heartbeat threshold
Phase J5: report grace-window critical failures as degraded
Phase J6: sync soak and release documentation
Phase J7: refresh post-H-series validation evidence
Phase J8: final pre-Telegram reliability review
```

Do not squash unrelated phases during development.

---

# 18. Stop Conditions

Stop and report `BLOCKED` if:

- ACK race test cannot reproduce the existing bug.
- Continuous-traffic ACK timeout cannot be made deterministic.
- Subscription locking introduces deadlock.
- Live Bybit staging shows unexpected ACK shape.
- WebSocket ticker freshness cannot be distinguished from control traffic.
- Full suite becomes flaky.
- Docker health configuration differs from application configuration.
- Real Telegram credentials are unavailable at F10.
- Any runtime code changes after final soak begins.

Do not weaken acceptance requirements.

---

# 19. Definition of Done for This Remediation

This J-series remediation is complete when:

```text
ACK registration race fixed
ACK timeout independent from receive silence
ticker freshness separated from heartbeat freshness
healthcheck uses configured heartbeat threshold
short critical failure reports degraded
SOAK.md corrected
README broken reference corrected
full evidence refreshed
```

At that point:

```text
READY FOR REAL TELEGRAM F10
```

not yet:

```text
PRODUCTION READY
```

Production readiness still requires:

```text
F10
+
fresh final 24-hour soak
+
final acceptance
```

---

# 20. Final Instruction to the AI Coding Agent

Do not optimize for test count.

Optimize for these invariants:

```text
1. Pending subscription state exists before a subscribe frame can be ACKed.

2. Missing ACK detection runs even when unrelated WebSocket traffic is continuous.

3. Ping/pong/control traffic can prove connection liveness,
   but only ticker traffic can prove ticker freshness.

4. Container health uses the same configured heartbeat threshold
   as the application/operator configuration.

5. A currently broken critical subsystem is degraded during grace,
   not healthy.

6. The old soak is invalid and cannot count toward production acceptance.
```

When J1–J8 are complete, regenerate evidence, complete F10 with real Telegram, then start the final 24-hour soak from zero.
