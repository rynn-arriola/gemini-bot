# Gemini current status

Updated September 21, 2026 at 22:22 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V38 stopped flat; no runner or supervisor is active.** At 20:02:36 ET a public pong timeout overlapped one private order awaiting acknowledgement. The exact `NEW` event arrived in the same second, but immediate global handling had already stopped the runner. Drain reached `stopped_flat` at 20:03:18. The later supervisor reconciliation and an independent 22:21 authenticated REST check both found zero orders and zero positions; the attention supervisor was dismissed. Stopped intervals earn no reward uptime. [Incident detail](BUGFIXES.md).
- **V39 acknowledgement-deferral repair is source-only pending deployment.** It holds all new entries while exact private evidence is pending, preserves unrelated resting orders, then recovers only the affected public shard. A mutation still unknown after the bounded existing send window remains globally fatal. Frames received during a slow pong prove that socket readable. The isolated candidate passed 1,086 production and 28 script tests, compilation, PowerShell parsing, offline release validation and whitespace checks. No risk, size, accounting, exit or HARD policy changes.
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Stopped runtime directory: `data/production-20260921-quiet-liveness-v38`. Its permit is consumed and cannot be reused. The owner's current request authorizes a controlled restart only after verified/pushed source, fresh read-only account/integrity preparation, a new exact build/policy/account permit and single-owner launch.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
