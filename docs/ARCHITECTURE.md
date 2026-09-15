# Production architecture

September 15 implementation update: [repair evidence](REPAIR_REPORT_2026-09-15.md) is authoritative where older design descriptions below differ. The runtime now uses host-wide canonical ownership, serialized order lifecycles, event-driven risk marks, full 1 Hz account snapshots (no mixed delta transition), account/build/policy-bound permits, and fail-closed recovery. Version-5 approval allows at most two same-supervisor restarts only after the prior runner durably proves flat; all other restarts require fresh approval. Maker commissioning uses the production safety path.

## Data and control flow

```text
Gemini public WebSocket ----> sequence-aware local books/trades -> bounded telemetry
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
3. applies a labeled quadratic distance proxy and a conservative competitor prior from the coarse qualifying-maker count;
4. estimates reward share, subtracts maker-entry/taker-exit fees, applicable maker rebate, adverse movement and haircut-adjusted exit-depth costs;
5. normalizes conservative net by two-leg reservation-hours and keeps only the highest-ranked contract for each unique pool;
6. retains a healthy selected cohort so noisy short-term telemetry does not destroy accumulated uptime.

The priority score is a net-profit-per-capital-hour proxy. Its reward, share, fill, cost and net components remain explicitly labeled proxies. Actual reward dollars require authenticated daily scores and the venue's total qualifying score, which is not public. Details and limitations are in [Profitability ranking v2](PROFITABILITY_RANKING_V2.md).

Local one-minute uptime samples persist by event ticker plus shared pool ID. `pool_id` deduplicates the advertised budget; event identity prevents one recurring instance from inheriting another instance's timing. The usable window is the intersection of the Eastern payout day, contract `effectiveDate`, published reward end, and the five-minute pre-expiry safety boundary. When the reward feed omits its activation timestamp, the conservative bound begins at the later of Eastern midnight or contract effective time; first observation is reported separately and never weakens admission. Zone-aware boundaries preserve DST days. A sample is locally qualifying when authenticated account state shows at least one working qualifying BUY outcome order of 10 or more contracts; two sides receive the separate multiplier. This is operational evidence and a feasibility input, not venue-certified scoring. The following authenticated daily summary's `total_snapshots` is the venue-authoritative denominator used to calibrate the proxy.

Sports events are not quoted within 20 minutes of play. Their published event `startTime` minus 20 minutes is both an entry cutoff and a hard position deadline, even when Gemini continues to report the contract active/open. Crypto interval markets are not removed merely because `isLive` is true; their contract effective/expiry timestamps define their intended trading interval and retain the five-minute pre-expiry cutoff.

## Order and position ownership

Pending orders have a hard cap of 30. Positions do not falsely increment the exchange pending-order count because a fill replaces an order with a different risk object. However, every open-position pool reserves one of the 15 active two-sided pool allocations (two planned quote slots), so the selector cannot replace it with a sixteenth market before the position is flattened. Positions also remain subject to the approved $300 gross and $90 per-market exposure gates, settlement state, and time exits. Up to 32 market owners may be tracked, but at most 15 can be fully two-sided simultaneously.

Each order has a durable client ID and intent state. A send timeout is ambiguous; no blind retry is allowed. Startup/reconnect reconstructs open orders, positions, balances, and unresolved intents before activation.

Maker sends recheck current quote geometry after transport pacing. The placement/exit owner remains serialized, while urgent depth-driven cancels use bounded per-symbol workers and deduplicated per-order cancellation tasks. A slow acknowledgement or confirmation in one market cannot prevent dispatching a cancellation for another. The private send queue gives cancels the next admitted slot; venue cooldowns still apply. Replacement remains contingent on terminal proof and a fresh target; end-to-end latency is not guaranteed.

Public books subscribe at `@depth@100ms`. Both WebSocket connections must establish `conninfo` limits before subscriptions. One shared governor accounts conservatively for reported weight/order windows and connection attempts; actual response counts can tighten it. Cancellation waits up to 350 ms for a dedicated terminal event before reading REST. This event is separate from placement acknowledgement waiters. Coalesced per-symbol work, bounded workers and sleeping quota waits keep the faster feed from adding busy polling. See `API_LIMITS.md` for weight uncertainty and headroom.

Startup and drain look up only unresolved intents missing from active orders, using symbol-filtered history. Manual-flat audits with unmatched entry basis leave the affected six-hour realized-P&L window incomplete until authentic closing evidence is reconciled; flat inventory alone cannot prove profit or loss.

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

The approved maximum inventory age is 300 seconds. Once due:

1. cancel orders that can add in the filled direction;
2. positively confirm their terminal state;
3. require fresh account and book evidence;
4. price the exact position against haircut-adjusted depth;
5. submit the exact reducing exit without applying a second monetary stop or loss cap;
6. retain the 300-second pre-expiry deadline as a separate time trigger so a position is not accidentally carried through settlement.

The deadline can only tighten through restart. Unknown account/book state halts new risk and triggers operator-visible recovery; it never licenses an approximate exit quantity.

## Six-hour loss windows

Independent risk windows begin at 00:00, 06:00, 12:00, and 18:00 America/New_York and use civil-time boundaries across daylight-saving transitions. The risk ledger replays durable bot fills, includes their recorded fees, and realizes P&L only when a matched position lifecycle returns flat. Operator-confirmed manual flattening resets unmatched local basis without fabricating a close. Open inventory and executable marks do not enter this meter. Pending-entry capacity remains fixed at 30 below $30 realized loss and becomes zero at HARD; the reached high-water persists for the window. This $30 threshold is the single monetary stop-loss authority; there is no daily, session, market, or position loss stop.

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
