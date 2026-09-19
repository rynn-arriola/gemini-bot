# Bug fixes

## September 19 — correlated recovery exhausted cancellation proof clocks

All five shard workers canceled stale entries concurrently as the private stream also recovered. Their cancellation clocks counted time queued behind one serialized authenticated REST reader and duplicated two terminal-history reads per market, causing safe but unnecessary runner failure after 45 seconds. Active-order proof now coalesces briefly across owners and history proof coalesces by symbol. Only a correlated continuous-service outage gains bounded full-width proof headroom, capped at 180 seconds. No mutation replay, absence proof, ordinary timeout relaxation or uptime credit is allowed. All 1,062 release tests/checks pass; restart preparation remains. [Evidence](docs/CORRELATED_STREAM_RECOVERY_2026-09-19.md).

## September 19 — inconsistent reward catalog caused a fatal drain

Exhausted inconsistent pagination now emits a typed recoverable catalog-read error, without returning partial rows. Same-process metadata recovery retains safety/exits and owner history; startup may wait read-only. Refresh requests cannot bypass metadata backoff, and longer server cooldowns win. Malformed data, account uncertainty, unknown mutations and HARD remain fail-closed. All 1,059 tests/release checks and fresh flat/order-free preparation passed. V31 initial live acceptance found owned best-bid orders, zero positions/unowned orders, healthy shards and no holds/errors/restarts. [Evidence and snapshot impact](docs/REWARD_CATALOG_RECOVERY_2026-09-19.md).

## September 19 — fractional-fill cash precision and NO-position exit crash

A 5.93-contract BUY NO fill at 0.68 has raw notional 4.0324, but the contract advertises two-decimal quote precision and Gemini's authenticated balance moved by 4.04. The raw-notional ledger therefore became incomplete, a concurrent risk timer persisted that transient state, and emergency exit attempts were blocked locally while incorrectly consuming all three retry slots. Negative inventory also used SELL NO even though collateral-return accounts document BUY YES as the reduction path. The repair parses/persists `quoteAssetPrecision`, stores exact rounded cash deltas per fill, performs only an exact fresh-balance-matched post-watermark legacy repair, routes negative inventory through BUY YES, distinguishes entry BUYs from exit BUYs, preserves entry deadlines, defers concurrent risk marks, and counts only transport-submitted exit attempts. Unknown accounting, missing metadata, ambiguous sends and exhausted real IOC attempts remain fail-closed. **1,023 production + 28 script tests (1,051 total)**, compilation, PowerShell parsing, whitespace and offline release validation pass. Source is restart-ready for controlled deployment; no restart is claimed. [Evidence](docs/PARTIAL_FILL_PRECISION_AND_NO_EXIT_2026-09-19.md).

## September 18 — incomplete reward contract stopped every market

The live reward refresh passed an entire event to the catalog parser. One Gemini contract omitted `priceIncrement`, so a direct dictionary lookup raised `KeyError` and invoked the correct account-wide unknown-failure drain even though other contracts were healthy. Catalog parsing now skips only contracts whose rule rows are incomplete, non-dictionary, nonnumeric or invalid. If the unavailable contract is already selected, refresh blocks new entry and immediately cancels only that symbol; valid sibling contracts and unrelated markets continue. Duplicate valid symbols, conflicting pool budgets, mutation ambiguity, account uncertainty and HARD still fail closed. The bot remains stopped flat pending a separately authorized fresh deployment. [Evidence](docs/MALFORMED_REWARD_CONTRACT_2026-09-18.md).

## September 18 — optional same-price refresh churn

V26 correctly rejoined best bid with new order identities, but the only interval was the 10-second safety cooldown shared with mandatory price-change re-entry. It produced repeated 12–26-second unquoted gaps and 35 optional refresh decisions in roughly 35 minutes. Explicit v7 policy adds a separate five-minute per-market optional decision interval and a 500-contract estimated-improvement threshold while retaining the 10-second cancellation/proof path for price tracking. The durable cancellation-breach identity is unchanged. This is an isolated candidate, not deployed behavior. [Evidence](docs/SAME_PRICE_REFRESH_CHURN_2026-09-18.md).

