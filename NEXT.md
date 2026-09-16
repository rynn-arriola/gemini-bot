# Next work

- **Current priority, September 16 07:23 ET:** bot stopped after maintenance failure. Source recovery fixes pass 479 production/28 script tests, not deployed. Resolve the sub-cent cash mismatch with exact authenticated evidence; never round it away or reset the baseline. Fresh final read had one position and one active order. Temporary force-exit helper stopped; no active monitoring or successful exit is claimed. See STATUS.md and the maintenance report.
- **Owner requested six-WebSocket recommendation:** proposed five public data shards plus one private account/execution connection. See `docs/WEBSOCKET_SHARDING_DESIGN_2026-09-16.md`. Implementation remains pending design review; preserve single mutation owner, shared rate authority and global account-safety gates.

## Historical deployment follow-ups (superseded operational state)

- **Pair-send repair deployed at 00:21:28 ET September 16:** source `87fab92`, runtime `data/production-20260916-churn-v11`, fresh continuous v7 authority with no uptime override. Exact first-order/generation guard plus diagnostics/telemetry pass 454 production/28 script tests. Observe natural prevented-orphan events and long-run churn; do not induce faults/fills. [Release](docs/CHURN_RELEASE_2026-09-16.md).
- **Churn observation:** adverse cancellation reasons now distinguish missing/stale/unsynchronized/one-sided/own-adjusted-empty books and invalid target prices. Observe these after deployment; additional bounded decision price/sequence context may be useful if ambiguity persists. Public/private depth timing mismatch remains unproven; never suppress genuine safety cancellation based on that theory.

- **Telemetry deployed and initially verified:** incorporated the fix/regression from `codex/telemetry-callback`. New-run counters at 00:22:16 ET recorded 40 NEW and 12 CANCELED across 14 symbols; NEW count matched raw logs. Continue validating sub-clock gaps during the long run; local continuity is not exchange qualification.

- **V7 availability/churn repairs are deployed:** bounded drain/admission, correlated read-error recovery, recovered-market holds and connection races passed offline verification. Measure safety/economics-bound retention, reason-coded quote decisions and sub-clock either-/two-sided gaps during the natural long run. The earlier 280/280 report is not a measured v7 result. [Repair and limits](docs/AVAILABILITY_AND_CHURN_2026-09-15.md).

- **Fill-flow repairs are deployed:** verify natural full/partial fills, cancellation timing, terminal-unfilled retry rounds and unaffected-market resting time. Startup cleared two old completed-lifecycle deadlines through authenticated flat proof. No induced live fills/faults. Unknown/rejected/accounting/ownership and held-inventory contract-lifecycle changes remain fail-closed. [Implementation and limits](docs/FILL_CONTINUITY_2026-09-15.md).

- **Authorized deployment completed:** v7 runs in `data/production-20260916-churn-v11` with fresh build/account/policy-bound continuous authority. Preserve this run for natural observations; future source deployment still needs fresh authority and reconciliation. See [latest release evidence](docs/CHURN_RELEASE_2026-09-16.md).
- Measure actual retained-order duration, replacement gaps, reward score and CPU/memory/disk growth during a long run. Startup acceptance is not sustained live acceptance. Single-symbol sequence-gap isolation remains a follow-up requiring safe unsubscribe/snapshot-generation proof; shared-stream failure currently still pauses all dependent markets.

1. Natural v7 midnight verified September 16: override false, refreshed new-day rankings, unchanged worker/supervisor and zero restarts; churn remained confined to XRP/ZEC in the measured window. Continue the long-run observation; this is not proof of exchange reward qualification. Never reuse an old build-bound permit.
2. Observe natural v7 public/private recovery with entries held through outages and authenticated reconciliation before resumption. Do not deliberately induce a live outage/fill. Unknown executions still require operator reconciliation. Do not claim complete Polymarket recovery parity.
3. Verify natural recurring-owner rollover. Bounded discovery retains healthy identities and replaces only expired/inactive flat owners; no live fault/fill injection is authorized. Missing candidates/snapshots can leave slots empty safely.
4. Calibrate local windows against a **nonempty** authenticated daily summary. Live freshness, scored-through time, exact reward activation and shared-pool uptime aggregation remain unconfirmed. No inferred 5pm reset or maintenance subtraction.
5. During the approved run, verify logs/heartbeat, same-symbol fill handling, exact timed exits and natural transient-read recovery. Synthetic tests are not live acceptance. Do not induce faults/fills without separate approval.

The 18:29 ET disappearance remains unexplained: both processes absent, empty old logs, no runner result. New observability does not repair an unproven external cause. [Repair evidence](docs/RUNTIME_HARDENING_2026-09-15.md).
