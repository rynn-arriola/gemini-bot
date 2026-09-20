# Gemini current status

Updated September 20, 2026 at 19:40 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V35 is live and quoting after the owner's September 20 restart instruction.** V34 drained to `stopped_flat` at 19:32:58 ET with zero orders/positions, and both old processes exited before the fast-forward to tested/pushed main `2bbb37a`. Fresh read-only preparation under policy v8 proved zero orders/positions, healthy journal backup and 29/29 full-depth ranked gate passes (14/14 otherwise actionable) at 19:34:56 ET. A new exact build/policy/account-bound v7 permit with no uptime override activated v35 through the persistent task; first NEW was 19:37:34 ET. The stopped-to-first-NEW interval earns no reward uptime. [Entry-quality deployment](docs/ENTRY_QUALITY_2026-09-20.md).
- **Initial v35 acceptance at 19:39 ET:** authenticated REST found 20 canonically owned size-30 entry orders across ten markets and zero positions. The runner was quoting with five connected/unheld public shards, no entry/stream holds or reward pause, zero supervisor restarts and no live warning/error/critical or stderr output. Its current synchronized rank report showed 15/15 otherwise actionable candidates passing the entry gate. This is point-in-time evidence, not future fill protection or Gemini reward qualification.
- **The separate 50% estimator/incumbent-funding branch remains unmerged and undeployed.** `feature/qualification-focus-20260920` commit `6bd1079` is pushed and verified; v35 did not include it. No cohort reset or risk-limit change accompanied this restart.
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The v35 entry-quality release passed 1,074 production tests, 28 script tests, compilation, native PowerShell parsing and offline release validation; its 14 focused tests and offline release check passed again after fast-forward. Partial-depth exact-position exit, collateral/accounting and correlated-stream recovery repairs remain included.

## Operator route

Active runtime directory: `data/production-20260920-entry-quality-v35`. Current permit: `activation-permit-entry-quality-v35.json` within it. Use the normal read-only runtime/account checks for observation and graceful stop route for future deployments. Do not edit active source/configuration or reuse consumed/obsolete permits.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
