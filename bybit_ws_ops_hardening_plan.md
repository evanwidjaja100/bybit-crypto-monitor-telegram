# Bybit Live Momentum Monitor → Telegram
## Final WebSocket & Operations Hardening Implementation Plan

**Document purpose:** This file is the authoritative implementation plan for the final five hardening items identified in the latest independent audit.

**Starting state:** The repository is already strongly remediated. Core market logic, SQLite task-aware transaction ownership, strict `>5%` qualification, durable Telegram retry, listing acknowledgement, Bybit REST contract handling, health clocks, Docker Python alignment, and the current automated acceptance gates are already in place.

**This plan is intentionally narrow. Do not add unrelated features.**

---

# 1. Scope

This plan covers only these five items:

1. Validate Bybit WebSocket subscription acknowledgements before considering topics subscribed.
2. Always mark the WebSocket client disconnected through a `finally` cleanup path.
3. Connect Docker/container health to critical application health rather than only DB freshness.
4. Remove the duplicate `_save()` method and correct README healthcheck documentation drift.
5. Refresh stale acceptance artifacts so final evidence consistently reflects the current hardening commit.

Everything outside this list is out of scope.

---

# 2. Locked Product Behavior

Do not change:

```python
change_1h > 5.0
```

Do not change:

```python
1 <= unique_qualifying_base_coins <= 3
```

Do not change:

- Spot monitoring.
- Linear USDT monitoring.
- Linear USDC monitoring.
- Unique `baseCoin` aggregation.
- 20-second debounce default.
- Durable SQLite outbox.
- Telegram retry semantics.
- Listing delivery acknowledgement.
- Automatic new-listing discovery.

Do not add:

- Trading.
- Options.
- Inverse contracts.
- Redis.
- PostgreSQL.
- Kubernetes.
- Web UI.
- Telegram commands.

---

# 3. Mandatory Execution Loop

For each issue:

```text
WRITE A REGRESSION TEST
        ↓
CONFIRM CURRENT CODE FAILS
        ↓
IMPLEMENT MINIMUM CORRECT FIX
        ↓
RUN TARGETED TEST
        ↓
RUN RELATED TESTS
        ↓
RUN FULL SUITE
        ↓
RUN LIVE/STAGING CHECK IF RELEVANT
        ↓
DOCUMENT RESULT
        ↓
COMMIT
```

Do not mark an issue fixed because the code merely “looks correct.”

---

# 4. Phase H0 — Freeze the Current Candidate

## Goal

Create an auditable starting point.

## Required actions

Create branch:

```text
final-ws-ops-hardening
```

Record:

```text
BASE COMMIT:
CURRENT TEST COUNT:
PYTHON VERSION:
DOCKER IMAGE ID:
GIT STATUS:
```

Run:

```bash
python -m pytest tests -q
```

Save:

```text
artifacts/ws-ops-hardening-baseline.txt
```

Confirm:

```text
.env is not tracked
no Telegram token is tracked
no chat ID is tracked
no private keys are tracked
```

## Exit gate

Do not begin runtime changes until the baseline is recorded.

---

# 5. Phase H1 — WebSocket Subscription ACK Validation

## Goal

Never treat a Bybit topic as successfully subscribed merely because a subscription request was sent.

A topic becomes **confirmed subscribed only after Bybit returns a successful subscription acknowledgement**.

---

## 5.1 Problem

Unsafe conceptual behavior:

```text
send subscribe request
        ↓
_subscribed.update(batch)
```

This assumes:

```text
request sent = server accepted
```

That is not a safe invariant.

The server can acknowledge success or failure.

---

## 5.2 Required state model

Move from:

```text
desired
confirmed
```

to:

```text
desired
pending
confirmed
```

Recommended fields:

```python
self._desired_symbols: set[str]
self._subscribed: set[str]
self._pending_subscriptions: dict[str, PendingSubscription]
```

Recommended model:

```python
@dataclass
class PendingSubscription:
    req_id: str
    symbols: tuple[str, ...]
    topics: tuple[str, ...]
    sent_at: float
    attempt: int
```

---

## 5.3 Unique request IDs

Every subscription request must contain a unique `req_id`.

Example:

```python
req_id = f"sub-{category}-{sequence}"
```

Payload:

