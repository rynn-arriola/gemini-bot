# Gemini Predictions API Reference

Implementation repair check, September 15, 2026: revalidated official streams, active-order pagination, rate limits, liquidity rewards and collateral-return restrictions. See [repair evidence](docs/REPAIR_REPORT_2026-09-15.md) for implemented behavior and unsupported account-program boundaries. This check used public documentation, not authenticated account access.

Working reference for the Gemini liquidity-rewards bot. This document summarizes the official Gemini Prediction Markets REST and WebSocket APIs and links to the canonical specification for every surface.

Last verified: **2026-09-15 (America/New_York)**

> Gemini changes markets, reward pools, program rules, schemas, and limits. Treat the official specifications as authoritative and re-check them before deploying trading changes.

## Canonical sources

- [Prediction Markets API overview](https://developer.gemini.com/prediction-markets-spec)
- [OpenAPI specification](https://developer.gemini.com/specs/openapi/prediction-markets.yaml)
- [WebSocket AsyncAPI specification](https://developer.gemini.com/specs/asyncapi/websocket.yaml)
- [Trading quickstart](https://developer.gemini.com/prediction-markets/trading-quickstart)
- [Order lifecycle and settlement](https://developer.gemini.com/prediction-markets/order-lifecycle)
- [Liquidity Rewards Program](https://developer.gemini.com/prediction-markets/liquidity-rewards-program)
- [Maker Rebate Program](https://developer.gemini.com/prediction-markets/maker-rebate-program)
- [Taker Rewards Program](https://developer.gemini.com/prediction-markets/taker-rewards-program)
- [Maker Fee-Free Trading Promo](https://developer.gemini.com/prediction-markets/maker-fee-free-trading-promo)
- [Market Maker Program](https://developer.gemini.com/prediction-markets/market-maker-program)
- [Collateral Return](https://developer.gemini.com/prediction-markets/collateral-return)
- [Predictions fee schedule](https://www.gemini.com/fees/predictions)
- [API-key authentication](https://developer.gemini.com/authentication/api-key)
- [Rate limits](https://developer.gemini.com/rate-limit)
- [Error codes](https://developer.gemini.com/error-codes)

## Reward-pool accounting and dated evidence

**Correction, September 14:** the earlier $196,150/day claim repeated shared category budgets once per event and is withdrawn. Category defaults are shared pools; event overrides are separate pools. Neither an event row nor a tradable contract necessarily owns an independent budget.

At 22:51 ET, a public scan returned 568 unique rewarded event rows across 9 shared category pools ($2,450/day) and 34 individual event overrides ($5,800/day): **43 distinct pools, $8,250/day advertised total**. These are venue budgets, not estimated bot income. The old contract-count census was not repeated in this scan.

For `category_default`, group globally by numeric `pool_id` and count its budget once. For `event_override`, use the event ticker as identity, including when `pool_id` is absent. Preserve and validate `pool_event_count` and `pool_category_name`; reject ambiguous identities or inconsistent budgets. The live API exposes these fields although the inspected OpenAPI omitted them. Gemini's current website groups shared pools and displays each budget only on its parent row.

The full dated analysis and public evidence live in [Rewards strategy review](docs/REWARDS_STRATEGY_REVIEW_2026-09-14.md) and [public snapshot](docs/history/REWARDS_PUBLIC_SNAPSHOT_2026-09-14.json). Fetch the live universe and join event tickers to exact contract symbols before future work; do not hard-code snapshot totals.

## Environments

| Interface | Production | Sandbox |
| --- | --- | --- |
| REST | `https://api.gemini.com` | `https://api.sandbox.gemini.com` |
| WebSocket | `wss://ws.gemini.com` | `wss://ws.sandbox.gemini.com` |

WebSocket connection options:

- `snapshot=0`: differential depth starts with deltas only.
- `snapshot=-1`: send a full depth snapshot first.
- `snapshot=N`: send the top N levels first.
- `cancelOnDisconnect=true`: cancel all open orders placed through that WebSocket session when it disconnects.

Recommended bot connection:

```text
wss://ws.gemini.com?snapshot=-1&cancelOnDisconnect=true
```

## Important conventions

- Discover and use the exact contract `instrumentSymbol`; do not construct symbols from titles or tickers.
- Before trading, require event `status: "active"` and contract `marketState: "open"`.
- Quantities and prices are normally decimal strings. Preserve them as decimals/strings rather than binary floating point.
- Contract prices are in the `0` to `1` range. A winning contract settles at `$1`; a losing contract settles at `$0`.
- REST order values use lowercase, such as `buy`, `yes`, `limit`, and `good-til-cancel`.
- WebSocket order values use uppercase, such as `BUY`, `YES`, `LIMIT`, and `MOC`.
- Rewards responses use `snake_case`; most other Prediction Markets REST responses use `camelCase`.
- An accepted order is not necessarily filled. Track accepted, open, partial-fill, fill, cancel, and settlement state independently.

## Authentication

### API-key requirements

- Use an **account-scoped** API key. Prediction Markets WebSocket rejects master/group keys.
- Enable **time-based nonce** for WebSocket authentication.
- `Trader` is required for placing and cancelling orders.
- `Auditor` or `Trader` is sufficient for `positions@account`.
- Keep credentials in environment variables or a secret manager. Never commit the API secret, signed payloads, or authenticated logs.

### Authenticated REST

Private V1 REST calls use an empty HTTP body. Merge the endpoint fields with:

```json
{
  "request": "/v1/prediction-markets/positions",
  "nonce": 1776294447
}
```

Then:

1. JSON-encode the payload.
2. Base64-encode the JSON bytes.
3. Sign the base64 payload using HMAC-SHA384 and the API secret.
4. Send an empty HTTP body with these headers:

| Header | Value |
| --- | --- |
| `X-GEMINI-APIKEY` | Account-scoped API key |
| `X-GEMINI-PAYLOAD` | Base64-encoded JSON payload |
| `X-GEMINI-SIGNATURE` | Lowercase hex HMAC-SHA384 signature |

Time-based REST nonces are Unix seconds and must remain within approximately 30 seconds of Gemini's server clock. Incremental nonce keys instead require a strictly increasing value per session key.

### Authenticated WebSocket

Authentication is supplied during the WebSocket upgrade; it cannot be added after connecting.

Live correction measured September 15, 2026: the authenticated Predictions socket returned HTTP 401 for an otherwise identical millisecond nonce and accepted a Unix-seconds nonce immediately afterward. The production client therefore uses monotonic Unix seconds and paces reconnects rather than relying on the broader seconds-or-milliseconds wording on the authentication page.

| Header | Value |
| --- | --- |
| `X-GEMINI-APIKEY` | Account-scoped API key |
| `X-GEMINI-NONCE` | Monotonically increasing decimal nonce |
| `X-GEMINI-PAYLOAD` | Base64 encoding of the nonce string |
| `X-GEMINI-SIGNATURE` | Hex HMAC-SHA384 of the base64 payload |

OAuth is also supported with `Authorization: Bearer <access_token>`. Do not mix OAuth and API-key headers.

### Terms prerequisite

Before placing any order:

1. Call `GET /v1/prediction-markets/terms/status`.
2. If `hasAcceptedLatest` is false, retrieve `GET /v1/prediction-markets/terms`.
3. Present the terms to the account owner.
4. Call `POST /v1/prediction-markets/terms/accept` only after authorized acceptance.

## REST endpoint catalog

### Events and contract discovery

All endpoints in this section are public.

| Method | Path | Purpose | Important inputs |
| --- | --- | --- | --- |
| GET | `/v1/prediction-markets/events` | Paginated event discovery | `status`, `category`, sports filters, search, `limit`, `offset` |
| GET | `/v1/prediction-markets/events/{eventTicker}` | Retrieve an event and its contracts | Exact event ticker |
| GET | `/v1/prediction-markets/events/{eventTicker}/strike` | Retrieve a strike/reference value | Exact event ticker |
| GET | `/v1/prediction-markets/events/newly-listed` | Find new events | Filters and pagination |
| GET | `/v1/prediction-markets/events/recently-settled` | Find recently resolved events | Filters and pagination |
| GET | `/v1/prediction-markets/events/upcoming` | Find upcoming events | Filters and pagination |
| GET | `/v1/prediction-markets/categories` | Retrieve the category tree | Optional category filters |

The bot should persist at least:

- Event: `ticker`, `title`, `status`, `category`, `subcategory`, `effectiveDate`, `expiryDate`, `startTime`, `sourceDetails`, and terms URL.
- Contract: `instrumentSymbol`, `ticker`, `label`, `status`, `marketState`, `quantityMinimum`, `quantityIncrement`, `priceMinimum`, `priceIncrement`, `expiryDate`, and current prices.

Official section: [Events API](https://developer.gemini.com/rest-api/prediction-markets/events)

### Public volume

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/prediction-markets/volume/{date}` | Public | Daily trade volume by UTC date/category |
| GET | `/v1/prediction-markets/volume/{date}/hourly` | Public | Hourly trade volume for a UTC date |
| POST | `/v1/prediction-markets/metrics/volume` | Private | Account/event volume metrics |

Official section: [Volume API](https://developer.gemini.com/rest-api/prediction-markets/volume)

### Terms

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/prediction-markets/terms` | Public | Retrieve the current terms |
| GET | `/v1/prediction-markets/terms/status` | Private | Check whether the account accepted the latest terms |
| POST | `/v1/prediction-markets/terms/accept` | Private | Accept a specific current terms version |

Official section: [Terms API](https://developer.gemini.com/rest-api/prediction-markets/terms)

### Order management

All endpoints in this section are private and require trading permission. Gemini recommends WebSocket order management for an active market-making bot; use REST primarily for one-off operations and reconciliation.

| Method | Path | Purpose | Important inputs |
| --- | --- | --- | --- |
| POST | `/v1/prediction-markets/order` | Place one order | `symbol`, `orderType`, `side`, `outcome`, `quantity`, `price`, `timeInForce`, optional `stopPrice`, `makerOrCancel` |
| POST | `/v1/prediction-markets/order/batch` | Place multiple orders | Array of order payloads |
| POST | `/v1/prediction-markets/order/cancel` | Cancel one order | `orderId` |
| POST | `/v1/prediction-markets/order/batch/cancel` | Cancel multiple orders | Order-ID array |
| POST | `/v1/prediction-markets/orders/active` | Snapshot current open orders | Optional `symbol`, `limit` (max 100), `offset` |
| POST | `/v1/prediction-markets/orders/history` | Retrieve filled/cancelled history | `status`, optional `symbol`, `from`, `to`, `limit`, `offset` |

REST maker-only order fields:

```json
{
  "request": "/v1/prediction-markets/order",
  "nonce": 1776294447,
  "symbol": "GEMI-FEDJAN26-DN25",
  "orderType": "limit",
  "side": "buy",
  "quantity": "10",
  "price": "0.49",
  "outcome": "yes",
  "timeInForce": "good-til-cancel",
  "makerOrCancel": true
}
```

`makerOrCancel: true` is the REST post-only control. Confirm the live contract's minimum quantity and price/quantity increments before submitting.

Official section: [Order Management API](https://developer.gemini.com/rest-api/prediction-markets/order-management)

### Positions and reconciliation

| Method | Path | Auth | Purpose | Important inputs |
| --- | --- | --- | --- | --- |
| POST | `/v1/prediction-markets/positions` | Private | Current filled positions | Optional `eventTicker`, `limit`, `offset`, `sort` |
| POST | `/v1/prediction-markets/positions/settled` | Private | Settled positions and payouts | Optional event, search/category, pagination and sort filters |

Use REST snapshots at startup, after reconnects, after suspected message gaps, and around settlement. Do not infer positions solely from the order-status stream.

Official section: [Positions API](https://developer.gemini.com/rest-api/prediction-markets/positions)

### Combo contracts

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/prediction-markets/combos` | Public | List combo contracts |
| GET | `/v1/prediction-markets/combos/{instrumentSymbol}` | Public | Retrieve one combo |
| POST | `/v1/prediction-markets/combos` | Private | Create or retrieve a canonical combo |

Combo orders use the normal place/cancel endpoints. Combo RFQ quoting has additional WebSocket methods and streams.

Official section: [Combos API](https://developer.gemini.com/rest-api/prediction-markets/combos)

### Maker rebates

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/prediction-markets/maker-rebate/rates` | Public | Current maker-rebate schedule |
| POST | `/v1/prediction-markets/maker-rebate/payouts` | Private | Account payout history |
| GET | `/v1/prediction-markets/maker-rebate/summary/total` | Private | Account lifetime summary |

Maker rebates are fill-based and independent of liquidity rewards. A qualifying resting order can potentially earn both.

The production selector reads the public rates endpoint once during preparation, chooses the latest currently effective category rule with the active default as fallback, and assumes zero rebate when the read is unavailable. This is ranking input only; authenticated payout data remains authoritative.

Official section: [Rewards API](https://developer.gemini.com/rest-api/prediction-markets/rewards)

### Liquidity rewards

| Method | Path | Auth | Purpose | Important inputs |
| --- | --- | --- | --- | --- |
| GET | `/v1/prediction-markets/liquidity-rewards/config` | Public | Current program-wide configuration | None |
| GET | `/v1/prediction-markets/liquidity-rewards/events` | Public | Current rewarded event universe | `category`, `search`, `sort`, `limit`, `offset` |
| GET | `/v1/prediction-markets/liquidity-rewards/summary/daily` | Private | Account daily payouts and per-event scores | `dateFrom`, `dateTo` in ET |
| GET | `/v1/prediction-markets/liquidity-rewards/summary/total` | Private | Account lifetime reward summary | None |

Current public event rows include:

```json
{
  "event_ticker": "BTC2605202100",
  "title": "BTC above $95,000?",
  "category": "Crypto",
  "daily_pool_usd": "500.00",
  "pool_source": "event_override",
  "ends_at": "2026-05-20T21:00:00Z",
  "qualifying_maker_count": 14,
  "pool_event_count": 1
}
```

Reward-event pagination uses a maximum `limit` of 100. Program endpoints return HTTP 503 when the program is unavailable.

Observed shared-category rows additionally contain `pool_id`, `pool_event_count`, `pool_category_name`, `recurring_duration_value`, and `recurring_duration_unit`. Preserve these fields even when an older generated schema omits them. The current response supplies `ends_at` but no reward `starts_at`; do not invent a reward activation timestamp. `last_score_date` is a scoring date, not a precise timestamp proving competition is fresh. Event maker counts are not distinct pool-level account counts.

Official endpoint: [List Liquidity Rewards Events](https://developer.gemini.com/rest-api/prediction-markets/rewards/list-liquidity-rewards-events)

## Liquidity reward rules

Gemini takes an order-book snapshot once per minute for each eligible contract.

```text
snapshot_score = spread_weight * scored_size * two_sided_multiplier
daily_payout = applicable_pool_budget * account_pool_daily_score / all_qualifying_pool_daily_scores
```

Rules verified on 2026-09-14:

| Rule | Value |
| --- | ---: |
| Maximum qualifying distance from midpoint | 10 cents |
| Minimum qualifying size | 10 contracts |
| Maximum size counted | 250 contracts |
| Two-sided multiplier | 1.5x |
| Required uptime | 50% of eligible snapshots |
| Minimum daily payout | $1.00 |
| Payment time | Daily at approximately 5:30 p.m. ET |

Important behavior:

- Spread weighting is quadratic; tight quotes score disproportionately more.
- The material reviewed does not specify an exact implementable quadratic curve. Do not present a guessed formula as Gemini's scoring algorithm.
- For a one-sided book, the reference is the best price on that side instead of midpoint.
- The size cap is a scoring cap, not necessarily an order-size cap.
- The 1.5x multiplier applies to snapshots where the maker has qualifying orders on both sides of the same contract.
- A maker below the daily uptime threshold receives no reward for that event/day. The denominator is eligible snapshots, not necessarily all 1,440 minutes in an Eastern day.
- Orders outside the maximum spread, below minimum size, or on post-only-mode contracts score zero.
- `qualifying_maker_count` is the most recent snapshot-window count, not a guarantee of final daily competition.
- Sub-$1 daily totals are suppressed rather than carried forward.
- Reward seeking does not remove fill, inventory, settlement, or adverse-selection risk.
- Event metadata supplies contract `effectiveDate`, event `startTime`, and expiry independently. A contract can be tradeable and rewarded days before its real-world resolution; entry is allowed only after `effectiveDate` and while the contract is open and present in the current reward feed.
- Shared-pool aggregation, exact cap aggregation and the live eligible-snapshot start remain unreported. Track `pool_id` for one budget, event ticker for timing, and reconcile the next authenticated daily summary's `snapshot_count`/`total_snapshots` before claiming an exact payout estimator.
- Sports markets are excluded at their published game `startTime`, even if in-play trading remains open. This local risk choice is stricter than Gemini's program-wide eligibility rule and does not apply to crypto interval contracts whose intended scoring window is live.
- The observed public config returns only enablement, maximum spread and minimum payout. The other table values came from the program page, not that API response.

## Other Predictions incentives and fee overlays

- Maker rebates currently use a 0.30 fallback multiplier of eligible taker fees; apply effective-date/category rules from the rates endpoint. The documented eligible fill-price band is $0.20-$0.80 and qualifying contracts were listed on/after March 18, 2026. Rebates round down and are scheduled at 5 p.m. ET.
- The September 2-December 31, 2026 maker-fee waiver makes eligible maker executions fee-free. The base fee schedule remains relevant outside the promotion. Reconcile actual fill fees and credited rebates.
- Taker rewards run July 1-September 30, 2026, subject to funding and eligibility; their volume thresholds and active-day requirement make them a separate strategy analysis.
- Contracted market making needs approval; no account eligibility or negotiated rate is assumed.
- Collateral return needs account/group onboarding and currently covers mutually exclusive groups, not directional strike ladders. It affects spendable cash and reduction semantics, not reward income. Resting-order holds remain applicable; affected accounts reduce No with Buy Yes rather than direct Sell No.

See the dated [full rewards review](docs/REWARDS_STRATEGY_REVIEW_2026-09-14.md) for conditions, examples and unresolved questions.

## WebSocket API

### Request/response envelope

```json
{
  "id": 1,
  "method": "SUBSCRIBE",
  "params": ["GEMI-FEDJAN26-DN25@bookTicker"]
}
```

Responses return the request `id` with either `result` or `error`. Streaming frames are asynchronous. Some streams include an `e` event-type field; book ticker, partial depth, and trade frames do not, so route them using subscription context.

### Control and trading methods

| Method | Auth | Purpose |
| --- | --- | --- |
| `conninfo` | Public | Connection rate-limit buckets; production queries each socket at connection startup |
| `ping` | Public | Liveness check |
| `time` | Public | Server time |
| `SUBSCRIBE` | Depends on stream | Add subscriptions |
| `UNSUBSCRIBE` | Depends on stream | Remove subscriptions |
| `LIST_SUBSCRIPTIONS` | Public/session | List current subscriptions |
| `depth` | Public | Request depth |
| `order.place` | Trader | Place an order |
| `order.cancel` | Trader | Cancel one order |
| `order.cancel_all` | Trader | Cancel all account/session orders covered by the method |
| `order.cancel_session` | Trader | Cancel orders for the current session |
| `rfq.submit_quote` | Authenticated | Submit a combo RFQ quote |
| `rfq.withdraw_quote` | Authenticated | Withdraw an RFQ quote |
| `rfq.confirm_quote` | Authenticated | Confirm an RFQ quote |

Use the exact casing and payload defined by the current AsyncAPI specification.

### Market-data streams

| Stream | Auth | Behavior |
| --- | --- | --- |
| `{symbol}@bookTicker` | Public | Best bid/ask updates |
| `{symbol}@depth5` | Public | Top 5 depth snapshot |
| `{symbol}@depth10` | Public | Top 10 depth snapshot |
| `{symbol}@depth20` | Public | Top 20 depth snapshot |
| `{symbol}@depth5@100ms` | Public | Fast top 5 depth |
| `{symbol}@depth10@100ms` | Public | Fast top 10 depth |
| `{symbol}@depth20@100ms` | Public | Fast top 20 depth |
| `{symbol}@depth` | Public | Differential L2 updates |
| `{symbol}@depth@100ms` | Public | Fast differential L2 updates |
| `{symbol}@trade` | Public | Market-wide trade executions |
| `{agency}:{index}@indexPrice` | Public | Reference/index price used by supported contracts |

For differential depth, initialize with `snapshot=-1` or a positive depth. Apply subsequent deltas in sequence using update IDs. If the next frame skips beyond the last applied update ID, discard the local book and resubscribe for a fresh snapshot.

### Private account streams

| Stream | Purpose |
| --- | --- |
| `orders@account` | Real-time order state and fills across the account |
| `orders@session` | Order state for the authenticated session |
| `balances@account` | Balance deltas |
| `balances@account@1s` | Full balance snapshot every second |
| `positions@account` | Position deltas, including settlement closure |
| `positions@account@1s` | Full open-position snapshot every second |

There is no separate settlement stream. Settlement arrives as a terminal `positionReport` with position `0` and a `settlement_payout` amount.

### Contract and RFQ streams

| Stream | Auth | Purpose |
| --- | --- | --- |
| `contractStatus` | Public | Contract status and strike availability changes |
| `requestForQuote` | Public | Public RFQ activity |
| `requestForQuote@account` | Private | Account RFQ delivery |
| `requestForQuote@session` | Private | Session RFQ delivery |

Official sections:

- [WebSocket introduction](https://developer.gemini.com/prediction-markets/websocket/introduction)
- [WebSocket authentication](https://developer.gemini.com/prediction-markets/websocket/authentication)
- [WebSocket message format](https://developer.gemini.com/prediction-markets/websocket/message-format)
- [WebSocket streams](https://developer.gemini.com/prediction-markets/websocket/streams)

## Order semantics

### REST versus WebSocket

| Meaning | REST | WebSocket |
| --- | --- | --- |
| Outcome | `outcome: "yes"` | `eventOutcome: "YES"` |
| Order type | `orderType: "limit"` | `type: "LIMIT"` |
| Side | `side: "buy"` | `side: "BUY"` |
| Maker only | `makerOrCancel: true` with GTC | `timeInForce: "MOC"` |

### WebSocket time in force

- `MOC`: maker-or-cancel/post-only. If it would take liquidity, Gemini cancels it with `MakerOrCancelWouldTake`.
- `IOC`: immediately fills available crossing quantity and cancels the remainder. Even a fully filled IOC may finish with a `CANCELED` event; use executed quantity `Z`, not final status, to determine fills.
- `FOK`: fills completely and immediately or cancels in full.
- Use the currently documented good-til-cancel value for ordinary resting WebSocket orders.

Track both cumulative and last-execution quantities. Cancellation never reverses fills that happened before cancellation.

Live commissioning on September 15, 2026 established that the WebSocket endpoint enforces a 36-character maximum for `clientOrderId`; a longer value returned status 400 / code `-1013` before placement. The production generator caps IDs at 34 characters. See [entry and exit semantics](docs/ORDER_SEMANTICS.md) for the venue-specific four-action mapping and exact-reducer rules.

## Rate limits and retry policy

Documented general REST limits:

| API class | Limit | Gemini recommendation |
| --- | ---: | ---: |
| Public | 120 requests/minute | Do not exceed 1 request/second |
| Private | 600 requests/minute | Do not exceed 5 requests/second |

Gemini provides a small queued burst and then returns HTTP 429. The bot should:

- Use WebSockets for market data and active order management.
- Cache static event metadata.
- Paginate discovery and reward endpoints deliberately.
- Apply bounded exponential backoff with jitter for 429 and transient 5xx responses.
- Reconcile state before retrying an order placement after a timeout; a timeout does not prove rejection.
- Never retry an unknown order submission blindly without an idempotency/reconciliation strategy.

## Error handling

REST errors normally contain:

```json
{
  "result": "error",
  "reason": "InvalidNonce",
  "message": "Human-readable explanation"
}
```

WebSocket errors use an `error` object with numeric `code` and string `msg`. Known general WebSocket codes include `-1000` for an internal error and `-1003` for rate limiting.

Important machine-readable reasons include:

- Authentication: `MissingApikeyHeader`, `MissingPayloadHeader`, `MissingSignatureHeader`, `MissingNonce`, `InvalidNonce`, `InvalidSignature`, `AmbiguousAuthentication`.
- Order validation: `LimitPriceOffTick`, invalid quantity/price/stop price, insufficient funds, and invalid total spend.
- Time-in-force cancellation: `MakerOrCancelWouldTake`, `ImmediateOrCancelWouldPost`, `FillOrKillWouldNotFill`.
- Exchange controls: `ExceedsPriceLimits`, rate limiting, and service/internal errors.

Log error codes, endpoint/method, request ID, server order ID, client order ID, symbol, and timestamps. Redact credentials, signed payloads, authorization headers, and sensitive account data.

## Minimum safe bot flow

1. Fetch the current liquidity-reward configuration.
2. Page through all current rewarded events.
3. Fetch/join the corresponding event records and contract `instrumentSymbol` values.
4. Reject events/contracts that are not active/open, are past expiry, lack accepted terms, or violate local risk rules.
5. Group shared budgets correctly, then rank incremental pool contribution using quote distance, attainable uptime, contract coverage, book depth, expected fills and inventory risk. Maker count is only a coarse competition observation.
6. Establish authenticated WebSocket connections with initial depth snapshots and cancel-on-disconnect.
7. Reconcile active orders, positions, and balances from REST before quoting.
8. Place maker-only orders and subscribe to order, balance, and position updates.
9. Detect sequence gaps, disconnects, stale prices, status changes, and strike changes; cancel or suspend quoting immediately.
10. Reconcile reward summaries, rebates, fills, realized P&L, and settlement payouts daily.

## Data that must remain configurable

Do not hard-code:

- Reward pool size, program enablement, min payout, max spread, min size, size cap, multiplier, or uptime threshold.
- Event/category lists, symbols, tick sizes, quantity increments, minimum sizes, price bands, strikes, source indices, start times, or expiry times.
- Maker-rebate rates, fee schedules, rate limits, or program end dates.
- Competition counts or expected reward share.

Fetch live configuration where an API exists; separately version and verify parameters published only in documentation. Fail closed when required configuration or pool identity is missing or inconsistent.
