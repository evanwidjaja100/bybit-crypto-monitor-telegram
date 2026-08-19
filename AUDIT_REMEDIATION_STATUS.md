# Audit Remediation Status

Phase R0 — Freeze and Baseline the Current Release Candidate

```
BASELINE COMMIT: 53089ffb0ae00267dc25956436e63291c3162e7f
BASELINE TEST COUNT: 238
BASELINE PASS/FAIL: 238 passed / 0 failed
PYTHON: 3.14.6 (cp314, Windows)
DATE: 2026-08-18
```

Dependency versions (baseline, `pip freeze`):

```
aiosqlite==0.22.1
annotated-types==0.8.0
anyio==4.14.2
certifi==2026.7.22
colorama==0.4.6
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.18
iniconfig==2.3.0
packaging==26.3
pluggy==1.6.0
pydantic==2.13.4
pydantic-settings==2.15.0
pydantic_core==2.46.4
Pygments==2.21.0
pytest==9.1.1
pytest-asyncio==1.4.0
python-dotenv==1.2.3
typing-inspection==0.4.4
typing_extensions==4.16.0
websockets==17.0.1
```

OS: Windows (win32), shell PowerShell 5.1.

Git status at baseline: only the authoritative implementation-plan document
was modified (provided for this remediation; included in the R0 commit so the
tree is clean).

Credentials check: `.env` is git-ignored and not tracked; only `.env.example`
(placeholders) is tracked. No real credentials present in the repository.

Baseline output: `artifacts/baseline-test-results.txt`.

## Phase log

| Phase | Status | Commit |
|---|---|---|
| R0 — baseline freeze | COMPLETE | `a5c33c1` |
| R1 — regression tests | COMPLETE | `156e62f` |
| R2 — listing wiring + debounce | COMPLETE | `b82924b` |
| R3 — atomic state + outbox | COMPLETE | `b2b8025` |
| R4 — durable Telegram retry | COMPLETE | `409b7b0` |
| R5 — listing delivery ack | COMPLETE | `be58dca` |
| R6 — Bybit API contract alignment | COMPLETE | `b798c20` |
| R7 — health and clock semantics | COMPLETE | `9d75412` |
| R8 — config + WS lifecycle | COMPLETE | `3e5a0b2` |
| R9 — docs + reproducible deps | COMPLETE | `7c253cc` |
| R10 — recovery and chaos acceptance | COMPLETE | `7ac2fc8` |
| R11 — live staging validation | PARTIAL (public Bybit done; Telegram items blocked on real credentials) | `6f7640b` |
| R12 — 24-hour soak | INVALIDATED — superseded by F-series/H-series runtime changes | |
| R13 — final acceptance | pending | |

Current test count (after R10): 283 passed / 0 failed (3 consecutive identical
full-suite runs, artifacts/remediation-full-tests-run{1,2,3}.txt), plus
`scripts/soak_test.py` (26h simulated in ~30s, deterministic).

R11 public Bybit validation recorded in `artifacts/live-bybit-validation.txt`:
spot 555 instruments (no pagination args), linear 824 trading (756 USDT +
68 USDC) + 5 PreLaunch, WS spot/linear connect with sane top-level `ts`.
R11 operator tasks (blocked, need real Telegram credentials + .env):
17.3 controlled Telegram delivery (transition + listing through outbox),
17.4 controlled Telegram outage, 17.5 container restart with pending outbox.

Project status: **NOT PRODUCTION READY** until Phase R12 succeeds.

## Production-readiness plan (F-series) phase log

Branch `final-production-readiness`. 313 tests, 5 consecutive identical runs
(`artifacts/final-suite-run{1..5}.txt`), plus clean-env acceptance run.

| Phase | Status | Commit |
|---|---|---|
| F0 — baseline freeze | COMPLETE | `ac3def8` |
| F1 — task-aware SQLite transaction ownership | COMPLETE | `980657f` |
| F2 — strict raw momentum qualification | COMPLETE | `267f6b7` |
| F3 — remove incomplete inverse feature exposure | COMPLETE | `7ba13ba` |
| F4 — supervise dispatcher worker and health | COMPLETE | `a158674` |
| F5 — correct hourly-only alert semantics | COMPLETE | `1141796` |
| F6 — align Docker and validated Python runtime | COMPLETE | `1b3666b` |
| F7 — complete Telegram health tracking | COMPLETE | `803cf83` |
| F8 — final concurrency and recovery acceptance | COMPLETE | `bb2410d` |
| F9 — live Bybit staging validation | COMPLETE | `32f6e67` |
| F10 — real Telegram staging validation | BLOCKED (no real credentials) | |
| F11 — final security audit | COMPLETE | `84261dd` |
| F12 — final 24-hour soak | INVALIDATED — H1–H3 changed runtime behavior; must restart from zero after F10 | `dba8eef` |
| F13 — production readiness acceptance | PARTIAL (clean-env 313 passed; migration v1→v3 validated, data preserved) | |

