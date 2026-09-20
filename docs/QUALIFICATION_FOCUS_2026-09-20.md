# Local 50% estimator and incumbent funding priority — prepared

The owner requested a 50% estimator and more focus on markets that already
accumulated resting time. This source is prepared in an isolated checkout; it
has not been deployed to the live v34 runner. No live orders or limits changed.

Gemini's daily summary supplies per-event `snapshot_count` and
`total_snapshots`. The report now shows the latest available counts, the
integer 50% threshold on those counts, and a separate local Eastern-day
scenario. It explicitly marks the exchange scoring-window alignment,
scored-through timestamp and final current qualification as unknown. Neither
the estimator nor a local reachability calculation grants order permission.
The venue references are the [liquidity-rewards rules](https://developer.gemini.com/prediction-markets/liquidity-rewards-program)
and [daily-summary API](https://developer.gemini.com/rest-api/prediction-markets/rewards/get-liquidity-rewards-daily-summary).

The continuous-service maintenance loop keeps the same selected owner set and
all existing economics, freshness, risk, HARD and lifecycle gates. Within that
set, already-working entry markets receive first attention; incumbent symbols
come before newly selected symbols; among equal incumbents, more locally
observed qualifying event-minutes are served first. Original model order
breaks ties. This only affects which otherwise-safe owner gets limited capital
first; it never releases a sticky owner, cancels an order, creates more slots,
changes price/size or counts downtime as resting.

Focused reporting/priority tests pass. A deliberate reversed local-minute
sort failed the new regression and was restored. The complete isolated
Windows suite passed 1,066 tests, compilation and offline release validation.
The running bot still uses the prior build; activation requires a separate
controlled terminal/flat deployment with fresh exact-build authority.
