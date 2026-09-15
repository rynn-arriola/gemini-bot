# Bug fixes

## September 15, 2026 — event timing, quiet liveness and lifecycle isolation

- Reward uptime is keyed by event ticker plus pool ID. Shared pools remain one budget, while recurring events cannot inherit one another's timing observations.
- Usable time intersects Eastern payout day, contract effective time, reward end and pre-expiry safety. The unpublished reward start uses a conservative bound plus a separately reported first-observed bound.
- New entries cannot occur before contract effective time. Sports entries stop at game start even if in-play trading stays open.
- Event-driven public streams now use protocol ping/pong as their quiet liveness proof; sequence gaps and stale/failed pongs still fail closed.
- A flat contract lifecycle transition blocks and drains only that symbol. Open inventory at transition remains a terminal global safety condition.
- Verification: 127 production and 28 script tests pass. A five-minute authenticated read-only run kept 29/29 books fresh in all 18 samples, sent zero mutations and closed flat.

## September 15, 2026 — reward-day timezone proxy

- Symptom: the bot incorrectly claimed it had to wait until 20:00 Eastern because its local reward day rolled at midnight UTC.
- Cause: the public pool row omits a score-day start, and the initial conservative implementation chose UTC despite Gemini's authenticated daily-summary dates being defined in Eastern Time.
- Correction: use America/New_York calendar midnights expressed in UTC for durable keys. Zone-aware construction preserves 23/24/25-hour daylight-saving days. Reports now label fields `local_eastern_day_*` and still call the result a proxy.
- Verification: 119 production tests pass, including both DST transitions. A live read-only preparation changed current feasible candidates from zero to 13 without submitting orders.

## September 15, 2026 — commissioning uptime-proxy isolation

- Symptom: the maker acknowledgement/cancellation canary could not run because the local UTC-day reward proxy began late, even though the canary's purpose is execution mechanics rather than earning that day's reward.
- Correction: a new explicit flag may ignore only the exact uptime-feasibility rejection, only in a bounded 2–30 second one-leg canary. Reward reports continue to show the candidate as ineligible. Any second rejection, continuous run, or two-sided run remains blocked.
- Regression: offline tests prove the single rejection can be admitted for mechanics and that stale or otherwise invalid candidates cannot use the exception.

## September 15, 2026 — canary cleanup replacement loop

- Symptom: the first mechanics-only canary placed four sequential 10-contract BUY YES orders at $0.02 during its five-second window. Each rested briefly, canceled with all 10 contracts remaining, and produced no fill or position; final reconciliation was flat.
- Cause: candidate maintenance honored the commissioning-only uptime exception, while the independent working-order cleanup loop still checked reward `eligible_now` directly. It canceled the working canary order, and maintenance replaced it.
- Correction: cleanup now uses the same narrowly scoped actionable-candidate predicate as placement. Other ineligible or stale working entries still cancel.
- Regression: a working uptime-only canary order is retained; adding any second rejection causes cancellation. A new live canary is required before this path is considered verified.

## September 15, 2026 — one-sided startup and quiet-book freshness

- Symptom: a five-minute preflight synchronized all 32 books, but four naturally one-sided markets blocked global readiness; only 2–5 books appeared “fresh” because freshness incorrectly meant a price level changed in the last three seconds.
- Correction: require every tracked subscription to receive its initial snapshot, require at least one usable two-sided market, and exclude one-sided books from the pair strategy. Use recent activity on the single gap-checked public WebSocket for continuity while retaining per-symbol sequence state. Stale shared transport still blocks all entries.
- Related safety: one-sided inventory is marked at its executable exit side, or zero with no exit bid. Prepare-only transports now enforce no mutations/no activation and disable cancel-on-disconnect.
- Regression: live-shape one-sided, quiet, stale-stream, all-one-sided, conservative-risk, and read-only tests pass. Another live read-only preflight is still required.

## September 15, 2026 — overall review repairs

R01–R15 now pass regression verification. Repairs include fill/cancel/exit serialization, durable unknown-exit guards, first-fill deadlines, stream/freshness/expiry gates, canonical Windows ownership, amount-based cash matching, timely WS risk marks, cooldowns, atomic storage, pagination, terminal status normalization and conservative uptime. The maker canary now uses the production safety path. Full details, deliberate fail-closed boundaries and test commands are in the [repair report](docs/REPAIR_REPORT_2026-09-15.md). No live run was authorized or started by these repairs.

## September 15, 2026 — client order ID length

- Symptom: the first commissioning entry received WebSocket status 400 / code -1013 before placement.
- Cause: generated client order IDs were capped at 64 characters, while the live Gemini endpoint enforces 36.
- Correction: IDs are now 34 characters and retain purpose, timestamp, an eight-hex symbol hash, and eight random hex characters.
- Regression: the commissioning-envelope test enforces a maximum length of 36.
- State: corrected before any fill; REST reconciliation proved zero active orders and zero positions.

## September 15, 2026 — late IOC terminal event

- Symptom: after a successful entry and submitted exit, the commissioning consumer stopped on a late lifecycle event belonging to the already-terminal entry.
- Cause: the sequential waiter treated every non-current client ID as unowned, even when the durable mutation owner had created it.
- Correction: late events for any locally owned intent are reduced and ignored by the current-order waiter; genuinely unowned orders still halt the canary.
- Regression: ownership is now an explicit ledger query. REST reconciliation proved the entry and exit both filled for 10 contracts and the account ended with zero positions and zero active orders.
# Production completion fixes — 2026-09-15

- Replaced SQLite floating-point `MAX` risk persistence with Decimal-preserving monotonic calculations in the risk authority before exact-string storage. Added a restart test at sub-cent precision.
- Separated read-only configuration preparation from the exact live acknowledgement so a non-ordering reconcile cannot accidentally become activation.
- Added one-use, expiring, policy-hash-bound activation permits and proved pre-reconciliation, reused, expired and mismatched permits fail closed.
- Added REST/WebSocket account brackets, unowned-order rejection, durable fill IDs, account snapshots, restart-safe inventory deadlines, equity classification, six-hour cap ratchets and local reward-uptime records.
- Added sequential maker-leg acknowledgment, fill-priority cancellation, exact REST-reconciled position exits and emergency drain attempts. A failed drain is terminal and operator-visible rather than reported flat.
- Added atomic status/rank output, redacted rotating logs and native Windows start/STOP scripts with runtime paths constrained below `data`.
# September 15 live follow-up

- Repaired and activated: a two-sided replacement stopped the first process when its second maker leg advanced from `NEW` to `CANCELED` with zero fill before the waiter inspected it. The new path accepts only an identified terminal `CANCELED` order with zero cumulative fill, cancels its sibling, proves REST and WebSocket positions agree that the symbol is flat, and applies a five-second symbol cooldown. Rejection, fill, missing identity, disagreement, or ambiguity still causes account-wide fail-closed drain. Regression coverage verifies both the recoverable and fatal cases; the repaired process is active under a fresh date-bounded permit.
- Active and offline-tested: sports entry and durable position deadlines use published game start minus 20 minutes. Crypto interval markets retain their separate five-minute pre-expiry cutoff.
- Planned: ingest Gemini Statuspage upcoming/active maintenance and incidents as read-only annotations. Never alter the 50% denominator directly; calibrate against authenticated daily `total_snapshots`.
