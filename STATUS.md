# Gemini current status

Updated September 21, 2026 at 18:36 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V38 is live and quoting.** Source `f8a0139` matched GitHub main; fresh read-only preparation proved zero orders/positions, no holds and a verified backup. A new exact build/policy/account v7 permit with no uptime override launched through the persistent task. First NEW was 18:34:09 ET. At the 18:36 acceptance check, authenticated REST found 20 canonically owned size-30 BUY orders in ten complete YES/NO pairs, zero unknown orders and zero positions. Five public shards were connected/unheld, no read/stream holds or reward pause, zero supervisor restarts and no current-run warning/error/critical or stderr. This is point-in-time evidence, not a guarantee of future uptime or rewards.
- **The v37 failed attempt drained flat.** Its public ping timeout coincided with an actually attempted maker send, so unknown-mutation safety halted; the runner reached `stopped_flat` at 18:25:34 with zero orders/positions. Its attention supervisor was dismissed. V38's quiet-only/five-second pings and no drain ping worker do not relax stale-market, mutation/accounting, risk or HARD gates. Stopped intervals earn no reward uptime. [Incident detail](BUGFIXES.md).
- New/released slots use a confidence-adjusted 70% live-model / 30% historical event preference. Four report dates supply evidence for 18 events. Healthy existing owners are not replaced for calibration. Historical payouts are not exact future dollar forecasts or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Active runtime directory: `data/production-20260921-quiet-liveness-v38`. Its `activation-permit-quiet-liveness-v38.json` is consumed; never reuse it. Use read-only runtime/account checks for observation and the graceful stop route for future deployments. Do not edit active source/configuration or start a second owner.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