## September 18 — profitability-driven owner churn

The ten-minute recovery timeout could repeatedly release and re-admit owners on fluctuating net-profit scenarios. Exact profitability-only failures now reserve the incumbent until the current Eastern day's end, with durable expiry and fresh failure proof after midnight. Quote safety/economics still cancel or block orders; structural faults keep existing release checks. Failure classifications cannot borrow each other's elapsed grace or stale post-proof decision. Deployed as v25 after terminal/flat v24 drain and fresh exact authority. [Behavior, tests and deployment](docs/DAILY_STICKY_ECONOMICS_2026-09-18.md).

## September 18 — own cancellation repeatedly reset adaptive promotion

Concurrent checks treated an owned cancellation as lost observation continuity, restarting warmup after withdrawing the fallback pair. Separate evidence health from mutation permission; keep bounded safety-clock observations during quiet cooldowns and wake ranking after flat proof/mode changes. Cancellation still blocks sends and all terminal, freshness, fill, generation, cooldown and HARD safeguards remain. Deployed as v24 after terminal/flat v23 drain and fresh exact authority. [Regression and deployment evidence](docs/ADAPTIVE_TRANSITION_REPAIR_2026-09-18.md).

## September 18 — all-selected adaptive scope and full-capacity work

The candidate still assumed exactly two fixed symbols despite the intended shared all-market strategy. Explicit v5 all-selected mode now binds to permitted durable owners, handles fresh replacement/readmission, bounds and prunes retired state, preserves shared cancellation breaches and serializes extra account proofs. Optional two-symbol v4 research remains explicit. Full-buffer profiling exposed repeated trade scans; exact incremental rolling totals remove that work with duplicate/window/overflow/clock regressions. This is isolated source, not a fix already active in production. Deployment and quote transitions can create uncredited gaps; no sticky reset, risk/HARD change or live trial was performed. [Tests and release](docs/ALL_SELECTED_ADAPTIVE_2026-09-18.md).

## September 17 — one missing public reward event aborted all discovery

The user-run startup failed on a per-event 404 before activation. Discovery now skips only that unavailable public event; saved owners keep recovery space and identity. Live refresh blocks/cancels only affected entries, preserving exact cancellation/account/inventory proof and normal later recovery. Catalog/account/authentication/cooldown/unknown-mutation failures retain their existing boundaries. [Evidence and tests](docs/MISSING_REWARD_EVENT_2026-09-17.md).

## September 17 — missing automatic journal protection and pre-write integrity guard

Added bounded hourly verified SQLite snapshots, off-loop I/O, safe managed retention and redacted backup health. Startup checks existing authority integrity and required tables read-only before schema/account writes; malformed/empty authority is preserved and blocked. Worker shutdown is joined before ownership release, and late backup exceptions are redacted inside the worker. No automatic restore, trading gate change or accounting/HARD reset. Existing native/packaged path and background-worker repairs remain intact. [Design and evidence](docs/JOURNAL_BACKUPS_2026-09-17.md).

## September 17 — packaged operator and native runner had different AppData WAL views

The packaged operator's apparent control journal omitted 22 new native-runner intents because AppData WAL/SHM/lock writes were redirected to a package cache. The actual native journal passed integrity and terminal/flat checks; the mixed packaged base/WAL view did not. Production was drained, a native consistent backup was verified, and every table was explicitly preserved into a user-profile control directory outside AppData. Runtime now uses that shared directory and refuses blank startup when legacy authority exists. Real native/packaged hash agreement and second-owner rejection passed before restart. This changes neither accounting baselines nor HARD. [Evidence](docs/CONTROL_LOCATION_2026-09-17.md).

## September 17 — failed background worker incorrectly closed the session normally

V17's private venue-restart disconnects and concurrent public-shard cancel-proof timeouts halted the gate between main-loop iterations. The loop then skipped retrieval of the worker exception and recorded `closed` after draining flat, preventing the intended bounded transient recovery. Completed worker failures are now retrieved before normal-close classification. The actual activation-loop regression reproduces the inter-iteration HALT and reaches `recoverable_flat` only after synthetic successful drain. Unknown/permanent errors, incomplete drain and HARD retain existing blocks; this neither retries an ambiguous cancel nor fixes the external venue outage. [Evidence](docs/QUEUE_PILOT_EXECUTION_2026-09-17.md).

