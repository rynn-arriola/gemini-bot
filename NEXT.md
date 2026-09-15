# Next work

The [September 15 repairs](docs/REPAIR_REPORT_2026-09-15.md) passed offline verification. The bot is stopped and live operation remains unapproved.

## Completed offline

1. All R01–R15 regressions pass; unsafe reconnect, mixed snapshot/delta transition and legacy IOC paths fail closed or are retired.
2. 106 production tests and 25 observer tests pass. Python compile, native Windows release and PowerShell syntax checks pass.
3. Maker canaries call production activation/safety/drain; an offline fake canary verifies the integrated path. Real execution is not tested by fake transports.
4. Read the repair report's limitations: incomplete external cash-flow automation, unsupported collateral-return reduction mode, conservative uptime, and manual reconciliation on unknown execution.

## Later, with explicit owner approval at each mutation boundary

1. Re-run offline release validation and review the 106 production plus 25 observer test results after any code change.
2. Obtain approval to run `prepare-live`; reconcile accepted terms, balance, positions, active orders, streams, current reward universe, and `rank-report.json` without submitting orders.
3. Show the owner the selected unique pool, exact 10-contract price/side, maximum reservation, current six-hour loss/cap mark, available USD, and every preflight result.
4. Verify standard same-outcome SELL is supported for the selected account/contract, and resolve any legacy journal/cash discrepancies without discarding evidence. Only after new explicit approval, create a one-hour, one-pool version-2 permit bound to current policy/build/account/exact symbol and run the one-leg MOC maker canary. Verify resting acknowledgment, cancellation terminality, and flat account.
5. With another new permit/approval, run the one-pool two-sided canary. Exercise fill cancellation and exact timed-exit handling if it fills; prove flat/zero orders at drain.
6. Review actual reward-summary and fee evidence. Only then request approval for a one-pool continuous run. Increase breadth one reviewed step at a time; never jump directly to 15 pools/30 orders.

An OS-level Task Scheduler restart policy is optional after live commissioning. Automatic restart must return to paused/reconcile and require a fresh permit; it must never automatically reactivate quoting.
