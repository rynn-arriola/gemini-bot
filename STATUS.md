# Gemini current status

Updated September 15, 2026. Current handoff, not venue proof.

## Operational truth

- **Active under the bounded Windows supervisor.** The owner explicitly requested restart on September 15, 2026. Runtime `data/production-20260915-nontrading-v8` completed a fresh read-only reconciliation at 20:15 ET, then activated at 20:17 ET with a new final-build/account/policy-bound version-5 permit; the old permit was not reused.
- Verified at **20:18 ET**: fresh runner and supervisor heartbeats, phase `quoting`, 30 pending orders, zero open positions, 32 tracked symbols, no entry-read or stream-recovery holds, zero dropped log records, and no error/critical log records. This is point-in-time evidence; continue using the runtime checker for current health.
- The permit and explicit uptime-proxy override expire at 00:00 America/New_York on September 16. The supervisor may make at most two same-session restarts after durable flat proof. All prior limits and fail-closed rules remain unchanged; the unexplained 18:29 ET process disappearance is still unconfirmed.
- Snapshot impact: the stopped interval and launch reconciliation lost qualifying time. The restart resumes quoting but cannot recover missed snapshots or guarantee Gemini's 50% qualification; safety and exact reconciliation continue to override uptime.

## Prepared and verified

- Conservative estimated venue window is separate from local crypto/sports exit cutoff. Exiting early reduces earning opportunities, never the denominator. Unknown reward activation, exact snapshot phase and maintenance deductions are not invented.
- Exchange daily-summary counts remain separate five-minute reporting, not guaranteed live counters or admission authority. Prior 18:41 ET read returned no rows; nonempty calibration is pending.
- Fixed second-leg immediate fills, periodic cash-event race, atomic fill/expected-state/deadline writes, explicit read-only manual-flat preparation and current-owner reward metadata refresh.
- Typed periodic REST failures and public/private stream loss have bounded same-process recovery. Entries remain held until fresh state, ownership and terminal-history proof pass; unknown fills/mutations still fail closed. Three reconnect attempts per affected stream/60 seconds overall; original deadlines, risk and session limits remain. Recurring-symbol replacement is still open.
- Structured/redacted runner lifecycle, minute heartbeat, order/recovery/failure logs; rotated supervisor history, ten-second status heartbeat and stale-state diagnostic command.
- Validation: **277 production + 28 script tests**, compilation, PowerShell parsing and whitespace checks passed. Five-minute read-only preflight: 29/29 fresh books in all 17 samples; two eligible candidates; zero depth gaps, dropped work/logs or mutation attempts; both WS rate limits verified; 0.38% of one CPU core. Nonempty redacted lifecycle logs verified. [Report](data/preflight-20260915-stream-recovery/preflight-report.json).
- The preflight overlapped final recovery hardening; final recovery branches are offline-tested, not induced on the live venue. The old disappearance's cause and natural recovery/exit timing remain unverified. [Repair evidence](docs/RUNTIME_HARDENING_2026-09-15.md).

## Operator route

Use `scripts/check_runtime.ps1 -RuntimeDirectory data/production-20260915-nontrading-v8 -Logs` for local state/logs. It cannot establish venue flatness. Read [NEXT.md](NEXT.md) before activation and [DECISIONS.md](DECISIONS.md) for unchanged strategy/risk authority.
