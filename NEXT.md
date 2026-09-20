# Next work

## Current release

- **Entry-quality policy v8 is live in v35.** Initial authenticated acceptance found 20 owned size-30 orders/ten markets, zero positions, five healthy shards and no holds/restarts; 15/15 otherwise actionable markets passed the synchronized live gate check at 19:38 ET. Observe natural gate failures, affected-market cancellation/recovery, resting continuity, later fills/exits and reward snapshots without inducing trades or faults. Do not infer future protection or 50% qualification from initial acceptance. [Design and deployment](docs/ENTRY_QUALITY_2026-09-20.md).

- **The 50% estimator/incumbent-funding change remains source-only.** It is verified and pushed on `feature/qualification-focus-20260920`, not included in v35. Any later deployment needs separate review, a controlled terminal-flat stop, exact account proof and new permit. The gate evaluates exit price as well as quantity but cannot foresee future repricing; measure natural outcomes before changing thresholds.
- **Reward focus plus size 30 is deployed and initially accepted.** At 18:33 ET v34 had 20 owned size-30 orders/10 markets, zero positions, 18 historical event preferences, all 20 prior owners preserved, five healthy shards and no holds/errors/restarts. All 1,088 production/script tests passed. Remaining work is natural live evidence: new/released-slot selection, continuity, fills/exits and subsequent reward outcomes. Do not reset healthy owners to demonstrate ranking. All monetary/HARD limits remain. [Evidence](docs/REWARD_FOCUS_2026-09-19.md).

- **Partial-depth exit repair is deployed (v33, retained in v34).** Continue observing natural exact-position/residual handling and unaffected-market continuity; available depth does not guarantee a fill or immediate flatness. [Evidence](docs/PARTIAL_DEPTH_EXIT_2026-09-19.md).

- **Correlated-stream recovery v32 is deployed and passed initial acceptance:** 20 owned size-15 best-bid orders/10 markets, zero positions/unowned orders/mismatches, five healthy shards and no holds/errors/restarts. Observe a natural multi-stream recovery without inducing a fault or crediting the gap. Initial startup does not prove that long-run path. [Evidence](docs/CORRELATED_STREAM_RECOVERY_2026-09-19.md).

- **Catalog-recovery v31 is deployed and passed initial acceptance:** 24 owned size-15 best-bid orders/12 markets, zero positions/unowned orders, five healthy shards and no holds/errors/restarts. Observe the next natural inconsistent metadata interval and recovery; never inject a live fault or count paused time as uptime. [Evidence](docs/REWARD_CATALOG_RECOVERY_2026-09-19.md).

- **Collateral-credit rounding v30 is deployed and passed initial acceptance.** The 5.93 NO position closed through BUY YES, the exact one-cent post-watermark correction applied once against fresh authenticated flat cash/position proof, and the new run started with 24 owned orders/12 markets, zero positions, healthy shards and no holds/errors/restarts. Continue read-only observation of natural fills/exits and cash reconciliation; never reset the baseline, reuse either earlier permit or induce a fill. [Evidence](docs/PARTIAL_FILL_PRECISION_AND_NO_EXIT_2026-09-19.md).

- **Malformed reward-contract isolation v28 is deployed and passed initial acceptance.** Continue read-only observation through the next natural reward-metadata refresh. Verify any unavailable contract is held/canceled only for its symbol while healthy markets remain quoted; never induce malformed venue data or claim initial startup proves every future schema change. [Evidence](docs/MALFORMED_REWARD_CONTRACT_2026-09-18.md).

- **Same-price churn v27 deployment completed.** Continue read-only observation after the 600-second warmup: verify no market receives two optional refresh decisions within 300 seconds, each optional decision has at least 500 estimated contracts of gain, mandatory price changes remain responsive, and unrelated pairs stay resting. Initial acceptance proves configured controls and best-bid prices, not long-run frequency, exact queue rank, rewards or profit. [Handoff](docs/SAME_PRICE_REFRESH_CHURN_2026-09-18.md).

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

**Partial-depth exit v33 is deployed and passed initial acceptance.** The manual close was reconciled from one exact authenticated SELL with the risk window preserved; the new source handles future thin depth through best-level exact-position IOC attempts plus exact residual proof. Initial live state has 24 owned size-15 orders/12 markets, zero positions, five healthy shards and no holds/errors/restarts. Continue observing natural partial exits; tests and initial flat startup do not prove a future venue fill or immediate flattening. [Evidence](docs/PARTIAL_DEPTH_EXIT_2026-09-19.md).

**Initial startup acceptance is complete; long-run adaptive acceptance remains unfinished.** Observe actual joins/cancel/proof/requeue/fallback and unaffected markets under natural conditions. Minute archives cannot establish sub-second races or execution P&L. Do not require manual calibration per market or describe code tests/startup alone as long-run acceptance.

- Best-bid acceptance: measure natural cancellation tails and gaps, distinguish operator drain/recovery from normal refresh, and verify reviewed policy-bound behavior. The September 18 settings review supersedes the old V18 zero-cancellation-sample checkpoint. No induced fills/faults, automatic renewal or silent research-to-all-selected policy change.
- Observe natural partial/full fills, exact exits/no-buyer isolation, shard/private recovery and long-run continuity. Tests are not live acceptance. No induced fills/faults or HARD bypass.
- Compare authenticated daily rewards, snapshot counts, attributed losses and fees against local estimates. Exact scoring windows/queue position/spread curve remain unavailable; no inferred 5pm reset or guaranteed payout.
- Hourly verified backups now preserve every table, including future uptime evidence. Older lost local telemetry cannot be restored or called exchange-qualified from order presence alone. Keep the earlier incomplete-history warning in interpretation; do not fabricate backfill.
- Historical venue rejection and unexplained host disappearance were not fully logged. Improved diagnostics/recovery do not prove their original causes. The AppData mismatch is confirmed for September 17, only a possible explanation for older corruption symptoms.

[Archived backlog/checkpoints](docs/history/NEXT_BEFORE_BACKUP_RELEASE_2026-09-17.md) is historical context, not active runtime authority.