```json
{
  "req_id": "sub-linear-42",
  "op": "subscribe",
  "args": [
    "tickers.BTCUSDT",
    "tickers.ETHUSDT"
  ]
}
```

The request ID must correlate the ACK to the exact pending batch.

---

## 5.4 Required subscribe flow

```text
desired symbol detected
        ↓
symbol not confirmed
symbol not pending
        ↓
create request batch
        ↓
assign req_id
        ↓
send subscription request
        ↓
store batch as pending
        ↓
WAIT FOR ACK
```

Do **not** update `_subscribed` at send time.

---

## 5.5 Successful ACK

When:

```text
op == "subscribe"
success == true
req_id matches pending request
```

then:

```text
remove pending request
        ↓
add exact batch symbols to confirmed `_subscribed`
        ↓
record ACK success
```

Recommended log:

```text
event=ws_subscription_ack
category=linear
req_id=sub-linear-42
success=true
symbols=10
```

---

## 5.6 Failed ACK

When:

```text
op == "subscribe"
success == false
```

then:

```text
remove pending request
        ↓
DO NOT add symbols to `_subscribed`
        ↓
record failure
        ↓
trigger safe recovery
```

### Recommended recovery for this release

Keep it simple:

```text
failed ACK
→ mark subscription state unhealthy
→ reconnect category WebSocket
→ rebuild subscriptions from desired set
```

Avoid introducing a complex partial-subscription retry engine unless truly needed.

---

## 5.7 ACK timeout

Add configuration:

```text
WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS=10
```

A pending subscription must not remain pending indefinitely.

Periodic rule:

```text
if monotonic_now - pending.sent_at > ack_timeout:
    record ACK timeout
    trigger reconnect
```

---

## 5.8 Duplicate ACK

If the same successful ACK arrives twice:

```text
first ACK:
pending -> confirmed

second ACK:
no matching pending request
→ ignore safely
→ no duplicate state mutation
```

---

## 5.9 Unknown `req_id`

If an ACK references an unknown request ID:

```text
do not crash
do not confirm unrelated symbols
log debug/warning
```

---

## 5.10 Reconnect semantics

After a reconnect:

```text
confirmed = empty
pending = empty
desired = preserved
```

Then normal sync rebuilds:

```text
desired - confirmed - pending
```

---

## 5.11 Dynamic listing guarantee

Required path:

```text
new market becomes Trading
        ↓
registry updates desired universe
        ↓
subscribe request sent
        ↓
pending
        ↓
successful ACK
        ↓
confirmed
        ↓
ticker monitoring
```

If the ACK fails:

```text
the market must never silently appear "confirmed monitored"
```

---

## 5.12 Required tests

Add tests equivalent to:

```text
test_subscribe_send_does_not_mark_symbols_confirmed
test_successful_subscribe_ack_marks_symbols_confirmed
test_failed_subscribe_ack_does_not_mark_symbols_confirmed
test_failed_ack_triggers_recovery
test_unknown_subscribe_ack_is_safe
test_duplicate_subscribe_ack_is_idempotent
test_subscription_ack_timeout_triggers_recovery
test_reconnect_clears_pending_and_confirmed_but_keeps_desired
test_dynamic_listing_only_becomes_confirmed_after_ack
```

---

## 5.13 Critical batch test

Simulate:

```text
25 desired Linear symbols
batch size = 10
```

Server behavior:

```text
batch 1: 10 → ACK success
batch 2: 10 → ACK failure
batch 3: 5  → ACK success
```

Expected before recovery:

```text
confirmed = 15
failed batch symbols = not confirmed
```

Then reconnect/retry.

Expected:

```text
all 25 eventually confirmed
```

---

## 5.14 Live staging extension

Extend `scripts/staging_validation.py` to explicitly validate:

```text
subscribe request emitted
req_id recorded
ACK received
ACK success=true
symbol moves from pending to confirmed
ticker arrives
```

Validate both:

```text
Spot BTCUSDT
Linear BTCUSDT
```

Do not infer ACK correctness only from ticker arrival.

---

## Loop

```text
write failed-ACK test
→ confirm current code fails
→ add req_id
→ add pending state
→ parse subscribe ACK
→ implement success path
→ implement failure path
→ implement ACK timeout
→ run WS tests
→ run recovery tests
→ run full suite
→ run live staging
```

