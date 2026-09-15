# Gemini API limits

Checked September 14, 2026 (America/New_York).

Rechecked official documentation September 15, 2026: the REST recommendations above remain published. The [stream reference](https://developer.gemini.com/trading/websocket/streams) documents plain differential `@depth` at one second and `@depth@100ms` at 100 ms; the [WebSocket playground](https://developer.gemini.com/trading/websocket/playground) exposes `conninfo` for connection rate limits. [Message format](https://developer.gemini.com/websocket/message-format) maps code `-1003` to status `429`. These are supported interfaces, not evidence that production already consumes connection limits.

Current production subscribes to `@depth@100ms`, keeps public/private REST at 1/5 requests per second, and reads `conninfo` before subscriptions on both sockets. Missing or unrecognized limit metadata blocks connection preparation. Rate metadata on subsequent responses updates the same governor. Incoming depth frames do not imply one outbound request per update.

The governor tracks every reported request-weight/order interval and connection-attempt interval. It shares conservative usage across both sockets and adopts the lower reported limit when scope is uncertain. Normal application traffic uses up to 70% of each bucket; emergency cancellations may use up to 90%, always subject to server cooldowns. Local usage reservations cannot be reduced by a lower response count, and rolling expiration waits a full interval from local observation to avoid assuming a server reset boundary. Connection attempts are paced separately.

The published [AsyncAPI source](https://developer.gemini.com/specs/asyncapi/websocket.yaml) does not enumerate conninfo results or all per-method weights. Production therefore reserves 100 request-weight units per application request, one order unit per order method (30 for cancel-all/session), and retains a 50 ms minimum local spacing. This is a conservative implementation estimate, not a Gemini-published weight table or proof throttling is impossible. Reported quotas can tighten pacing; high quotas never remove the local ceiling. Malformed limits or an unknown bucket require review. Reconnects never bypass observed cooldowns.

September 15 verification: a 60-second authenticated read-only check established both connection limits, subscribed 32 books at 100 ms, observed no gaps or throttles, and disconnected flat/order-free with zero order/cancel mutations. CPU averaged 1.032% of one logical core including startup; only about 12 seconds followed discovery/startup, so this is not a sustained trading performance benchmark.

## Published REST limits

Official source: https://developer.gemini.com/rate-limit

- Public endpoints: 120 requests/minute; Gemini recommends no more than one request/second.
- Private endpoints: 600 requests/minute; Gemini recommends no more than five requests/second.
- Some endpoints have stricter limits, notably transfer and deposit-address queries. Do not assume the general limit overrides an endpoint-specific limit.
- Excess requests can receive HTTP 429. Short bursts may be queued; this is not additional dependable capacity.

## Observed public WebSocket limits

The public server's `conninfo` response returned these limits during this check:

| Bucket | Limit | Window |
| --- | ---: | --- |
| REQUEST_WEIGHT | 7,000 | 10 seconds |
| ORDERS | 3,500 | 10 seconds |
| ORDERS | 10,000,000 | One day |
| CONNECTION_ATTEMPTS | 300 | Five minutes |

These are server-reported buckets on the queried public connection, not a promise of account trading capacity. Request weight is not necessarily one unit per request. These values do not establish subscription limits or the exact scope of shared limits across accounts, IPs and connections. Re-check before a materially larger deployment.

## Overnight observer traffic

- One shared public WebSocket for up to fifty selected contracts in the expanded run.
- Streaming best prices, trades and contract status; up to fifty depth requests per minute over that socket.
- Outgoing WebSocket application requests spaced at least 0.55 seconds apart.
- Public REST only for initial discovery and hourly rewards/metadata refresh; calls spaced at least 1.1 seconds apart.
- No recurring private REST calls and no order submission.
- Subscriptions batched into at most twenty names per acknowledged request. Server weight information is inspected when present; 70% usage triggers a sixty-second application-request cooldown.
- After user-authorized recovery hardening: ordinary 429 responses trigger at least sixty seconds of exponential backoff and honor longer Retry-After values, including handshake errors. Five WebSocket throttles or an explicit 418 ban stop requests for the remaining bounded run. Cooldowns survive in-process recovery. Connection limits are queried every five minutes. These are local safeguards, not a claim that the exchange cannot throttle us.
- Reconnect backoff grows from two to sixty seconds. Errors and missing observations are recorded rather than treated as successful data.

The four-minute smoke test completed with 41 WebSocket requests, 29 startup REST calls, one WebSocket connection, 40 snapshots and no recorded errors. This is a small-run observation, not a guarantee against future throttling.
