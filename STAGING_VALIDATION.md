# Phase H5 — Real Bybit Staging Validation (refreshed)

Date: 2026-08-19 · Branch: `final-ws-ops-hardening` · Commit: `aaae752` ·
Image: `bybit-monitor:final` (`92fc5de35353`)

Live public Bybit endpoints only. No credentials involved.

## Actual instrument counts (2026-08-19, UTC)

| Universe | Count |
| --- | --- |
| Spot | 555 |
| Linear USDT | 757 |
| Linear USDC | 68 |
| Linear PreLaunch | 4 |

## Contract checks

Run via `scripts/staging_validation.py` (public endpoints):

```
REST:
  [PASS] spot discovery - count=555
  [PASS] linear pagination drains - count=825
  [PASS] settlement filtering - settle_coins=['USDC', 'USDT']
  [PASS] linear USDT count - count=757
  [PASS] linear USDC count - count=68
  [PASS] linear PreLaunch discovery - count=4
  [PASS] 1h derivative reference (prevPrice1h) - 829/829
  [PASS] announcements fetched - count=50
  [PASS] announcement nested type/tags
WEBSOCKET (subscription ACK confirmed, H1):
  [PASS] WS spot subscribe request emitted (req_id recorded)
  [PASS] WS spot subscription ACK success (pending -> confirmed)
  [PASS] WS spot ticker received
  [PASS] WS spot top-level ts
  [PASS] WS spot dynamic subscription - BTCUSDT ticker arrived
  [PASS] WS linear subscribe request emitted (req_id recorded)
  [PASS] WS linear subscription ACK success (pending -> confirmed)
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

- PHASE: H5 (refresh of F9 evidence on the hardened candidate)
- STATUS: DONE
- Exit gate: no API-contract mismatch on current Bybit public endpoints;
  subscription ACKs confirmed for both Spot and Linear (H1).
- Remaining: F10 (real Telegram delivery + 15.3 controlled failure) is
  BLOCKED until real credentials are provided via environment; F12 soak
  must restart from zero on the H5 image.