## Exit gate

- Sent requests are not automatically confirmed.
- Success ACK confirms only the matching batch.
- Failed ACK does not create false confirmed state.
- Missing ACK recovers automatically.
- Reconnect restores the desired universe.
- Live Spot and Linear ACK handling passes.

## Commit

```text
Phase H1: confirm Bybit WebSocket subscriptions by ACK
```

---

# 6. Phase H2 — Guaranteed Disconnect-State Cleanup

## Goal

Ensure `client.connected` always reflects the actual socket state, including exception and cancellation paths.

---

## 6.1 Required structure

Use:

```python
async def _connect_and_listen(...):
    try:
        async with websockets.connect(...) as ws:
            self._ws = ws
            self._set_status(True)
            ...
            await receive_loop()
    finally:
        self._ws = None
        self._set_status(False, reason="disconnected")
```

The `finally` cleanup must execute for:

```text
normal close
network error
protocol error
recv exception
cancellation
unexpected exception
```

---

## 6.2 Cancellation

On shutdown:

```text
task cancelled
→ finally executes
→ connected=False
→ _ws=None
→ CancelledError propagates
```

Do not swallow cancellation.

---

## 6.3 Status callback idempotency

Preferred:

```python
if self.connected != connected:
    self.connected = connected
    callback(...)
```

Avoid duplicate disconnect notifications if state is already false.

---

## 6.4 Required tests

Add:

```text
test_recv_exception_sets_connected_false_immediately
test_connection_context_exception_sets_connected_false
test_cancelled_ws_task_sets_connected_false
test_ws_reference_cleared_after_disconnect
test_disconnect_status_callback_is_emitted
test_reconnect_delay_health_sees_disconnected_not_connected
```

---

## 6.5 Critical timing test

Simulate:

```text
connect
connected=True
receive one message
raise connection exception
reconnect backoff=5 seconds
```

Before reconnect starts:

```python
assert client.connected is False
assert client._ws is None
```

---

## Loop

```text
write reconnect-delay test
→ confirm failure
→ move cleanup into finally
→ run WS tests
→ run health tests
→ run recovery tests
→ full suite
```

## Exit gate

No dead WebSocket can remain reported as connected during backoff.

## Commit

```text
Phase H2: guarantee WebSocket disconnect state cleanup
```

---

# 7. Phase H3 — Connect Docker Health to Critical Application Health

## Goal

Make container health represent critical application failure, not merely whether SQLite is still writable or changing.

---

## 7.1 Critical health dimensions

Docker/container health should account for:

```text
database
dispatcher worker
REST discovery freshness
Spot WebSocket health if Spot enabled
Linear WebSocket health if Linear enabled
application health heartbeat freshness
```

Telegram API temporary failures should generally be treated as degraded, not automatically critical.

---

## 7.2 Do not add a new web stack

Do not add Flask/FastAPI/Redis solely for health.

Preferred architecture:

```text
HealthMonitor
        ↓
persist compact health snapshot to SQLite/KV
        ↓
container healthcheck script reads snapshot
        ↓
exit 0 / exit 1
```

---

## 7.3 Persisted health snapshot

Prefer the existing `kv` table if suitable.

Example keys:

```text
health:last_updated_at
health:overall
health:database
health:dispatcher
health:rest
health:spot_ws
health:linear_ws
health:last_discovery_at
health:last_spot_ticker_at
health:last_linear_ticker_at
```

Alternative: one small `application_health` table.

Do not create unnecessary schema complexity.

---

## 7.4 Health classification

### Healthy

```text
DB healthy
dispatcher healthy
REST discovery within freshness threshold
enabled Spot WS within threshold
enabled Linear WS within threshold
health heartbeat recent
```

### Degraded but container healthy

Examples:

```text
temporary Telegram failure
short reconnect attempt
small retry queue
```

### Unhealthy

Examples:

```text
dispatcher remains unhealthy beyond grace
REST discovery stale beyond threshold
Spot WS unavailable beyond threshold
Linear WS unavailable beyond threshold
health heartbeat stale
database inaccessible
```

---

## 7.5 Grace periods

Avoid restart loops.

Recommended:

```text
critical_health_failure_seconds=180
health_heartbeat_stale_seconds=120
```

