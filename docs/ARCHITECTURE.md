# Production architecture

September 15 implementation update: [repair evidence](REPAIR_REPORT_2026-09-15.md) is authoritative where older design descriptions below differ. The runtime now uses host-wide canonical ownership, serialized order lifecycles, event-driven risk marks, full 1 Hz account snapshots (no mixed delta transition), exact-symbol/account/build-bound version-2 permits, and fail-closed recovery requiring fresh reconciliation/approval. Maker commissioning uses the production safety path. No automatic live restart is installed.

## Data and control flow

```text
Gemini public WebSocket ----> sequence-aware local books ----> dirty-symbol queue
                                                               |
Gemini reward/event REST ---> pool-deduped catalog ------------+--> reward/risk ranker
                                                               |
Gemini private WebSocket ---> account/order reducer ----------> desired-state engine
Gemini private REST --------> startup/recovery brackets -------+        |
                                                                        v
                                                               intent ledger
                                                                        |
                                                               private WebSocket orders
```

All outbound work passes through one priority admission governor. Cancellation and position safety outrank reconciliation, book resync, reward metadata, and reporting.

`ProductionOrchestrator` is the sole mutation owner. Object construction is inert. `prepare()` performs discovery, REST brackets and WebSocket synchronization only, ending at `ready_paused`. `activate()` is unreachable until the engine is ready and additionally validates the exact live acknowledgement plus an unexpired, unconsumed permit whose SHA-256 binds it to the reviewed policy. The permit is consumed before the first possible mutation.

## WebSocket-first boundaries

- Public socket: differential depth with an initial snapshot, book ticker/trade/status only where they add evidence. Sequence gaps discard the affected local book and trigger bounded resubscription.
- Authenticated socket: startup uses `orders@account` plus complete one-second balance/position snapshots; after reconciliation it switches to event-driven `balances@account` and `positions@account` deltas for lower CPU/network load. The same socket owns `order.place` and `order.cancel` and uses `cancelOnDisconnect=true`.
- REST: active reward pools, event/contract discovery, program config, terms, initial active orders/positions/balances, daily reward summaries, and bounded recovery proof.
- A stale or disconnected stream removes permission to quote. REST may prove state during recovery; it does not become an uncontrolled substitute streaming loop.

## Reward ranking at 10 contracts

Each candidate constructs two maker-only BUY legs: YES and NO, 10 contracts each. Prices begin two ticks behind the corresponding touch. The ranker then:

1. rejects inactive pools/contracts, post-only-mode exclusions, stale books, off-increment quantities, invalid prices, quotes over 10 cents from midpoint, and impossible 50% uptime;
2. caps counted size at 250 and applies the published 1.5 two-sided factor;
3. applies a labeled quadratic distance proxy solely to order candidates;
4. discounts by coarse qualifying-maker count, observed fill hazard, and exit risk;
5. keeps only the highest-ranked contract for each unique pool in the first allocation pass.

The priority score is unitless. Actual reward dollars require authenticated daily scores and the venue's total qualifying score, which is not public.

Local one-minute uptime samples persist by unique reward pool. When the public reward row lacks a score-day start, the report uses a clearly named UTC-day proxy bounded by the advertised reward end. A sample is locally qualifying only when authenticated account state shows working BUY YES and BUY NO orders of at least 10 contracts. This is operational evidence and a feasibility input, not venue-certified scoring. `rank-report.json` exposes observed and best-possible proxy ratios without converting them to promised dollars.

## Order and position ownership

Pending orders have a hard cap of 30. Positions do not count as pending orders because a fill replaces order reservation with a different risk object. This does not make positions free: the approved $300 gross and $90 per-market exposure gates, settlement state, and time exits remain independent controls. Up to 32 market owners may be tracked, but at most 15 can be fully two-sided simultaneously.

Each order has a durable client ID and intent state. A send timeout is ambiguous; no blind retry is allowed. Startup/reconnect reconstructs open orders, positions, balances, and unresolved intents before activation.

### Verified entry-to-close lifecycle

1. Create and durably record two 10-contract BUY intents only after risk, reward, market, book, and capacity gates pass.
2. Submit each as `LIMIT` + `MOC`; an accepted `NEW` is not treated as a fill.
3. Reduce order events by server order ID and deduplicate executions by trade ID. On `FILLED`/`PARTIALLY_FILLED`, `Z` is the last execution; on terminal events it is cumulative. The reducer handles those meanings separately.
4. On any entry execution, prioritize cancellation of every still-working BUY order for that symbol. Do not declare cancellation from a request acknowledgment; wait for terminal order evidence.
5. Use the authenticated signed position report and REST reconciliation to determine the exact remaining position. Positive is YES and negative is NO.
6. Price an exact SELL outcome reducer against synchronized full depth. YES exits consume YES bids; NO exits consume the complementary prices represented by YES asks. Apply the configured depth haircut.
7. If haircut-adjusted depth is insufficient, do not truncate the position or claim it is closed. Continue the passive/safety path and retry only from fresh evidence.
8. Submit the exact reducer as `LIMIT` + `IOC`. A terminal `CANCELED` does not mean zero fill; use its executed quantity and then reconcile the remaining authenticated position.
9. Flat is proven only by a fresh zero/absent position snapshot plus terminal owned orders. Settlement is confirmed separately.

## Time-aware inventory exit

The approved maximum inventory age is 900 seconds. Once due:

1. cancel orders that can add in the filled direction;
2. positively confirm their terminal state;
3. require fresh account and book evidence;
4. price the exact position against haircut-adjusted depth;
5. submit the exact reducing exit without applying a second monetary stop or loss cap;
6. retain the 300-second pre-expiry deadline as a separate time trigger so a position is not accidentally carried through settlement.

The deadline can only tighten through restart. Unknown account/book state halts new risk and triggers operator-visible recovery; it never licenses an approximate exit quantity.

## Six-hour loss windows

Independent risk windows begin at 00:00, 06:00, 12:00, and 18:00 America/New_York and use civil-time boundaries across daylight-saving transitions. Reconciled adjusted equity supplies the opening/current/marked values. At $5, $10, $15, $20, and $25 of loss, the maximum pending-order cap ratchets from 30 to 25, 20, 15, 10, and 0. Open marks can provisionally tighten or pause entry but cannot erase realized loss. The $25 window threshold is the single monetary stop-loss authority; there is no daily, session, market, or position loss stop.

## Operational logging

Production logging is JSON Lines through a bounded non-blocking queue so a slow disk cannot stall book/order handling. Files rotate at 20 MiB with five backups under ignored `data/production/logs/`. Every error carries an error class, event name, UTC timestamp, process/thread ID, safe identifiers, retry/cooldown context and exception trace. Critical events are also written to the SQLite audit journal. API keys, secrets, auth headers, payloads, signatures, nonces and registered secret values are recursively redacted. Queue drops are counted and surfaced as health degradation.

## Windows production shape

- Python virtual environment at `.venv`.
- One hidden process launched and supervised by PowerShell/Windows Task Scheduler after commissioning.
- Atomic JSON status and SQLite journal under an ignored `data/production/` directory.
- Exclusive Windows-compatible owner lock prevents two mutation processes.
- `STOP` requests drain: block new risk, cancel resting entry orders, reconcile, reduce positions under policy, then exit.
- `scripts/start_production.ps1` launches a hidden native process; Prepare and Live are distinct modes. Live requires a permit path. `scripts/stop_production.ps1` only requests graceful drain.
- No WSL, systemd, Bash, cron, or POSIX-only locking.
