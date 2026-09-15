# Next work

The repaired owner-approved September 15 live observation is active under a fresh, one-use, midnight-Eastern permit. Do not start a second mutation process or reuse either consumed permit. Monitor the first real fill and verify immediate sibling cancellation, authenticated signed-position accounting, the exact same-outcome SELL exit, the five-minute or earlier deadline, fees and final flat reconciliation.

For normal operation after today, omit the uptime override. Form a sticky top-ranked cohort for each scoring window, target 75% local qualifying snapshots against Gemini's 50% requirement, and rotate only when a pool ends, becomes unsafe/ineligible, cannot still qualify, or has completed its target. Do not scatter uptime across more pools than the 30-order/15-pool capacity can support.

The [September 15 repairs](docs/REPAIR_REPORT_2026-09-15.md) passed offline verification. The repeat preflight and production preparation passed data/account readiness. The [maker canary record](docs/MAKER_CANARY_2026-09-15.md) now includes successful one- and two-sided resting/cancellation tests. The separately authorized September 15 continuous observation is active.

## Preflight blockers repaired offline

1. Readiness accepts a fully initialized cohort when at least one current, two-sided, pre-expiry market is usable. One-sided contracts remain tracked but cannot enter this two-sided strategy.
2. Quiet books retain their sequenced state while recent traffic on the one shared public socket proves continuity. A stale connection or sequence gap still blocks entry. One-sided held inventory is marked at its executable side, or zero when no exit bid exists.
3. `prepare-live` and the diagnostic now use a transport-enforced read-only mode and `cancelOnDisconnect=false`; they cannot create intents, place/cancel orders, or activate.
4. 113 production and 28 script/observer tests pass. The repeat live read-only preflight synchronized all 32 books, retained 25–27 usable two-sided books, matched account WebSocket state to REST, and shut down cleanly.

## Reward timing proxy

The unsupported UTC boundary is replaced with an event-specific window inside the America/New_York payout day. Contract effective time, reward end and pre-expiry safety bound the window; shared pool budgets and event uptime have separate identities. The active process enforces the 20-minute sports pregame entry/flat cutoff. Quiet-stream liveness is proven with protocol ping/pong, and flat lifecycle transitions isolate one symbol rather than halt every pool. The exact unpublished reward activation time remains a measurement item. Calibrate it against the next authenticated daily summary's `total_snapshots` before treating the proxy as exact.

Add a read-only Gemini Statuspage monitor in a later tested release. Record upcoming/active maintenance and incidents as annotations, but never subtract minutes from the 50% denominator without Gemini's authoritative daily `total_snapshots`. The developer changelog is a separate API-change source.

## Completed offline

1. All R01–R15 regressions pass; unsafe reconnect, mixed snapshot/delta transition and legacy IOC paths fail closed or are retired.
2. 106 production tests and 25 observer tests pass. Python compile, native Windows release and PowerShell syntax checks pass.
3. Maker canaries call production activation/safety/drain; an offline fake canary verifies the integrated path. Real execution is not tested by fake transports.
4. Read the repair report's limitations: incomplete external cash-flow automation, unsupported collateral-return reduction mode, conservative uptime, and manual reconciliation on unknown execution.

## Commissioning record and remaining work

1. Re-run offline release validation and review the 127 production plus 28 script/observer test results after any code change.
2. The repeat one-leg live canary is complete: exactly one five-second MOC order rested for approximately 5.25 seconds and terminally canceled unfilled; final account reconciliation was flat with zero active orders.
3. The two-sided canary and owner-approved date-bounded reward-catalog activation are complete/active; do not duplicate them.
4. Observe the first real fill. Verify immediate sibling-order cancellation, exact position accounting, 300-second timed exit, fee classification, and final reconciliation. Any ambiguity must halt rather than be inferred.
5. Compare authenticated daily-summary snapshot totals against the Eastern proxy after the completed reward day and review actual reward and fee evidence.

An OS-level Task Scheduler restart policy is optional after live commissioning. Automatic restart must return to paused/reconcile and require a fresh permit; it must never automatically reactivate quoting.