Tune against existing WS/discovery intervals.

---

## 7.6 Health writer loop

Persist health every approximately:

```text
30 seconds
```

or align with existing health loop if already shorter.

Do not block the market path.

---

## 7.7 Container healthcheck script

Create:

```text
scripts/container_healthcheck.py
```

Required behavior:

```text
open SQLite
→ verify DB readable
→ read latest application health
→ heartbeat recent?
→ critical health acceptable?
→ exit 0
```

Failure:

```text
exit 1
```

No network calls from healthcheck.

---

## 7.8 Dockerfile

Replace/extend the old DB freshness healthcheck with the new script.

Example conceptual form:

```dockerfile
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3   CMD python scripts/container_healthcheck.py || exit 1
```

Use actual final values consistently in README.

---

## 7.9 Important Docker behavior note

Do **not** claim:

```text
Docker restart policy automatically restarts any unhealthy container
```

unless your deployment environment actually provides that behavior.

Correct documentation:

```text
HEALTHCHECK exposes health status.
restart policy handles process exit.
operator/orchestrator can act on unhealthy state.
```

---

## 7.10 Required tests

Add:

```text
test_health_snapshot_persisted
test_container_healthcheck_passes_when_critical_subsystems_healthy
test_container_healthcheck_fails_when_heartbeat_stale
test_container_healthcheck_fails_when_dispatcher_unhealthy
test_container_healthcheck_fails_when_spot_ws_stale
test_container_healthcheck_fails_when_linear_ws_stale
test_temporary_telegram_failure_is_degraded_not_critical
test_disabled_stream_is_not_required_for_health
```

---

## 7.11 Docker integration tests

### Healthy

Start container and verify:

```text
docker inspect → healthy
```

### Sustained dispatcher failure

Using safe staging injection:

```text
dispatcher unhealthy beyond grace
→ container health unhealthy
```

### Restore

Expected:

```text
critical subsystem healthy
→ health returns healthy
```

---

## Loop

```text
define health policy
→ add regression tests
→ persist snapshot
→ create healthcheck script
→ update Dockerfile
→ run health tests
→ build Docker
→ run healthy integration
→ run unhealthy integration
→ run full suite
```

## Exit gate

Sustained critical application failure is visible at the container health layer.

## Commit

```text
Phase H3: connect container health to application health
```

---

# 8. Phase H4 — Remove Duplicate `_save()` and Synchronize Documentation

## Goal

Remove known maintenance drift without changing intended runtime behavior.

---

## 8.1 Duplicate `_save()`

Inspect:

```text
app/alerts/state_machine.py
```

There must be exactly one `_save()` implementation.

Before deleting:

```text
compare both definitions
identify which definition Python currently uses
verify no test relies on duplicate source structure
remove only redundant definition
```

Run all state-machine and alert-service tests afterward.

---

## 8.2 README healthcheck values

Read the final Dockerfile values first.

README must exactly match:

```text
interval
timeout
start-period
retries
```

Do not preserve old values.

---

## 8.3 README production status

Do not claim full production readiness before F10 + new 24h soak.

Recommended wording:

```text
Release candidate.
Core automated validation is complete.
Real Telegram staging and the final 24-hour soak remain mandatory before production readiness.
```

---

## 8.4 README market support

Ensure documentation states:

```text
Supported:
Spot
Linear USDT
Linear USDC

Unsupported in this release:
Inverse
Options
```

---

## 8.5 README WebSocket behavior

After H1, document:

```text
subscription requests are tracked as pending
topics become confirmed only after successful Bybit ACK
failed/timed-out ACKs trigger recovery
```

---

## 8.6 Documentation verification

Verify every referenced:

```text
file
script
Docker service name
test command
healthcheck command
```

exists and matches reality.

---

## Loop

```text
remove duplicate method
→ run state-machine tests
→ inspect Dockerfile
→ update README values
→ update release wording
→ verify referenced paths/commands
→ run full suite
```

## Exit gate

- Exactly one `_save()` remains.
- README matches actual runtime.
- README does not overstate release status.

## Commit

```text
Phase H4: remove duplicate state helper and sync docs
```

---

# 9. Phase H5 — Refresh Final Validation Evidence

## Goal

Ensure all acceptance artifacts reflect the same new final hardening commit and image.

