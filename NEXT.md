# Next work

- **P2 telemetry deployment pending:** branch `codex/telemetry-callback` restores `_account_event` after shared-journal preparation, with a regression through real preparation and the account reducer. Merge/apply only with the next authorized deployment and fresh build-bound authority. Owner explicitly prohibited restart for this fix; active v10 remains on its matching source. Until deployment, use structured logs for exact transition timing; its per-event counters remain zero and continuity uses the clock.

- **V7 availability/churn repairs are deployed:** bounded drain/admission, correlated read-error recovery, recovered-market holds and connection races passed offline verification. Measure safety/economics-bound retention, reason-coded quote decisions and sub-clock either-/two-sided gaps during the natural long run. The earlier 280/280 report is not a measured v7 result. [Repair and limits](docs/AVAILABILITY_AND_CHURN_2026-09-15.md).

- **Fill-flow repairs are deployed:** verify natural full/partial fills, cancellation timing, terminal-unfilled retry rounds and unaffected-market resting time. Startup cleared two old completed-lifecycle deadlines through authenticated flat proof. No induced live fills/faults. Unknown/rejected/accounting/ownership and held-inventory contract-lifecycle changes remain fail-closed. [Implementation and limits](docs/FILL_CONTINUITY_2026-09-15.md).

- **Authorized deployment completed:** v7 runs in `data/production-20260915-continuous-v10` with fresh build/account/policy-bound continuous authority. Preserve this run for natural observations; future source deployment still needs fresh authority and reconciliation. See [continuous-service evidence](docs/CONTINUOUS_SERVICE_2026-09-15.md).
- Measure actual retained-order duration, replacement gaps, reward score and CPU/memory/disk growth during a long run. Startup acceptance is not sustained live acceptance. Single-symbol sequence-gap isolation remains a follow-up requiring safe unsubscribe/snapshot-generation proof; shared-stream failure currently still pauses all dependent markets.

1. Verify the natural v7 midnight transition: override becomes false, new-day rankings refresh, the same process continues, and existing safe quotes remain. The superseded v6 expiry no longer controls this run. Never reuse an old build-bound permit.
2. Observe natural v7 public/private recovery with entries held through outages and authenticated reconciliation before resumption. Do not deliberately induce a live outage/fill. Unknown executions still require operator reconciliation. Do not claim complete Polymarket recovery parity.
3. Verify natural recurring-owner rollover. Bounded discovery retains healthy identities and replaces only expired/inactive flat owners; no live fault/fill injection is authorized. Missing candidates/snapshots can leave slots empty safely.
4. Calibrate local windows against a **nonempty** authenticated daily summary. Live freshness, scored-through time, exact reward activation and shared-pool uptime aggregation remain unconfirmed. No inferred 5pm reset or maintenance subtraction.
5. During the approved run, verify logs/heartbeat, same-symbol fill handling, exact timed exits and natural transient-read recovery. Synthetic tests are not live acceptance. Do not induce faults/fills without separate approval.

The 18:29 ET disappearance remains unexplained: both processes absent, empty old logs, no runner result. New observability does not repair an unproven external cause. [Repair evidence](docs/RUNTIME_HARDENING_2026-09-15.md).
