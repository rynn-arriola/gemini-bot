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

The source is restart-ready but not live. Deployment requires the standard fresh read-only account/integrity/backup preparation and new exact build/policy/account authority; the consumed v27 permit must not be reused.
