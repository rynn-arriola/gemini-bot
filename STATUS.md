# Gemini current status

Updated September 16, 2026, 07:23 ET. Current handoff, not a perpetual venue-state guarantee.

## Operational truth

- **The bot is stopped, not restarted.** V11 (`data/production-20260916-churn-v11`, previously source `87fab92`) halted at 02:02:49 ET after both sockets received service-restart closes and a private handshake EOF was treated as fatal. Emergency cancellation could not send over the disconnected socket. The old supervisor and temporary exit helper have exited; no new live permit was issued.
- The incident's surviving owned entries were canceled earlier. At **07:23:01 ET**, a fresh authenticated read found **one position and one active order**; this supersedes the earlier order-free proof. No order was changed by this final read. The temporary helper had observed no executable NO-side bid and sent no exit; it is no longer monitoring.
- Exact authenticated trade IDs and fees repaired the missed executions and identified the external close. **Positions match, but a sub-cent cash discrepancy remains unresolved.** Rounding to cents happens to match; that is not authoritative evidence for a rounding rule. Exact cash classification remains blocked. No tolerance, baseline reset, loss reset or activation bypass was introduced.
- The recovery repairs below are source changes, **not a deployed or naturally accepted release**. Earlier v11 startup and midnight/churn observations remain historical, not current availability claims. No risk/size/breadth/exit-rule or normal 50% admission relaxation; recovery gaps receive no reward credit.

## Verified release and limits

- **479 production + 28 script tests = 507 passed.** Compilation and offline Windows release checks passed. No induced live faults/fills or new deployment.
- Source repairs: typed handshake-EOF recovery, bounded drain transport restoration, exact missed-fill reconstruction, explicit external-reduction reconciliation, and attempt-bound supervisor preparation results.
- Offline no-buyer regression retains original inventory deadlines, blocks new exposure on the affected symbol, and waits for fresh executable liquidity in the same runner. Unknown sends/accounting still fail closed; missing bids never prove flat. [Maintenance incident and repair evidence](docs/MAINTENANCE_RECOVERY_2026-09-16.md).
- Authenticated reward-summary calibration remains pending. Local clock/continuity estimates never prove exchange snapshots or rewards. The earlier unexplained process disappearance remains unproven; no host-crash recovery guarantee.

## Operator route

Resolve the cash discrepancy from authoritative evidence before live readiness can pass; current external-order state must also be reconciled. Owner requested a six-WebSocket recommendation: proposed five public shards plus one private account connection, not implemented or approved for deployment. [NEXT.md](NEXT.md) lists pending work and [DECISIONS.md](DECISIONS.md) retains unchanged strategy/risk authority.
