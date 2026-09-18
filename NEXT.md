# Next work

## Current release

- **Enable-later source complete, isolated and not deployed:** one-tick nonpilot compatibility plus an offline candidate builder passed 855 tests. Active v22 remains on `840813b`, one tick, size 15, pilot disabled; recent read-only venue proof found 40 owned orders/20 markets and no positions. No live interruption or configuration change. [Workflow](docs/PILOT_ENABLE_LATER_2026-09-17.md).

The following release-preparation notes are historical; the user has since started v22:

- Missing public reward-event 404 isolation is implemented for discovery and live refresh; 826 total production/script cases pass across full-suite and targeted runs. Fresh repaired read-only readiness passed at 20:57:21 ET without orders/positions or history/owner changes. User can rerun the prepare-then-launch command; no current permit or live acceptance yet. [Evidence](docs/MISSING_REWARD_EVENT_2026-09-17.md).
- Automatic journal backups/startup integrity are implemented and verified: 799 tests and fresh read-only startup passed; the integrated first backup succeeded. Confirmed path and worker-error repairs and best-bid execution integration are also implemented. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- One-tick normal quoting with separate observation-only queue measurement passes 808 tests and offline validation. Bot remains stopped; user must initiate live trading after fresh exact-build/policy authority and startup reconciliation. V21 and earlier permits are obsolete. No reminder/review automation was created. Verify owned resting orders and growing measurement windows after user launch.

## Live evidence still required

**Best-bid live acceptance remains unfinished; the known enable-later code work is complete on this branch.** Measurement is running. Remaining work is evidence review, explicit calibrated two-incumbent configuration, owner approval, controlled deployment/fresh authority and observed live pilot behavior. One-tick nonpilot compatibility is implemented and tested but not deployed. Evidence may expose new issues; no automatic enablement or sufficiency claim.

- Best-bid trial: collect natural cancellation success/failure timings with depth and side-specific sell flow, distinguish operator drain/recovery observations, select exactly two existing owners and reviewed expiring thresholds, then validate fresh policy-bound authority. V18 had 30 complete flow windows but zero natural cancellation samples. Warmup/test thresholds are not calibration; no automatic expansion.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
