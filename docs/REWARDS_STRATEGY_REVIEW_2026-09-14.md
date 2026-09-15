# Gemini Predictions rewards: opportunity and bot design

Reviewed September 14, 2026 (Eastern Time). Scope: all six Predictions incentives/capital programs linked by Gemini's developer site. Crypto staking, card rewards and referral offers are outside this trading-bot review.

## Decision

Test a small maker strategy combining resting-liquidity rewards, eligible maker rebates and the temporary maker-fee waiver. Gemini is not yet proven more profitable than the existing Polymarket US bot. No Gemini strategy, capital allocation or live trading has been activated by this research.

The earlier approximately $196,150/day claim was incorrect: it counted repeated shared-pool budgets once per event. The earlier conclusion that Gemini generally offers a separate pool for each event must also be withdrawn.

## 1. Correct pool accounting

Public API capture: **2026-09-15 02:51:28 UTC / September 14 22:51:28 ET**. Six paginated responses yielded 568 rows, 568 distinct event tickers and a final reported total of 568. The catalog can change during pagination; this is a bounded observation, not an atomic exchange snapshot. `last_score_date` was September 14.

| Pool scope | Distinct pools | Member events | Advertised daily budget |
| --- | ---: | ---: | ---: |
| Shared category pools | 9 | 534 | $2,450 |
| Individual event overrides | 34 | 34 | $5,800 |
| Total | 43 | 568 | $8,250 |

The raw row sum would incorrectly report $198,800. These are program budgets, not our expected earnings or proof every dollar will be distributed.

