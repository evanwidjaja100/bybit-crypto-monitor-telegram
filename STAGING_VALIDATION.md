# Phase F9 — Real Bybit Staging Validation

Date: 2026-08-19 · Branch: `final-production-readiness` · Base commit: `bb2410d`

Live public Bybit endpoints only. No credentials involved.

## Actual instrument counts (2026-08-19, UTC)

| Universe | Count |
| --- | --- |
| Spot | 555 |
| Linear USDT | 756 |
| Linear USDC | 68 |
| Linear PreLaunch | 5 |

## Contract checks

Run via `scripts/staging_validation.py` (public endpoints):

```
REST:
  [PASS] spot discovery - count=555
  [PASS] linear pagination drains - count=824
  [PASS] settlement filtering - settle_coins=['USDC', 'USDT']
  [PASS] linear USDT count - count=756
  [PASS] linear USDC count - count=68
  [PASS] linear PreLaunch discovery - count=5
  [PASS] 1h derivative reference (prevPrice1h) - 829/829
  [PASS] announcements fetched - count=50
  [PASS] announcement nested type/tags
WEBSOCKET:
  [PASS] WS spot ticker received
  [PASS] WS spot top-level ts
  [PASS] WS spot dynamic subscription - BTCUSDT ticker arrived
  [PASS] WS linear ticker received
  [PASS] WS linear top-level ts
  [PASS] WS linear dynamic subscription - BTCUSDT ticker arrived
NEW-LISTING DRY-RUN:
  [PASS] discovery refresh stable - instruments=1384
  [PASS] no synthetic new events on refresh - new_events=0
  [PASS] synthetic new market produced registry event
  [PASS] synthetic new market produced listing event
  [PASS] dispatcher delivered synthetic listing - count=1
  [PASS] outbox listing row marked sent - status=sent
  [PASS] listing telegram_sent = 1
STAGING VALIDATION OK
```

Full output: `artifacts/staging-validation.txt`.

## New-listing dry-run

Synthetic registry event `STAGE<epoch>USDT` (linear) driven through the real
production callback path:

```
registry event
→ ListingTracker.handle_registry (real)
→ durable outbox insert (real)
→ AlertDispatcher poll + deliver (real)
→ Telegram stubbed (no credentials configured)
```

Verified: registry event emitted, listing event recorded, outbox row reached
`sent`, `listing_events.telegram_sent = 1`.

## Phase status

- PHASE: F9
- STATUS: DONE
- Exit gate: no API-contract mismatch on current Bybit public endpoints.
- Remaining: F10 (real Telegram delivery + 15.3 controlled failure) is
  BLOCKED until real credentials are provided via environment.