## September 17, 2026 — fatal account reader stalled emergency drain

V16 stopped at 00:00 ET after a correlated WebSocket rejection killed the account reader. Without a transient recovery hold, drain skipped reconnection and cancellation confirmations timed out. Drain now restores failed readers for exact shutdown proof. Lost maker acknowledgements resolve only from unique exact zero-fill terminal history. Rejections retain redacted method/status/code; the original venue reason was not recorded and remains unknown. Confirmed service/rate errors can qualify for the existing bounded supervised restart only after complete terminal/flat/account proof; permanent/auth/unknown errors and HARD remain blocking, including when followed by an acknowledgement timeout. [Evidence and limits](docs/MIDNIGHT_REQUEST_RECOVERY_2026-09-17.md).

## September 16, 2026 — competition and shared-pool strategy v3 (source only)

The old reward denominator scaled assumed competitor size with our own quantity, so increasing our size could leave estimated share unchanged. It also used event maker headcount as a score denominator and imposed Gemini's account-daily $1 threshold per candidate. V3 uses fresh own-adjusted L2 pressure plus an explicit fixed unseen-contract scenario; maker count breaks economic ties, own size no longer resizes competitors, and the payout floor is reported only at account level.

Discovery now checks alternate quotable contracts and bounded same-pool events. Active/held owners are exact pool-plus-symbol identities rather than one dictionary entry per pool. Marginal allocation counts the budget once, scores actual incumbent prices, preserves healthy owners and excludes additions that would make retained members uneconomic. Event uptime observes any qualifying tracked contract once per minute. Fee estimates respect the current promotion's unambiguous interior and reward reporting distinguishes earned/below-threshold/pending/paid amounts without fabricated contract attribution or net profit.

This strategy change is not deployed and creates no current resting gap. Future deployment requires fresh authorization; revised economics can reject an old incumbent, and the competition/score/window models remain uncertain proxies. Risk limits, exact recovery, HARD and the active checkout are unchanged.

## September 16, 2026 — priced BUY reservation and eighth-market churn

Doubling entry size to 20 exposed inconsistent accounting: whole-pair admission used price times quantity, but existing live orders and final sends reserved a blanket $1 per contract. Seven pairs consumed 280 under the latter calculation. An eighth first leg could pass pair admission, then the second leg failed at 320 and the sibling was canceled repeatedly. The owner explicitly rejected reducing size or this blanket order reserve.

Working BUYs now reserve remaining quantity times limit price and new BUY legs use submitted quantity times limit price, matching complete-pair admission. Existing held-position reserves, no-netting against pending SELLs, available funds, $300 gross/$90 market, 30-order/15-pair caps, exact fill/exit safety and $30 six-hour HARD remain unchanged. Regression tests reproduce the old eighth-pair cancellation and artificial full-width block, then verify 30 size-20 orders within cost limits, exact/over-cap boundaries, funds, partial remaining quantity, SELL/held inventory and genuine second-leg state changes. This reduces avoidable unquoted intervals and preserves existing pairs; it does not promise 30 eligible orders or reward qualification.

## September 16, 2026 — settled held contract no longer crashes every market

At 17:00 ET Gemini changed the held contract's lifecycle while exact inventory still existed. The public status handler raised a global runtime error, canceled all unrelated resting orders, and emergency drain then failed because that contract had no executable exit depth. Gemini subsequently settled the position, but the durable expected position/cash basis remained stale, so the prior startup accounting could not safely resume.

The affected symbol now enters the same market-local settlement-wait boundary used by the local Polymarket bot: preserve its exact deadline/exposure, block only that symbol and let unrelated reconciled markets continue. Gemini's authenticated terminal zero-position/payout frame commits settlement live; a stable event-filtered settled-position REST row recovers a missed terminal message on restart. Settlement has its own immutable ledger row, credits exact payout, closes exact owned quantity, clears the deadline, and realizes fill basis plus payout into the unchanged $30 six-hour HARD calculation. Duplicate proof is idempotent; quantity/payout/ownership/history/account disagreement remains fail-closed. Production quote size is now the owner-approved 20 contracts, while every existing cap and the $30 HARD remain unchanged.