Evidence: the [live event endpoint](https://api.gemini.com/v1/prediction-markets/liquidity-rewards/events) supplies `pool_id`, `pool_event_count` and `pool_category_name`. Those fields were absent from the OpenAPI text inspected in this review. The [official website bundle](https://assets.gemini.com/builds-2/web/main/Predictions.a020fde4280245ebbc02d7517139.js), loaded by the [rewards page](https://www.gemini.com/predictions/liquidity-rewards), groups category-default entries by pool ID, shows one parent budget and suppresses child budgets. That independently corroborates sharing despite the REST page's older per-event wording.

Accounting rule: group `category_default` rows by numeric `pool_id`; count each `event_override` by event ticker. Do not combine overrides with missing pool IDs into one pool. Conflicting amounts or missing shared-pool identity make a budget unknown. Maker counts are event observations; summing them does not produce unique pool competitors or their scores.

### Shared pools to investigate

| Pool | ID | Daily budget | Member events | Research priority |
| --- | ---: | ---: | ---: | --- |
| Zcash | 62 | $300 | 3 | High budget relative to coverage; fast reference-price risk |
| Commodities | 15 | $500 | 18 | Inspect usable books and settlement indices first |
| Bitcoin | 14 | $250 | 22 | Needs reliable underlying-price tracking |
| Ether | 60 | $100 | 22 | Compare reward after fills and exits |
| Solana | 55 | $50 | 23 | Lower budget; weak default priority |
| XRP | 56 | $50 | 23 | Lower budget; weak default priority |
| Weather | 53 | $500 | 98 | Many siblings and observation-time information risk |
| Midterms | 54 | $500 | 197 | Broad coverage competes for one budget |
| Pro Football | 63 | $200 | 128 | Large family, relatively small shared budget |

Event counts alone do not rank profitability. There were 99 zero-maker event rows, but this does not imply 99 uncontested pools.

Separate event pools include `PROFB2027` ($250; 16 event-level makers), `FED260917` ($250; 26), `DEMNOM2028` ($250; 20), and `CRYPTOSTRUCT26` ($200; 25). One event pool still covers all its contracts.

Full public evidence: [snapshot JSON](history/REWARDS_PUBLIC_SNAPSHOT_2026-09-14.json).

## 2. Programs and conditions

| Program | Verified published terms | Fit |
| --- | --- | --- |
| Liquidity rewards | Qualifying resting quotes earn a proportional pool share | Primary target |
| Maker rebates | Qualifying passive fills earn a percentage of taker fees | Additional income |
| Maker fee waiver | September 2–December 31, 2026; eligible maker fills have zero maker fees; automatic eligibility | Improves current economics |
| Taker rewards | July 1–September 30, 2026; volume tiers and monthly bonuses | Poor initial fit for size-10 making |
| Contracted market maker | Application-based; public page gives no payout schedule | Possible later negotiation |
| Collateral return | Account/group rollout for mutually exclusive outcomes | Capital efficiency, not income |

### Liquidity rewards

Minimum qualifying quantity is 10, scored quantity is capped at 250, qualifying distance is within 10 cents of midpoint, and qualifying two-sided presence earns a 1.5 multiplier. Per-minute snapshots accumulate into daily scores; 50% eligible-snapshot uptime is required. Daily rewards below $1 are suppressed; amounts round down to cents and payment is scheduled for 5:30 p.m. ET. A one-sided book uses that side's best price as reference. Exchange contracts in post-only mode are excluded; that is distinct from submitting our own maker-only order. [Program](https://developer.gemini.com/prediction-markets/liquidity-rewards-program).

The live [config](https://api.gemini.com/v1/prediction-markets/liquidity-rewards/config) returned enabled=true, maximum distance 10 cents and minimum payout $1. It did not expose minimum size, size cap, multiplier or uptime. Version those remaining documented parameters separately.

### Maker rebates and fees

The [rebate program](https://developer.gemini.com/prediction-markets/maker-rebate-program) covers eligible fills from $0.20 through $0.80 on contracts listed on/after March 18, 2026. Formula: rebate multiplier × taker rate × quantity × price × (1-price), subject to the stated notional cap; round down to cents. Published payment is 5 p.m. ET. The [live rate schedule](https://api.gemini.com/v1/prediction-markets/maker-rebate/rates) supports a 0.30 fallback now; higher historical category rules have expired. Select by category and effective date, not array order.

The [fee schedule](https://www.gemini.com/fees/predictions) gives taker coefficient 0.07 and base maker coefficient 0.0175, with fees rounded up. The [temporary waiver](https://developer.gemini.com/prediction-markets/maker-fee-free-trading-promo) overrides eligible maker fees during its term. Check actual fill charges and credited rebates before treating the combined forecast as account-proven income.

Calculated example: a single eligible 10-contract fill at $0.50 gives $0.0525 before rebate rounding, or $0.05. A taker fill at that same size/price costs $0.175 before fee rounding, or $0.18. A ten-cent adverse move on ten contracts costs $1 before fees. Small rebates cannot rescue consistently bad entries; partial-fill rounding also matters.

### Taker rewards

[Published terms](https://developer.gemini.com/prediction-markets/taker-rewards-program): daily rates are 0.5%, 1%, and 1.5% at trailing volume thresholds of $500,000, $2 million, and $10 million. Daily eligibility also requires 15 active trading days. Monthly bonuses range from $2,000 at $500,000 to $50,000 at $10 million. Eligible execution prices are $0.10–$0.90. Program and individual caps are $1.5 million and $250,000. Contracted maker eligibility conflicts with this program; maker rebates may coexist. Only bona fide qualifying executions count.

Recommendation: value any naturally eligible exits later, but do not create turnover to chase these thresholds. A new participant starting September 14 has little time before the stated September 30 end. Remaining program funding and the detailed active-day definition were not verified; no taker payout forecast is warranted.

### Contracted making and collateral

The [market maker program](https://developer.gemini.com/prediction-markets/market-maker-program) requires approval; no guaranteed rate is public. We have not contacted Gemini or established eligibility.

[Collateral return](https://developer.gemini.com/prediction-markets/collateral-return) launched for supported mutually exclusive groups September 11. It requires account/group onboarding, applies to No positions and can reverse when positions change. Resting-order holds still apply. Direct Sell No is unsupported for affected accounts; reducing No uses Buy Yes. Directional strike ladders are not included. Treat this as a later account-specific execution/accounting feature, not free profit or assumed buying power.

## 3. How to adapt our Polymarket approach

The current Polymarket decisions document selects size 10, 32 slots, a six-hour pregame entry horizon, a 900-second inventory deadline and $90 HARD at that size/breadth. These are historical/current-document context, not live process verification or Gemini settings.

The [Polymarket rules](https://docs.polymarket.us/incentives/liquidity) score relative to the best price every second, normalize each eligible side per snapshot, and stop scoring beyond aggregate target depth. Gemini's documented accumulated-score method is different. A Polymarket target is not a personal minimum; Gemini's size cap does not establish that a size-10 account wins against every larger maker.

Carry forward the accounting and execution lessons:

- Track paid liquidity rewards, pending rewards, maker rebates, fill P&L, fees and open-position marks separately. Avoid double-counting a rebate already in cash/commission accounting.
- Rank each candidate by its incremental contribution to the correct pool. Adding another market in our existing pool partly reallocates our own share; multiplying the pool by slot count invents revenue.
- Evaluate executable exit depth and subsequent price movement. The [Polymarket reward sensitivity study](../../liquidity-bot/docs/history/ENTRY_QUALITY_REWARD_TRADEOFF_2026-09-07.md) found $365.73 paid plus $544.16 pending over its matched six days, while coherent completed episodes lost $381.97. These scopes do not establish account net, but show why rewards and losses must be assessed together.
- Preserve exact order/fill ownership, startup/reconnect reconciliation, fresh books, cancel confirmation and priority for reducing exposure.

Change the research model for Gemini:

1. **Uptime first.** Track earned snapshots and remaining possible snapshots against the actual eligibility denominator. If that denominator covers a full 24-hour day, six hours yields only 25%. The precise treatment of late entry, short events and shared pools must be resolved; do not call every market a simple 12-hour requirement.
2. **Compare realistic quote distances.** Test maker-only prices at different distances and sizes in observation. Tightness raises score but can increase adverse selection. No exact quadratic formula was published in the material inspected, so a guessed curve is a sensitivity model only.
3. **Keep the two sides economically justified.** A bonus is useful only if expected extra rewards exceed the added risk. At size 10, even a partial fill can leave less than the qualifying minimum; do not blindly replenish inventory to restore eligibility.
4. **Start measurement narrow.** Observe a handful of contracts across several distinct pools, including selected individual-event pools. Size 10 is a reasonable initial simulation quantity; 5–10 observed contracts is a research scope, not an approved live allocation.
5. **Scale measured net contribution.** Estimate added reward plus eligible rebate and spread P&L, less adverse movement, taker exits and capital cost. Compare results per resting hour and per dollar of reserved capital, retaining total net as the objective.

For an illustrative $250 pool, a verified 1% daily score share gives $2.50 gross. That is arithmetic, not a forecast. Headcount cannot establish the 1% share.

## 4. What the sampled markets actually showed

Five additional public event reads found active events but uneven usability. Zcash September 15 included an open contract at 0.43/0.47; the first three Copper September 15 contracts showed only a 0.99 ask; sampled Idaho Senate contracts were near 0.02/0.03 or 0.94/0.95; the football championship contained 32 sibling contracts with sampled prices below the rebate band. These were discovery prices, without order-book quantities or a continuous feed. None is an executable trade recommendation.

This supports researching Zcash/commodities and selected event overrides, but not assuming that fewer makers or more listings means easy profit. Weather near observation time, live sports, and fast crypto strikes need specific information/latency models.

## 5. Remaining implementation questions

- Resolve shared-pool score aggregation and uptime qualification with account summaries or explicit venue clarification. Public maker counts do not reveal total daily competitor scores.
- Resolve cap aggregation, multiple orders at one price, partial fills, the exact quadratic curve and payout-day attribution before implementing an exact reward estimator.
- Establish live full-depth data, genuine quote duration, exit depth, order-state recovery and settlement behavior. Shadow observations cannot prove fills, queue position or received payouts.
- The saved credential is a master-scoped key. Gemini's [WebSocket authentication documentation](https://developer.gemini.com/prediction-markets/websocket/authentication) requires an account-scoped key with time-based nonce and rejects master/group keys. Authenticated WebSocket readiness has not been established.
- Build the read-only pool/book scorer next, then compare estimates with actual credited rewards during any separately authorized bounded trading trial. No dependable daily-profit number exists yet.

## Work performed

Public documentation, public API and official website-code reads; local Polymarket Markdown review; saved this analysis and public snapshot; corrected the Gemini API reference. No authenticated requests, account enrollment, trading orders or Polymarket runtime changes were performed.
