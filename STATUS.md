# Gemini current status

Updated September 15, 2026 at 17:13 America/New_York.

## Operational state

- **Stopped, flat, and order-free while the fill-race repair is prepared for deployment.** The old supervisor is dismissed and no Gemini runner is active.
- Fresh authenticated read-only reconciliation passed: 32 tracked symbols, 29 ranked candidates, four currently eligible candidates, zero positions, zero active orders, and no mutation or permit consumption.
- Current six-hour Eastern realized P&L recalculates to **-$0.28**, including recorded fees; classification is complete, the $30 HARD is not reached, and the permitted pending-order cap remains 30.
- Current source passes 205 production tests, 28 observer/script tests, compilation, offline Windows release validation, and PowerShell parsing. The incident regression was also reversed once and reproduced the prior failure.

## Latest incident and repair

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

The prior runtime is `data/production-20260915-manual-history-v6`; the latest read-only proof is `data/production-20260915-fill-race-v7-preflight`. Push the reviewed repair before activation, create a fresh build-bound version-5 permit under a new runtime directory, then use the owner's standing instruction to restart. Verify `RUNNING`, `quoting`, 30 or fewer orders, and zero unexpected error logs.
