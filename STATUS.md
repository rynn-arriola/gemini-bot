# Gemini current status

Updated September 15, 2026 at 17:40 America/New_York.

## Operational state

- **Stopped; last authenticated recovery proved flat and order-free.** The supervisor is dismissed and no Gemini runner is active.
- The attempted restart stopped before placing orders because a paid $0.08 maker rebate changed confirmed cash. It was not a trading loss; current six-hour realized trading P&L remains **-$0.28**.
- The repair separates authenticated deposits, withdrawals, liquidity rewards, and maker rebates from fill-only realized P&L. Exact non-trading activity reconciles cash; unknown activity still blocks.
- Full validation passes: 214 production tests, 28 observer/script tests, compilation, Windows release validation, PowerShell parsing, and a deliberate regression reversal.
- Fresh authenticated read-only preparation passed: 32 tracked, 29 ranked, four eligible, zero positions, zero orders, one exact non-trading adjustment, complete -$0.28 fill P&L. No permit or mutation was used.

## Prior fill-race repair

- At 17:00 ET, BUY YES 10 on `GEMI-XRP2609162100-HI1D24` filled at $0.81 while the paired NO order waited for its send slot. The final pre-send guard correctly refused the now-stale second leg, but orchestration incorrectly treated that normal fill/account-transition race as fatal.
- Fail-safe drain canceled all pending orders and sold the exact XRP YES 10 position at $0.77. The account finished flat/order-free. That round trip realized -$0.49 including the recorded $0.09 exit fee.
- The repair gives expected unsent maker races a typed result, cancels only the affected symbol's remaining entries, and leaves the five-minute fill-safety worker responsible for the position. Policy changes, stale account snapshots, unapproved symbols, stream loss, and ambiguous mutations remain fatal.
- A first leg that fills before its acknowledgement waiter runs is now handled by the same fill-safety path instead of causing an account-wide drain.
- The Windows supervisor now consumes a same-session atomic runner result instead of relying only on a process exit code that was misreported as zero. Missing, malformed, stale-session, or unknown results fail closed.

## Approved live scope

- Native Windows; WebSocket-first; REST only for discovery and bounded reconciliation.
- 10 contracts per leg; two ticks behind touch; 15 pools/30 pending entry orders; $300 gross, $90 per market, $40 stressed-entry filter.
- One monetary stop: $30 realized P&L per six-hour Eastern window. Five-minute inventory and pre-expiry exits; sports flat 20 minutes before start.
- The September 15 uptime-feasibility override ends at midnight Eastern and does not claim reward eligibility. Every safety action may reduce qualifying snapshots.

## Handoff

Commit/push the reviewed repair, create a new build-bound version-5 permit, and then use the owner's standing restart instruction. Verify `RUNNING`, `quoting`, at most 30 orders, and no unexpected error logs.
