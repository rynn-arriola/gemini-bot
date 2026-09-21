# AI operator entry point

This repository is managed operationally by AI on native Windows. The human account owner makes business and risk decisions; the AI implements, tests, documents, operates, monitors, and reports those decisions.

## Mandatory reading order

1. [STATUS.md](STATUS.md) — current operational truth and handoff.
2. [DECISIONS.md](DECISIONS.md) — active human decisions; never silently override them.
3. [AI_ENGINEERING.md](AI_ENGINEERING.md) — permanent engineering and evidence rules.
4. [docs/SAFETY_INVARIANTS.md](docs/SAFETY_INVARIANTS.md) before any trading/runtime change.
5. [NEXT.md](NEXT.md) for unfinished work; [WATCHLIST.md](WATCHLIST.md) for measurement-only ideas.
6. [FINDINGS.md](FINDINGS.md) for measured evidence and corrections.
7. [BUGFIXES.md](BUGFIXES.md) for confirmed defects.

The canonical venue surface is [GEMINI_PREDICTIONS_API_REFERENCE.md](GEMINI_PREDICTIONS_API_REFERENCE.md). Re-check official Gemini documentation before live protocol or reward-rule changes.

## Mandatory model selection

Only GPT-6 Astra and GPT-5.6 Sol are used for this repository. Before every substantive task, tell the owner which model to use in exactly one of these forms, with one short reason:

`Recommended agent: Astra — [reason]`

`Recommended agent: 5.6 Sol — [reason]`

GPT-5.6 Sol is the default. Use Sol for approved-plan implementation, focused fixes with an established cause, tests and verification, routine logs/status/reports/data checks, documentation and formatting, deployment/restart/standard operations, and fixes requested by an Astra review.

Use GPT-6 Astra only when deeper reasoning is materially needed: new feature architecture or end-to-end design; strategy, risk or business-logic changes; difficult incidents with an uncertain cause; cross-system concurrency, recovery, security or account-state problems; API research that may affect architecture, strategy, compliance or risk; and final review of meaningful or high-risk live-money code changes.

For meaningful development, keep each phase on one model and use this handoff:

1. Astra analyzes and designs only, then stops.
2. Sol implements the approved design, tests and verifies, then stops.
3. Astra reviews the implementation for architectural violations, races, security problems, edge cases and incorrect assumptions, then stops.
4. Sol implements the review findings and reruns verification.

Do not use Astra for routine work Sol can reliably complete. Escalate from Sol only when evidence reveals material architectural uncertainty or risk, and do not switch models repeatedly within one phase. If the selected model does not match the recommendation, stop before development so the owner can switch. Urgent safety action—such as stopping a live bot or preventing exposure—never waits for a model switch. Small low-risk changes may be implemented and verified by Sol without Astra review. This routing changes no safety, testing, deployment, restart or activation requirement below.

## Authority split

The account owner decides capital, loss limits, position limits, active breadth, quote placement policy, timing policy, and whether live activation is allowed. AI may recommend values, but recommendations are not authority.

AI owns implementation details and may make conservative fail-closed choices within approved limits. AI must stop and ask before materially increasing risk, enabling a new mutation path, changing approved business policy, or starting live trading without an explicit activation decision.

## Current product boundary

This is a live-trading product, not a paper simulator. Construction and startup are nevertheless mutation-safe: production always starts paused, reconstructs the account from authenticated REST and WebSocket evidence, and requires explicit activation before submitting an order.

The prior paper observer under `scripts/` and `data/` is retained as research evidence. It is not the production execution path.

## Platform boundary

The canonical authority directory is `%USERPROFILE%\GeminiRewardBot\control`, outside AppData. MSIX-packaged operator processes can virtualize AppData WAL/SHM/lock files while the native Scheduled Task sees the physical files. Never use the legacy `%LOCALAPPDATA%` directory or a package-cache view for new preparation, permits, accounting or ownership. Existing deployments require explicit native verified migration; missing new authority with an existing legacy database fails closed, never starts a blank ledger. The September 17 migration preserved every table and both old/cache copies. See `docs/CONTROL_LOCATION_2026-09-17.md`.

The supported host is native Windows. Use PowerShell, Windows paths, a Windows process supervisor/task mechanism, and Windows-compatible locking. Do not introduce dependencies on Ubuntu, WSL, Bash, systemd, cron, `fcntl`, or POSIX signals.

## Documentation discipline

- Keep `STATUS.md` short and current; move completed narratives to `docs/history/`.
- Record a user-approved policy in `DECISIONS.md` before relying on it.
- Record measured evidence in `FINDINGS.md`, not assumptions.
- Put unfinished implementation in `NEXT.md`; ideas that cannot alter runtime go in `WATCHLIST.md`.
- Update API/reference dates and source links when venue facts change.
- Never place credentials, signatures, signed payloads, nonces, balances, or sensitive account details in Markdown, logs, tests, command output, or chat.