Do not manually change old test counts.

Regenerate from real executions.

---

## 9.1 Required artifacts

Regenerate:

```text
artifacts/docker-test-results.txt
artifacts/final-suite-run1.txt
artifacts/final-suite-run2.txt
artifacts/final-suite-run3.txt
artifacts/final-suite-run4.txt
artifacts/final-suite-run5.txt
artifacts/final-suite-gate.txt
artifacts/staging-validation.txt
```

Update:

```text
STAGING_VALIDATION.md
AUDIT_REMEDIATION_STATUS.md
```

---

## 9.2 Final Docker validation

Build:

```bash
docker build --no-cache -t bybit-monitor:final .
```

Record:

```text
UTC timestamp
commit
image ID
Python version
test count
test result
```

Run the complete test suite inside that image.

---

## 9.3 Five-run gate

Run the full suite five consecutive times on the exact same commit.

If any run fails:

```text
gate fails
investigate
restart from run 1
```

Do not select only passing runs.

---

## 9.4 Live staging rerun

Re-run:

```bash
python scripts/staging_validation.py
```

The updated staging artifact must include:

```text
Spot subscription ACK success
Linear subscription ACK success
pending → confirmed transition
Spot ticker received
Linear ticker received
top-level ts valid
dynamic subscription valid
new-listing dry-run valid
```

---

## 9.5 Evidence consistency audit

Every current release artifact should refer to:

```text
same final commit
same final Docker image where applicable
same current test count
```

Old historical artifacts may remain if clearly labeled historical, but no current release summary should cite stale `304 passed` evidence as final.

---

## 9.6 Status update

Add H-series status:

```text
H1 COMPLETE
H2 COMPLETE
H3 COMPLETE
H4 COMPLETE
H5 COMPLETE
```

But keep:

```text
F10 real Telegram → pending/blocked
F12 final soak → must restart
PRODUCTION READY → NO
```

---

## Loop

```text
freeze code
→ full suite
→ Docker no-cache build
→ Docker suite
→ 5 consecutive local/full runs
→ live staging
→ regenerate evidence
→ inspect consistency
→ commit artifacts
```

## Exit gate

All final validation evidence represents the same hardening candidate.

## Commit

```text
Phase H5: refresh final validation evidence
```

---

# 10. Post-Hardening Release Sequence

Because H1–H3 change runtime behavior, any currently running final soak is invalid for final acceptance.

After H5:

```text
H1–H5 FINAL COMMIT
        ↓
REAL TELEGRAM F10
        ↓
BUILD FINAL IMAGE
        ↓
START FRESH 24H SOAK FROM ZERO
        ↓
COMPLETE ALL INTERVENTIONS
        ↓
F13 FINAL ACCEPTANCE
```

---

# 11. Real Telegram Gate

Provide credentials via runtime environment only:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Never commit or print them.

Validate real production paths:

```text
transition alert
listing alert
retryable failure
recovery
restart with pending retry
no duplicate application-level delivery
```

Do not start the final soak until F10 passes.

---

# 12. Fresh Final 24-Hour Soak

Record at start:

```text
START UTC:
COMMIT:
IMAGE ID:
PYTHON VERSION:
DEPENDENCY LOCK HASH:
MIGRATION VERSION:
```

Run for:

```text
>=24 actual elapsed hours
```

Any runtime code/config change after start:

```text
invalidate soak
restart clock from zero
```

---

# 13. Required New Soak Interventions

Perform:

1. Three container restarts.
2. One temporary network outage.
3. One forced Spot WS reconnect.
4. One forced Linear WS reconnect.
5. One Telegram failure and recovery.
6. One restart with pending/retry outbox.
7. One restart while state is `ACTIVE_RANGE`.
8. One synthetic `OVER_RANGE → ACTIVE_RANGE`.
9. Verify Spot history continuity.
10. Verify dispatcher recovery after transient repository failure.
11. **Verify failed subscription ACK recovery.**
12. **Verify subscription ACK timeout recovery.**
13. **Verify a sustained critical subsystem failure becomes container `unhealthy`.**
14. **Verify recovery returns container health to `healthy`.**

---

# 14. Soak Failure Criteria

Fail if any:

