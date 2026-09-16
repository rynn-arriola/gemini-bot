# Gemini findings

- September 15 fill-flow audit: one market's exit failure can still propagate to global drain; shared lifecycle locking can delay another fill's entry cancellation. Synthetic offline reproductions confirm net-flat paired fills and flat-during-cancel retain stale blocks/deadlines. Not repaired or live-accepted. Fresh production suite passed 349 tests; current v9 logs had no fills at the approximately 21:58 ET check. Polymarket was inspected read-only. [Audit, priorities and limits](docs/FILL_FLOW_AUDIT_2026-09-15.md).

- September 15 continuous-service source verification: **349 production + 28 script tests passed**, including 47 new continuous-service cases and a native PowerShell harness that mocks all child launches/sleeps. Compilation, PowerShell parsing, offline Windows policy/storage validation and whitespace checks passed. Quote retention includes actual resting-price profitability/payout re-evaluation; extended transport waits do not relax terminal/ownership proof. Local operational gap estimates remain distinct from exchange snapshots. **Not deployed: owner prohibited restart.** A read-only check at 21:39:55 ET found the unchanged v9 supervisor/runner alive and quoting, with restart count zero; no venue flatness or new-build live behavior is inferred. [Release evidence and limits](docs/CONTINUOUS_SERVICE_2026-09-15.md).

- September 15 overnight deployment: old v8 runner drained flat and its supervisor stopped cleanly at 21:13 ET. Preserved 14 working/position market identities, with loss history unchanged. Fresh read-only preparation passed (22 tracked, 20 ranked, two normally eligible); v6 build `7509656` then reached fresh quoting heartbeats at 21:15:56 ET with 28 pending orders, zero positions, no holds/errors/critical logs or dropped logs. Override ends midnight September 16; the separate session expiry is 21:14:47 Eastern September 16. Natural midnight/rollover remains unobserved.

- September 15 overnight continuity: 302 production and 28 script tests passed, including 25 new midnight/identity/rollover cases. Version 6 can end the override without stopping the session; stale-day sends fail closed, unchanged healthy orders survive the day transition, and inventory/deadlines/loss history remain intact. The earlier same-pool switch reproduction concerns the generic ranker; production discovery already selects one representative per pool per load. Durable identity now also protects rediscovery across restart. [Release evidence](docs/OVERNIGHT_CONTINUITY_2026-09-15.md); see STATUS.md for actual deployment, which is separate from these offline results.

- September 15 restart-readiness follow-up: 19:59:45–20:04:45 ET read-only preflight passed. Authenticated account flat at start/finish; all 29 books fresh in 17 samples, matched cash/positions, two eligible candidates, verified public/private WS quotas, no mutations/gaps/dropped work. Observer CPU averaged 0.38% of one core, not a live-trading CPU benchmark. Final source: 277 production/28 script tests. Recovery faults remain synthetic; process disappearance remains unproven. [Report](data/preflight-20260915-stream-recovery/preflight-report.json).

- September 15 runtime follow-up: both old processes are absent, the saved 18:29 ET heartbeat is stale and no durable result/log explains termination. New conservative windows, scoped recovery and logging are prepared, not deployed. [Evidence and remaining uncertainty](docs/RUNTIME_HARDENING_2026-09-15.md).

## September 15, 2026: complete Events REST reference scan

- Followed all seven official Events links, the generated `Event`, `Contract`, `Strike`, `EventsResponse`, sports-taxonomy and pagination schemas, core concepts, and the raw OpenAPI paths. The stable details are consolidated in [the API reference](GEMINI_PREDICTIONS_API_REFERENCE.md).
- The API supports complete paginated listing, exact event lookup, strike lookup, flat category discovery, and 24-hour newly-listed/recently-settled plus approved-upcoming feeds. General/specialized lists cap `limit` at 500; sports filters OR repeated values within a field and AND across fields.
- Published timing fields do not reveal reward activation. `createdAt`, `effectiveDate`, `expiryDate`, `resolvedAt`, strike `availableAt`, and reward `ends_at` have distinct meanings. Live event responses currently add `startTime`/`isLive`, but those are absent from the published `Event` OpenAPI schema and still are not reward-start fields.
- A live MLB sample made the separation concrete: event/contract `effectiveDate` was September 14, `startTime` was the September 15 game time, and `expiryDate` was September 16. Therefore neither effective time nor expiry is a safe substitute for sports start, and sports entry/exit logic must use a validated `startTime` with a conservative missing-field fallback.
- Read-only public endpoint validation at 19:12 EDT exercised every collection endpoint, exact-event lookup and a 5-minute BTC reference-strike lookup. Live responses contained additional undocumented event/contract metadata. No authenticated request, order, cancellation, process restart or runtime behavior change occurred.
- Snapshot impact: documentation only. Event discovery remains REST metadata transport; WebSocket remains the live book/status path. No ranking, pool rotation, quote, stop, or 50%-uptime rule changed.

