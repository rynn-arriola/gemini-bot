# Next work

## Current release

- **Shared settings proposal prepared, not approved:** [September 18 review](docs/ADAPTIVE_SETTINGS_PROPOSAL_2026-09-18.md) proposes a common 250-contract buffer, full-burst reserve, 1.5 depth multiplier, ten-minute warmup, one-minute stable promotion, ten-second cooldown/cancellation budget and 250-contract requeue gain. House/Senate GOP are research samples only. Final scope is all selected markets and replacements, per owner clarification. The current two-symbol assumptions still need bounded all-selected scope integration and full-capacity tests before broad deployment. No live settings, process, orders or permits changed.

- **September 18 adaptive implementation supersedes manual per-market calibration as the candidate approach.** `feature/adaptive-queue-policy` is isolated, disabled and not deployed. The same shared rule computes side-specific thresholds and performs safe one-tick/best-bid transitions for two fixed pilot symbols. No real envelope/symbols have been configured. Existing running main is untouched. [Evidence and acceptance checklist](docs/ADAPTIVE_QUEUE_2026-09-18.md).

- **Enable-later source complete, isolated and not deployed:** one-tick nonpilot compatibility plus an offline candidate builder passed 855 tests. Active v22 remains on `840813b`, one tick, size 15, pilot disabled; recent read-only venue proof found 40 owned orders/20 markets and no positions. No live interruption or configuration change. [Workflow](docs/PILOT_ENABLE_LATER_2026-09-17.md).

The following release-preparation notes are historical; the user has since started v22:

- Missing public reward-event 404 isolation is implemented for discovery and live refresh; 826 total production/script cases pass across full-suite and targeted runs. Fresh repaired read-only readiness passed at 20:57:21 ET without orders/positions or history/owner changes. User can rerun the prepare-then-launch command; no current permit or live acceptance yet. [Evidence](docs/MISSING_REWARD_EVENT_2026-09-17.md).
- Automatic journal backups/startup integrity are implemented and verified: 799 tests and fresh read-only startup passed; the integrated first backup succeeded. Confirmed path and worker-error repairs and best-bid execution integration are also implemented. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- One-tick normal quoting with separate observation-only queue measurement passes 808 tests and offline validation. Bot remains stopped; user must initiate live trading after fresh exact-build/policy authority and startup reconciliation. V21 and earlier permits are obsolete. No reminder/review automation was created. Verify owned resting orders and growing measurement windows after user launch.

## Live evidence still required

**Best-bid live acceptance remains unfinished.** The adaptive candidate adds reusable rules, not permission to apply arbitrary defaults. Review a common expiring envelope (latency allowance, burst/depth multipliers, buffer, continuity, promotion and cooldown), stage exactly two current incumbents, obtain owner approval, deploy with fresh authority and observe the pilot. Wider scope/replacement auto-admission requires a separate approved rollout; the pilot keeps its exact two symbols even if one expires. Minute archives cannot establish sub-second races or execution P&L. Do not ask for an unspecified amount of waiting or describe code tests as evidence sufficiency.

- Best-bid trial: collect natural cancellation success/failure timings with depth and side-specific sell flow, distinguish operator drain/recovery observations, select exactly two existing owners and reviewed expiring thresholds, then validate fresh policy-bound authority. V18 had 30 complete flow windows but zero natural cancellation samples. Warmup/test thresholds are not calibration; no automatic expansion.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
