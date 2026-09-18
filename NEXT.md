# Next work

## Current release

- Missing public reward-event 404 isolation is implemented for discovery and live refresh; 826 total production/script cases pass across full-suite and targeted runs. Fresh repaired read-only readiness passed at 20:57:21 ET without orders/positions or history/owner changes. User can rerun the prepare-then-launch command; no current permit or live acceptance yet. [Evidence](docs/MISSING_REWARD_EVENT_2026-09-17.md).
- Automatic journal backups/startup integrity are implemented and verified: 799 tests and fresh read-only startup passed; the integrated first backup succeeded. Confirmed path and worker-error repairs and best-bid execution integration are also implemented. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- One-tick normal quoting with separate observation-only queue measurement passes 808 tests and offline validation. Bot remains stopped; user must initiate live trading after fresh exact-build/policy authority and startup reconciliation. V21 and earlier permits are obsolete. No reminder/review automation was created. Verify owned resting orders and growing measurement windows after user launch.

## Live evidence still required

**The overall best-bid/back-of-queue request is unfinished.** Verification of the current one-tick measurement strategy is not completion or live acceptance of that request. Finishing it requires a user-started trading measurement run, reviewed calibration, a configured two-market release and observed pilot behavior; do not describe it as fully done. The pilot's v3 configuration still requires two-tick nonpilot quotes; compatibility with one-tick nonpilot quotes needs a separate reviewed change before any pilot rollout.

- Best-bid trial: collect natural cancellation success/failure timings with depth and side-specific sell flow, distinguish operator drain/recovery observations, select exactly two existing owners and reviewed expiring thresholds, then validate fresh policy-bound authority. V18 had 30 complete flow windows but zero natural cancellation samples. Warmup/test thresholds are not calibration; no automatic expansion.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
