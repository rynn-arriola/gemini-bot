# Next work

## Current release

- **Same-price churn candidate is verified and published; deployment remains.** Full regression and offline release checks pass on the isolated branch. Deployment is not authorized; a later controlled deployment must drain terminal/flat, preserve history, use fresh exact authority and verify price-change responsiveness separately from optional same-price refresh frequency. [Handoff](docs/SAME_PRICE_REFRESH_CHURN_2026-09-18.md).

- **Stay-best-bid v26 deployment completed.** Continue read-only observation of natural price-change repositioning, useful same-price refresh, fills/exits, economic pauses and unaffected markets. Initial acceptance proved owned best-bid prices at two consecutive archive points, not uninterrupted future presence, exact queue position, reward qualification or profitability. Queue-settings expiry disables optional same-price optimization only; v6 best-bid placement continues under all existing safety gates. [Handoff](docs/STAY_BEST_BID_2026-09-18.md).

- **V25 daily sticky deployment completed.** Continue read-only observation of profitability-only reservations, same-owner recovery and post-midnight fresh grace. Initial acceptance found 32 owned orders/16 markets, zero positions, five healthy public shards and three economic reservations; reserved idle slots grant no uptime or order permission. [Details](docs/DAILY_STICKY_ECONOMICS_2026-09-18.md).

- **V24 controlled deployment completed.** Continue read-only observation of real promotion, requeue/demotion, natural fills/exits and unaffected markets. The initial 34-order/17-market flat acceptance proves startup health, not long-run behavior, queue position, snapshot qualification or reward. Do not extend the unchanged envelope without a new owner decision. [Evidence](docs/ADAPTIVE_TRANSITION_REPAIR_2026-09-18.md).

- **All-selected v23 is live under the approved shared envelope.** Initial acceptance passed with exact build/policy/account authority, zero positions, owned resting orders, five healthy public shards and no holds/restarts. Continue read-only observation through natural promotions/demotions, fills/exits and recovery; do not induce fills/faults or edit the active source/configuration.

- **Before the September 19 05:45 ET adaptive expiry:** review live evidence and make a new explicit owner decision only if renewal or changed settings are desired. Without renewal the service remains 24/7 and safely uses its one-tick fallback. Do not infer reward qualification or queue position from local presence.

## Earlier implementation checkpoints (historical)

- **September 18 adaptive implementation supersedes manual per-market calibration as the candidate approach.** `feature/adaptive-queue-policy` is isolated, disabled and not deployed. The same shared rule computes side-specific thresholds and performs safe one-tick/best-bid transitions for two fixed pilot symbols. No real envelope/symbols have been configured. Existing running main is untouched. [Evidence and acceptance checklist](docs/ADAPTIVE_QUEUE_2026-09-18.md).

- **Enable-later source complete, isolated and not deployed:** one-tick nonpilot compatibility plus an offline candidate builder passed 855 tests. Active v22 remains on `840813b`, one tick, size 15, pilot disabled; recent read-only venue proof found 40 owned orders/20 markets and no positions. No live interruption or configuration change. [Workflow](docs/PILOT_ENABLE_LATER_2026-09-17.md).

The following release-preparation notes are historical; the user has since started v22:

- Missing public reward-event 404 isolation is implemented for discovery and live refresh; 826 total production/script cases pass across full-suite and targeted runs. Fresh repaired read-only readiness passed at 20:57:21 ET without orders/positions or history/owner changes. User can rerun the prepare-then-launch command; no current permit or live acceptance yet. [Evidence](docs/MISSING_REWARD_EVENT_2026-09-17.md).
- Automatic journal backups/startup integrity are implemented and verified: 799 tests and fresh read-only startup passed; the integrated first backup succeeded. Confirmed path and worker-error repairs and best-bid execution integration are also implemented. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- One-tick normal quoting with separate observation-only queue measurement passes 808 tests and offline validation. Bot remains stopped; user must initiate live trading after fresh exact-build/policy authority and startup reconciliation. V21 and earlier permits are obsolete. No reminder/review automation was created. Verify owned resting orders and growing measurement windows after user launch.

## Live evidence still required

**Initial startup acceptance is complete; long-run adaptive acceptance remains unfinished.** Observe actual joins/cancel/proof/requeue/fallback and unaffected markets under natural conditions. Minute archives cannot establish sub-second races or execution P&L. Do not require manual calibration per market or describe code tests/startup alone as long-run acceptance.

- Best-bid acceptance: measure natural cancellation tails and gaps, distinguish operator drain/recovery from normal refresh, and verify reviewed policy-bound behavior. The September 18 settings review supersedes the old V18 zero-cancellation-sample checkpoint. No induced fills/faults, automatic renewal or silent research-to-all-selected policy change.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
