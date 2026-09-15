# Overnight paper test: September 14-15, 2026

## Authorized scope and preferences

The user requested testing tonight through tomorrow, then explicitly required WebSocket priority and CPU optimization. The communicated test is paper/read-only through 9 a.m. Eastern on September 15 (13:00 UTC). The user subsequently requested more conservative quote positioning. No real-money allocation, live loss limit or live order placement is part of this experiment.

Authentication prerequisite passed at approximately 23:17 ET: terms status 200/accepted, authenticated WebSocket delivered balances, positions and an order event. Receiving an account order event does not mean the observer placed an order. Account keys are confined to the separate connection probe.

## Research design

- User authorized an expansion for testing only after the initial ten-market launch. The expanded run targets up to fifty qualifying contracts, taking one per distinct reward pool first and then additional contracts in round-robin pool order. Multiple contracts in the same pool are correlated comparisons, not additional reward budgets.
- Expanded discovery reads at most 160 event metadata records at the existing REST pace. Each pool contributes a bounded candidate set; the count can be below fifty if too few contracts pass the existing filters. This is not an exhaustive census or a guaranteed profit ranking.
- Initial candidates must be active/open, have two-sided metadata prices in 0.20-0.80, support quantity ten, and expire after the experiment. These are discovery filters, not a profit ranking.
- Sample fresh order-book depth through the WebSocket `depth` method once per minute per selected symbol. Record actual depth, pool identity, quote geometry and ten-contract exit availability.
- Stream `bookTicker`, trades and contract lifecycle on the same socket. Update in-memory state on arrivals; retain only bounded/coalesced stream state.
- Compare ten-contract quotes at the touch, one tick behind and two ticks behind. **Two ticks behind is the primary cautious candidate**; the others are comparison observations. Quotes at or outside the maximum-distance boundary are not assumed to earn positive score.
- Paper quotes are observed for at most one minute. A subsequent public trade at/through the proposed quote is only a possible execution, because we do not observe the hypothetical queue or effects of inserting our own order.
- Measure hypothetical price marks after 1, 5 and 15 minutes, plus available ten-contract exit cash. Distinguish YES prices from NO complements. Insufficient depth remains unknown rather than a partial exit reported as complete.
- Connection changes, late observations and closed/unknown markets censor forward measurements. Do not infer fills, paid rewards or realized P&L from them.
- Keep the initial selected cohort stable overnight to avoid silently replacing bad results with better markets. Refresh metadata/reward configuration hourly; market-status stream updates apply immediately. Missing/old metadata or missing reward membership suppresses quote eligibility.

## Transport and CPU design

One shared public WebSocket handles streams and correlated read-only requests. Protocol Ping/Pong checks connection liveness. Reconnects use a bounded backoff; gaps are recorded. There is no parallel fleet of sockets and no busy polling loop.

REST supplies discovery, pool configuration and contract metadata because those have no documented equivalent complete WebSocket catalogs. Startup and hourly refresh are paced at approximately one request per 1.1 seconds, in a background thread during steady observation. The script has no REST book polling path. If WebSocket data cannot be obtained, mark it missing; do not present REST event headline prices as equivalent full-depth evidence.

Quotes are computed only once per minute for up to fifty books and three offsets, not on every tick. Stream callbacks do no disk I/O or full-universe scans. SQLite writes are committed once per cycle, and history is bounded to twenty observations per symbol. The report is generated at completion or on explicit request rather than rewritten per tick.

WebSocket application requests retain 0.55-second minimum spacing (about 28 seconds of pacing for fifty depth requests per minute). Subscriptions are acknowledged in batches of at most twenty stream names. Connection limits are rechecked every five minutes, and server-provided request-weight usage is inspected when returned: reaching 70% imposes a sixty-second cooldown. Ordinary 429 throttling uses at least 60 seconds of backoff, increasing on repeated throttles, and honors longer Retry-After values. Five WebSocket throttles or an explicit 418 ban block further requests for the remaining run. WebSocket handshake throttling is also respected. No transport switching or reconnection is used to evade a cooldown. REST requests retain 1.1-second minimum spacing.

Each depth request has a three-second response timeout and the overall sampling cycle has a fifty-second budget. Budget-exhausted cycles record skipped samples and rotate the next starting market so slow early markets cannot permanently starve later markets. No catch-up request burst is scheduled.

Metadata failures retry after 60 seconds, doubling to at most ten minutes, while respecting any longer REST cooldown. Successful metadata refreshes return to the hourly schedule. Metadata remains ineligible when stale; raw order books can still be retained.

Storage commits and atomic status writes retry three times with short delays. Uncommitted rows remain in a bounded 10,000-record memory buffer for later cycles. Overflow is explicitly counted as lost records, not claimed as durable data. Sustained disk failure cannot be made lossless indefinitely. Unexpected collector exceptions have up to three in-process restarts, with 60/120/240-second delays; pending rows, counters, cohort and throttle cooldowns are retained, while cross-restart forward marks are invalidated. This does not protect against the operating system killing the whole Python process.

Status records process CPU time divided by wall time, as a percentage of one logical core. Average and peak measured sample intervals exclude the initial discovery phase. This is measured overhead, not a claim that every future strategy will have the same cost.

On Windows the observer holds a process-scoped system-awake request while it runs and releases it when finished. It does not change the laptop's saved power plan or keep the display on. Loss of power, explicit sleep or lid-close policy can still interrupt collection.

## Interpretation limits

This overnight window is shorter than a full reward day and crosses Eastern midnight. It cannot demonstrate 50% daily uptime eligibility. Public pool headcounts are not competitor scores, and Gemini's exact quadratic curve/cap aggregation remain unverified. No dollar reward forecast is produced.

Quote geometry at a sampled instant is not proven continuous quote uptime. Forward marks assume entries at specified prices; they are not evidence that both sides filled, that the account captured a spread or that future trades will be profitable. Fees, rebates, actual rewards and settlement are excluded from these hypothetical marks.

## Outputs

Each run under `data/` has `discovery.json`, `observations.sqlite`, `status.json` and a final `REPORT.md`. `quote_interval` rows retain possible-touch observations; `snapshot` rows retain books and proposed quotes; `markout` rows retain hypothetical subsequent value and executable-depth comparisons. Records include timestamps and connection generations. The local run lock prevents duplicate writers, and an existing run cannot be overwritten by a fresh start.

Morning review should report coverage, missing samples, errors/reconnects, actual CPU usage, quote-distance tradeoffs, exit depth and unresolved questions. It should propose a live pilot only if the evidence supports one; it must not start trading automatically.

## Expansion handoff

Preserve the original `data/overnight-20260914` directory and the first expanded `data/overnight-expanded-20260914` directory. The user subsequently authorized recovery hardening and a restart into `data/overnight-hardened-20260914`, using the same recent fifty-market discovery cohort. Seed metadata is refreshed shortly after startup; there is no new selection designed to replace unfavorable observations. All runs retain their actual windows; do not concatenate overlapping observations into falsely independent evidence. Morning review must inspect all three directories, with the hardened run as the primary cohort. All retain the September 15 09:00 Eastern deadline; no real-money activity is authorized.
