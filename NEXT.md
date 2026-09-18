# Next work

## Current release

- **All-selected integration and offline verification are complete, not deployed.** The shared rule now follows selected owners/replacements within 20/40 capacity; state cleanup, cold starts, partial fills/no-buyer/exact exit, proof pacing and full-width shared quota are tested. 972 production/script tests pass. [Restart handoff](docs/ALL_SELECTED_ADAPTIVE_2026-09-18.md).

- **Shared settings response and deployment remain:** [September 18 review](docs/ADAPTIVE_SETTINGS_PROPOSAL_2026-09-18.md) proposes a 250-contract buffer, full-burst reserve, 1.5 depth multiplier, ten-minute warmup, one-minute stable promotion, ten-second cooldown/cancellation budget and 250-contract requeue gain. The explicit settings question remains unanswered; the valid all-selected draft is non-active. Review its September 19 05:45 ET expiry if delayed. Later deployment needs a separate restart instruction, controlled source/configuration promotion, fresh account/build/policy authority and user launch. No hot edit or old v22 permit/helper reuse. Two markets are execution research samples, never a permanent cap or manual calibration requirement.

## Earlier implementation checkpoints (historical)

- **September 18 adaptive implementation supersedes manual per-market calibration as the candidate approach.** `feature/adaptive-queue-policy` is isolated, disabled and not deployed. The same shared rule computes side-specific thresholds and performs safe one-tick/best-bid transitions for two fixed pilot symbols. No real envelope/symbols have been configured. Existing running main is untouched. [Evidence and acceptance checklist](docs/ADAPTIVE_QUEUE_2026-09-18.md).

- **Enable-later source complete, isolated and not deployed:** one-tick nonpilot compatibility plus an offline candidate builder passed 855 tests. Active v22 remains on `840813b`, one tick, size 15, pilot disabled; recent read-only venue proof found 40 owned orders/20 markets and no positions. No live interruption or configuration change. [Workflow](docs/PILOT_ENABLE_LATER_2026-09-17.md).

The following release-preparation notes are historical; the user has since started v22:

- Missing public reward-event 404 isolation is implemented for discovery and live refresh; 826 total production/script cases pass across full-suite and targeted runs. Fresh repaired read-only readiness passed at 20:57:21 ET without orders/positions or history/owner changes. User can rerun the prepare-then-launch command; no current permit or live acceptance yet. [Evidence](docs/MISSING_REWARD_EVENT_2026-09-17.md).
- Automatic journal backups/startup integrity are implemented and verified: 799 tests and fresh read-only startup passed; the integrated first backup succeeded. Confirmed path and worker-error repairs and best-bid execution integration are also implemented. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- One-tick normal quoting with separate observation-only queue measurement passes 808 tests and offline validation. Bot remains stopped; user must initiate live trading after fresh exact-build/policy authority and startup reconciliation. V21 and earlier permits are obsolete. No reminder/review automation was created. Verify owned resting orders and growing measurement windows after user launch.

## Live evidence still required

**Best-bid live acceptance remains unfinished.** All-selected implementation is now present, not a future coding prerequisite. Review/approve the common expiring settings and concrete activation scope, deploy with fresh authority and observe actual joins/cancel/proof/requeue/fallback and unaffected markets. Optional two-name research mode stays fixed; explicit all-selected mode automatically admits replacements through normal ownership/cold-start checks. Minute archives cannot establish sub-second races or execution P&L. Do not require manual calibration per market, ask for unspecified waiting, or describe code tests as live acceptance.

- Best-bid acceptance: measure natural cancellation tails and gaps, distinguish operator drain/recovery from normal refresh, and verify reviewed policy-bound behavior. The September 18 settings review supersedes the old V18 zero-cancellation-sample checkpoint. No induced fills/faults, automatic renewal or silent research-to-all-selected policy change.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