## September 15, 2026: exchange uptime reporting implemented, not deployed

- New `ExchangeRewardUptime` reports authenticated qualifying/total snapshots, percentage, scoring date and fetch time. It matches numeric event IDs from event discovery, never titles or pool IDs. It does not replace local future-feasibility estimates or affect ranking/admission/stop decisions.
- Continuous activation starts one background today/yesterday REST refresh every five minutes. It shares the existing rate governor, uses a separate short-timeout read lock to avoid holding the safety REST lock, respects server cooldowns, bounds its cache, and cancels on shutdown. Prepare-only and short commissioning runs do not start this polling.
- Empty rows, missing/ambiguous IDs, zero denominators, fetch failures and stale data show unavailable. Yesterday's report remains dated historical evidence, never today's percentage. Gemini supplies no scoring-through timestamp: per-minute cadence does not justify inventing one from fetch time or total count.
- Verified 17 dedicated tests plus full 231 production/28 script suite, compilation and diff checks. Authenticated read-only smoke at 18:41 EDT succeeded with no summary rows. No live-count calibration is claimed.
- Snapshot impact: reporting only; no intended quote churn, pool rotation, risk-policy change or new unquoted interval. REST adds one scheduled query per five minutes plus bounded existing retries, under shared limits. Working source changed but the live process was not restarted; its old build-bound permit cannot authorize this new build.

## September 15, 2026: reward timing verification remains incomplete

- Checked official prose, raw OpenAPI, changelog, live public reward/event responses, authenticated daily summaries and general web searches. [Evidence and unsent support questions](docs/REWARD_TIMING_VERIFICATION_2026-09-15.md).
- SOL 18:00 EDT sampled contract has effective time 17:00 EDT, while the event listing/effective timestamp is earlier. Neither proves the reward-activation timestamp; assuming midnight eligibility for this contract is unsupported.
- Live hourly/15-minute/5-minute SOL rows share pool 55. Shared budget identity does not establish shared 50% uptime aggregation. Published schema omits the live pool/recurrence fields.
- September 13–15 authenticated daily summaries were empty: no exchange snapshot denominator available. Sub-$1 rows can be suppressed, so missing rows are not proof of zero participation. Payment schedules are 17:00 ET maker rebates and 17:30 ET liquidity rewards, not proven scoring resets.
- No bot logic, policy, order or process changes. Exact scoring boundaries/aggregation still need Gemini confirmation or authoritative snapshot evidence.
- Representative follow-up at 18:06–18:07 EDT inspected 24 reward events/276 contracts across five categories. Every sampled contract had effective/expiry timestamps; no reward row supplied a reward-start timestamp. SOL and BTC both showed 5/15/60-minute windows, while other contracts spanned days/months. When available, matched exchange `snapshot_count / total_snapshots` supersedes time-derived observed-uptime estimates; missing rows remain unknown and partial counts do not prove final eligibility.

## September 15, 2026: $0.08 maker rebate and cash classification

- Authenticated payout history identifies one paid $0.08 maker rebate from two SOL maker fills totaling $10.90 of volume. It is separate from liquidity rewards.
- The rebate is non-trading cash. Current six-hour realized P&L remains -$0.28 because that meter reads only completed matched fills and recorded trading fees.
- Gemini's authenticated `/v2/transfers` feed can classify bounded USD deposits and withdrawals. The bot uses the documented one-request-per-five-seconds lane and rejects full/ambiguous history.
- Commit `bcc5e18` passed authenticated preparation and entered supervised live quoting. The verified heartbeat returned to 30 pending orders with zero positions and no error/critical logs.

## September 15, 2026: 17:00 fill race and fail-safe drain

