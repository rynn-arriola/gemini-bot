# Next work

## Current release

- Automatic journal backups/startup integrity are implemented and verified: 799 tests and fresh read-only startup passed; the integrated first backup succeeded. Confirmed path and worker-error repairs and best-bid execution integration are also implemented. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- Bot remains stopped. V21 fresh preparation and exact-build/account/policy permit validation passed at 20:26:55 ET with unchanged history/20 owners and zero orders/positions. The unconsumed v21 permit is ready for a later explicit two-tick measurement restart; v19 is obsolete. Normal fresh startup proof and post-launch resting-order acceptance remain required.

## Live evidence still required

**The overall best-bid/back-of-queue request is unfinished.** Restart-readiness of the current two-tick measurement strategy is not completion or live acceptance of that request. Finishing it requires an authorized trading measurement run, reviewed calibration, a configured two-market release and observed pilot behavior; do not describe it as fully done.

- Best-bid trial: collect natural cancellation success/failure timings with depth and side-specific sell flow, distinguish operator drain/recovery observations, select exactly two existing owners and reviewed expiring thresholds, then validate fresh policy-bound authority. V18 had 30 complete flow windows but zero natural cancellation samples. Warmup/test thresholds are not calibration; no automatic expansion.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
