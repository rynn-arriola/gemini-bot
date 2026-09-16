# Gemini current status

Updated September 16, 2026. Current handoff, not independent venue proof.

## Operational truth

- **V7 is active**, deployed source commit `03c57da` (runtime repairs `c1c6876`), runtime `data/production-20260915-continuous-v10`. Owner authorized restart after review. Old v9 drained to authenticated `stopped_flat` and its supervisor stopped at 23:08:02 ET. Fresh authenticated preparation passed; risk/fill history and 14 sticky identities were preserved. V7 activated at 23:11:02 ET.
- At **00:08:33 ET September 16**, local quoting heartbeat showed 28 pending orders, zero positions, 21 tracked symbols, no read/stream/exit holds and zero dropped logs. Supervisor restart count zero. Logs contained no warnings/errors/critical events or new fills. Natural midnight passed in the same worker: override false, September 16 reward epoch, and 14 eligible candidates under normal day-specific admission. A tracked-owner refresh caused a second same-day reranking event as designed.
- **No scheduled daily/session-expiry drain.** Fresh v7 continuous authority replaces the bounded v6 permit. The uptime exception ended at 00:00 Eastern September 16; normal 50% admission is active in the same process. All risk/size/breadth/exit limits remain. STOP revokes this run; unknown/integrity/HARD conditions still require attention. No OS/boot/power changes.
- Initial retention evidence at 23:14:00 ET: 969 retained-pair checks, one incomplete-pair maintenance replacement, ten reason-coded inside-two-tick safety cancellations. This short, different market window does not establish a percentage churn improvement or reward qualification.
- **Telemetry repair prepared separately, not deployed:** branch `codex/telemetry-callback` reconnects the event callback after shared-journal account replacement. The active v10 run still has the original zero per-event counters and clock-only continuity; raw order logs remain available. Owner prohibited restart. Keeping this source change in an isolated worktree preserves the running checkout's permit/build match and automatic recovery authority.
- **Churn investigation:** 23:35:22–00:05:22 ET contained 141 CANCELED/141 NEW events, exclusively XRP/ZEC. An offline probe confirms a second maker leg may be sent after its first leg is canceled during admission, then canceled as an incomplete pair. P2 repair and clearer unavailable-book diagnostics are pending; live source remains unchanged. [Evidence and limits](docs/CHURN_INVESTIGATION_2026-09-16.md).
- Deployment incurred approximately **180.5 seconds** between the last old cancellation and first new quote, plus per-market restoration time. Safety, exact exits and eligibility override uptime. Midnight is now observed; recurring-owner replacement was logged, but exact owner/cause acceptance, new fill/exit flow and sustained recovery remain pending for v7.

## Verified release and limits

- **435 production + 28 script tests = 463 passed**, including 41 availability/churn cases and three negative controls. Compilation, whitespace, PowerShell and offline Windows release checks passed. Deployment repeated offline storage/policy validation and fresh authenticated preparation (21 tracked, 19 ranked, two normally eligible).
- Deployed repairs: safe/economic resting retention, continuous standby and extended transport recovery, scoped terminal exit retry rounds, independent fill cancellation, exact flat cleanup, bounded mutation/drain waits, correlated read errors, reason-specific holds and socket-generation guards.
- Release evidence: [availability/churn](docs/AVAILABILITY_AND_CHURN_2026-09-15.md), [fill continuity](docs/FILL_CONTINUITY_2026-09-15.md), [continuous service](docs/CONTINUOUS_SERVICE_2026-09-15.md). Their source-only labels describe predeployment checkpoints.
- Authenticated reward-summary calibration remains pending. Local clock/continuity estimates never prove exchange snapshots or rewards. The earlier unexplained process disappearance remains unproven; no host-crash recovery guarantee.

## Operator route

Use `scripts/check_runtime.ps1 -RuntimeDirectory data/production-20260915-continuous-v10 -Logs` for local state/logs; it cannot independently establish venue flatness. [NEXT.md](NEXT.md) lists pending work and [DECISIONS.md](DECISIONS.md) retains the unchanged strategy/risk authority.
