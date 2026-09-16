# Bug fixes

## September 15, 2026 — availability and churn review (source only)

Bound cancellation/exit/drain waits end to end and start owned entry cancellation before slow exit joins. Correlate WS replies so known read throttles/service errors recover while mutation/auth/integrity failures remain closed. Separate recoverable lifecycle/reward holds, requiring current metadata and terminal/flat proof before releasing only those reasons. Recheck connection generation and terminal cancels after pacing; never replay attempted sends. Share safe retention criteria, reason-code quote cancellations and measure sub-clock either-/two-sided gaps. **463 tests passed (41 new)** plus three negative controls and offline release checks. No restart/deployment or guarantee of live churn reduction. [Full evidence](docs/AVAILABILITY_AND_CHURN_2026-09-15.md).

## September 15, 2026 — fill/exit continuity (source only, no restart)

V7 uses independent market exit owners and paced additional rounds only after known-terminal, fresh exact residual proof. Known thin/stale exit books and exhausted rounds no longer inherently drain unrelated markets. Fill cancellation bypasses unrelated lifecycle/exit waits while retaining the existing per-order deduplicated mutation owner. Exact flat proof atomically clears both completed outcome deadlines and only the fill hold, fixing net-flat paired fills and flat-during-cancel. Startup reconstructs those holds from durable deadlines.

Final flow review also added bounded same-symbol REST/WS convergence, obsolete fill-notification refusal, post-pacing round-deadline checks and a rejected-exit latch that emergency drain cannot bypass. Unknown mutations/cancellation/ownership, persistent accounting disagreement and integrity failures remain fail-closed. Stop/HARD joins existing workers before bounded forced drain; no automatic HARD reset. **394 production + 28 script tests passed**, with 45 new cases, three deliberate reversals and offline compilation/release verification. No live deployment or live acceptance. [Scope, reward impact and limitations](docs/FILL_CONTINUITY_2026-09-15.md).

## September 15, 2026 — continuous service and resting-time protection (not deployed)

Normal maintenance previously replaced an entire working pair on any target-price difference. V7 retains intact pairs that are still fresh, safely priced, qualifying and profitable under the existing model evaluated at actual resting prices. It still cancels unsafe/partial/ineligible pairs and places replacements at the exact two-tick target. This reduces avoidable gaps without promising improved rewards; wider retained prices can score less.

New explicit v7 authorization has no daily session-expiry shutdown, while the uptime exception still expires. Reward-program disablement/empty eligibility becomes standby. Transient reconnect/read-only startup outages may wait with paced retries; server retry deadlines survive runner boundaries. Known transient-flat exits may recover within a same-session burst limit; unknown/integrity/HARD remains blocked for attention. Source is offline-tested only and the owner prohibited restart. [Full evidence and limitations](docs/CONTINUOUS_SERVICE_2026-09-15.md).

## September 15, 2026 — overnight continuity

Version 6 separates session expiry from the one-day uptime exception: midnight ends the exception and refreshes the local day/ranking in the same process. The final send guard rejects stale day/override decisions. Versions 3–5 retain their drain behavior, and all permits still expire within 24 hours. Healthy quotes need not be canceled simply because the day changes.

Persist selected pool/event/symbol identities and prefer them during rediscovery/ranking. Bounded recurring discovery may replace expired/inactive owners only after authenticated flat and terminal-intent proof; filled owners retain their original deadlines. New subscriptions require fresh depth before quoting. This improves continuity but cannot recover missed snapshots or certify the exchange denominator. See [overnight release evidence](docs/OVERNIGHT_CONTINUITY_2026-09-15.md) and STATUS.md for deployment state.

## September 15, 2026 — bounded WebSocket recovery, not yet activated

Transport EOF/errors, depth sequence gaps and stale account snapshots now initiate bounded reconnect/reconciliation instead of an unconditional process stop. Added generation deduplication, no-replay guards, exact terminal-history checks and cancel-on-disconnect handling without duplicate cancellation. Storage/protocol/ownership/unknown-fill failures remain fatal. Original deadlines and 50%-uptime history survive; lost quoting time is not forgiven. Offline fault tests passed; natural live recovery remains unverified. [Boundaries and evidence](docs/RUNTIME_HARDENING_2026-09-15.md).

## September 15, 2026 — unreleased runtime hardening

Separate venue denominator/local cutoff; handle second-leg fills without unrelated drain; avoid periodic cash-race stops; atomically persist fill/expected state/deadline; unblock explicit manual-flat preparation; refresh current-owner reward metadata. Added typed periodic-read hold/backoff/resume and rotated lifecycle/order/heartbeat logs with stale-supervisor checks. **Not deployed; unexplained process disappearance is not claimed fixed.** [Tests and uptime effects](docs/RUNTIME_HARDENING_2026-09-15.md).

## September 15, 2026 — non-trading cash no longer trips realized-loss accounting