## WebSocket & operations hardening (H-series) phase log

Branch `final-ws-ops-hardening`. Baseline 313 tests
(`artifacts/ws-ops-hardening-baseline.txt`, commit `4dc8c77`).

| Phase | Status | Commit |
|---|---|---|
| H0 — freeze baseline | COMPLETE | `4dc8c77` (baseline) |
| H1 — confirm WS subscriptions by ACK | COMPLETE | `50971da` |
| H2 — guarantee disconnect-state cleanup | COMPLETE | `194dd26` |
| H3 — connect container health to app health | COMPLETE | `059474a` |
| H4 — remove duplicate state helper + sync docs | COMPLETE | `aaae752` |
| H5 — refresh final validation evidence | COMPLETE | `artifacts/final-suite-gate.txt` + `artifacts/docker-test-results.txt` + `artifacts/staging-validation.txt` |

Current test count: 347 passed / 0 failed (5 consecutive identical runs,
`artifacts/final-suite-run{1..5}.txt` + `final-suite-gate.txt`; 347 passed
inside the final image, `artifacts/docker-test-results.txt`). Live staging
with real Bybit subscription ACKs: `artifacts/staging-validation.txt`.

H1–H3 changed runtime behavior, so the previously running F12 soak is
invalid for final acceptance and must restart from zero.

## Post-H-series reliability repair (J-series) phase log

Branch `final-ws-reliability`. Baseline 347 tests (`artifacts/ws-reliability-baseline.txt`,
commit `23821264`).

| Phase | Status | Commit |
|---|---|---|
| J0 — freeze master baseline | COMPLETE | `23821264` |
| J1 — eliminate WebSocket ACK registration race | COMPLETE | `2764ae9` |
| J2 — enforce ACK timeout during continuous WebSocket traffic | COMPLETE | `d754938` |
| J3 — separate WebSocket heartbeat and ticker freshness | COMPLETE | `6759c01` |
| J4 — honor configured health heartbeat threshold | COMPLETE | `5e095c0` |
| J5 — report grace-window critical failures as degraded | COMPLETE | `529d82a` |
| J6 — sync soak and release documentation | COMPLETE | `6bcda13` |
| J7 — refresh post-H-series validation evidence | COMPLETE | `ccdb562` |
| J8 — final pre-Telegram reliability review | COMPLETE | `256cb86` (review: `artifacts/j8-review.txt`) |

Current test count: 373 passed / 0 failed (5 consecutive identical runs,
`artifacts/j-final-suite-run{1..5}.txt` + `j-final-suite-gate.txt`; 373 passed
inside the final image `bybit-monitor:final-j`, `artifacts/j-docker-test-results.txt`).
Live staging with real Bybit subscription ACKs, dynamic race-safe path, and
freshness separation: `artifacts/j-staging-validation.txt`.

J1–J7 changed runtime behavior (ACK registration race, ACK timeout watchdog,
ticker-vs-heartbeat freshness), so the F12 soak remains invalid for final
acceptance and must restart from zero after F10 passes.

Per the production-ready decision rule (§19), `PRODUCTION READY: YES` requires
real Telegram staging (F10) and a completed 24h soak restarted after H1–H5 —
both still pending, so:

```
STATUS: IN PROGRESS
PRODUCTION READY: NO
```

## Final acceptance checklist (§18.3)

### SQLite
- [x] Task-aware transaction ownership (`test_database.py::TestTaskAwareTransactionOwnership`).
- [x] Cross-task SQL blocks during transaction (chaos + ownership tests).
- [x] Owner task reentrant SQL works.
- [x] Exception rollback releases lock.
- [x] Cancellation releases lock.
- [x] No nested transaction race (`RuntimeError` nested guard + chaos tests).