## September 16, 2026 — sticky markets no longer stop on an uncertain local uptime bound

The live cohort had been resting since shortly after midnight, but reconstructed local uptime rows began at 11:27 and made the bot report that 50% was unreachable. Order history proved earlier resting presence, while Gemini's authenticated daily summary still contained no usable snapshot rows. The local calculation therefore could not safely establish either qualification or disqualification.

The local 50% rejection is now reporting-only. Every other rejection remains blocking. Exact healthy incumbents stay pinned across midnight/restarts and ahead of higher-ranked reserves; completed/expired/unsafe markets request an immediate reward-catalog refresh and only their slots rotate. The release passed 559 production and 28 script tests plus compilation, PowerShell parsing, whitespace and offline release validation. Live acceptance restored the same 12-market cohort with 24 canonically owned orders, no uptime override, no stream holds and no post-start ERROR/CRITICAL event.

## September 16, 2026 — verified journal recovery and live market isolation

Deployment uncovered a physically malformed canonical SQLite file after the controlled stop. The bot was not started against it. A fail-closed reconstruction combined the last verified backup, 13 exact rows recovered from the damaged file, authenticated per-symbol terminal order history, stable REST/private-stream positions and cash, and the already-consumed one-time cash incident boundary. Independent checks proved one position, zero active orders, one deadline, no nonterminal intents, complete current risk classification and SQLite `quick_check=ok`; the damaged original remains preserved.

The pushed market-local repair then passed read-only preparation and activated. Venue truth showed 22 active orders on 11 healthy markets and none on the held ZEC symbol. Its no-depth exit retries remain symbol-scoped and do not consume pending-order slots; the position still consumes exposure and one pool allocation. Unknown orders, ambiguous sends, account disagreement and storage/integrity failures remain account-wide stops. [Recovery evidence](docs/CONTROL_JOURNAL_RECOVERY_2026-09-16.md).

## September 16, 2026 — catalog-omitted inventory no longer blocks unrelated markets

The typed catalog-standby repair kept the runner alive but still stopped every market before activation whenever one exact held contract was omitted from a discovery response. That contradicted the approved market-isolation and resting-time objective.

V7 now scopes only fully proven incumbent inventory: exact REST/WS quantity, durable sticky owner and original deadline, with no active order or unresolved exit intent. The held symbol stays entry-blocked and unsubscribed, counts exposure and one pool allocation, consumes zero pending-order slots, and receives no uptime credit. Healthy markets can activate normally. Discovery retries every 60 seconds while the hold exists; current metadata plus fresh synchronized depth restore the same symbol and its existing exit worker. Unknown or incomplete evidence still enters global read-only standby. Four new regressions bring verification to 557 production and 28 script tests. [Design and evidence](docs/SCOPED_INVENTORY_CATALOG_RECOVERY_2026-09-16.md).

## September 16, 2026 — transient catalog omission with held inventory

The first persistent-task launch proved the launcher stayed alive, but the runner never activated. Its initial REST read found the exact existing position while that discovery attempt returned only 20 subscribable markets and omitted the held ZEC symbol, so startup raised a generic fatal error and the supervisor entered attention. No permit was consumed and no order was submitted. A later authenticated position read still showed the one known position and zero active orders; a fresh public discovery returned 32 markets and included the same held symbol, establishing a transient catalog omission rather than unknown inventory.

The exact pre-activation condition is now a typed `InventoryMarketUnavailable` reconciliation blocker. V7 remains in the existing nonactivated, STOP-responsive, 60-second read-only standby loop and repeats complete preparation and normal permit validation only after the exact market becomes subscribable. It never fabricates contract rules, subscribes stale data, sends an exit, resets the original deadline/loss/uptime, or changes legacy/post-activation/unknown/accounting failure handling. Two regressions cover real preparation classification and full retry routing; 553 production and 28 script tests plus compilation, PowerShell parsing and offline release validation pass. Snapshot impact: prevents a transient discovery omission from requiring manual restart, but all waiting time remains uncredited and safety still overrides the 50% objective.