- Symptom: Gemini paid an $0.08 maker rebate after the SOL maker fills. Confirmed cash correctly rose, but the durable expected-cash ledger knew only fills, so the next launch stopped before quoting.
- Correction: authenticated maker-rebate payouts, liquidity-reward payouts, and bounded USD deposit/withdrawal history reconcile exact non-trading cash separately. Only completed matched execution fills and their fees feed six-hour realized P&L. Unknown or non-exact cash still fails closed.
- Rate/snapshot impact: transfer history uses Gemini's separate five-second REST lane; payout/transfer reads occur only during startup mismatch or a live unmatched-cash state. No quote, size, ranking, stop, or 50%-snapshot rule changed. Avoiding false payout halts can preserve uptime, but safety still wins.

## September 15, 2026 — maker fill race no longer drains every market

- Symptom: an XRP first quote filled while its opposite leg waited for transport pacing. The last-moment guard correctly blocked the second entry, but the generic rejection stopped the runner, canceled all pending orders, and forced an immediate exact exit.
- Correction: price/book races and ordinary entry-state races are explicit definitely-unsent outcomes. The bot cancels any remaining entry on that symbol and lets the existing fill-safety/deadline path own inventory. A first maker leg filled before its acknowledgement waiter is scheduled follows the same path. True global permission loss, stale account snapshots, scope/policy violations, and ambiguity remain fatal.
- Supervision: the CLI writes an atomic result bound to the supervisor session. The Windows supervisor requires that result and uses its code for bounded recovery; missing, malformed, cross-session, or unknown results are non-recoverable.
- Snapshot impact: the repair preserves other markets' resting time after a normal fill. The affected symbol loses uptime while its residual entry is canceled and inventory is managed. No reward, risk, price, size, breadth, or exit limit changed.


## September 15, 2026 — authenticated manual-close history repair

- A prior manual-flat audit correctly blocked the current six-hour risk window because two closing fills and their fees were absent from the WebSocket journal. The authenticated Predictions order-history endpoint contains the exact server orders, quantities and average execution prices, but its documented response does not expose execution fees.
- `reconcile-manual-flat` now locates only exact unmatched inventory at an existing manual-flat boundary, reads a symbol- and time-bounded `status=filled` history window, and accepts exactly one full SELL of the correct outcome and quantity. Missing, malformed, full-page or ambiguous results fail closed.
- Imported closes retain the Gemini order ID as durable identity. The loss ledger charges the published standard taker fee rounded up to the next cent as a conservative upper bound; maker execution or a promotion can only make the actual fee smaller. The import is idempotent and audited, and a reached loss high-water/HARD remains irreversible.
- The stale version-5 supervisor was cleanly dismissed. Its final recovery error came from the superseded global-history startup code; the current build already uses targeted unresolved-symbol history and a fresh preparation passed.
- Snapshot impact: no quote rule changes. The repair only removes a false restart block after exact authenticated manual-close evidence is reconciled; incomplete or ambiguous evidence still blocks all quoting.

## September 15, 2026 — follow-up review corrections

- Maker entries now recompute the current two-tick target immediately before socket send, after pacing. A moved quote is terminalized as definitely unsent; any sibling is canceled and the next rank cycle rebuilds it. A stale ranked candidate is skipped before preparing an intent.
- Urgent public-depth cancellation no longer waits on the placement/exit lifecycle lock or another market's cancellation confirmation. One worker per symbol and one shared cancellation task per order prevent duplicate mutation. Both sibling cancels are issued promptly through the paced transport. The private send queue prioritizes cancels without holding its lock through cooldown sleeps.
- Shutdown uses the same targeted unresolved-intent lookup as startup. A flat account with no unresolved intents never scans complete order history, so the 1,000-row history ceiling no longer breaks a clean drain.
- A manual-flat audit with unmatched fill basis now makes the affected risk window incomplete. Activation blocks before permit consumption; live detection stops entries immediately. Reconciled closing evidence may restore completeness while preserving all reached loss high-water/HARD values. No missing execution or fee is invented.
- Snapshot impact: fresh-price rejection and genuine safety cancellation can create short unquoted intervals; replacement waits for terminal proof. Faster independent cancellation reduces exposure but provides no guaranteed latency or reward eligibility. Missing manual-close evidence intentionally blocks quoting in the affected window until reconciled.
- Owner-approved WebSocket follow-up: switch public subscriptions/resync to `@depth@100ms`, discover actual buckets via `conninfo` before subscriptions on each socket, update from live response counters, reserve cancel headroom, and pace connection attempts. Missing/unknown limits fail closed. A 350 ms terminal-event wait avoids immediate REST cancellation polling; timeout still uses the bounded REST proof path. Conservative method-weight reservations and a local maximum rate remain because complete method costs are not published.

## September 15, 2026 — realized HARD, fixed breadth and pre-send exit race

