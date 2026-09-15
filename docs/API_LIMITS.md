# Gemini API limits

Checked September 14, 2026 (America/New_York).

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