- Gemini filled BUY YES 10 on the XRP 21:00 September 16 contract at $0.81 while the paired NO leg was waiting for its private-WebSocket send slot. The pre-send guard correctly did not transmit the stale second leg.
- The guard's ordinary account-transition rejection was wrapped as a generic failure, so orchestration drained the whole account. It canceled every pending entry and sold XRP YES 10 at $0.77; final authenticated state was flat/order-free. The round trip was -$0.49 including the recorded $0.09 fee.
- Current-window realized P&L is -$0.28 after all matched lifecycles, including the previously authenticated manual closes. Classification is complete and the 30-order cap remains available below the $30 HARD.
- A dedicated regression reproduces the second-leg race and fails under the old handler. The repaired handler cancels affected-symbol entry remnants without halting unrelated markets; full offline verification passes 205 production and 28 script tests.
- The runner durably recorded `recoverable_flat`, but the old supervisor surfaced exit code zero and did not use its bounded restart. A same-session atomic runner result now carries the classification; malformed or missing results fail closed.


## September 15, 2026: manual-close reconciliation and restart readiness

- Authenticated, symbol-bounded history uniquely matched the earlier manual closes: SELL NO 10 on each of the exact ETH and SOL contracts, both fully filled before the manual-flat audit. The server supplied exact average prices but no fee field.
- The risk ledger imported the two server orders with the published standard taker-fee calculation rounded against the account. This is deliberately conservative, not a claim about the actual fee charged.
- Full fill replay now reports conservative current-window realized P&L of +$0.21, complete classification, zero manual-close gaps, a 30-order cap and a clean SQLite integrity check. A fresh authenticated preparation remained flat/order-free and found 32 tracked, 29 ranked and five currently eligible markets.
- The superseded supervisor was in `ATTENTION_REQUIRED` because its older recovery subprocess used global order-history pagination and hit the 1,000-order ceiling. It was dismissed cleanly; no runner or mutation owner remained.
- After commit `8260f95` and a final flat/order-free preparation, the owner-authorized version-5 launch reached 30 resting entry orders, zero positions, 32 tracked symbols and 29 ranked pools. Supervisor and runner reported healthy active states with no dropped, error or critical log records at the verification point.

## September 15, 2026: second production fill/exit evidence

- The supervised run filled BUY YES 10 on one ETH and one SOL hourly interval. Their five-minute SELL YES exits both filled completely; the account snapshot ended flat/order-free. Recorded round-trip P&L was approximately -$2.42 including fees.
- The old $13.10 risk reading was not closed trading loss; it came from open-position executable marks. The two completed lifecycles are the correct durable realized evidence under the owner's replacement policy.
- A filled IOC can flatten after one REST view still reports the former position. Preparing a retry before that convergence is safe only if the final pre-send guard can prove non-send and control then waits for fresh REST/WebSocket agreement. The prior code proved non-send but persisted ambiguity anyway.

## September 15, 2026: first production fills and exit-recovery defect

- The live reward-catalog run filled BUY NO 10 on one ETH and one SOL interval contract. The ETH five-minute exit sent SELL NO 10 @ $0.90 IOC; Gemini terminated it `CANCELED` with zero filled. No SOL exit was attempted because the runtime halted on the first symbol.
- The send was not ambiguous. Gemini documents that IOC orders finish with `CANCELED` and execution quantity determines whether they filled. The defect was local: terminal zero-fill was treated as failure-to-prove-flat, then the retained ambiguity latch prohibited emergency drain from retrying the known residual.
- The owner manually closed both positions. Subsequent authenticated REST plus WebSocket preparation proved zero positions and zero active orders. The supported manual-flat command adopted that truth, cleared two stale deadlines, left all prior bot intents terminal, and preserved the existing risk window.
- Gemini's `c` balance-stream field is documented as confirmed balance and `f` as available balance, confirming the runtime mapping used during reconciliation.
- Post-repair activation from pushed commit `6f52ed2` passed a fresh authenticated preflight and entered supervised quoting. A bounded REST check found 20 active orders and all 20 prices exactly equal to the latest rank report's two-tick targets; the account remained position-free at that checkpoint.

## September 15, 2026: profitability ranking v2

