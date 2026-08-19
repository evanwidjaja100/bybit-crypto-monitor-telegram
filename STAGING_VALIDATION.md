# Phase J7 — Real Bybit Staging Validation (refreshed)

Date: 2026-08-19 · Branch: `final-ws-reliability` · Commit: `6bcda13` ·
Image: `bybit-monitor:final-j` (`06c7ed7d0434`)

Live public Bybit endpoints only. No credentials involved.

## Actual instrument counts (2026-08-19, UTC)

| Universe | Count |
| --- | --- |
| Spot | 555 |
| Linear USDT | 760 |
| Linear USDC | 68 |
| Linear PreLaunch | 4 |

## Contract checks

Run via `scripts/staging_validation.py` (public endpoints):

```
REST:
  [PASS] spot discovery - count=555
  [PASS] linear pagination drains - count=828
  [PASS] settlement filtering - settle_coins=['USDC', 'USDT']
  [PASS] linear USDT count - count=760
  [PASS] linear USDC count - count=68
  [PASS] linear PreLaunch discovery - count=4
  [PASS] 1h derivative reference (prevPrice1h) - 832/832
  [PASS] announcements fetched - count=50
  [PASS] announcement nested type/tags
WEBSOCKET (subscription ACK confirmed, H1; race-safe dynamic path + J3 freshness):
  [PASS] WS spot subscribe request emitted (req_id recorded)
  [PASS] WS spot subscription ACK success (pending -> confirmed)
  [PASS] WS spot dynamic subscribe request emitted (race-safe path)
  [PASS] WS spot dynamic subscription ACK success (pending -> confirmed)
  [PASS] WS spot ticker received
  [PASS] WS spot top-level ts
  [PASS] WS spot dynamic subscription - BTCUSDT ticker arrived
  [PASS] WS spot ticker freshness (last_ticker_at)
  [PASS] WS spot heartbeat freshness separate from ticker (last_any_message_at)
  [PASS] WS linear subscribe request emitted (req_id recorded)
  [PASS] WS linear subscription ACK success (pending -> confirmed)
  [PASS] WS linear dynamic subscribe request emitted (race-safe path)
  [PASS] WS linear dynamic subscription ACK success (pending -> confirmed)
  [PASS] WS linear ticker received
  [PASS] WS linear top-level ts
  [PASS] WS linear dynamic subscription - BTCUSDT ticker arrived
  [PASS] WS linear ticker freshness (last_ticker_at)
  [PASS] WS linear heartbeat freshness separate from ticker (last_any_message_at)
NEW-LISTING DRY-RUN:
  [PASS] discovery refresh stable - instruments=1387
  [PASS] no synthetic new events on refresh - new_events=0
  [PASS] synthetic new market produced registry event
  [PASS] synthetic new market produced listing event
  [PASS] dispatcher delivered synthetic listing - count=1
  [PASS] outbox listing row marked sent - status=sent
  [PASS] listing telegram_sent = 1
STAGING VALIDATION OK
```

Full output: `artifacts/j-staging-validation.txt`.

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

## Dynamic subscription (race-safe path)

After the initial BTCUSDT ACK, the live stream is extended mid-flight to
`{BTCUSDT, ETHUSDT}` through the production synchronization path (subscription
lock, pending registered before send, confirmed only by ACK). Verified for both
Spot and Linear: request emitted with a `req_id`, pending → confirmed via ACK,
ETHUSDT ticker delivered.

## Freshness separation (J3)

Live streams confirmed to track `last_ticker_at` (ticker frames only) and
`last_any_message_at` (any frame) as independent fields; ticker age observed at
0s on both streams.

## Phase status

- PHASE: J7 (refresh of H5 evidence on the final J-series candidate)
- STATUS: DONE
- Exit gate: no API-contract mismatch on current Bybit public endpoints;
  subscription ACKs confirmed for both Spot and Linear (H1), dynamic
  race-safe path verified live, ticker freshness separate from heartbeat (J3).
- Remaining: F10 (real Telegram delivery + 15.3 controlled failure) is
  BLOCKED until real credentials are provided via environment; F12 soak
  must restart from zero on the final image.
