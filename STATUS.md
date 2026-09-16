# Gemini current status

Updated September 15, 2026. Current handoff, not venue proof.

## Operational truth

- **Stopped; no restart authorized or performed.** Prior runtime: `data/production-20260915-nontrading-v8`. Last runner heartbeat 18:29:54 ET; recorded runner/supervisor PIDs are absent. Saved RUNNING/quoting labels are stale. Empty logs and missing runner result do not explain termination; root cause remains unconfirmed.
- Read-only preflight passed at **20:04:45 ET**: authenticated REST/WS confirmed no open positions or orders at start/finish; cash and positions matched persisted expectations. This is point-in-time evidence, not continuing monitoring.
- Source is ready for an **owner-approved bounded restart**, not activated. Fresh launch reconciliation and a new matching build-bound permit remain mandatory. Do not reuse the old permit. No order, cancel, live fault injection, risk reset or restart occurred.

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