- The prior live top-15 candidates all had the same 8.96 quote-quality proxy and $9.50 reservation proxy at the fixed two-tick placement. Selection therefore mostly reduced to pool budget divided by coarse maker count; it did not subtract execution costs.
- The preserved observer had 450 uncensored intervals at each tested offset and no possible trade touches. That is insufficient to claim zero fill risk, so v2 reserves a cold-start fill and learns only from bounded public depth/trade telemetry.
- V2 ranks conservative reward share less fee/rebate, adverse-move and exit-depth costs per capital-hour. Healthy incumbents remain sticky. Authenticated fills, rewards and fees—not the proxy—will determine whether it is profitable.
- The current process reached 30 working maker orders across the 15-pool cap with zero positions, ranking version 2, advancing minute buckets, zero dropped/error logs and an empty launcher stderr. Each immediately preceding healthy process was deliberately drained to authenticated `stopped_flat` before replacement.
- Live launch exposed and repaired two independent REST recovery defects: retryable read timeouts lacked bounded retries, and complete-history pagination treated a deliberately small ceiling as a fatal truncation during terminal lookup. Both failed closed; later authenticated preflights proved zero active orders and positions.

## September 15, 2026: reward-day proxy corrected to Eastern

- The owner directed removal of the unsupported midnight-UTC assumption. Local reward feasibility now uses America/New_York calendar dates, consistent with Gemini's authenticated daily-summary API. Day boundaries are zone-aware and tests prove 23-hour spring, 24-hour normal and 25-hour fall days.
- Rank-report fields are renamed from `local_utc_day_*` to `local_eastern_day_*`; historical reports retain their original fields as evidence of the old behavior.
- 119 production and 28 script/observer tests pass, plus compilation and native Windows policy/storage validation.
- An authenticated read-only preparation at 11:26 Eastern tracked 32 markets and ranked 28 pools; 13 candidates were feasible under the corrected 50% gate. Their best possible local ratio was about 52.29%. No order was sent and no process remained.
- Eastern is better supported than UTC but remains a proxy: Gemini documents daily scoring and Eastern summary dates without publishing the precise eligible-snapshot boundary.

## September 15, 2026: repeat one-leg maker canary — passed

- After the cleanup-loop repair, the owner approved a fresh five-second test of `GEMI-ZEC2609152100-HI1220`, BUY YES 10 @ $0.02. The exact price was asserted at runtime.
- Exactly one new durable intent and one Gemini history row were created. The order rested from 15:18:49.750Z to 15:18:54.996Z (approximately 5.25 seconds), then terminally canceled with all 10 contracts remaining.
- There were zero execution fills, positions, active orders or dropped log records. Authenticated drain reached `stopped_flat`; no matching process remains. The one-use repeat permit is consumed.
- This verifies one-leg maker acknowledgement, stable resting behavior, cancellation and final flat reconciliation. It does not exercise a fill or exact position exit and does not authorize continuous trading.

## September 15, 2026: first one-leg maker canary — flat but failed one-order invariant

- The authorized mechanics canary targeted `GEMI-ZEC2609152100-HI1220`, BUY YES 10 @ $0.02, for five seconds. Gemini authenticated four sequential orders, each canceled with the full quantity remaining. No fills, fees, positions, or active orders remained; the runtime ended `stopped_flat`.
- The test is not accepted as a commissioning pass because exactly one order was intended. Candidate maintenance allowed the mechanics-only uptime exception, but independent cleanup canceled each reward-ineligible order and maintenance replaced it.
- Cleanup now uses the same narrow actionable-candidate predicate. Offline regression verification is required, followed by a newly approved one-use live canary. Continuous trading remains blocked.

## September 15, 2026: one-market maker-test preparation

- The owner directed work to advance through the preparation boundary. Production `prepare-live` completed read-only at 10:57:01 Eastern, reconciled a flat account with zero active orders, tracked 32 symbols, ranked 27 unique pools, wrote paused status/rank evidence, and disconnected. No permit exists and no order/cancel was sent.
- Historical behavior: zero candidates were eligible because only 37.64% of the former local UTC-day proxy remained. This unsupported boundary was subsequently replaced by the Eastern-date proxy.
- Post-preparation verification passed: 113 production tests, 28 script/observer tests, offline Windows policy/storage checks, compilation, and PowerShell launcher parsing. Configuration still lacks the exact live acknowledgement, so activation fails closed.

## September 15, 2026: repeat five-minute read-only preflight — data health passed, schedule gate held