```text
failed subscription ACK leaves symbols falsely confirmed
ACK timeout leaves symbols silently missing
dead WS remains connected=true
critical application failure remains falsely healthy beyond grace
dispatcher silently dies
SQLite deadlock
notification loss
duplicate storm
false listing storm
4+ coins produce range alert
debounce bypass
unbounded retry queue
unbounded memory growth
```

---

# 15. Final Production Checklist

## WebSocket ACK

- [ ] Requests have unique `req_id`.
- [ ] Sent request does not equal confirmed subscription.
- [ ] Success ACK confirms exact batch.
- [ ] Failure ACK does not confirm batch.
- [ ] Failure ACK triggers recovery.
- [ ] ACK timeout triggers recovery.
- [ ] Unknown ACK safe.
- [ ] Duplicate ACK safe.
- [ ] Reconnect rebuilds desired universe.
- [ ] Dynamic new listing becomes confirmed only after successful ACK.

## Disconnect state

- [ ] Normal close sets disconnected.
- [ ] Receive exception sets disconnected immediately.
- [ ] Cancellation sets disconnected.
- [ ] `_ws` cleared on every path.
- [ ] Health sees disconnected during reconnect delay.

## Container health

- [ ] DB represented.
- [ ] Health heartbeat recent.
- [ ] Dispatcher represented.
- [ ] REST represented.
- [ ] Spot WS represented when enabled.
- [ ] Linear WS represented when enabled.
- [ ] Temporary Telegram issue does not force critical failure.
- [ ] Sustained critical failure produces `unhealthy`.

## Cleanup

- [ ] Duplicate `_save()` removed.
- [ ] README healthcheck values match Dockerfile.
- [ ] README market universe accurate.
- [ ] README release status accurate.
- [ ] README ACK semantics accurate.

## Evidence

- [ ] Docker test artifact regenerated.
- [ ] Five-run artifacts regenerated.
- [ ] Staging artifact regenerated.
- [ ] Evidence references same hardening commit/image.
- [ ] Current release summary has no stale final-test count.

## Final release

- [ ] Real Telegram staging passed.
- [ ] Fresh final image built.
- [ ] New 24-hour soak passed.
- [ ] All mandatory interventions passed.
- [ ] Final F13 acceptance passed.

Only then:

```text
STATUS: DONE
PRODUCTION READY: YES
```

---

# 16. Mandatory Agent Phase Report

After each phase:

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

TESTS RUN:
- ...

TEST RESULTS:
- ...

LIVE/STAGING VALIDATION:
- ...

FAULT/RECOVERY VALIDATION:
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
Phase H1: confirm Bybit WebSocket subscriptions by ACK
Phase H2: guarantee WebSocket disconnect state cleanup
Phase H3: connect container health to application health
Phase H4: remove duplicate state helper and sync docs
Phase H5: refresh final validation evidence
```

Do not combine all changes into one commit.

---

# 18. Stop Conditions

Stop and report `BLOCKED` if:

- ACKs cannot be reliably correlated with batches.
- Failed subscriptions can still become confirmed.
- Reconnect becomes flaky.
- Container health cannot distinguish temporary recovery from sustained failure.
- Full suite becomes intermittent.
- Live staging contradicts assumptions.
- Real Telegram credentials are unavailable at F10.
- Any runtime code changes after the new soak starts.

Do not weaken the gates to continue.

---

# 19. Definition of Done for This H-Series Plan

H-series completion means:

```text
H1 ACK validation PASS
H2 disconnect cleanup PASS
H3 container health PASS
H4 cleanup/docs PASS
H5 evidence refresh PASS
```

At that point:

```text
READY FOR FINAL TELEGRAM + SOAK VALIDATION
```

but **not yet production-ready**.

Full production readiness still requires:

```text
H1–H5
+
F10 real Telegram
+
fresh >=24h soak
+
F13 final acceptance
```

---

# 20. Final Instruction to the AI Agent

The highest-priority invariant is:

```text
A WebSocket topic is not confirmed subscribed
until Bybit explicitly acknowledges the request successfully.
```

The second critical invariant is:

```text
A dead WebSocket must never remain reported as connected.
```

The operational invariant is:

```text
Container health must represent sustained critical application failure,
not merely SQLite freshness.
```

After H1–H5, regenerate all release evidence and restart the final production soak from zero.
