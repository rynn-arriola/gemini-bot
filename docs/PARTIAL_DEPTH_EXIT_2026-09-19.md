# Partial-depth exact-position exit repair

## Live evidence

On September 19 the running v32 service received a complete 15-contract BUY NO fill on `GEMI-UKGEN2029-GREEN` at 0.95. It canceled the sibling entry, preserved the original deadline, blocked only that market and continued quoting unrelated markets. Authenticated REST subsequently showed one exact 15-contract NO position, no active order on that symbol and other active orders elsewhere.

The worker reported `exit_depth_unavailable`. A separate read-only synchronized public-book capture showed 10 YES contracts at the 0.05 best ask and no deeper ask level. Gemini collateral-return accounts reduce NO inventory by buying complementary YES, so the direction was correct. The local pricing gate was the blocker: it required haircut-adjusted displayed depth for the entire 15-contract position before sending any IOC.

## Repair

`exact_crossing_exit` still submits the complete exact authenticated position quantity. When synchronized haircut-adjusted depth covers the position, it retains the existing depth walk and worst required price. When positive depth exists but cannot cover the position, it now uses only the current best executable price rather than sweeping a deeper thin book. IOC semantics permit a terminal partial fill; the existing worker then requires fresh REST/WebSocket agreement on an exact same-sign, non-increasing residual before another bounded attempt.

No executable depth still returns a market-local wait. The repair does not change entry behavior, quote size, order/owner caps, gross/market exposure, the six-hour HARD, deadlines, the three-transport-attempt/30-second round, server pacing, rejection handling or ambiguous-mutation behavior.

## Verification and limits

Deterministic tests cover full-depth YES and collateral-return NO exits, thin multi-level best-price-only fallback, zero-depth refusal, a 15-to-5-to-flat authenticated residual sequence and the existing no-liquidity/unrelated-market cases. The complete application suite passed 1,035 tests, the script suite passed 28 tests, source/tests/scripts compiled, every PowerShell script parsed and offline Windows release validation passed.

Before deployment, the owner manually sold the held NO position. The active service correctly treated that unowned account event as an accounting boundary, canceled all remaining quotes and stopped without automatic restart. Fresh authenticated REST then proved zero positions and zero active orders. The existing explicit manual-flat workflow uniquely matched and imported one authenticated SELL from bounded Gemini filled-order history, preserved the six-hour loss-risk window and submitted no orders. This ledger repair does not replace the partial-depth exit code repair.

This repair can reduce inventory using available liquidity; it cannot guarantee that Gemini fills the IOC or that the residual immediately reaches zero. A controlled deployment creates an uncredited resting-order gap. The halted production checkout was not hot-edited and no restart was performed.