- Completed 300.447 seconds at 10:51:09 Eastern. All 32 tracked books synchronized and were accepted as current through the shared sequenced WebSocket; 25–27 were two-sided. The original global one-sided-book and quiet-book freshness failures did not recur.
- Public streaming delivered 615 depth frames with no sequence gaps or dropped work/log records. Private streaming delivered 249 balance and 249 position snapshots; three periodic REST reconciliations matched the account view. CPU averaged 0.411% of one logical core.
- Zero orders, cancels, blocked mutation attempts, or permit consumption occurred. The account was flat at recorded endpoints, both sockets closed, and no matching process remained.
- The diagnostic still returned `blocked`: a new local UTC-day proxy store started at 14:46 UTC, when only 38.33% of that historical proxy day remained. This was not a data-health defect; the unsupported UTC boundary was later corrected to Eastern.
- Full evidence: [repeat preflight report](docs/PREFLIGHT_2026-09-15-POSTFIX.md). Continuous live trading remains unapproved.

## September 15, 2026: preflight blockers repaired offline

- Startup now requires snapshots for all tracked books but only one usable two-sided market; one-sided markets are isolated from the pair strategy. Quiet books use current shared-WebSocket continuity rather than last-price-change time. A stale shared stream and any per-symbol sequence gap still fail closed.
- Risk marks now value held inventory at the executable exit side and use zero if that side is absent, so a one-sided book cannot freeze the six-hour high-water stop. Read-only transports cannot mutate or activate and use `cancelOnDisconnect=false`.
- 113 production and 28 script/observer tests pass. No network connection, order, cancellation, permit or process launch occurred for this fix. A repeat read-only preflight remains required.

## September 15, 2026: five-minute authenticated read-only preflight — blocked

- Completed 300.516 seconds at 10:34:43 Eastern; 32 markets observed, account streams/reconciliation healthy, no sequence gaps or throttle failures, 0.411% average of one logical CPU core. Zero order/cancel commands, no permit consumed; all connections closed.
- Production readiness timed out because four selected books had only one side, while startup requires all 32 to be two-sided. Only 2–5 books passed the three-second last-depth-change freshness rule at each sample. These real-feed conditions were not covered by earlier passing offline tests.
- Full evidence and limits: [preflight report](docs/PREFLIGHT_2026-09-15.md). Trading code was not changed to bypass the failure. Live activation remains blocked pending new repairs and another preflight.

## September 15, 2026: review repairs verified offline

- 106 production tests plus 25 observer tests pass; this includes all 15 previously failing review regressions and realistic late-fill, exact-exit, transient-loss, cash-ordering, approval, storage, and fake-canary integration cases.
- Native Windows offline release, Python compilation and launcher syntax checks pass. No Gemini process was found at the final process check. No authenticated account call, live mutation, or activation occurred in this repair turn.
- [Repair evidence](docs/REPAIR_REPORT_2026-09-15.md) supersedes the pre-repair implementation conclusions below. Unknown cash/settlement/collateral flows halt; this is not proof of complete cash-flow automation or live readiness across all Gemini account programs.

## September 15, 2026: overall code review — launch blocked

- Reproduced 15 defects offline (14 assertion failures and one runtime error) in `tests/test_review_regressions.py`; existing 68 production plus 25 observer tests still pass. The previous readiness statement missed integration defects.
- Confirmed failures include normal-depth resubscription loops, partial-fill second-leg submission, stale/wrong-quantity and repeated ambiguous exits, missing deadlines, clean socket closure, directory-scoped rather than account-scoped locking, timing-only cash classification, missing throttle cooldowns, expired-contract entry, non-atomic snapshots, truncated REST history, cancelled-status mismatch, and optimistic uptime denominators.
- Full evidence, severity, source locations, additional gaps, and repair order: [code review](docs/CODE_REVIEW_2026-09-15.md). This review added tests and documentation only; runtime behavior was not modified and the bot remains stopped.

## September 15, 2026: Windows live architecture foundation

