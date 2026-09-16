# Six-connection architecture

Owner approved the recommended design September 16. Source implementation and
offline validation only; no live trading is started by this change.

- Five public market-data shards, one authenticated account/execution connection.
- One event loop, journal, mutation owner and shared outbound rate governor.
  Connections do not multiply order quota. Every socket learns `conninfo`; the
  shared governor retains the strictest limits and never subtracts locally
  reserved usage because another socket reports a smaller count. Reconnects are
  globally paced; server cooldowns apply across sockets.
- Assign symbols deterministically and evenly. Keep existing assignments while
  adding/removing markets; never rebalance healthy owners during a failure.
- Each public shard owns independent book generation, freshness, subscriptions,
  reader, ping and reconnect state. Old-generation frames cannot revive books.
  All public sockets subscribe to contract lifecycle notifications; process a
  tracked symbol's notification only on its assigned shard.
- A typed public transport outage or sequence gap blocks only that shard,
  cancels its risk-adding entries through the one private owner, and rebuilds its
  books. Restore entries only after terminal cancellation, fresh synchronized
  books and unchanged fresh classified account state. Unknown cancellation or
  execution, malformed data and account failures remain global safety failures.
- A private outage still pauses all entries and requires full authenticated
  reconciliation. No socket-count design can make stale account truth safe.
- Known no-liquidity exit waits remain per market with original deadlines and
  reservations. All-market outages may wait; they never earn inferred snapshots.
- Expose per-shard membership, generation, hold, reconnect attempts and freshness
  in local status. Preserve sticky pool reservations through an outage.

Official references checked September 16:
[stream roles](https://developer.gemini.com/trading/websocket/streams),
[conninfo](https://developer.gemini.com/trading/websocket/playground), and
[REST rate limits](https://developer.gemini.com/rate-limit).
These pages do not establish a universal six-connection entitlement or a
sixfold throughput increase. Do not invent a concurrency ceiling or use shards
to evade quotas. Five public shards is an isolation choice, not a measured
throughput requirement for the current maximum 32 tracked markets/15 pools.

Required offline acceptance: one/all shard outages, independent quiet liveness,
old-frame rejection, gaps, private/global failures, reconnect storms, shared
429/cancel priority, fill during quarantine, no-depth exit, second-leg admission,
stable rollover/ownership, shutdown task cleanup, and unchanged snapshot rules.

## Implementation and verification

Implemented in `public_shards.py` and the production orchestrator. Membership is
persisted, new symbols go to the least-populated shard, and healthy assignments
are retained. Catalog rollover waits during a shard recovery; selected held pools
remain reserved. No routine subscription request is sent to unaffected shards.
Per-symbol freshness reaches ranking, quote admission, retention and uptime.

507 production tests plus 28 script tests passed, including the additional
startup-cancel-fill regression and 28 dedicated shard/startup cases. Cases cover
adapter fan-in, generation/membership, shared cooldown/counters, healthy sibling
admission, same-run recovery, repeated EOF, private/shard coordination, unsafe
cancel escalation, and known-inventory startup. The fractional-cash test remains
in the separate execution-recovery module.
Two deliberate in-memory reversals of generation defenses were detected.
Compilation and offline Windows policy/storage validation passed.

Real read-only smoke connected five public sockets and one authenticated private
socket, synchronized five books and received account snapshots. It created zero
order intents and disabled cancel-on-disconnect; no activation or cancellation
occurred. This is connectivity acceptance, not live fault injection, a long-run
performance benchmark or proof of reward eligibility. Imports confirm the local
editable runtime uses this checkout. No new live permit or trading process was
started. Final read-only startup stopped on an unowned active order. Exact cash
and external-order reconciliation remain restart blockers.

50% impact: healthy shards retain their quotes; affected-shard quarantine and
resynchronization create necessary unquoted intervals. Those gaps are uncredited.
No pooled/day uptime transfer, renewed override, or admission/risk relaxation.
