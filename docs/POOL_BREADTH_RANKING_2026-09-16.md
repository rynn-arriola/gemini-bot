# Pool-breadth discovery ranking

The owner approved ranking, not excluding, broad shared reward pools. Source is
verified in isolation. The owner has now explicitly authorized replacement of
the current cohort with a fresh selection once, followed by normal stickiness.

## Method and limits

Discovery v2 orders unique reward pools by daily budget divided by the square root
of sharing breadth. This is a soft research-priority heuristic, not Gemini's score,
an equal event allocation, or a dollar payout forecast. A $250 one-event pool ranks
above a $500/197-event pool at the metadata stage; sufficiently greater budget can
still make a broad pool rank first. There is no event-count or maker-count ban.

Before event reads, breadth is the larger of distinct catalog events and published
pool_event_count. After bounded event reads, known active-contract counts are
counted once per event. Unread events use a rounded-up mean of sampled contract
counts, or one when none is known. This is explicitly an imputation; sampled events
need not represent the whole pool, and missing pool count uses only observed scope.
The heuristic remains uncalibrated and may misrank actual future profitability.
Maker headcount only breaks equal discovery priorities; missing/zero historical
qualification is not interpreted as proof that a pool has no competition.

The final L2 competition, unseen-score prior, marginal pool economics, quote/size,
timing, exposure and HARD gates remain unchanged. Event counts do not multiply
budgets. No live quote price, quantity, payout formula or eligibility rule changes.
Discovery still watches at most 32 contracts with at most 64 event reads; no extra
streams, rate quota or full-pool surveillance is added.

Current implementation preserves exact incumbents first, then ranks reserve
capacity using breadth instead of letting pinned pools dominate the reserve order.
Read order keeps pinned events first for ownership continuity. Revised reserve
coverage can change the existing pool-wide economics estimate; any resulting
concrete failure still uses the existing grace, terminal/flat proof and safe exits.
No automatic sticky reset is added to the source. The separately authorized
operator transition archives and clears the current selection once after flat
proof; runtime retains its normal sticky policy thereafter.

## Continuity and deployment

An authorized deployment requires graceful drain, authenticated account proof,
preserved economic/risk history, verified backup, and fresh build/policy/account
v7 authority. The deployment gap earns no resting-time credit. Healthy incumbents
must not be deliberately rotated without a clear owner decision. The owner has
authorized one transition here; this is not recurring reset authority. Existing
markets can independently requalify in the fresh selection. Old event uptime
remains history and never transfers to new events. Source changes alone are not
live activation.

Official reward rules checked September 16:
https://developer.gemini.com/prediction-markets/liquidity-rewards-program
Gemini distributes by qualifying score, not equal shares per event. The discovery
heuristic does not claim full-day qualification, competitor identities or payout.

## Verification

655 production tests plus 28 script tests passed (683 total). Eleven dedicated
tests cover budget/breadth tradeoffs, broad-pool admission, contract-level scope,
duplicate budget/event deduplication, missing metadata, imputed unseen scope,
headcount tie breaks, deterministic ordering, invalid metadata and actual bounded
watch allocation with incumbent retention. The two allocation regressions both
fail behaviorally against prior source and pass with this source. Compilation,
all PowerShell parse checks, whitespace and offline Windows policy/storage checks
passed. Tests used the isolated source explicitly; no live fault or fill was induced.
No deployment or restart occurred during these tests. The owner's subsequent
slot-selection clarification authorizes the controlled deployment separately.