### Momentum
- [x] No pre-qualification rounding (`MomentumEngine._raw_change`, Decimal arithmetic).
- [x] Exactly 5.0 does not qualify (105 -> 100 vector).
- [x] Any raw value >5.0 qualifies (105.000000001 vector).

### Market support
- [x] Spot supported (live: 555).
- [x] Linear USDT supported (live: 756).
- [x] Linear USDC supported (live: 68).
- [x] PreLaunch discovery supported (live: 5).
- [x] No false claim of inverse support (feature removed; SPEC.md documents it).

### Alert semantics
- [x] 0 = no range alert.
- [x] 1 / 2 / 3 = active.
- [x] 4+ = suppress.
- [x] Debounce works.
- [x] Hourly cannot bypass debounce.
- [x] Hourly-only mode works (`TestHourlyOnlyMode`).
- [x] Same-bucket duplicate suppressed.

### Durable outbox
- [x] State + outbox atomic (R3/F5).
- [x] Crash recovery works (`test_listing_retry_survives_restart`, R10 chaos).
- [x] Retry survives restart.
- [x] 429 honored (`retry_after` handling + `TelegramRetryableError`).
- [x] Listing ack tied to real delivery (dispatcher marks `telegram_sent` post-send).
- [x] Dedupe keys prevent duplicate creation (unique partial index + exact vector test).

### Dispatcher
- [x] Worker supervised (F4 loop).
- [x] Worker health visible (`worker_healthy`, health summary).
- [x] Transient DB error does not kill delivery permanently (supervised loop + tests).

### API
- [x] Spot no pagination args (live F9).
- [x] Linear pagination complete (live: 824 drained).
- [x] `isPreListing` (live F9).
- [x] Announcement nested type/tags (live F9).
- [x] WS top-level `ts` (live F9).
- [x] Settlement filters correct (live: USDT/USDC only).

### Runtime
- [x] Production Docker Python validated (3.14.7, 347 passed in container, H5).
- [x] Non-root container (`USER monitor`, uid 10001).
- [x] Persistent `/data`.
- [x] Healthcheck reflects critical application health (H3: `scripts/container_healthcheck.py` on persisted `health:snapshot`, 60s/10s/30s/3; unhealthy on sustained critical failure, degraded on transient).
- [x] Graceful shutdown (SIGTERM -> `shutdown_complete`, F6 lifecycle).

### WebSocket & operations hardening (H-series)
- [x] Subscriptions confirmed by server ACK (`req_id` echo, pending -> confirmed; H1).
- [x] Failed/timed-out ACKs trigger reconnect that rebuilds the desired universe (H1).
- [x] Disconnect state cleanup on every exit path — `try/finally`, dead socket never reported connected (H2).
- [x] Idempotent status transitions (single source of truth, H2).
- [x] Duplicate `_save()` removed from `app/alerts/state_machine.py` (H4).
- [x] Docs synced: README healthcheck values/semantics, release status, market support, WS ACK behavior (H4).
- [x] 5-run gate + in-container suite + live staging ACK checks refreshed (H5).

### Security
- [x] No secrets tracked.
- [x] `.env` ignored.
- [x] No credentials in logs/artifacts (F11 scan).
- [x] Dependencies pinned/reproducible (`requirements.txt`, `artifacts/pip-freeze.txt`).

### Staging
- [x] Live Bybit validation passed (F9, 22/22 checks).
- [ ] Real Telegram delivery passed — BLOCKED (no credentials).
- [ ] Real Telegram retry/recovery passed — BLOCKED (no credentials).

### Soak
- [ ] Final candidate unchanged during soak — NOT APPLICABLE: H1–H3 changed runtime; previous soak invalidated, F12 must restart from zero.
- [ ] >=24 elapsed hours — clock must restart after the new F12 run begins.
- [ ] All mandatory interventions completed — Telegram-dependent items pending F10.
- [ ] Soak passed.

### Migration validation (§18.2)
- [x] Fresh DB -> all migrations -> app starts (F6 lifecycle + unit tests).
- [x] Previous release candidate DB (migration v1, 11.8 MB, instruments/price_samples/listing_events/alert_state) -> migrations 1,2,3 applied -> all data preserved (instruments 1385, price_samples 555, listing_events 4, alert_state 1) -> app starts clean, `first_run=False events=0` (no false listing storm).