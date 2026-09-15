# Reward timing verification — September 15, 2026

## Official behavior

Gemini pays liquidity rewards daily and applies the 50% threshold to **eligible snapshots**, not automatically to every minute in a 24-hour day. A quote can count only while its contract is actively tradeable and its event or contract type has an active reward pool. The public reward feed supplies an exact `ends_at` but no reward activation timestamp. Event details separately supply contract `effectiveDate`, event `startTime`, and expiry.

Therefore, a contract may legitimately score days before its real-world outcome if it is already past `effectiveDate`, open for trading, and present in the current reward feed. The bot must never quote before `effectiveDate` or backdate an unknown reward start.

The authenticated daily summary is the venue-authoritative after-the-fact check: `snapshot_count` is this account's qualifying count and `total_snapshots` is the event's denominator for that Eastern payout date.

## Implemented model

- Shared `pool_id` deduplicates advertised budget and allocation.
- `event_ticker` isolates uptime and prevents one recurring instance from inheriting another's observations.
- The conservative usable window is Eastern payout day intersected with contract effective time, published reward end, and the five-minute pre-expiry exit boundary.
- When reward activation is not published, Eastern midnight/effective time is the conservative start. First observation is stored as a separate optimistic bound and never relaxes admission.
- Sports contracts stop accepting new entries at the published game `startTime`, even if in-play trading remains open. Crypto interval events retain their intended live interval behavior.
- Flat lifecycle changes isolate and block only the affected symbol. Lifecycle change with inventory remains a global fail-closed condition.
- Recurring replacement symbols are not silently authorized: the current version-2 permit remains exact-symbol bound. Rollover requires a newly reviewed symbol/permit.

## Live read-only evidence

The initial ten-minute shadow completed with 31 pools, eight REST reconciliation brackets, a flat account at both ends, no mutations, no permit, and 0.45% average use of one logical core. It exposed two issues: event-driven public streams can be quiet longer than three seconds, and a noon contract transition halted the full runtime.

The repairs use WebSocket protocol ping/pong for quiet-stream liveness while retaining per-symbol sequence checks, and isolate flat contract lifecycle changes. A final 300.376-second authenticated read-only run then passed:

- 18/18 samples had all 29 selected books fresh;
- one candidate remained conservatively capable of reaching the 50% threshold;
- three bounded REST reconciliation cycles completed;
- account state was flat and matched at start and finish;
- zero order or cancel commands, zero activation permits;
- 0.546% average of one logical core;
- all connections closed and no matching process remained.

Evidence: `data/reward-timing-shadow-lifecyclefix-5m-20260915/preflight-report.json`. The interrupted and pre-repair shadows remain preserved as diagnostic evidence and must not be presented as passing runs.

## Verification

- 127 production tests pass.
- 28 script/observer tests pass.
- Python compilation passes on native Windows.

This verifies local timing and transport behavior, not venue-certified reward eligibility or payout. Exact denominator calibration still requires a completed authenticated daily-summary row.
