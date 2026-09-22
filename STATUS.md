# Gemini current status

Updated September 21, 2026 at 23:21 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V40 is live and quoting with realized-score calibration v2 active.** Source `2473c5b` passed 1,089 production and 28 script tests, compilation, PowerShell parsing, whitespace and offline release validation, then matched GitHub main. V39 drained to `stopped_flat`; both old processes exited. Fresh read-only preparation proved zero orders/positions, five healthy shards and a verified backup. A new exact build/policy/account v7 permit with no uptime override launched through the persistent task. At the 23:21 ET acceptance check, authenticated REST found 20 canonically owned size-30 BUY orders in ten complete YES/NO pairs, zero unknown orders and zero positions. Runtime showed calibration report version 2, six authenticated reward dates, five connected/unheld public shards, no entry/stream holds, reward pause, warning/error/critical or supervisor restart. This is point-in-time evidence, not a guarantee of future recovery, qualification, payout or profit.
- **V39 stopped flat before this deployment.** Its tested acknowledgement-deferral recovery remains in v40. The stopped-to-resting interval earns no reward snapshots.
- **V38 stopped flat before this deployment.** At 20:02:36 ET a public pong timeout overlapped one private order awaiting acknowledgement. The exact `NEW` arrived in the same second, but immediate handling stopped the runner. Drain, later supervisor preparation and an independent authenticated check proved zero orders/positions before v39 preparation. Stopped intervals earn no reward uptime. [Incident detail](BUGFIXES.md).
- New/released slots now directly use a confidence-adjusted 50% live marginal-economics / 50% authenticated event-history preference. History uses normalized score per qualifying snapshot times qualification reliability; a local below-50% scenario is a soft ranking haircut, not an entry gate. Healthy existing owners are not replaced for calibration. Historical evidence is not an exact future dollar forecast or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Active runtime directory: `data/production-20260921-reward-calibration-v40`. Its permit is active and must not be copied to another session. Use read-only observation and the graceful stop route for future deployments; do not edit active executable source/configuration or start a second owner.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
