# Same-price refresh churn — September 18, 2026

## Live finding

V26 is performing real same-price cancel/rejoin operations. By 19:01 ET, logs contained 35 `estimated_queue_refresh_benefit` decisions. New size-15 order identities reappeared at the same best-bid prices, which is the available mechanism for joining behind already-resting same-price depth. Aggregate public L2 cannot prove the account's exact queue rank.

Measured examples show the uptime cost. BALLONDOR26-KANE canceled its 0.57/0.42 pair at 18:59:42 and restored it about 13 seconds later, then canceled again at 19:00:31 and restored it about 26 seconds later. UKGEN2029-GREEN canceled 0.04/0.95 at 19:01:00 and restored both legs about 15 seconds later. A gap may lose a reward snapshot; local order presence cannot prove exchange credit.

## Candidate repair

Explicit policy version 7 adds two stay-best-only fields:

- `same_price_refresh_minimum_seconds=300`: at most one optional same-price refresh decision per market in five minutes.
- `same_price_refresh_minimum_gain=500`: require at least 500 contracts of estimated improvement from cancel/rejoin.

The legacy `refresh_cooldown_seconds=10` remains unchanged for terminal cancellation, later public depth, fresh account proof and safe re-entry. Mandatory external best-price movement is checked before optional refresh limits and therefore still cancels promptly. Thin/depleted queue estimates continue to keep valid best-bid orders resting.

The new controls are outside the adaptive cancellation envelope, so the existing durable breach identity remains identical and cannot be reset by tuning. Optional timestamps are per symbol and removed only with retired membership. Process restart still requires the existing 600-second fresh warmup before optional refresh can occur. Settings expiry or a cancellation-envelope breach disables optional refresh, not best-bid placement or mandatory price tracking.

Size 15, 40 orders/20 owners/32 watched, five public plus one private socket, one mutation/rate owner, all monetary limits, exact fills/exits and HARD remain unchanged. Candidate work is isolated from running v26 and requires a later controlled deployment with fresh authority.

## Verification

Final checks passed: **1,017 production tests plus 28 script tests (1,045 total)**, compilation, offline Windows policy/storage validation and whitespace. Dedicated cases prove that an estimated gain of 499 keeps orders resting while 500 permits refresh; 299 seconds suppresses another optional decision while 300 permits it; a genuine best-price change still withdraws immediately inside that optional interval. Legacy v6 loads without the new fields, v7 requires both exact controls, staging roundtrips v7, membership cleanup removes per-symbol refresh history, and the old/new policies retain the same cancellation-breach key. Existing fake-wire fill, unknown-cancel, proof, shared-rate, 40-order and HARD suites pass.

No live process, permit, order or runtime configuration was changed. At the verification checkpoint running v26 remained `quoting` with 38 orders, zero positions, five connected shards, no holds and no supervisor restart.
