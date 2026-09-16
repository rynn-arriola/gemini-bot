# Reward timing verification — September 15, 2026

Research only. No execution code, policy, permit, orders, or running process changed. The 50% estimator is not exchange-confirmed eligibility.

## Verified documentation

- [Liquidity program](https://developer.gemini.com/prediction-markets/liquidity-rewards-program): per-minute scoring, 50% of eligible snapshots, active tradability and an active reward association required; post-only contracts excluded. Liquidity payouts are scheduled for 17:30 ET. Recurring contract types can share a reward budget.
- [Maker rebates](https://developer.gemini.com/prediction-markets/maker-rebate-program): separate executed-maker-fill program, scheduled for 17:00 ET. Its payment time does not define liquidity scoring boundaries.
- [Daily summary](https://developer.gemini.com/rest-api/prediction-markets/rewards/get-liquidity-rewards-daily-summary): Eastern date parameters; qualifying `snapshot_count` and event/date `total_snapshots`; reward date is separate from `paid_at`. No documented live-update guarantee.
- [Raw OpenAPI](https://developer.gemini.com/specs/openapi/prediction-markets.yaml): reward-event schema has `ends_at` but no `starts_at`; no explicit scoring reset or recurring-pool uptime aggregation rule. `effectiveDate` fields lack an explanatory reward-scoring definition. Config documentation says sub-threshold daily amounts can have no row.
- Checked [revision history](https://developer.gemini.com/changelog/revision-history), schemas, official pages and general web/community searches. No credible additional source found resolving exact reset time, late pool activation, or recurring shared-pool eligibility aggregation. Search absence is not proof of any particular rule.

## Live read-only evidence

Public production endpoints queried on September 15:

- `/liquidity-rewards/config`: enabled; maximum spread 10 cents; minimum payout $1.
- `/liquidity-rewards/events?category=Crypto&search=SOL&limit=100`: 24 rows, `last_score_date=2026-09-15`, no reward-start field. Observed hourly, 15-minute and 5-minute SOL events ending at 18:00 EDT all carry `pool_id=55`, `pool_event_count=24`, and `daily_pool_usd=50.00`. Do not multiply this shared budget by event count. These pool/recurrence fields are absent from the published reward-event schema.
- `/events/SOL2609152200`: event effective time 15:57:03.419 EDT; event `startTime` 17:00 EDT; sampled contract `GEMI-SOL2609152200-HI97` effective time 17:00 EDT and expiry 18:00 EDT. Event listing time, contract effective time, and reward eligibility start are different concepts.
- Authenticated `/liquidity-rewards/summary/daily`, September 13–15 inclusive: successful request, empty summaries. No account snapshot denominator is available from this response. Empty does not establish zero participation or failure to meet uptime.

## Interpretation and implementation constraints

- A 17:00–18:00 scoring window would imply roughly 60 snapshots and 30 required; this is conditional arithmetic, not Gemini confirmation that this event has that denominator.
- Eastern-calendar-day boundaries are an estimate inferred from date semantics, not a published explicit reset rule. Neither 17:00 nor 17:30 payout time proves a scoring reset.
- Keep event-level counts separate from shared budget identity; do not assume rollover carries uptime or resets it until documented or measured.
- Our early-exit buffer reduces achievable quoting time, not the venue denominator. Maintenance must not be subtracted without evidence that Gemini excludes those snapshots.
- Retain estimated/unknown/venue-reported distinctions. Do not convert empty summaries to zero observed uptime or claim reward profitability from an unverified denominator.

## Representative follow-up sample (18:06–18:07 EDT)

Owner narrowed the task from an exhaustive scan to a representative sample. The initial exhaustive research process was stopped; the trading bot was not touched. The completed public GET-only sample inspected 24 reward events and their 276 contracts: 10 crypto events/62 contracts, 6 sports/165, 4 politics/22, 2 commodities/13, and 2 weather/14. Selection diversified pool/recurrence where available and is not a random or exhaustive sample.

- All 276 contracts supplied an effective timestamp and an expiry timestamp; none of the 24 reward rows supplied a reward-start timestamp.
- Measured contract effective-to-expiry durations: 2 five-minute, 2 fifteen-minute, 19 one-hour, 64 over one day through one week, and 189 over one week. These are contract durations, not exchange-confirmed scoring denominators.
- SOL and BTC both showed five-minute, fifteen-minute and hourly intervals. Sampled daily SOL, BTC and WTI contracts each spanned 25 hours; sampled weather contracts spanned 38 hours. A recurrence label alone therefore cannot substitute for timestamps or reward snapshots.
- Several sports/politics/commodity/weather events lacked `startTime` even though their contracts had effective timestamps. Event start, contract effective time and scoring start must not be conflated.
- Raw public evidence: `data/reward-market-scan-20260915/representative_sample.json`; bounded sampler: `sample.py` in the same directory. Dynamic catalog results are observations, not an atomic venue-wide snapshot.

### Authoritative counts when available

For a matched event and scoring date with positive `total_snapshots`, reported uptime is `snapshot_count / total_snapshots`; the 50% count threshold is `ceil(total_snapshots / 2)`. This avoids inferring the observed denominator from timestamps. A partial-period ratio is not a final-day guarantee, and a missing/zero denominator is unknown, not zero uptime. Counts alone do not prove the exact start timestamp or how many snapshots remain. Our authenticated summary currently supplies no rows to use for this calculation.

## Questions for Gemini support — drafted, not sent

1. What exact timezone and clock boundaries define a scoring date? How are DST days handled?
2. What starts an event's eligible snapshots: listing, contract effective time, open market state, or reward activation? Can the API expose that timestamp?
3. Is the 50% test per event/date or pooled across recurring events sharing a pool ID? How are overlapping contracts aggregated?
4. Which pauses, post-only periods and maintenance snapshots are excluded from the denominator?
5. When are daily snapshot counts published/final, and can nonqualifying or sub-$1 accounts obtain them?

Snapshot impact: none from this research/documentation. Existing approved runtime policy is unchanged; these findings do not authorize a new activation or estimator change.
