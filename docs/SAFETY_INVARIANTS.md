# Safety invariants

Violation of an invariant blocks new risk and requires reconciliation or a terminal halt.

1. Production begins paused. Process startup alone can never submit an order.
2. Live activation requires accepted terms, fresh balances/positions/open orders, live public and private streams, a synchronized book, complete risk configuration, exact live acknowledgement, and an unexpired one-use version-2 owner permit bound to policy, Python build, account fingerprint and exact symbols. Run expiry ends quoting.
3. One Windows process owns all Gemini mutations. A second process fails closed.
4. Every mutation has a recorded intent before network send. An ambiguous order submission is never blindly retried.
5. Initial quotes are maker-only (`LIMIT` + `MOC`) and must satisfy current contract tick, increment, quantity, price, market-state, reward, effective-time, and expiry rules. Sports entry stops and any position becomes due for exact exit 20 minutes before the published game start even if in-play trading remains open. Crypto interval markets retain the separate five-minute pre-expiry cutoff.
6. Pending orders never exceed 30. Filled positions are excluded from that count but remain inside separate position and capital gates.
7. A first fill immediately removes permission to add on the same directional exposure and prioritizes cancellation of the opposite/risk-adding leg.
8. Exact authenticated position quantity—not intended or locally inferred quantity—controls reduction.
8a. A close is exact and outcome-correct: SELL YES for a positive YES position, SELL NO for a negative NO position. Full haircut-adjusted depth must cover it; a terminal IOC status alone never proves flat.
9. A differential-depth gap, crossed local book, stale shared market-data stream, contract closure, stale account state, or unknown ownership removes permission to quote. A quiet symbol may retain its last sequenced book while the single shared WebSocket has recent verified traffic; a one-sided book cannot enter the two-sided quote strategy.
10. REST cannot silently replace WebSocket streaming. It is bounded reconciliation and metadata transport.
11. Server throttles and `Retry-After` are honored. Safety priority never bypasses a server cooldown, and extra keys/connections are not used to evade limits.
12. A timed/expiry exit cancels risk-adding orders and confirms terminal state before sending an exact reducer.
13. The inventory deadline persists across restart and cannot be reset by a reconnect or process restart.
14. The single $25 six-hour Eastern stop is based on reconciled adjusted account equity with deposits, withdrawals, settlements, rewards, and fees classified separately. Unknown classification blocks new risk. No other monetary stop authority exists.
15. Six-hour Eastern loss-window baselines persist across restart. Window loss can only tighten the pending-order cap; a boundary, restart, reward, or cash transfer cannot silently forgive trading loss. The 300-second inventory and pre-expiry exits act on time, not on another loss threshold.
16. Shared reward budgets are counted once by pool ID, while uptime is isolated by event ticker. An unreported reward start uses a conservative Eastern-day/effective-time bound and is calibrated later against authenticated `total_snapshots`. Ranking v2 reward/share/cost/net values are conservative proxies and are never presented as guaranteed payout. Non-positive net and sub-$1 estimated rewards cannot enter, even under an uptime-only override.
17. Secrets, signatures, nonces, signed payloads, and sensitive account state are redacted from every output.
18. A prepared or restarted process never resumes quoting automatically. Stream loss, process restart, an expired/consumed permit, or a policy-file change requires reconciliation and fresh owner approval.
19. The uncertain daily-uptime feasibility proxy may be ignored only by explicit activation. Bounded commissioning requires exact owner-reviewed prices. Continuous use additionally requires a permit with a timezone-aware `uptime_override_until` no later than permit expiry. Version 4 can authorize dynamic selection only from Gemini's reconciled reward catalog; pool and pending-order caps still apply. The candidate remains ineligible in reward reports and the exception cannot bypass market, price, size, exposure, ownership, stream, permit, cancellation, fill, exit, or reconciliation gates. Both permit expiry and override expiry trigger a drain.