## September 16, 2026 — Live supervisor detached from temporary launcher job

A user-started Live run remained healthy until both the runner and supervisor vanished together about 40 minutes after launch. The final heartbeat and order transitions were normal; there was no STOP, durable runner result, stderr, bot error/critical event, Windows application failure or power event. The supervisor was a descendant of the temporary launching PowerShell, so external job-tree termination is the strongest evidence-based explanation, not a proven bot exception.

Live startup now registers one stable project-scoped, on-demand Windows Scheduled Task and starts the supervisor through Task Scheduler. The task has unlimited execution time, permits only one instance, has no trigger and carries no authority beyond the supplied build/account/policy permit. Startup refuses a running task or process-backed supervisor, waits for a fresh supervisor heartbeat, and records non-sensitive task metadata. Runtime health output includes the task state. An offline mutation-mocked harness proves Live never falls back to `Start-Process`; a real harmless scheduler probe ran, returned result zero and was removed. Full verification passed 551 production and 28 script tests plus compilation, PowerShell parsing and offline release validation. This reduces avoidable resting-order gaps but credits no missed reward snapshots and changes no ranking, 50% admission, size, breadth, exit or loss rule.

## September 16, 2026 — one-time exception binds both cash surfaces

After the manual SELL cancellation, stable order-free proof reached the approved exception but refused because REST `amount` and WebSocket `c` exposed different precision. Applying the REST value would leave runtime reconciliation broken because the engine compares against exact WS `c`. The operator-only incident path now binds stable REST and WS values separately, requires each to remain inside the same approved sub-cent incident, and adopts exact WS cash once. No general rounding/tolerance or automatic future exception was added.

## September 16, 2026 — absent valuation on an untradeable position

Gemini documents that a position can omit `marketValue` when no sell quote exists, but REST reconciliation indexed that field unconditionally. Offline no-buyer startup reproduced `KeyError` before the existing scoped exit wait. Parsing now retains the exact position and exposure basis with an unavailable valuation; saved snapshot text explicitly says `unavailable`, never zero or last-trade price. Nonfinite/malformed amounts still fail closed. Integration covers startup, no-buyer wait, original deadline preservation, liquidity return and exact flatten, plus unchanged cash/external-order checks. No live order or restart.

## September 16, 2026 — startup evidence standby and STOP race

Known external-order/unclassified-account startup blockers previously terminated the v7 runner immediately. They now enter paced read-only standby without adopting orders or resetting cash. Complete preparation and permit validation still precede any activation; malformed/auth/unknown/post-activation failures are not reclassified as recoverable. A STOP received during final preparation now prevents activation. This repairs service availability, not the unresolved cash evidence. [Scope and reference review](docs/RECONCILIATION_STANDBY_2026-09-16.md).

## September 16, 2026 — shard isolation and known-inventory startup

- Five public shards now have independent book generation, liveness and recovery. A typed public outage/gap no longer requires canceling healthy groups; unknown/private/account failures still block globally. Shared quotas, cancellation priority, sticky reservations and late-frame rejection remain enforced.
- Startup previously forced immediate liquidation before entering the continuous no-depth wait flow. A newly authorized v7 session can instead hand exactly reconciled inventory with original ownership/deadlines to the existing scoped worker. Legacy startup, unknown cash, missing deadline/ownership, pending reducers and HARD are not relaxed.
- Quiet ping completion cannot freshen a replacement connection. Rank/uptime calculations use each symbol's own shard freshness. Private recovery does not wait on paused public shards that are themselves waiting on private reconciliation.
- Offline tests and read-only six-connection smoke pass. Source is installed locally, not activated. Cash reconciliation remains unresolved; this is not a successful restart claim.

## September 16, 2026 — maintenance recovery and missing terminal execution evidence

