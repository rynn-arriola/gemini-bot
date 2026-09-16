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
