# Gemini current status

Updated September 22, 2026 at 07:59 ET. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **V41 is live and quoting after the DNS/cancellation-clock repair.** At 01:17 ET v40 lost DNS on the private stream and all five public shards while adverse-quote cancels were pending. Those cancels were proven unsent, but their old 45-second clocks expired during reconnect and incorrectly halted the global gate. Later authenticated REST proved zero active orders/positions, and the attention-only supervisor was dismissed. Source `b7b9406` keeps each actual cancel attempt bounded while allowing only a proven-unsent v7 transport-replacement wait outside the expired attempt clock; fresh exact REST and terminal-history proof remain mandatory, and unknown sends remain non-replayable. It passed 1,093 production and 28 script tests plus compilation, PowerShell parsing, whitespace and offline release validation, then matched GitHub main. Fresh read-only preparation found 32 tracked/31 ranked/14 currently eligible candidates and ended `ready_paused`; a second authenticated check proved flat/order-free. A new exact build/policy/account v7 permit with no uptime override launched through the persistent task. At 07:58 ET authenticated REST found 20 exact journal-owned size-30 BUY orders in ten complete pairs, zero unknown orders and zero positions. Five public shards were connected/unheld, journal integrity and backup health passed, entry/stream holds and live warning/error/critical events were empty, and the supervisor had zero restarts. This is initial acceptance, not proof of future recovery, qualification, payout or profit.
- **V40 stopped during the DNS outage and is superseded.** Its stopped interval and the v41 deployment gap earn no reward snapshots.
- **V39 stopped flat before this deployment.** Its tested acknowledgement-deferral recovery remains in v40. The stopped-to-resting interval earns no reward snapshots.
- **V38 stopped flat before this deployment.** At 20:02:36 ET a public pong timeout overlapped one private order awaiting acknowledgement. The exact `NEW` arrived in the same second, but immediate handling stopped the runner. Drain, later supervisor preparation and an independent authenticated check proved zero orders/positions before v39 preparation. Stopped intervals earn no reward uptime. [Incident detail](BUGFIXES.md).
- New/released slots now directly use a confidence-adjusted 50% live marginal-economics / 50% authenticated event-history preference. History uses normalized score per qualifying snapshot times qualification reliability; a local below-50% scenario is a soft ranking haircut, not an entry gate. Healthy existing owners are not replaced for calibration. Historical evidence is not an exact future dollar forecast or contract-level attribution. [Formula, limits and deployment](docs/REWARD_FOCUS_2026-09-19.md).
- Maker size is 30 contracts at best bid. Unchanged limits: 40 orders, 20 owners, 32 watched markets, $300 gross pending exposure, $90 per market, $40 stressed-entry cap and $30 six-hour HARD. Five public plus one shared private socket remain. Optional same-price refresh retains 300-second / 500-contract controls. At ordinary prices the unchanged capital cap supports about ten pairs at this size; reserved ownership alone earns no snapshots.
- The prior v36 entry-quality v8, partial-depth exact-position exit, collateral/accounting and correlated-stream recovery behavior remains included; size, risk and HARD policy are unchanged.

## Operator route

Active runtime directory: `data/production-20260922-dns-recovery-v41`. Its permit is active and must not be copied to another session. Use read-only observation and the graceful stop route for future deployments; do not edit active executable source/configuration or start a second owner.

Canonical authority is `%USERPROFILE%\GeminiRewardBot\control`, never legacy AppData or its package-cache view. Never start a second owner, reset the cohort/economic history, bypass HARD, or give downtime snapshot credit. Unknown accounting/mutation/integrity failures remain fail-closed; bounded known recovery and market-local isolation are unchanged.

## Remaining evidence

Observe natural fills/partial exits, continuity, reward outcomes and newly allocated slots. Initial startup and tests cannot guarantee fills, exact queue rank, 50% qualification, reward, profit or crash-free long-run behavior. Daily report publication time is not proof of the scoring-window reset. No monitoring automation was requested or created.

[NEXT.md](NEXT.md) contains the evidence backlog. [Earlier checkpoints](docs/history/STATUS_BEFORE_REWARD_FOCUS_2026-09-19.md) are historical only.