- Owner-authorized aggressive LIMIT+IOC round trips validated both directions on current reward contracts. YES used BUY 10 at $0.03 then SELL 10 at $0.02. NO used BUY 10 at $0.06 then SELL 10 at a $0.05 average. All four orders filled completely; final authenticated REST reconciliation showed zero positions and zero active orders, and the journal had no unknown intents. Total gross spread cost was $0.20 before any fees. The fully filled NO IOC was reported historically as `cancelled` with filled quantity 10, confirming that executed quantity—not terminal status—must determine fills. These tests validate Gemini's same-outcome SELL reduction paths, not reward eligibility, maker uptime, or profitability.
- Live commissioning established an empirically enforced 36-character maximum for `clientOrderId`: a longer ID received status 400 / code -1013 and created no order or position. The generator and regression test now enforce the observed limit.
- The user chose a native-Windows live product, 10-contract base per reward leg, and a 30-pending-order ceiling distinct from actual positions. Later explicitly authorized bounded live sessions did occur; their fills and recovery evidence are recorded above and in `STATUS.md`.
- The useful Polymarket lessons are ownership, restart durability, cancel-confirm-exact-exit ordering, time-aware inventory reduction, fail-closed state, and disciplined documentation. Gemini-specific scoring, two-outcome order representation, 50% daily uptime, shared pools, WebSocket protocol, limits, and settlement remain independent designs.
- Production core now passes 49 deterministic tests. The preserved observer suite still passes 25 tests. Coverage includes shared-pool deduplication, eligibility, stale books, impossible uptime, sequence gaps, MOC and IOC envelopes, entry/exit purpose separation, all four side/outcome deltas, ambiguous-send retry refusal, partial/duplicate/terminal fill semantics, signed-position exact YES/NO exits, complete-position snapshots, haircut-adjusted depth refusal, activation reconciliation, 30-order capacity, six-hour Eastern risk windows, timed/expiry exits, server cooldowns, coalescing, durable intents/deadlines, and native Windows process ownership.
- Ranking uses Gemini's known rules but intentionally calls its local curve a `quality_proxy`. The exact quadratic spread function and final competing daily scores are not public, so priority cannot honestly be labeled expected dollars.
- The overnight observer was stopped at 08:02:29 Eastern after 24,599 successful snapshots from the hardened run. It averaged 0.503% of one logical CPU core; five connection errors recovered; 51 samples were missed; no throttles or orders occurred. The final report and SQLite database are preserved.
- A production read-only differential-depth smoke exposed that live Gemini depth frames lowercase `s` even when discovery/subscription symbols are uppercase. Without canonicalization, this creates two market states and leaves the subscribed book unusable. Symbols are now canonicalized at every domain/transport boundary and the live smoke reaches a synchronized book.
- Entry/exit reduction follows Gemini's asymmetric `Z` semantics: last-execution quantity on fill events, cumulative executed quantity on terminal events. It deduplicates fill trade IDs, keeps canceled orders separate from filled positions, and derives closes from the authenticated signed position rather than the intended entry quantity.
- A September 15 authenticated read-only handshake returned HTTP 401 with a millisecond nonce and succeeded immediately with a Unix-seconds nonce, despite the public authentication page saying either is accepted. Production now uses monotonic seconds and must pace reconnects to avoid moving its nonce too far ahead of server time.

## September 14, 2026: recovery hardening approved and tested

- User requested edge-case auditing, explicitly excluded laptop concerns, then authorized the recommended protections and a restart. No power-plan or laptop configuration was changed.
- Twenty-five offline tests passed, including simulated throttling, longer server cooldowns, explicit bans, storage lock recovery without duplicate rows, atomic status-write retries, bounded sampling with fair rotation, bounded buffering, metadata retries and supervisor preservation of cooldowns/pending rows.
- The new run preserves the existing fifty-contract cohort using its recent discovery file, and refreshes metadata shortly after startup. The earlier run was gracefully stopped before launching the new process. All earlier files are preserved.
- Recovery does not manufacture missing observations, bypass rate limits, enable trading, protect against OS process termination, or guarantee lossless storage during a sustained outage. Pending and dropped record counts remain explicit.
- Current run directory, verification and process are recorded in [STATUS](STATUS.md). Morning review now covers all three observation stages and their real windows.

## September 14, 2026: authorized 50-market paper expansion

- Expanded observer verified live at 23:40:39 Eastern: 50 contracts across 25 distinct reward pools, 100/100 successful observations over two cycles, no recorded errors or throttling, one WebSocket connection. Two primary quote geometries were ineligible across those observations; they are not silently replaced.
- Average observer CPU measured 0.429% of one logical core (excluding startup discovery); working set approximately 41.5 MiB at the check. These are early measurements, not an overnight performance guarantee.
- Preserved original ten-market data and requested its graceful stop after verifying the expanded process. New primary directory is `data/overnight-expanded-20260914`; morning review is updated to inspect both runs and exclude overlapping duplicate evidence.
- REST retains 1.1-second spacing. Expanded steady-state WebSocket depth traffic is 50 requests/minute, paced at 0.55 seconds minimum, plus bounded connection setup requests. Seventeen tests passed including throttle handling. Read-only/no-order restrictions are unchanged.
- Pool-first diversity is selection for research, not a verified reward-yield ranking; multiple contracts in one pool do not multiply its advertised budget.

