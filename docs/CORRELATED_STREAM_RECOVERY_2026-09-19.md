# Correlated stream recovery — September 19, 2026

## Confirmed incident

At 11:19:43 ET all five public market-data shards became stale within 36
milliseconds. The private account snapshots then became stale at 11:19:45 ET.
The private transport reconnected on its first attempt at 11:19:47 ET, but the
public recovery workers had already started cancellation owners for the 26
working entries seen by the preceding heartbeat.

Each cancellation correctly had one mutation owner and never replayed an
attempted request. Its 45-second confirmation clock, however, also counted time
queued behind the one intentionally serialized authenticated REST reader.
Full-width cancellation confirmation performed repeated active-order reads and
two duplicate history reads per market. At 11:20:29 ET later owners expired
while waiting for that REST lock, and their latched timeout propagated through
two shard workers and the ranking cancellation path. The runner failed safely,
restored both transports for drain proof, and proved zero positions/orders at
11:20:52 ET. The supervisor performed one authorized flat recovery and returned
to quoting at 11:22:00 ET.

The user then directed fixing the issue and restarting. The recovered runner
was gracefully stopped at 11:23:10 ET and again proved flat. No live process is
being edited.

## Repair

- Concurrent cancellation owners coalesce an in-flight/fresh active-order read
  for 500 milliseconds. Terminal-history reads are separately coalesced by
  exact symbol, so the two outcome legs do not duplicate the same page.
- Cached evidence cannot terminalize an order by absence. Exact matching
  terminal history remains mandatory; a stale nonterminal view merely causes
  another paced read. Read failures are discarded and retried through the
  existing shared rate authority.
- Only continuous-service cancellation during a correlated outage—an active
  stream hold or more than one held public shard—receives proof headroom. The
  budget is the existing 45 seconds plus three seconds per current working
  entry, capped at 180 seconds. Ordinary single-market/single-shard cancellation
  remains 45 seconds, and explicit shorter recovery proofs remain unchanged.
- The added time is confirmation-only while entries are blocked. It never
  resends an attempted cancellation, retries an entry/exit mutation, clears a
  hold, changes account truth or bypasses HARD. Failure after the finite budget
  remains latched and fatal.
- Shared proof tasks survive cancellation of one waiter and are explicitly
  canceled/joined during runner close, preventing orphaned authenticated reads.

## Snapshot impact

Correlated transport loss still invalidates affected books and cancels unsafe
entries, so that interval cannot earn resting/snapshot credit. The repair avoids
an additional process restart caused only by local proof-reader contention and
therefore can shorten the later recovery gap. Sticky, accounting, reward and
deadline history remain unchanged. It does not guarantee uninterrupted quoting
or reward qualification.

## Verification and deployment state

Focused transport/cancellation/shard suites passed 141 cases. Full verification
passed **1,034 production tests plus 28 script tests (1,062 total)**, Python
compilation, PowerShell parsing, whitespace checks and offline Windows
policy/durable-storage validation. Tests cover coalescing across 40 waiters,
same-symbol history fan-in, waiter cancellation without orphaning, bounded
full-width scaling, unchanged short/ordinary timeouts, ambiguity latching, all
five shards, private recovery and drain behavior.

The bot is stopped flat pending commit publication, fresh authenticated
preparation, exact new authority and the owner-requested restart.
