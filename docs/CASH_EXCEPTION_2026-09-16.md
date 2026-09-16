# One-time unexplained cash exception

Owner explicitly approved the September 16 incident exception. This is a policy exception, not a diagnosed venue rounding defect. Exact cash enforcement remains the default.

## Scope

- `record-cash-exception` holds the shared Windows owner lock and reads two authenticated REST account/cash brackets. It records one immutable approval against the account fingerprint, exact before/after/delta and economic-history digest. It does not change expected cash. Rebinding the fixed incident ID is refused.
- `apply-cash-exception` is a separate operator action. It runs read-only production preparation with cancel-on-disconnect disabled, accepts only the typed unknown-cash preparation error, and requires fresh stable REST/WS cash/position agreement, zero active orders, no missing executions and terminal durable intents. Ownership, malformed evidence, changed histories and stopped stream tasks refuse application.
- Application binds the stable REST and WebSocket confirmed-balance surfaces separately, because the incident currently exposes different precision on those two authenticated views. Runtime expected cash adopts the exact WebSocket `c` value it actually reconciles against. Both exact values remain only in the private canonical SQLite journal. This is not a general rounding/tolerance rule. Audit summaries contain no amounts. No fills/fees are fabricated and no position basis, deadline, loss high-water, HARD, reward uptime or cash-history watermark changes.
- Fixed approval date expires at midnight Eastern September 16. Once consumed, it cannot apply again—even to the same-sized discrepancy. No live/startup loop imports or invokes this operator path. A fresh full preparation remains mandatory after application; this utility cannot activate trading.
- Future authoritative ledger evidence must be reviewed against this unexplained adjustment to avoid double-counting. Do not pretend this record proves a deposit, reward, fee or rounding rule.

## Current outcome

The owner canceled the external working SELL. Stable REST and private-stream proof then showed zero active orders, unchanged position/fill evidence and both cash surfaces stable. The exception was applied once and consumed. A subsequent full read-only production preparation passed with 21 tracked markets, 18 ranked and 13 eligible. Exact authenticated history cleanup imported zero executions and used the existing flat-lifecycle proof to remove one unrelated stale deadline. Final evidence showed one position, zero active orders and one matching deadline market; SQLite integrity passed. No order, permit, activation or restart.

The consumed command was:

```powershell
.venv\Scripts\python.exe -m gemini_bot.cli apply-cash-exception --acknowledgement I_ACCEPT_UNEXPLAINED_CASH_20260916_ONCE
```

Do not run it again or delete/recreate the approval. Fresh read-only `prepare-live` has passed. Neither command starts trading.

## Automatic market-local behavior

Known no-buyer or terminal exit waits pause entries only in the affected market and retry with existing bounded backoff/fresh evidence. Original deadlines persist; healthy unrelated resting orders remain. A position is not a pending order, but its exposure and pool allocation remain occupied. Actual working exit orders still count as orders. A new regression holds illiquid inventory alongside 30 unrelated orders and verifies no cancellation, no immediate exit, unchanged deadline, and a pending-order count of exactly 30. Existing tests cover automatic scheduling, liquidity return, exact residual exit and unrelated-market send guards.

Unexplained account-wide cash is not proven market-local. Startup can wait read-only; a live accounting/integrity failure remains fail-closed and requires review. Today's exception cannot automatically forgive tomorrow's mismatch.

## Verification and reward impact

550 production + 28 script tests = **578 passed**. Twenty focused exception cases and eight no-liquidity cases pass. A deliberate in-memory fixed-digest reversal was caught by the history-change test. Compilation, offline native-Windows release/storage and whitespace checks passed.

Focused exception cases cover once-only durable consumption, changed history/account/cash, expiry, invalid numbers, signed ledger cash, atomic rollback, HARD/history preservation, future mismatch refusal, fresh proof, active orders, stale snapshots, missing fills, fill-during-proof and CLI ownership refusal. No induced live fills/faults.

No 50% admission, denominator, quote size/distance, breadth, sticky-pool selection or loss limit changes. Market-local waiting preserves healthy resting time when safe, but does not credit missed snapshots or guarantee rewards. The exception creates no quote by itself; trading is still stopped.
