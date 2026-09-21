# Local 50% estimator and incumbent funding priority — v36 deployed

The owner requested a 50% estimator and more focus on markets that already
accumulated resting time. The prepared source was integrated with the v35
entry-quality code in an isolated checkout, then deployed as v36 after a
controlled terminal-flat handoff and new exact-build permit.

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

## Controlled deployment and initial acceptance

The owner explicitly chose deployment with a controlled restart. V35 reached
`stopped_flat` at 20:02:50 ET with zero orders/positions and both old processes
exited. GitHub and main matched the tested integration commit `aa220cd`.
Fresh read-only preparation proved zero orders/positions, a verified journal
backup, and 16/16 otherwise-actionable full-depth entry-quality passes. A
new policy-v8/account/build-bound v7 permit had no uptime override and retained
the 20-owner ceiling. The persistent task began v36 quoting; first NEW was
20:06:57 ET. The four-minute stop gap is not reward uptime.

At 20:08 ET authenticated REST found 20 exactly journal-owned size-30 BUY
orders across ten markets, no unknown orders and zero positions. All five
public shards were connected/unheld, with no entry/stream holds or reward
pause, zero supervisor restarts, no warning/error/critical log entries and
empty stderr. All 29 ranked candidates carried estimator fields; the current
gate report showed 16/16 otherwise-actionable passes. This is a point-in-time
acceptance, not proof of future venue qualification, reward or continuous
resting.
