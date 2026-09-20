# Gemini current status

Updated September 20, 2026 at 19:18 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V34 is running and quoting after an operator-approved recovery restart.** The previous supervisor disappeared without a caught exception; its exact host termination trigger remains unproved. Later, an authenticated external manual close reached the account stream as an unowned event, so the runner halted safely. Two authenticated flat/order-free snapshots and exact filled-order history supported the explicit manual-flat repair; the original fill and six-hour HARD history were preserved. Fresh read-only preparation passed; a new exact v7 permit started the scheduled supervisor at 18:20 ET. At 18:33 ET, fresh REST showed 20 canonically owned BUY orders, no positions, no unowned orders and a healthy journal. The supervisor and runner were still sending fresh quoting heartbeats at 18:39 ET. No production source or business policy changed in this recovery.
- **The requested 50% estimator and incumbent funding order are reviewed but not deployed.** Isolated branch `feature/qualification-focus-20260920` commit `6bd1079` is pushed to GitHub; 1,066 tests, deliberate reversal, compilation and offline validation passed. The active v34 build is unchanged. A second controlled flat deployment and new authority are required to activate it. The separate owner-approved 70% entry-quality/15-minute crypto candidate passed 1,074 tests and is pushed on `feature/entry-quality-20260920`; it is not live. A 19:11 ET near-contemporaneous archive/public-book sample found 15/15 otherwise economically actionable markets passing, not a future breadth guarantee. At 19:17 ET, v34 still reported a running supervisor, quoting phase, five public shards and no stream holds or reward pause. [Entry-quality scope](docs/ENTRY_QUALITY_2026-09-20.md).
- Production is running and quoting on reward-focus / size-30 v34, deployed code `57e7036`, pushed to GitHub main. The owner approved implementation and restart. The old v33 run drained terminal/flat at 18:27:58 ET; verified backup and fresh authenticated preparation preserved economic history, sticky owners and the complete non-HARD risk window.
- Initial authenticated acceptance at 18:33 ET proved 20 canonically owned size-30 orders across 10 markets, zero positions, all 20 prior sticky owners retained, five connected/unheld public shards, no entry/stream holds or reward pause, and zero supervisor restarts. Runtime logs contained no warnings/errors and stderr was empty. Exact build/policy/account v7 authority has no uptime override. The automatic backup succeeded.
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- All 1,060 production tests plus 28 script tests passed, as did compilation, native PowerShell parsing and offline release checks. Post-merge reward-focus/size tests passed again. Partial-depth exact-position exit, collateral/accounting and correlated-stream recovery repairs remain included.

## Operator route

Active runtime directory: `data/production-20260919-reward-focus-v34`. Current permit: `activation-permit-recovery-20260920.json` within it. Use the normal read-only runtime/account checks for observation and graceful stop route for future deployments. Do not edit active source/configuration or reuse consumed/obsolete permits.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
