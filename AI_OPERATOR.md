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

