# Gemini production code review — September 15, 2026

**Historical pre-repair report.** The owner subsequently authorized fixes. All fifteen regressions below now pass. Current implementation evidence and remaining live commissioning limits are in [the repair report](REPAIR_REPORT_2026-09-15.md). Original findings/locations are retained to preserve the review trail; they describe the pre-repair version.

## Verdict: launch blocked

The previous statement that only live commissioning remained was too strong. The production modules exist, but their integration violates several documented safety requirements. Do not start maker canaries or continuous trading on this version.

Review covered the production orchestrator, public/private adapters, account reducer, execution ledger, discovery/ranking/uptime, risk accounting, SQLite persistence, Windows launch/stop scripts, and canary tools. Public official documentation was checked; no exchange account connection, real order, or live process was used. Production code was not modified in this review.

## Verification

- Existing tests: 49 core + 19 production-component + 25 observer tests pass.
- Added `tests/test_review_regressions.py`: 15 deterministic offline cases expose 14 assertion failures and one runtime error. All exchange transports are fake; credentials and account data in fixtures are synthetic.
- These failures are intentional release blockers, not skipped or marked expected failures. Tests state required behavior so future fixes must make them pass.
- The earlier passing startup fixture replaced the public adapter with a fake returning `None` for every frame, masking the real adapter/orchestrator return-value mismatch below.

## Confirmed defects reproduced offline

### R01 — P1: valid depth frames trigger resubscription and cancellation

Location: `src/gemini_bot/orchestrator.py:119`, `src/gemini_bot/public_ws.py:113`.

`apply_frame()` returns the symbol after a successful update and after a sequence gap. `_public_pump()` interprets either result as a gap, queues a safety cancellation, and calls `resync()`, which marks the book unsynchronized. One valid snapshot reproduced one unnecessary resubscription. Real startup can cycle through snapshots indefinitely or overflow the 64-item safety queue before activation. Active use would churn cancellation/resubscription traffic.

