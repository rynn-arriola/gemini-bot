# Overnight continuity release

The owner requested continuation through midnight without a midnight restart. One controlled deployment restart is necessary because the old process has already loaded expiry/drain behavior.

## Behavior

- Version 6 retains the account/build/policy/session binding, 24-hour maximum permit lifetime, two flat-only supervisor recoveries, and all standing risk/exit controls.
- An optional override deadline can precede session expiry. At that deadline the local 50% feasibility exception ends. Legacy permits still drain at override expiry; all versions drain at session expiry.
- A new Eastern day or override transition rebuilds uptime and rankings before new entries. A pre-send check rejects stale epoch decisions. Existing valid quotes can remain resting; changed eligibility still cancels normally.
- Selected pool/event/symbol identities are persisted in the shared journal. Healthy incumbents are preferred by discovery and ranking, including across restart. Uptime is never transferred between events or days.
- Existing five-minute metadata refresh can discover replacements for expired/inactive flat owners. It reads at most three event representatives per pool and 64 event details per discovery pass, through the shared paced REST client. Healthy tracked owners remain fixed. Cancel/terminal proof, fresh REST/WS flat agreement and absence of unresolved intents/deadlines precede retirement. Subscriptions use the existing public socket, and new books start unsynchronized.

## Snapshot impact and limits

The deployment drain/restart costs a short unquoted interval. Midnight itself no longer forces a global drain under v6. Ordinary two-tick repricing, fill handling and safety cancellation can still lose snapshots. No quote-placement rule changed, no missing minute is forgiven, and the 75% target remains a model parameter rather than a guaranteed completion scheduler. Exact exchange snapshot phase, denominator and recurring-pool aggregation remain unverified.

New-day, expired-override recovery, stale pre-send, unchanged resting quotes, unchanged deadline/loss state, durable identity, safe recurring replacement, fill-during-cancel, unresolved intent, and late retired-frame tests use synthetic clocks/transports. Natural midnight and rollover behavior still require observation during an approved run.

Validation before deployment: 302 production tests (including 25 overnight-continuity tests) and 28 script tests passed. Python compilation, PowerShell parsing, and whitespace checks passed. Runtime deployment evidence belongs in STATUS.md.

Official stream behavior rechecked September 15: [Gemini stream reference](https://developer.gemini.com/prediction-markets/websocket/streams) documents initial snapshots on subscription with `snapshot=-1`, differential updates and depth/trade streams. The release uses those existing protocols and the shared rate governor.
