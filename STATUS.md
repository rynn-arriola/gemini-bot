# Gemini current status

Updated September 16, 2026. Current handoff, not independent venue proof.

## Operational truth

- **V7 is active on source `87fab92`**, runtime `data/production-20260916-churn-v11`. Owner authorized this restart with the pair-send repair. V10 drained to authenticated `stopped_flat` at 00:19:17 ET; its supervisor and both Python processes exited. Fresh authenticated preparation and startup reconciliation passed; risk/fill/deadline history and 14 sticky identities were preserved. New run activated at **00:21:28 ET**.
- At **00:22:16 ET**, local quoting heartbeat showed 28 pending orders, zero positions, 21 tracked symbols, no read/stream/exit holds and zero dropped logs. Supervisor 17788/runner launcher 36444, restart count zero. Logs contained no warnings/errors/critical events or new fills. Source build digest matches the new permit.
- **Pair-send and telemetry repairs are deployed.** Final admission binds NO to its exact still-working, unchanged, unfilled YES and connection generations. The prepared account callback repair is integrated; live counters recorded 40 NEW and 12 CANCELED events across 14 symbols, with NEW count matching raw logs at the check. Distinct cancellation reasons are live, including one `external_book_one_sided` and five inside-two-tick guard decisions. This does not yet establish sustained churn reduction.
- **No scheduled daily/session-expiry drain or uptime override.** Fresh v7 continuous authority has null expiry and null override; normal 50% admission applies. All risk/size/breadth/exit limits remain. STOP revokes this run; unknown/integrity/HARD conditions still require attention. No OS/boot/power changes.
- This deployment incurred **131.710 seconds** from the last v10 cancellation to first v11 quote, plus per-market restoration time. Gaps receive no reward credit. Prior v10 naturally crossed midnight in the same process; new fill/exit flow, exact recurring-owner acceptance and sustained recovery remain pending for v11.

## Verified release and limits

- **454 production + 28 script tests = 482 passed**, including 18 new pair-guard/diagnostic cases and the real-preparation telemetry regression. Compilation, whitespace, PowerShell and offline Windows release checks passed. Deployed/tested normalized source matches; 18 tests passed again in the deployment checkout. Fresh authenticated preparation found 21 tracked, 19 ranked and 14 normally eligible candidates.
- Deployed repairs: safe/economic resting retention, continuous standby and extended transport recovery, scoped terminal exit retry rounds, independent fill cancellation, exact flat cleanup, bounded mutation/drain waits, correlated read errors, reason-specific holds and socket-generation guards.
- Latest [pair-send release](docs/CHURN_RELEASE_2026-09-16.md) and [investigation](docs/CHURN_INVESTIGATION_2026-09-16.md). Earlier [availability/churn](docs/AVAILABILITY_AND_CHURN_2026-09-15.md), [fill continuity](docs/FILL_CONTINUITY_2026-09-15.md) and [continuous-service](docs/CONTINUOUS_SERVICE_2026-09-15.md) source-only labels describe historical predeployment checkpoints.
- Authenticated reward-summary calibration remains pending. Local clock/continuity estimates never prove exchange snapshots or rewards. The earlier unexplained process disappearance remains unproven; no host-crash recovery guarantee.

## Operator route

Use `scripts/check_runtime.ps1 -RuntimeDirectory data/production-20260916-churn-v11 -Logs` for local state/logs; it cannot independently establish venue flatness. [NEXT.md](NEXT.md) lists pending work and [DECISIONS.md](DECISIONS.md) retains the unchanged strategy/risk authority.