Fix: distinguish successful updates from resync requests; consume the gap set explicitly and wait for a confirmed new subscription snapshot. Test the real reducer through the pump. Gemini documents the first subscribed differential frame as the snapshot and subsequent frames as deltas; resubscription is for broken sequence continuity. [Official stream reference](https://developer.gemini.com/prediction-markets/websocket/streams).

### R02 — P1: a partial first fill still permits the second BUY

Location: `src/gemini_bot/orchestrator.py:291`, `src/gemini_bot/account_runtime.py:127`.

The first `PARTIALLY_FILLED` event sets the intent to `ACKNOWLEDGED`. `_quote_pair()` checks that state alone and sends the NO leg even after a YES execution is known. Reproduction filled three YES contracts and still submitted NO. The cancellation worker can run concurrently and does not make the pair transition atomic.

Fix: immediately block entries for the symbol on any fill; before every send recheck order execution, position, pending safety actions, current book, and engine state. Use one serialized lifecycle owner for pair placement, cancellation, and exits.

### R03 — P1: exit quantity is captured before cancellation finishes

Location: `src/gemini_bot/orchestrator.py:373`.

The loop copies signed positions before awaiting entry cancellations. A fill during cancellation changes inventory, but the exit uses the earlier quantity. Reproduction changed the authenticated position from 10 to 15 while canceling; the code sent SELL 10. It can leave residual inventory or oversell if the position instead shrinks.

Fix: after terminal cancellation proof, reconcile exact remaining inventory and outstanding exit intents, then price and send the reducer from that current state.

### R04 — P1: exit pricing accepts stale books

Location: `src/gemini_bot/orchestrator.py:381`, `src/gemini_bot/exit_pricing.py:27`.

The exit path checks synchronization and depth, but never book age. A one-hour-old book still produced an IOC exit. The error text claims fresh depth without enforcing freshness.

Fix: validate book age and socket continuity at the final send boundary, with bounded resynchronization and explicit unresolved-inventory reporting when fresh data is unavailable.

### R05 — P1: emergency drain can duplicate an ambiguous exit

Location: `src/gemini_bot/orchestrator.py:385`, `src/gemini_bot/orchestrator.py:535`.

After an exit send or acknowledgment timeout, the exception handler calls `drain()`. That path can submit a new exit without resolving the first `SENT_UNKNOWN` intent. Reproduction invoked the exit path twice after missing acknowledgments and created two distinct sent intents. A lagging REST position can still show inventory while the first exit is in flight.

Fix: block further mutation for that symbol until the previous client ID is conclusively reconciled; absence from an immediate active-order snapshot is insufficient proof of a never-accepted or completed IOC.

### R06 — P1: positions without deadlines silently remain unmanaged

Location: `src/gemini_bot/orchestrator.py:351`, `src/gemini_bot/orchestrator.py:378`.

The fill worker waits up to five seconds for a position update and then drops the work if none arrives. Later position reports do not create deadlines. `_exit_due_positions()` silently skips a position with no deadline, while quoting remains enabled. The deadline is also based on post-cancellation time rather than the first execution time.

Fix: persist first-fill time immediately, create/reconstruct deadlines on position reconciliation, and never regard missing deadline state as permission to leave inventory unattended. Cover delayed reports, worker backlog, restart, and settlement.

### R07 — P1: clean WebSocket closure leaves quoting enabled

Location: `src/gemini_bot/orchestrator.py:116`, `src/gemini_bot/orchestrator.py:134`.

Both pumps halt only on exceptions. A normal end of the async message iterator returns successfully and leaves the engine in `QUOTING`. Reproduction closed the private stream normally and `may_submit` stayed true. The active loop does not supervise completion of these pump tasks. Reconnect/reconcile behavior described in the architecture is not implemented.

Fix: treat every unexpected reader termination as loss of account/market continuity, supervise both tasks, invalidate readiness, and recover with bounded reconnect plus full reconciliation while keeping activation paused.

### R08 — P1: process exclusivity is per directory, not per account

Location: `src/gemini_bot/orchestrator.py:69`, `scripts/live_ioc_round_trip.py:302`.

Different runtime directories receive independent locks and journals. Reproduction acquired two locks using the same fake account in two directories. Production and canary processes can therefore become concurrent mutation owners; permit consumption is also isolated by directory.

Fix: use a common account-scoped Windows lock and durable activation/intent ownership across every order-capable entry point. Do not key exclusivity to a caller-selected output folder.

### R09 — P1: cash-flow classification can conceal losses

Location: `src/gemini_bot/account_runtime.py:65`, `src/gemini_bot/orchestrator.py:402`.

Any confirmed-balance change within five seconds of a fill/settlement is considered explained, regardless of amount. Reproduction added an unrelated synthetic cash increase next to a trade and classification remained complete. The risk mark directly adds confirmed cash to position market value; it does not separately adjust deposits, withdrawals, rewards, or other flows. Incoming cash can mask trading losses before the next observation.

Fix: classify cash changes by reconciled amounts and identities and persist adjustments; timing proximity alone is insufficient. Reconcile changes across downtime before continuing the prior risk window.

### R10 — P1: HTTP throttles do not set a cooldown

Location: `src/gemini_bot/rest_client.py:119`, `src/gemini_bot/orchestrator.py:535`.

`Retry-After` is attached to `ApiError`, but no production caller installs it in the admission governor. Reproduction raised 429 with a 120-second retry interval; only the ordinary roughly one-second local pacing delay remained. The generic error handler can immediately perform drain REST reads during the server cooldown. The separate recovery-decision module is not wired into runtime handling.

Fix: apply venue cooldowns before scheduling any recovery work; preserve them through reconnect. Give rate-limit and ban responses explicit recovery paths. Also audit weighted WebSocket quotas: current lanes limit request spacing, and emergency cancellations bypass ordinary local spacing.

### R11 — P1: expired contracts still receive entry attempts

Location: `src/gemini_bot/orchestrator.py:270`, `src/gemini_bot/orchestrator.py:433`.

Expiry is loaded but only used when building an inventory deadline. New entries and quote maintenance do not check it. Reproduction set an expired contract and still submitted both fake maker legs. Refresh checks pool identity, not each selected contract's status, expiry, post-only mode, or rules. No `contractStatus` subscription is present.

Fix: enforce the approved expiry boundary before every entry and cancel resting entries at that boundary; consume contract-status updates and refresh rules without treating another event in a shared pool as proof the chosen contract remains tradeable.

### R12 — P1: snapshot replacement is not atomic

Location: `src/gemini_bot/storage.py:107`, `src/gemini_bot/storage.py:221`.

SQLite is opened with `isolation_level=None`. `with self.connection` does not begin a transaction in this mode. Snapshot deletes and inserts commit independently. A malformed order row reproduced loss of the previously persisted position even though replacement raised an error.

Fix: validate/serialize rows before mutation and use an explicit transaction with rollback covering the complete replacement. Test failure after each write and reopening the database.

### R13 — P1: pagination truncates reconciliation history

Location: `src/gemini_bot/rest_client.py:212`.

The code treats `pagination.count` as a total-result count. A 150-row fake history returned only the first 100 rows. This prevents resolution of older unknown intents and can hide additional rows in other account snapshots. Gemini documents `count` as the number of items in the current response. [Active-order response schema](https://developer.gemini.com/rest-api/prediction-markets/order-management/get-active-orders).

Fix: distinguish current-page length from an explicit total; continue until a short page, with duplicate-page detection and a clear bounded-history strategy.

### R14 — P1: REST cancelled status cannot resolve an unknown intent

Location: `src/gemini_bot/orchestrator.py:168`.

REST `cancelled` becomes `CANCELLED`, but reconciliation accepts only `CANCELED`. Reproduction raised “an earlier mutation is not terminal” for a terminal cancelled order. Gemini's REST schema uses the double-L spelling. [Order-history schema](https://developer.gemini.com/rest-api/prediction-markets/order-management/get-order-history).

Fix: normalize REST and WebSocket statuses independently, preserving executed quantities and terminal evidence.

### R15 — P2: local uptime forgives unobserved minutes

Location: `src/gemini_bot/uptime.py:70`, `src/gemini_bot/storage.py:365`.

The denominator increases only when the process samples. Reproduction skipped 61 minutes of an explicitly configured active interval; eligible samples totaled two. Restart/downtime can therefore inflate the best-possible qualification estimate. The local qualifying predicate also ignores price distance and book freshness and requires two sides even though one-sided quotes can earn score. The 75% target is loaded but not used by the runtime.

Fix: distinguish observed qualification, missed/unknown intervals, and venue denominator uncertainty. Do not use an optimistic proxy as certified eligibility. Track one-sided qualification separately from the two-sided bonus. Gemini describes daily per-minute accumulation and a 50% eligible-snapshot requirement. [Liquidity reward rules](https://developer.gemini.com/prediction-markets/liquidity-rewards-program).

## Further code findings requiring closure

- **P1 — risk timing:** `_risk_mark()` runs every 60 seconds using REST values. WebSocket fills and price moves do not update the loss high-water mark immediately. A threshold breach followed by recovery inside one sampling interval is never recorded. Sequential quote/cancel waits can delay both risk and timed exits further.
- **P1 — live maker canary coverage:** `live_maker_canary.py` does not start the production fill-safety worker or run the inventory timer/risk loop. Its two-sided mode sleeps and drains. It therefore cannot prove the fill-triggered cancellation and timed-exit behavior claimed in commissioning documentation. Its one-leg path also bypasses pair risk admission.
- **P1 — restart reconstruction:** REST active orders are persisted but not loaded into `AccountReducer`; later identity-sparse terminal events can fail. Existing positions outside the newly selected reward universe have no subscribed book and cannot be priced for startup drain. Startup requires terminal history for every unknown send, even if an active-order snapshot could prove the send succeeded.
- **P1 — account stream transition:** switching from full snapshots to deltas subscribes to the delta stream while the reducer still treats all position frames as complete. A delta arriving during that interval can remove unrelated positions. Subscription acknowledgments are not correlated by request ID.
- **P2 — self-referencing reprices:** aggregate public depth includes our own orders. Once external touch liquidity disappears, rebuilding quotes two ticks behind our own touch can repeatedly move our orders away and create needless churn. No own-order subtraction or reprice hysteresis exists.
- **P2 — CPU/memory:** the dirty-symbol queue is populated but never consumed. The runtime reranks the entire universe and synchronously serializes/fsyncs the report every loop. Orders, intents, waiter events, and trade-ID sets are never pruned. Cost grows with lifetime order count despite the 30-pending-order cap.
- **P2 — telemetry:** status is written at phase changes, not periodically. A long-running status file can retain initial zero counts and a stale timestamp; successful mutations/fills have no complete structured operational log trail. The Windows script is a launcher, not a process supervisor; its argument joining also needs verification with paths containing spaces.
- **P2 — permit scope:** validation is one-time at activation, while expiry is not checked during quoting. The permit identifies policy and pool count but does not bind an account, exact market, or code build. Clarify whether expiry bounds startup only or the full run, and enforce the documented interpretation.

## Repair sequence

1. Correct depth/stream supervision and throttle handling; prove startup using the real adapters with recorded or realistic fixtures.
2. Unify lifecycle ownership and block every mutation behind current state, unresolved-intent, freshness, and expiry checks.
3. Correct cancellation/reconciliation/exit ordering and persist first-fill deadlines.
4. Correct account-wide locking, atomic storage, restart reconstruction, pagination, and status normalization.
5. Implement amount-based equity adjustment and timely loss evaluation; repair uptime qualification and bound memory/reporting work.
6. Make all review regressions and existing tests pass, then verify the canary actually exercises the production safety path.
7. Present the repaired release evidence before requesting the already-required owner approval to run live.

This review does not establish profitability, venue rewards, or current account state. It establishes concrete defects in the present code and reproducible checks for their repair.
