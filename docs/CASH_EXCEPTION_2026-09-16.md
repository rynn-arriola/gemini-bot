# One-time unexplained cash exception

Owner explicitly approved the September 16 incident exception. This is a policy exception, not a diagnosed venue rounding defect. Exact cash enforcement remains the default.

## Scope

- `record-cash-exception` holds the shared Windows owner lock and reads two authenticated REST account/cash brackets. It records one immutable approval against the account fingerprint, exact before/after/delta and economic-history digest. It does not change expected cash. Rebinding the fixed incident ID is refused.
- `apply-cash-exception` is a separate operator action. It runs read-only production preparation with cancel-on-disconnect disabled, accepts only the typed unknown-cash preparation error, and requires fresh stable REST/WS cash/position agreement, zero active orders, no missing executions and terminal durable intents. Ownership, malformed evidence, changed histories and stopped stream tasks refuse application.
- Application atomically changes expected cash by the exact recorded delta and records consumption. Audit summaries contain no amounts; exact evidence remains only in the private canonical SQLite journal. No fills/fees are fabricated and no position basis, deadline, loss high-water, HARD, reward uptime or cash-history watermark changes.
- Fixed approval date expires at midnight Eastern September 16. Once consumed, it cannot apply again—even to the same-sized discrepancy. No live/startup loop imports or invokes this operator path. A fresh full preparation remains mandatory after application; this utility cannot activate trading.
- Future authoritative ledger evidence must be reviewed against this unexplained adjustment to avoid double-counting. Do not pretend this record proves a deposit, reward, fee or rounding rule.

## Current outcome

Approval recorded; exception **not applied**. A fresh application attempt stopped on the remaining external working SELL. Cash remained unchanged; SQLite integrity passed. No order/cancel, permit or restart. Canceling that manual order does not require selling its held position. Its terminal status and any intervening fills must be reconciled; changed evidence invalidates this approval.

Once the operator handles the manual order, the explicit local command is:

```powershell
.venv\Scripts\python.exe -m gemini_bot.cli apply-cash-exception --acknowledgement I_ACCEPT_UNEXPLAINED_CASH_20260916_ONCE
```

Then run fresh read-only `prepare-live`. Neither command starts trading. Do not delete/recreate the approval to bypass a failure.

## Automatic market-local behavior

Known no-buyer or terminal exit waits pause entries only in the affected market and retry with existing bounded backoff/fresh evidence. Original deadlines persist; healthy unrelated resting orders remain. A position is not a pending order, but its exposure and pool allocation remain occupied. Actual working exit orders still count as orders. A new regression holds illiquid inventory alongside 30 unrelated orders and verifies no cancellation, no immediate exit, unchanged deadline, and a pending-order count of exactly 30. Existing tests cover automatic scheduling, liquidity return, exact residual exit and unrelated-market send guards.

Unexplained account-wide cash is not proven market-local. Startup can wait read-only; a live accounting/integrity failure remains fail-closed and requires review. Today's exception cannot automatically forgive tomorrow's mismatch.

## Verification and reward impact

548 production + 28 script tests = **576 passed**. Eighteen focused exception cases and eight no-liquidity cases pass. A deliberate in-memory fixed-digest reversal was caught by the history-change test. Compilation, offline native-Windows release/storage and whitespace checks passed.

Focused exception cases cover once-only durable consumption, changed history/account/cash, expiry, invalid numbers, signed ledger cash, atomic rollback, HARD/history preservation, future mismatch refusal, fresh proof, active orders, stale snapshots, missing fills, fill-during-proof and CLI ownership refusal. No induced live fills/faults.

No 50% admission, denominator, quote size/distance, breadth, sticky-pool selection or loss limit changes. Market-local waiting preserves healthy resting time when safe, but does not credit missed snapshots or guarantee rewards. The exception creates no quote by itself; trading is still stopped.
