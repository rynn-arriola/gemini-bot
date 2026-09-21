# Gemini current status

Updated September 21, 2026 at 18:27 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V37 reached quoting, then stopped flat; no runner is active.** The verified/pushed build `664c08f` launched after fresh flat preparation and permit. At 18:24:59 ET another public ping timeout coincided with an actually attempted maker send, so unknown-mutation safety halted the runner. Drain reached `stopped_flat` at 18:25:34 with zero orders/positions; its attention supervisor was dismissed. No stopped interval earns reward uptime. [Incident detail](BUGFIXES.md).
- **V38 liveness follow-up is source-only pending deployment.** It avoids per-second pings on public shards that have recent traffic, lengthens the bounded pong wait from two to five seconds, and omits the quoting-only ping worker during drain. Stale-market entry gates, unknown mutation/accounting, exact drain proof, risk limits and HARD remain unchanged. The isolated candidate passed 1,085 production and 28 script tests, compilation, PowerShell parsing and offline release checks; these cannot guarantee a future uninterrupted run.
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Stopped runtime directory: `data/production-20260921-shard-recovery-v37`. Its consumed permit cannot be reused. The owner's current request authorizes a controlled restart only after verified/pushed source, fresh read-only account/integrity preparation, exact new build/policy/account permit, and single-owner launch. Do not edit an active source/configuration or bypass an uncertain drain.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
