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

Git status at baseline: only `bybit_telegram_momentum_master_implementation_plan.md`
modified (the authoritative plan document provided for this remediation; included
in the R0 commit so the tree is clean).

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
| R12 — 24-hour soak | pending | |
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