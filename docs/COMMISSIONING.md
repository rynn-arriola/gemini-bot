# Live commissioning

Latest evidence: the owner-approved repeat preflight verified the one-sided-cohort and quiet-book repairs, and the repaired one-leg maker canary verified one resting order plus terminal cancellation. The unsupported UTC-day reward proxy is replaced by an Eastern-calendar-day proxy. A live read-only preparation at 11:26 Eastern tracked 32 symbols, ranked 28 pools and found 13 candidates feasible for the 50% minimum. Continuous activation remains a separate owner decision.

**Offline review repairs verified, September 15:** all 15 regression cases now pass. Live preparation has completed read-only; maker commissioning remains unperformed and requires exact-order approval. Read the [repair report and remaining limits](REPAIR_REPORT_2026-09-15.md) before activation.
Separately authorized LIMIT+IOC entry/exit round trips have validated both YES and NO side/outcome semantics and ended globally flat. They did not activate the reward bot or complete the maker-quote stages below. Commission remaining stages in bounded steps and record evidence after each stage.

## Stage 0 — offline release

- All legacy observer and production tests pass on native Windows.
- Configuration validation fails closed when any risk value is absent.
- Pending-order, position, capital, single-window-stop, stale-state, sequence, deadline, and log-redaction tests pass.
- Production storage and exclusive process ownership survive restart tests.

Offline evidence: 106 production tests and 25 observer tests pass, plus native Windows policy/storage, Python compile and PowerShell syntax checks. No authenticated network call or real order was used. Passing offline checks is not proof of venue execution/reward behavior.

## Stage 1 — authenticated read-only startup

- Verify account-scoped key, time-based nonce, Trading role, and accepted Predictions terms.
- Subscribe to account orders/balances/positions over WebSocket.
- Bracket those streams with bounded REST snapshots and prove consistent state.
- Discover and rank current reward pools with 10-contract geometry; submit nothing.

Tooling status: `prepare-live` completed at 10:57:01 Eastern on September 15. It has no order-mutation call path and disconnected after writing rank/status evidence. A fresh run is required at the later execution window because market prices and rank can change.

## Stage 2 — approved owner policy

The reviewed source is `config/live_policy.json`: $300 gross, $90/market, $40 stressed-exit entry gate, one $30/six-hour realized-loss stop, size 10, fixed 30 pending below HARD, 15 two-sided quotes, 32 tracked owners, two ticks behind touch, 75% uptime target, 300-second inventory exit and five-minute pre-expiry exit. Configuration must prove `monetary_stop_authorities=1`; no daily or per-position loss stop exists.

## Stage 3 — first bounded live quote

Requires a separate explicit owner activation after Stages 0–2. Use one selected unique pool and one 10-contract maker-only leg first, with the second leg only after order/account state is positively observed. Verify acknowledgment, resting state, cancel, terminal proof, balances, and zero unintended position.

Tooling status: the first live attempt exposed a replacement loop and ended flat; the repaired owner-approved repeat passed with exactly one five-second resting order, one terminal unfilled cancellation and final flat reconciliation. Evidence: [maker canary report](MAKER_CANARY_2026-09-15.md). The tool requires an exact live acknowledgement plus a one-hour, one-pool, one-use version-2 permit bound to policy, Python build, account fingerprint and exact selected symbol. A consumed canary permit cannot authorize a later stage. Expiry bounds the run, not just startup. Confirm the account/contract supports standard same-outcome SELL and is not using the incompatible collateral-return reduction mode.

## Stage 4 — bounded two-sided canary

Use one pool, two 10-contract legs, and the full fill/timed-exit path. Confirm no blind retries, opposite-leg cancellation after fill, exact position reconciliation, reward-summary evidence, fees/rebates, and clean shutdown.

Tooling status: implemented as the same script with `--mode two-sided`; not run and not authorized.

## Stage 5 — breadth increases

Increase one reviewed step at a time. The 30-pending-order cap means at most 15 flat two-sided contracts before fills/cancels and safety headroom. Active-pool policy may be smaller. Never infer that all 30 slots should be consumed.
