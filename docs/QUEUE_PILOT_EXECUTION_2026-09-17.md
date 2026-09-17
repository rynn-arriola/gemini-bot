# Queue pilot execution integration, September 17

## Implemented path

The production orchestrator now supports an explicitly configured two-existing-
market best-bid pilot. This is no longer only an offline recommendation function.
The checked-in version-2 policy leaves the pilot unconfigured. Version 3 accepts
an exact immutable `queue_pilot.markets` list of two symbols, each with separate
YES/NO depth, warning-ahead, minimum-refresh-gain, maximum-60-second-selling,
cooldown, calibration-expiry and evidence-reference values. Policy/build hashes
bind it to fresh authority. Activation checks v7 and both exact incumbent owners;
no dynamic symbol expansion or automatic threshold selection exists.

- Ranking, profitability scoring, current targets, actual-price retention and
  final maker-send checks use zero offset only for the two configured symbols.
  Other markets retain two ticks and their existing admission/retention rules.
- Live queue anchors start at the final paced send check with the exact client
  identity, side, price and public/private generations. They are not the shadow
  observer's hypothetical joins. Positive external depth never replenishes an
  old anchor's estimated ahead quantity; price changes never transplant it.
- Both outcome sides must pass fresh stream/account evidence, a complete observed
  selling window and unexpired explicit calibration. Queue evidence failures
  withdraw/wait only on the affected pilot symbol; unknown accounting/mutation,
  unowned orders, integrity and HARD remain globally blocking.
- Actual aggregated depth subtracts our working quantity; public/private size
  disagreement invalidates the evidence. YES/NO trade direction and complemented
  prices are explicit. Duplicate executions cannot double-deplete. Uncertain or
  overflowing flow needs a new complete window. Memory is bounded to two pilot
  markets and 4,096 rolling trades each.
- Depth and trades wake the existing coalesced adverse guard. The independent
  250-ms safety clock also wakes pilot checks for evidence/calibration expiry.
  There is no new connection, transport owner or rate authority.
- Cancel/requeue uses existing shared per-order cancellation tasks. Unknown or
  failed cancellation remains latched. For pilot REST terminal fallback, exact
  identity and cumulative fill quantities must match before terminal adoption.
  A partial/full fill belongs to normal exact inventory/exit handling, never a
  zero-fill requeue. Original deadlines and realized loss history are unchanged.
- Every cancellation, spontaneous terminal update or fill invalidates entry
  proof. Re-entry needs a later applied depth sequence, cooldown after terminal
  confirmation, no working/unresolved intents, fresh classified REST/WS flat
  proof, unchanged fill/public/private generations, and fresh pricing/economics.
  Read-only proof runs outside the lifecycle lock, at most once a minute per
  pilot market, under the shared rate governor; it expires after three seconds.
  The final post-pacing send rechecks proof and queue conditions for both legs.
- The existing first-leg identity/generation guard, MOC treatment, no-buyer exit
  waits, exact position sizing, exposure/capacity and HARD guards remain active.
  Filled lifecycles can re-enter only after the existing exact-flat cleanup.
- Reporting distinguishes `queue_shadow` (virtual, reporting-only) and
  `queue_pilot` (configured live decision state). Shadow failure cannot stale the
  live working-order cache. Quiet synchronized books may seed a missing observer
  on the reporting cadence; no history or warmup is backfilled.

These are queue *scenarios*, not positive lower bounds on protection. Public L2
can hide cancel/add churn. Real fills and cancel races remain possible, and
quotes must be acceptable to execute. Official [stream documentation](https://developer.gemini.com/trading/websocket/streams)
and [reward rules](https://developer.gemini.com/prediction-markets/liquidity-rewards-program)
were rechecked September 17; no published exact reward curve or queue IDs were
inferred. The pilot does not promise a higher net payout.

## Separate live incident repaired

V17 stopped flat at 19:09:20 ET after private 1012 server-shutdown disconnects at
19:06 and 19:07 and concurrent public-shard cancellation proof timeouts. Worker
failure set the gate to HALTED between main-loop iterations, so the loop exited
without retrieving that exception and recorded a normal `closed` result.
The release explicitly checks completed background workers before normal drain/
close classification. A known transient error may reach the existing bounded
same-supervisor restart only after complete terminal/flat reconciliation; unknown
or permanent errors and HARD gain no retry. No attempted cancel is replayed.
This corrects error propagation, not the venue shutdown or every maintenance race.

## Verification and rollout

New offline tests exercise real pair placement, transport admission, reducer,
journal and final-send checks with fake transport only. They cover size-15 best-
bid MOC joins, 7-of-15 fill before the second send, depth/proof changes while
pacing, spontaneous terminal updates, selective depletion, scoped withdrawal,
unknown-cancel no-replay, terminal cancellation through fresh depth/flat proof
to distinct replacement orders, per-symbol ranking and strict policy parsing.
The actual activation-loop regression reproduces worker HALT between iterations
and verifies `recoverable_flat`, never a false normal close, after fake flat drain.

Live calibration remains a prerequisite, not a code flag to bypass: collect
natural depth, outcome-side selling and successful/failed cancellation timings;
review missing coverage and choose two current owners plus expiring thresholds.
One warmup minute or the synthetic test values are not sufficient evidence.
Deploy measurement under the unchanged two-tick policy first, preserving sticky
identities. Do not claim best-bid live acceptance until the configured pilot is
actually observed on Gemini. No induced fill/fault, cohort reset or automatic
expansion is authorized.

## Snapshot and risk impact

Deployment/requeue creates real unquoted gaps with no snapshot credit. The
per-symbol proof pacing and cooldown can deliberately leave pilot slots unquoted;
measure this cost against actual rewards and attributed trading losses/fees.
The 50% local estimate remains reporting-only, not a trading admission gate.
Size 15, 40 pending/20 owner/32 watched limits, $300 gross/$90 market/$40 stressed
entry, five-minute exits and the single $30 six-hour HARD remain unchanged.
