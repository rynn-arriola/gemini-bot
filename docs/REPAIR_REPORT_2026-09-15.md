# September 15 repair evidence

## Result and authority

The fifteen review regressions now pass. Production was repaired and tested offline on native Windows. This is **not live activation or a guarantee of error-free trading**. No authenticated network request, real order, canary, or bot launch was performed during these repairs. Approved quantity, capital, pending-order ceilings, quote offset, time exits, and the single monetary stop were not changed.

## Changes

- R01/R07: valid depth no longer triggers resubscription. Sequence gaps, crossed books, contract lifecycle changes, and clean/error stream endings remove quoting permission. Buffered deltas cannot restore an invalidated book. Recovery requires a fresh process, reconciliation, and approval; unattended reconnect/requote is intentionally not implemented.
- R02–R06: serialized lifecycle, immediate fill entry block, first-fill deadlines persisted before cancellation/position arrival, final-send freshness/expiry/exposure checks, terminal cancellation evidence, REST/WS position agreement after cancellation, exact same-outcome SELL, and durable guards against repeated unknown exits. Flat requires position, terminal-intent, and cash evidence; an active-order list alone is insufficient.
- R08: a host-wide Windows lock and canonical journal under `%LOCALAPPDATA%\GeminiRewardBot\control` replace independent runtime-directory ownership. This is deliberately stronger than one-key locking: different keys cannot run concurrent mutation owners either. Credential changes and legacy runtime journals fail closed pending explicit migration. Existing research/canary journals are preserved, not automatically imported or deleted.
- R09: removed the five-second cash-classification heuristic. Expected cash and signed position movements are matched to durable fill identities and amounts and persisted across restart. Unmatched transfers, rewards, settlement, fee/collateral adjustments, or missing executions block entries and require reconciliation; they are **not silently counted as trading profit**. This is conservative classification, not a complete automatic external-cash-flow accounting service.
- R10/R12–R14: explicit SQLite transactions/rollback, page-count versus total distinction, repeated-page rejection, terminal `cancelled` normalization, installed HTTP/WS cooldowns, and no emergency-cancel pacing bypass.
- R11: enforce the five-minute pre-expiry entry boundary, subscribe to contract status, and refresh the selected contract's actual rules rather than accepting another event's shared-pool identity.
- R15: missed minutes remain in the conservative daily uptime denominator. Local qualification checks size, distance, freshness, contract state and either qualifying side. Reports expose the 75% target. UTC-day and score values remain **proxies**, not Gemini-certified qualification or payout estimates. Uncertain denominator assumptions can conservatively exclude a market.
- Additional review findings: event-driven WS loss high-water updates plus an independent safety clock; stale account-snapshot checks; no mixed full/delta account subscription transition; persisted startup order identity; terminal intent recovery from history or proven active orders; own-depth subtraction; coalesced ranking with stale checks; reports/heartbeat every approximately 15 seconds; bounded recent terminal memory; durable order-transition/fill evidence; cleanup even when a socket close fails; Windows launcher argument quoting.
- Maker canaries now call the production activation, fill-safety, risk-clock, cancellation and drain path. A canary with no positively working maker order cannot claim success. The older standalone IOC script is explicitly retired from execution; its source and historical evidence remain available.
- Version-2 permits bind account fingerprint, reviewed Python build, policy, exact symbols, breadth and run expiry. They remain one-use in the shared journal. No real permit was created. Policy changes block subsequent entries; run expiry ends quoting.

## Offline verification

- `python -m unittest discover -s tests -p 'test_*.py'`: **106 passed**, including all 15 original review regressions and a bounded end-to-end fake maker canary.
- `python -m unittest discover -s scripts -p 'test_*.py'`: **25 passed**. The observer suite retains a harmless mocked-HTTPError resource warning; its printed supervisor-failure example is an injected test condition.
- `python -m compileall -q src scripts`: passed.
- `python -m gemini_bot validate-release --directory data/review-validation`: passed offline Windows policy/storage checks.
- PowerShell launcher parser: passed; launcher was not executed.
- Process-name/command matching found no Gemini Python runtime at the final check. This is local process evidence, not a current exchange-account snapshot.
- A synthetic startup database created while isolating the shared-state test was moved to `data/review-test-artifacts/isolated-fake-startup.sqlite`. It is preserved and is not production account state. Later startup tests use temporary isolated control directories.

## Remaining commissioning and deliberate limits

1. With separate owner approval, read-only preparation must verify current account state, API shapes, selected symbols, fees, and reward availability. Do not reset/import the prior commissioning journal or clear any unknown execution merely to pass startup.
2. Confirm standard same-outcome SELL reduction is valid for the account and selected contracts. Gemini documents a different NO reduction path for accounts enabled for mutually exclusive collateral return. This bot does not enable or implement that account program. The earlier standard YES/NO canaries do not prove every future account/product configuration.
3. Obtain a new exact-symbol/version-2 permit for each live maker commissioning step. Observe actual resting ACK, cancellations, fill/cash timing, exact exits, and authenticated fee/reward evidence. Do not infer a fill test succeeded if the order never filled.
4. A stale/insufficient book, unknown IOC result, incomplete cash classification, unsupported account program, or missing history can require manual intervention with residual inventory. No code can guarantee an immediate exact fill or a hard realized-loss ceiling through an outage. The runtime does not blindly retry or add a second monetary stop to conceal these cases.
5. Full account snapshots remain at 1 Hz to avoid unsafe snapshot/delta mixing. Performance must still be measured with real traffic before scaling. The launcher is not an OS supervisor; no automatic live resurrection was installed.
6. Durable database history is retained; recent in-memory terminal history is bounded. Very old sparse events, missing historical fills, or history exceeding the bounded REST ceiling fail closed rather than being guessed. Operational archive/migration work needs separate evidence before long-term deployment.

Public protocol checks: [Gemini streams](https://developer.gemini.com/prediction-markets/websocket/streams), [active-order pagination](https://developer.gemini.com/rest-api/prediction-markets/order-management/get-active-orders), [rate limits](https://developer.gemini.com/rate-limit), [liquidity rewards](https://developer.gemini.com/prediction-markets/liquidity-rewards-program), [collateral-return restrictions](https://developer.gemini.com/prediction-markets/collateral-return).
