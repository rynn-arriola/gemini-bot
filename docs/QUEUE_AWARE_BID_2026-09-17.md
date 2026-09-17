# Queue-aware best-bid strategy

## Completion follow-up

The execution integration described as missing below has now been implemented
and tested in `queue_pilot.py` and the production orchestrator. The first-release
sections below are historical. See [live integration and rollout status](QUEUE_PILOT_EXECUTION_2026-09-17.md).
The checked-in policy still uses two ticks: real calibration and exact pilot
selection must precede activation; synthetic test thresholds are not live values.

## Release boundary

The owner approved a staged recommendation on September 17: measure first, then
assess a two-existing-market best-bid pilot. This branch implements the observer,
bounded history and offline decision/proof model. **It does not implement or
enable live best-bid execution.** The production policy file is unchanged.
`guarded_bid.py` has no transport, journal or production execution caller.

Source lives in the isolated `feature/queue-aware-bid` worktree. V17's checkout,
running orders, source digest, permit, monetary controls and selection are not
modified. Deployment of even this observer requires a fresh build-bound
release; no hot loading or old-permit reuse. The current turn does not restart
the service. Collection in the real runner starts only after deployment.

## Why uncertainty is essential

Gemini's [rulebook, Rule 5.9](https://www.gemini.com/static/titan/gemini_titan_rulebook.pdf)
uses price then time priority. A replacement at the same price follows orders
that remain there, but aggregate depth cannot tell how much was behind us.
The [stream specification](https://developer.gemini.com/trading/websocket/streams)
publishes price-level quantities, batches differential updates at 100 ms, and
provides a separate trade tape. It does not supply public per-order queue IDs.
These sources were rechecked September 17, 2026.

Equal size before and after a batch may hide cancellations ahead and additions
behind. There is therefore **no positive guaranteed quantity ahead** inferred
from these feeds. A large incoming trade or a cancellation race can fill us.
Quotes in any future pilot must be genuine executable orders acceptable to fill,
not a promise of collecting rewards without execution.

## Implemented observer

- Uses existing production depth/trade streams and their per-symbol public plus
  private generations. No new socket, subscription or REST polling.
- Tracks virtual best-bid joins independently for YES and NO. NO uses complemented
  YES-space asks and the correct trade aggressor direction.
- Own working orders are subtracted from copied public levels. Missing own size
  is treated as publication uncertainty, not as external protection. Public book
  data is never changed by the observer.
- Positive depth changes do not increase the scenario's quantity ahead. Decreases
  reduce it; trade-before-depth overlap is not debited twice. Delayed trades can
  make the scenario pessimistic. All outputs explicitly remain scenarios, not
  exact positions, bounds or actual/simulated fills.
- A possible touch remains marked until a new virtual join; a larger depth
  update cannot magically refill a depleted position ahead. Price changes start
  a new *hypothetical* join, without claiming real cancellation/placement latency.
- Drops queue history across own order transitions, stale evidence, stream holds,
  public/private generation changes, resynchronization, clock reversal and
  unwatching. An initial/recovered window needs new observations; no downtime or
  restart earns credit.
- Tracks 60-second outcome-side selling totals, warmup, uncertain timestamps and
  truncated flow. Missing/old/future trade timestamps cannot become proof of a
  current queue. Duplicate/nonmonotonic trade IDs are ignored.
- Measures local cancellation-call-to-terminal-confirmation time once per shared
  cancel task, including admission/proof delay. This is not exchange network
  latency. Failed/interrupted calls are reported separately, never converted to
  successful samples or permission to retry.
- Observer exceptions disable only the observer and expose a redacted error
  class. They cannot halt trading, clear an entry hold, retry an order or alter
  risk/exit state.

`rank-report.json.queue_shadow` is explicitly reporting-only and marks
`live_pilot_ready=false`. `queue-shadow.jsonl` archives at most once per observed
minute. It includes queue scenarios, observed working prices/remaining sizes,
cancel diagnostics and allowlisted reward-model fields (including pool budgets).
It contains no credentials, order IDs, account balances or raw account payloads.
Missing minutes remain missing, not backfilled. Collection is not a proven
reward-window backtest, and aggregate flow does not establish actual queue fill
probabilities.

Memory is bounded to 32 markets, 256 recent trades per market and 256 successful
cancel samples; the active-order cache is capped at 40. Hot-path membership is a
set lookup and only the affected book is copied. Disk history rotates at 16 MiB
with three backups (about 64 MiB plus at most one record); retention is byte-based,
not a promise of a certain number of complete days. Writes occur on the existing
report cadence, once per minute, not per depth tick. Archive I/O failure is
reported separately without changing trading behavior.

## Implemented offline decision model

The planner requires exactly two canonical pilot symbols, size 15 and explicit,
positive, expiring calibration values with an evidence reference. There are no
live threshold defaults. Numbers in tests are synthetic, not recommended live
depth/cooldown/flow values.

In priority order it advises: existing fill/exit handling; reconciliation for an
unknown/pending cancel; withdrawal/wait for missing safety/evidence/calibration;
withdrawal/wait for thin depth or excessive selling; cancellation and fresh
reassessment on a best-price move; cooldown-bound new MOC joining; selective
same-price refresh only with a material estimated gain; otherwise keep a healthy
quote. Cooldowns gate entry/refresh, never mandatory safety withdrawal. Nonpilot
symbols explicitly retain the two-tick policy.

The separate offline replacement predicate requires exact requested versus
zero-fill-cancelled identity sets, ordered/fresh proof times, unchanged public and
private generations, classified flatness, no working/unresolved orders and no
fill since the request. It does not replace existing runtime proofs or confer
mutation permission. Prices and sizes must be rebuilt after cancellation; neither
a proposed replacement nor a successful cancel can authorize stale-price send.

## Verification

45 new deterministic tests cover queue additions/depletion, the hypothetical
100-ahead/500-behind case, hidden churn, delayed/duplicate trades, YES/NO mapping,
possible touches, recovery generations, stale/invalid evidence, own-order skew,
bounded state/rotation/redaction and isolated disk/observer failure. Tests also
exercise the actual public pump and shared cancel task with fake transport,
ensure no extra mutations, and use the real account reducer for 7-of-15 fill then
terminal cancellation. Offline replacement remains blocked after that fill.

Full verification passed 715 production plus 28 script tests (743 total). All 45
new tests passed again after adding observed working-quote fields to the archive.
Compilation and whitespace checks passed. Existing fill/deadline/HARD/shard
regressions remain part of that full run; they do not constitute a live best-bid
fill/requeue acceptance test.

A synthetic 32-market/98-level observer benchmark over 300 iterations measured
median 0.440 ms / p95 1.073 ms per complete 32-market batch on this host. A separate
32-market minute-report/archive microbenchmark measured median 0.527 ms / p95
1.029 ms over 60 writes. These are local measurements, not sustained live CPU or
cancel-latency claims.

## Remaining before a live pilot

1. Deploy the reporting-only release under fresh authority when a controlled
   restart is approved; preserve current sticky identities and economic history.
2. Collect natural depth/trades and successful/failed cancellation timings, with
   explicit missing coverage. Select two existing markets from the evidence;
   don't rotate markets merely to make the pilot convenient.
3. Review symbol/outcome-specific depth, depletion, flow, cooldown and freshness
   thresholds. A one-minute warmup is not sufficient calibration or proof of
   safety. Do not choose a fixed “500 is safe” value or claim to observe the back
   of the queue exactly.
4. Implement and regression-test real pilot execution integration, including
   ranking, actual-resting-price retention, adverse guard, post-pacing final-send
   checks, both-leg generation/identity safety, terminal-fill races, market-local
   recovery, shared pacing and bounded selective requeue. The current pure
   planner is deliberately not wired to those mutation paths.
5. Bind exact pilot configuration to policy/build authority and obtain live
   activation after readiness review. Compare authenticated rewards and separately
   attributed execution losses/fees, actual fills and lost resting time. No
   automatic expansion beyond two markets and no forecast-only profit claim.

## Snapshot/risk impact

This source-only work creates no live quote gap or rotation. Observer deployment
will incur a controlled restart gap; it receives no reward credit. Any later
best-bid pilot may fill more often, and cancel/requeue intervals can lose Gemini
snapshots and two-sided bonus time. Uptime never overrides safety. The 15-contract,
40-order/20-owner, $300 gross/$90 market/$40 stressed-entry, five-minute exit and
$30 six-hour HARD rules remain unchanged. No queue rule can clear those gates.
