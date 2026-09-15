# AI engineering rules

## Architecture

- Keep domain decisions pure and transport-independent. Network adapters convert venue messages into typed domain state.
- One mutation owner controls order intents. Record an intent before sending; after any attempted send, treat the outcome as unknown until reconciled.
- Use one public market-data WebSocket and one authenticated account/execution WebSocket by default. Add connections only from measured capacity evidence, never as quota evasion.
- Maintain one shared rate authority across all adapters. Emergency cancellation has scheduling priority but never bypasses an explicit server cooldown.
- Do not silently turn a WebSocket outage into high-frequency REST polling. Mark state stale, cancel/suspend risk safely, and use bounded REST reconciliation.
- Reward budgets are keyed by unique pool identity. Multiple contracts sharing a pool never multiply its advertised budget.

## CPU and memory

- Use one `asyncio` event loop and event-driven dirty-symbol scheduling.
- Maintain local L2 books from one initial snapshot plus differential updates. A sequence gap invalidates the book and requires resynchronization.
- Coalesce superseded book work; do not enqueue an unbounded calculation per tick.
- Never scan the full universe or serialize/write files on every market event.
- Batch telemetry/storage writes, bound all queues and retained history, and expose dropped/coalesced counters.
- Use `Decimal` only at risk/order boundaries and stable snapshots. Parsers may normalize once; avoid repeated conversions in hot loops.
- Profile before adding concurrency. Threads/processes require measured benefit and deterministic ownership.

## Evidence

- Before accepting any runtime or policy change, state whether it can reduce qualifying snapshot uptime, rotate a sticky pool, or create an unquoted interval against Gemini's 50% rule. Uptime never overrides a safety or reconciliation invariant.
- “Eligible” means the locally known published requirements pass; it is not proof Gemini awarded a snapshot.
- Ranking v2's `priority_score` is a conservative net-profit-per-capital-hour proxy, not expected or guaranteed dollars. Its component reward/share/cost values must retain `_proxy` labels because Gemini does not publish the exact quadratic spread curve or final competitor scores.
- Public qualifying-maker count is a coarse competition signal, not a pool-level unique-account denominator.
- Paper trade touches are possible fills, not actual fills.
- Missing profitability telemetry is never treated as zero risk: reserve one potential fill through the 30-minute cold start, assume zero maker rebate if the rate endpoint is unavailable, and ignore duplicate/non-monotonic public trade IDs.
- Daily reward summaries, order/fill events, settlement, fees, and account equity are authoritative only when authenticated and reconciled.

## Testing and release

- Every mutation, rate-limit, sequence, ownership, stale-state, position, deadline, and pool-accounting rule needs deterministic offline tests.
- Network smoke tests are read-only until an owner-approved commissioning step explicitly authorizes a bounded mutation.
- A passing test suite is necessary but not sufficient for activation.
- Production launches hidden on Windows, has one owner lock, writes atomic status, and recovers without duplicating order intent.