- The six-hour meter incorrectly let open executable marks ratchet pending orders from 30 down to 20. Owner policy now keeps 30 pending entry slots below one $30 HARD and counts only completed matched bot fill lifecycles plus recorded fees. The legacy marked-equity table is retained as history; a separate realized-risk table prevents silent reinterpretation.
- Both deadline exits actually filled and flattened, but a retry was prepared while REST/position snapshots lagged. The last-moment guard correctly rejected the now-stale exact quantity before `websocket.send`; the transport incorrectly left that definitely-unsent intent ambiguous. Guard rejection now terminalizes the local intent, and exit control waits up to five seconds for REST/WebSocket agreement before deciding flat, retryable residual, or unsafe disagreement.
- A dedicated public-WebSocket dirty queue now triggers fast cancellation when a working entry moves inside the approved two-tick distance. It does not churn an order merely because the touch moved away. Normal sticky replacement remains serialized after terminal cancellation, preserving reward uptime when safe.
- Startup no longer scans global order history, which exceeded the 1,000-row safety ceiling during recovery. It reads only the newest symbol-filtered page for unresolved intents; a full page that omits a target remains ambiguous and blocks startup.

## September 15, 2026 — terminal IOC residual and manual-close recovery

- Symptom: the first five-minute live exit submitted an exact IOC SELL, received a terminal `CANCELED` event with zero execution, then timed out because the position remained open. The symbol stayed marked ambiguous, so emergency drain refused a second reducer even though Gemini had positively terminated the first order.
- Correction: distinguish terminal-unfilled/partial IOC outcomes from ambiguous sends. Retry at most three times within 30 seconds, only after REST and authenticated WebSocket agree on the same-sign, non-increasing exact residual; re-read fresh exit depth before each attempt. Rejection, missing terminal proof, sign reversal, increased exposure, stale depth, or state disagreement never retries.
- Manual recovery: `reconcile-manual-flat` adopts an operator-confirmed external close only after fresh flat/order-free REST and WebSocket proof and terminal bot intents. It clears stale inventory deadlines but preserves the six-hour risk-window rows and records an audit event without writing balances to logs.
- Supervision: the Windows live launcher now starts a single supervisor. A version-5 permit authorizes at most two same-session recovery restarts, and the prior runner must durably mark itself recoverable only after successful account-wide drain. Cross-session reuse, active/nonflat state, ambiguous exit, the third fast failure, or exhausted authority enters `ATTENTION_REQUIRED`; a final read-only preparation records current venue health.
- Regression: terminal zero-fill retry, partial-fill exact residual, rejected/ambiguous no-retry, manual-flat refusal/success, deadline cleanup, durable restart state, and loss high-water preservation are covered offline.

## September 15, 2026 — reward-only ranking replaced by profitability v2

- Symptom: fixed two-tick candidates often had identical quote-quality and capital proxies, so pool dollars and coarse maker count dominated ranking while fill/exit inputs remained unpopulated placeholders.
- Correction: subscribe to bounded public trade telemetry, resolve active maker-rebate rules once at startup, estimate conservative reward share and execution cost, then rank positive-net candidates per capital-hour. Duplicate trade IDs are ignored across restart and healthy cohorts are sticky.
- Safety: quote distance, 10-contract legs, 15-pool/30-order breadth, exposure gates, one monetary stop, and exact five-minute exits are unchanged. Sub-$1 estimated payout and non-positive net remain ineligible even during the date-bounded uptime exception.
- Post-activation audit found that normal depth frames updated the local book but were not copied into v2 telemetry because the updated-symbol return value was named and tested as a gap. Normal synchronized updates now populate bounded minute buckets; actual sequence-gap behavior is unchanged and still halts for reconciliation.

## September 15, 2026 — transient cancellation-proof timeout

- Symptom: the prior live process halted while canceling stale working entries; a private order-history read timed out, then the same immediate failure prevented emergency drain from completing its local proof.
- Correction: accept authenticated WebSocket terminal order state first. After a cancel has been sent once, retry only bounded active/history reads for retryable transport/server failures; never resend the cancel mutation blindly. Limit the history fallback to the newest page and retain a hard confirmation deadline.
- Live read-only reconciliation after the halt proved zero active orders and zero positions before restart work continued.
- The next launch attempt then failed safely before activation on another order-history timeout. All idempotent REST reads now receive at most two backoff retries, and every retry is admitted through the shared rate governor. Rate-limit/ban responses still install their venue cooldown and are not immediately retried.
- Private retry attempts rebuild their signed request with a current timestamp nonce; they never replay the authentication payload from a timed-out read.
- The following activated attempt exposed a separate pagination bug: asking the complete-history helper for a 100-row ceiling correctly reported that the result might be truncated, but cancellation treated that as an error. Terminal lookup now uses Gemini's symbol filter and one newest page at the documented 1,000-row maximum; a full page is valid lookup input rather than an overflow failure.

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
