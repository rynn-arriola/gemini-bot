# Gemini competition-aware strategy v3

## Scope and deployment boundary

Owner approved the full reviewed strategy September 16. Source is prepared in
`feature/competition-ranking`, separate from the running checkout. No live
mutation, activation, permit change, stop or restart is part of this release.
Deployment needs fresh owner authorization and exact-build account preparation.
Polymarket remains reference-only.

## Selection

- Watch at most 32 contracts. Restore exact incumbents first, inspect alternatives
  in up to three events per pool (more only for pinned events), and never exceed
  64 event reads. Keep a bounded multi-contract frontier instead of discarding
  the event because its first contract has an invalid two-tick quote.
- Initial public catalog screening may use budget/maker count as a search
  heuristic; it is not a dollar forecast. Final fresh-book ranking uses weighted
  competing size, midpoint distance, estimated execution costs and capital-hours.
  Maker count is only a tie-breaker; unknown/zero count never means no competition.
- Subtract our working remaining quantities from a copy of each synchronized L2
  book, preserving public state. A different shard cannot freshen the book.
- Weight external depth within 10 cents using the existing quadratic proxy.
  Never apply a single maker's 250-contract cap to an aggregate price level.
  Pair-average scaling assumes the 1.5 multiplier for pressure comparison; it
  does not identify actual two-sided competitors.
- Sum watched pressure once per pool. Add one fixed score reserve of 30
  (20 x 1.5) for each known unobserved event/contract and one uncertainty reserve.
  Missing depth receives the reserve, not zero competition. This explicit prior
  is uncalibrated and independent of our selected order size or maker headcount.
- Own score uses capped size, quote quality and the 75% target. Short shared
  recurring windows scale by their estimated fraction of a day. Local window
  estimates remain uncertain; a 50% reachability failure is reporting-only.
- Incumbent selection economics use actual safe resting prices, while any new
  replacement still uses the approved current two-tick target. Failed unused
  reserves can be refreshed without rotating active healthy owners.

For pool budget B, external-pressure scenario C and combined own score S:

`pool reward proxy = B * S / (C + S)`

Adding a candidate of score s earns the marginal difference between S+s and S,
not another copy of B. Compare marginal reward less execution costs per reserved
capital-hour. Charge each market's costs without assuming independent fills or
offsetting inventory; the unchanged account/gross/market admission checks remain.
Do not add a member if it would make a retained member's marginal economics
non-positive. Retain healthy incumbents before filling vacancies; maker-count
changes alone do not cancel them. Expiry, invalid metadata, unsafe quotes and
failed economics still take precedence over resting time.

The $1 minimum applies to the account's daily total. Removing the per-market
floor does not remove positive marginal economics, depth, time, exposure, account
ownership, fill, cancellation or HARD checks.

## Ownership, uptime and recovery

An owner key combines pool ID with exact symbol; the reward budget remains keyed
only by pool ID. Version-2 sticky state stores multiple same-pool owners. Legacy
state is migrated in memory without overwriting its historical record. Duplicate,
malformed or mismatched identity fails closed. Inventory/shard holds reserve
individual owners; the 15-owner ceiling still includes held markets, while held
inventory itself counts zero pending orders. A working SELL still is an order.

Event-local uptime samples at most once per minute and observes any qualifying
member, rather than whichever contract happened to come first. Mixed contract
windows use an explicitly labeled event envelope proxy. Per-symbol continuity
remains separate; neither representation is proof of exchange snapshots.
Known fill/exit/shard recovery stays scoped; unknown execution/account integrity
and HARD remain global. There is no automatic 5pm/midnight rotation or new stop.

## Fees and result reporting

Estimates use zero maker fees in the unambiguous interior of the published
promotion (September 3 through before December 31 UTC), retaining the standard
rate on boundary days because an exact promotion timezone is not specified.
Taker exits remain charged; missing time defaults to standard fees. Revalidate
the promotion before deployment because Gemini may amend it. Real fees and the
loss meter still come exclusively from existing authenticated reconciliation.

Rank reports distinguish standalone, non-additive candidate forecasts from a
selected-portfolio forecast with one budget per pool. Four-times/quarter-pressure
sensitivity is a scenario range, not a confidence interval or mathematical bound.

Authenticated reward reports retain BELOW_THRESHOLD, PENDING and PAID separately.
Paid amounts require both PAID status and a paid timestamp. Account totals are
not replaced by sums of independently rounded event amounts. Event rewards are
not split among our contracts, and normalized score is not labeled exact pool
share. Up to 14 reported dates persist in the canonical journal; malformed
reporting history is unavailable, never fabricated account/risk state.

No combined reward/trading net is claimed until score-window and cost attribution
are proven. Existing realized trading P&L, fees and inventory safety remain
independent. Several complete post-deployment reports are needed to calibrate the
prior, curve, shared allocation and profitability. No live earnings guarantee.

## Continuity impact and limitations

Source preparation introduces no unquoted interval. Deployment will incur a
controlled reconciliation gap. Revised economics may reject an incumbent that
the old headcount model accepted, so compare fresh read-only rankings before
activation. Alternatives are bounded, not an exhaustive all-market optimizer.
Aggregate books cannot reveal competitor identity, exact caps, historical
presence or future fills. Own/public-feed timing skew remains possible. Journal
backup/root-cause work and long-run recovery evidence remain separate pending
work; this release does not claim to repair every crash or guarantee rewards.

## Verification

611 production tests + 28 script tests = 639 passed. The 33 new strategy cases
passed again after final sticky-state persistence review. Compilation, all
PowerShell parse checks, whitespace checks and offline native-Windows policy/
storage validation passed. No authenticated account call or live operation was
performed for this source release.

The new offline suite covers headcount versus weighted depth, missing evidence,
own-size independence, uncapped aggregate levels, observed/unseen pool scope,
sub-$1 contributions, decreasing marginal rewards, incumbent protection, separate
held owners, 15-owner capacity, exact own-order subtraction, shard freshness,
actual resting-price economics, alternative discovery, bounded reads, fee
promotion expiry, v1-to-v2 owner migration, multi-contract uptime and reward
result persistence/validation. Existing execution, full-width 20-contract,
no-buyer, settlement, unknown-mutation, HARD and native supervisor tests remain.

Two deliberate in-memory reversals (repeating a pool's standalone reward for
every member, and ignoring the maker-count tie-breaker) were detected by their
regressions. No live fault or fill was induced.

A synthetic 32-market / 98-level-per-book rank-and-select benchmark over 100
iterations measured median 6.159 ms and p95 9.468 ms on this host, with no network
calls. This is a local microbenchmark, not live CPU or sustained latency proof.
