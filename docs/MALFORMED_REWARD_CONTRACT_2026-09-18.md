# Malformed reward-contract isolation — September 18, 2026

## Incident

At 20:08:59 America/New_York the active v27 reward refresh received an event payload containing a contract without `priceIncrement`. `join_reward_contracts` directly indexed that field and raised `KeyError`. Because the exception escaped the market-refresh boundary, the runtime correctly treated it as an unknown execution failure and performed an account-wide drain. At 20:09:05 it recorded `stopped_flat`; the supervisor's subsequent read-only reconciliation passed and required fresh approval. There were no positions or active orders after the drain.

## Repair

Contract catalog parsing now treats an individual non-dictionary row, missing required rule field, nonnumeric rule value or nonpositive rule as unavailable. Valid contracts in the same event remain available. If an unavailable contract was already selected, the refresh blocks entry and confirms cancellation for that symbol only. Duplicate valid symbols and pool-level identity/budget inconsistencies still raise, because those are catalog-integrity conflicts rather than an unavailable market.

The repair changes no quote price, quantity, owner capacity, exposure, exit, accounting, rate, WebSocket, permit or HARD rule. It may remove resting time only from the malformed affected market; that gap is required safety behavior and receives no reward credit. It prevents the previous unnecessary all-market interruption.

## Verification

- Regression tests reproduce a missing `priceIncrement`, an invalid numeric increment, healthy sibling survival and affected-symbol-only cancellation.
- The full production suite passed 1,020 tests.
- The operator-script suite passed 28 tests.
- Python compilation, offline Windows policy/storage validation and `git diff --check` passed.
- No Gemini connection, order mutation, permit creation or restart was used for this repair.

## Deployment and initial acceptance

The owner authorized restart after verification. The old attention-only v27 supervisor accepted `SUPERVISOR_STOP`, exited without force, and left its existing `stopped_flat` result intact. Fresh preparation in `data/production-20260918-contract-schema-v28` completed at 20:35:48 Eastern with zero positions/orders, 32 tracked and ranked markets, 17 eligible markets, no entry/stream holds and a verified 12,189,696-byte backup.

New version-7 authority is exact-build/policy/account bound, has null expiry and no uptime override. The persistent task launched supervisor 41980 and runner 39108. Initial acceptance at approximately 20:41 Eastern found 34 acknowledged size-15 BUY entry orders across 17 complete markets: 17 YES and 17 NO. Every journal price matched the current rank report's best-bid target. Runtime reported zero positions, five connected/unheld public shards, no entry/stream/inventory holds, no current-process warning/error, no disabled queue symbol or cancellation-envelope breach, and zero supervisor restarts. Optional same-price controls remained 300 seconds and 500 contracts.

This checkpoint proves startup and current-payload compatibility only. The next natural metadata refresh remains a read-only observation gate; no test or startup snapshot guarantees every future venue schema, continuous reward qualification, fills, exits or profit.