## September 14, 2026: terms accepted and observer verification

- Superseding the earlier prerequisite status below: at approximately 23:17 ET the read-only account probe verified latest Predictions terms accepted. Authenticated streaming delivered balance, position and order updates. The probe did not place an order.
- Twelve observer tests passed. The bounded smoke test ended on its deadline at 23:27:46 ET with 40/40 successful snapshots, no recorded errors, one connection and 0.233% average CPU usage measured against one logical core (excluding discovery).
- Public REST limits and live public WebSocket `conninfo` buckets are recorded in [API limits](docs/API_LIMITS.md). Request-weight capacity is not raw request capacity or proof of account trading entitlement.
- The observer is public-data-only and contains no trading routes. Two ticks behind both best prices is the primary research candidate, not a guarantee of safety or maximum reward score.
- See [current status](STATUS.md) for the actual overnight run and morning review. Earlier entries below describe their historical checkpoint, not current readiness.

## September 14, 2026: replacement key verified

- User supplied replacement named `new primary`, saved as active local credentials; previous account key preserved in an ignored backup.
- At approximately 23:14 ET, authenticated REST terms status returned HTTP 200 with `hasAcceptedLatest=false`.
- Authenticated WebSocket connected and acknowledged the read-only subscriptions. During the bounded 15-second observation it delivered 15 `positionReport` and 15 `balanceUpdate` frames. No order-event delivery was observed in that interval; no trading permission or order-execution test was performed.
- Public WebSocket depth also returned status 200 with a valid order book.
- Key authentication and balance/position streaming now work. Latest Predictions terms remain unaccepted. No account terms were accepted and no trades were placed by the test.
- Overnight collector remains pending implementation/start. User requires WebSocket priority, REST only for unavailable information/fallback, and low CPU use. This successful key check does not mean the overnight test is running.

## September 14, 2026: rewards review

- **Previous budget claim corrected:** $196,150/day double-counted shared category pools. A later public capture at 22:51 ET contains 568 events but only 43 distinct pools totaling $8,250/day. Official website code corroborates shared-pool grouping. This is advertised venue budget, not bot income.
- **Program coverage:** liquidity rewards, maker rebates, temporary maker-fee waiver, taker rewards, contracted market making and collateral return. The preferred research direction is small maker quoting; realized superiority over Polymarket US is unproven.
- **Strategy mismatch:** a six-hour pregame window may fail Gemini's 50% eligible-snapshot uptime requirement. Exact denominator and shared-pool scoring need clarification.
- **Public config is partial:** only enablement, maximum spread and minimum payout were returned; do not assume minimum size/cap/multiplier/uptime arrive through that endpoint.
- **Account credential follow-up:** the user supplied a replacement account-scoped key, now saved as the active local credential. The previous master key is retained in a separate ignored backup. Time-based nonce settings, roles, creation completion and authentication remain unverified; no authenticated request or connection test was performed. No credential values belong in Markdown.
- **23:10 ET connection retry:** the saved account-scoped key returned HTTP 400 / `InvalidApiKey` on the read-only terms-status endpoint and HTTP 401 on authenticated WebSocket connection. Public WebSocket depth returned HTTP-style status 200 with a valid book. The settings screenshot shows Trading and time-based nonce selected; key activation and actual permissions remain unproven. The connection-check script and isolated Python environment are prepared; the overnight collector has not yet been implemented or started. No order/account mutation was made.
- **Next work:** read-only pool/book scorer with explicit estimation uncertainty, then an independently authorized bounded trading evaluation. No Gemini live strategy or allocation selected; Polymarket runtime was not touched.

Evidence and full proposal: [Rewards strategy review](docs/REWARDS_STRATEGY_REVIEW_2026-09-14.md). Public data: [snapshot](docs/history/REWARDS_PUBLIC_SNAPSHOT_2026-09-14.json). Stable API surface: [API reference](GEMINI_PREDICTIONS_API_REFERENCE.md).
