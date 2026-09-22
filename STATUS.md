# Gemini current status

Updated September 21, 2026 at 22:33 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V39 is live and quoting.** Source `c43b814` matched GitHub main after 1,086 production and 28 script tests plus release checks. Fresh read-only preparation proved zero orders/positions, no holds and a verified backup. A new exact build/policy/account v7 permit with no uptime override launched through the persistent task. At the 22:33 acceptance check, authenticated REST found 16 canonically owned size-30 BUY orders in eight complete YES/NO pairs, zero unknown orders and zero positions. Five public shards were connected/unheld, no entry/stream holds or reward pause, and zero supervisor restarts. A natural private timeout at 22:31 recovered and reconciled in the same runner by 22:32, then quoting resumed. This is point-in-time evidence, not a guarantee of future recovery, uptime or rewards.
- **V38 stopped flat before this deployment.** At 20:02:36 ET a public pong timeout overlapped one private order awaiting acknowledgement. The exact `NEW` arrived in the same second, but immediate handling stopped the runner. Drain, later supervisor preparation and an independent authenticated check proved zero orders/positions before v39 preparation. Stopped intervals earn no reward uptime. [Incident detail](BUGFIXES.md).
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Active runtime directory: `data/production-20260921-ack-deferral-v39`. Its permit is consumed and cannot be reused. Use read-only observation and the graceful stop route for future deployments; do not edit active source/configuration or start a second owner.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