- Treat typed transport EOF inside the WebSocket handshake wrapper as retryable, not malformed complete protocol/authentication responses.
- Restore transports within the existing emergency-drain budget; release only definitely-unsent cancellation failures, never ambiguous mutations.
- Join authenticated trade IDs/fees to exact terminal order identities for atomic, idempotent missed-fill recovery. Preserve original inventory deadlines and loss history. Explicit external reduction repair never adopts a replacement baseline.
- Bind read-only supervisor preparation results to the current attempt token instead of relying on the observed child ExitCode.
- Added no-buyer/fractional-fill regression coverage. A known no-depth exit wait can remain symbol-scoped; unexplained cash/accounting stays global and blocked. These repairs are not deployed; the current cash discrepancy remains unresolved.

## September 16, 2026 — second maker leg admission and telemetry

Confirmed offline: YES could cancel during NO's transport wait, then NO still sent and was canceled as `pair_incomplete`. Final admission now checks the exact first order/client identity, acknowledged intent, unchanged price/full unfilled quantity and public/private generations. Invalidated second intents remain definitely unsent and terminal; existing symbol cleanup continues. Attempted-send failures retain unknown state and are not replayed. The guard is removed after send/failure without leaking into later pairs.

Adverse-guard diagnostics now distinguish missing contract/book, unsynchronized/stale/one-sided public book, empty external side after own-order subtraction and invalid two-sided target. Cancellation decisions are unchanged. Included the prepared shared-journal account callback repair and its real-preparation regression.

Verification: 454 production + 28 script tests passed, including 18 new guard/diagnostic tests and the telemetry regression. Compilation, whitespace, PowerShell parsing and offline Windows policy/storage checks passed. Source verified; deployment status is in STATUS.md. Snapshot impact: avoids some orphan orders/cancel traffic; genuine safety cancels remain, no reward guarantee or policy change. Controlled deployment incurs an unquoted interval.

## September 15, 2026 — account callback survives preparation (source only)

Shared-journal startup constructed a new account and assigned a logging-only sink, bypassing constructor-installed `_account_event`. Restore that callback so logged order transitions also update NEW/CANCELED counters and sub-clock either-/two-sided continuity. A new offline integration regression runs real preparation with fake transports and an isolated journal, then drives the account reducer through a 120ms quote/cancel/replacement cycle. It failed on the prior code and passes with the fix, preserving raw logs, order reduction and paused/no-activation behavior. Reporting only: no quote, size, risk, admission or 50%-denominator changes, and no unquoted interval is introduced by source preparation. The repair is isolated on `codex/telemetry-callback`; no process, permit or live source change/restart.

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
# September 16, 2026 — transient sticky-owner loss and approved 40-order capacity

The production selector dropped a sticky owner on a single missing/unusable or uneconomic rank cycle, immediately admitting a challenger. Six of 15 owners changed in the observed September 16 interval; at least two cancellation triggers were one-sided external depth and inside-two-tick movement. Other historical selection causes were not recorded, so they remain unproven. Quote safety is now separated from durable ownership: temporary failures reserve their slot for a continuously observed ten-minute recovery grace, then require fresh terminal/flat account proof and fresh failed ranking before release. Reason-coded owner transitions and selection cancellations are recorded. Unsafe orders still cancel immediately; recovery time is unquoted, not earned reward time. Capacity is now owner-approved 40 orders/20 owners, without monetary/HARD relaxation. [Release and limits](docs/STICKY_RECOVERY_2026-09-16.md).
# September 18, 2026 — adaptive construction regressions (not deployed)

- The new transition path initially requested a three-second account proof during a longer cancellation cooldown, which could waste the proof and impose the next proof-attempt delay. Adaptive entry now waits for cooldown and later canonical public depth before scheduling the proof. Synthetic full cancel/prove/rejoin and promotion/demotion tests cover this boundary.
- An in-memory-only cancellation-envelope breach would be lost on same-policy process restart. The candidate now durably latches that restriction by canonical envelope/scope identity, restores it during canonical-journal preparation and treats persistence failure as global rather than swallowing it in a callback. Restart, malformed-record and storage-failure tests pass. No current live process was changed.
