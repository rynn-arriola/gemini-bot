# Local 50% estimator and incumbent funding priority — integration candidate

The owner requested a 50% estimator and more focus on markets that already
accumulated resting time. The prepared source has been integrated with the
live v35 entry-quality code in an isolated checkout. Deployment, if completed,
requires a controlled terminal-flat handoff and new exact-build permit.

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

The integrated release passed 1,080 production tests, 28 script tests,
compilation and offline release validation. Integration also hardened rank
reporting for a candidate lacking event/pool identity: that row gets no local
scenario rather than a runtime exception. A stopped or paused interval is not
local resting time or a Gemini reward snapshot.
