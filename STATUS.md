# Gemini current status

Updated September 20, 2026 at 20:09 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V36 is live and quoting after the owner's explicit controlled-restart choice.** V35 drained to `stopped_flat` at 20:02:50 ET with zero orders/positions; both old processes exited. Main and GitHub match tested commit `aa220cd`. Fresh read-only preparation proved zero orders/positions, verified backup and 16/16 otherwise-actionable full-depth entry-gate passes. A new exact build/policy/account-bound v7 permit with no uptime override activated v36 through the persistent task; first NEW was 20:06:57 ET. The stopped-to-first-NEW interval earns no reward uptime. [Qualification-focus deployment](docs/QUALIFICATION_FOCUS_2026-09-20.md).
- **Initial v36 acceptance at 20:08 ET:** authenticated REST found 20 canonically owned size-30 BUY orders across ten markets, zero unknown orders and zero positions. The runner was quoting with five connected/unheld public shards, no entry/stream holds or reward pause, zero supervisor restarts and no live warning/error/critical or stderr output. Its rank report showed 16/16 otherwise-actionable entry-gate passes and estimator fields on 29 ranked markets. This is point-in-time evidence, not future 50% qualification or payout.
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The v36 integrated release passed 1,080 production tests, 28 script tests, compilation, native PowerShell parsing and offline release validation; its 37 focused tests and offline release check passed again after fast-forward. Entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery repairs remain included.

## Operator route

Active runtime directory: `data/production-20260920-qualification-focus-v36`. Current permit: `activation-permit-qualification-v36.json` within it. Use the normal read-only runtime/account checks for observation and graceful stop route for future deployments. Do not edit active source/configuration or reuse consumed/obsolete permits.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
