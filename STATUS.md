# Gemini current status

Updated September 25, 2026. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **The bot is still stopped. Policy v9 is in source only, not deployed.** It covers shielded placement, the 50% uptime ledger on a 5 pm ET window, crypto exclusion, $400 gross and 25 per leg, plus the pair-netting accounting fix. A run needs fresh read-only preparation, a new v9 permit and an explicit owner go-ahead, ideally just after 17:00 ET. The Sept 25 reward catalog differs from the one v41 ran against: only College Football and small crypto pools remain. [Design](docs/SHIELDED_PLACEMENT_2026-09-25.md).

- **V41 is stopped flat after the owner-requested 20:20 ET drain.** The graceful route disabled supervisor restart, terminally canceled the owned entries and recorded `stopped_flat`; both runner and supervisor exited with zero restarts. A subsequent authenticated REST check found zero active orders, zero unknown orders and zero positions, and canonical journal integrity passed. The closed session cannot reactivate without fresh owner authority. Earlier v41 acceptance and the DNS/cancellation-clock repair remain valid source evidence, not current liveness.
- **V41 initial acceptance passed before the stop.** Source `b7b9406` keeps each actual cancel attempt bounded while allowing only a proven-unsent v7 transport-replacement wait outside the expired attempt clock; fresh exact REST and terminal-history proof remain mandatory, and unknown sends remain non-replayable. It passed 1,093 production and 28 script tests plus compilation, PowerShell parsing, whitespace and offline release validation. Fresh preparation proved flat/order-free state before launch. At 07:58 ET authenticated REST found 20 exact journal-owned size-30 BUY orders in ten complete pairs, zero unknown orders and zero positions; five public shards were connected/unheld with no holds, live warning/error/critical events or supervisor restarts.
- **V40 stopped during the DNS outage and is superseded.** Its stopped interval and the v41 deployment gap earn no reward snapshots.
- **V39 stopped flat before this deployment.** Its tested acknowledgement-deferral recovery remains in v40. The stopped-to-resting interval earns no reward snapshots.
- **V38 stopped flat before this deployment.** At 20:02:36 ET a public pong timeout overlapped one private order awaiting acknowledgement. The exact `NEW` arrived in the same second, but immediate handling stopped the runner. Drain, later supervisor preparation and an independent authenticated check proved zero orders/positions before v39 preparation. Stopped intervals earn no reward uptime. [Incident detail](BUGFIXES.md).
- New/released slots now directly use a confidence-adjusted 50% live marginal-economics / 50% authenticated event-history preference. History uses normalized score per qualifying snapshot times qualification reliability; a local below-50% scenario is a soft ranking haircut, not an entry gate. Healthy existing owners are not replaced for calibration. Historical evidence is not an exact future dollar forecast or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Most recent runtime directory: `data/production-20260922-dns-recovery-v41`. Its session is closed and its permit must not be reused or copied. A later restart requires fresh read-only preparation and new exact build/policy/account authority.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
