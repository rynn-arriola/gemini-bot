# Profitability ranking v2

Implemented September 15, 2026. This is the active selector for the fixed owner-approved quote policy: BUY YES 10 and BUY NO 10, each two ticks behind its corresponding touch. It changes market selection, not quote placement, size, breadth, exits, or monetary risk limits.

## Objective

For every unique reward pool, rank the best currently safe contract by a conservative daily net-profit proxy per capital-hour:

```text
own_score_proxy = quadratic_distance_proxy * min(quantity, 250) * 1.5 * target_uptime
competitor_score_prior = max(1, qualifying_maker_count) * min(quantity, 250) * 1.5
estimated_share = own_score_proxy / (own_score_proxy + competitor_score_prior)
estimated_reward = min(pool_budget, pool_budget * estimated_share)
expected_net = estimated_reward - expected_execution_cost
profitability = expected_net / (two_leg_reservation * planned_scoring_hours)
```

`profitability` is the active priority score. The candidate must also estimate at least Gemini's $1 minimum payout and a positive conservative net. These are local proxies, not a reward promise.

## Execution-cost evidence

One public WebSocket supplies both differential depth and `{symbol}@trade` streams. The bounded tracker retains 30 one-minute book buckets and at most 512 trades per symbol. Duplicate or non-monotonic trade IDs are ignored, and the cursor persists across restart.

Potential quote touches project a maximum of 48 fills per scoring day. Each projected fill is charged:

- the published base maker entry fee, minus only the currently active category/default maker rebate; this deliberately retains the base fee as a safety buffer during the temporary fee-waiver promotion;
- a conservative taker exit fee;
- the larger of one tick, observed five-minute midpoint movement, or half the current spread;
- a penalty for missing haircut-adjusted exit depth.

Until 30 observed minutes exist, the estimate reserves up to one additional fill. Missing rebate data means zero assumed rebate. Public trades are possible touches, never authenticated fills.

## Stability and CPU limits

Ranking runs once per second over at most 32 tracked contracts. Frame processing is constant-time apart from bounded deques. Telemetry persists once per minute, not per market event. REST adds one startup read for maker-rebate rules; it never polls prices or trades.

The selected cohort is sticky. A newly higher score does not displace a healthy incumbent and destroy its uptime. A pool is replaced only when it becomes unsafe, non-actionable, time-ineligible, or leaves an open capacity slot. Existing inventory remains locked until the exact exit path proves it flat.

## Known limits

- Gemini does not publish its exact quadratic curve or competitors' scores, so reward share is deliberately a conservative prior.
- `qualifying_maker_count` is coarse event metadata and may not equal unique pool competitors.
- Public trade direction/price provides touch evidence, not proof this account would fill.
- Authenticated fills, fees, rebates, daily reward summaries, and account equity remain authoritative. The model must be calibrated from those results.

Official references: [Liquidity Rewards](https://developer.gemini.com/prediction-markets/liquidity-rewards-program), [Maker Rebates](https://developer.gemini.com/prediction-markets/maker-rebate-program), [Predictions fees](https://www.gemini.com/fees/predictions), and [WebSocket streams](https://developer.gemini.com/prediction-markets/websocket/streams).
