# Live commissioning

Latest evidence: the owner-approved [five-minute read-only preflight](PREFLIGHT_2026-09-15.md) completed but failed production market readiness. Stage 1 is attempted, not passed. Repair one-sided-cohort startup and verify quiet-book freshness handling before any maker canary. Earlier "not run" statements below describe the pre-preflight checkpoint.

**Offline review repairs verified, September 15:** all 15 regression cases now pass. Live preparation and maker commissioning remain unapproved and unperformed. Read the [repair report and remaining limits](REPAIR_REPORT_2026-09-15.md) before connecting.
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

Tooling status: implemented as `prepare-live`, but not run. It has no order-mutation call path and disconnects after writing the rank/status evidence. Running it still requires the owner's approval to connect.

## Stage 2 — approved owner policy

The reviewed source is `config/live_policy.json`: $300 gross, $90/market, $40 stressed-exit entry gate, one $25/six-hour stop, size 10, max 30 pending, 15 two-sided quotes, 32 tracked owners, two ticks behind touch, 75% uptime target, 900-second inventory exit and five-minute pre-expiry exit. Configuration must prove `monetary_stop_authorities=1`; no daily or per-position loss stop exists.

## Stage 3 — first bounded live quote

Requires a separate explicit owner activation after Stages 0–2. Use one selected unique pool and one 10-contract maker-only leg first, with the second leg only after order/account state is positively observed. Verify acknowledgment, resting state, cancel, terminal proof, balances, and zero unintended position.

Tooling status: `scripts/live_maker_canary.py --mode one-leg` calls the production runtime and safety workers; not run live. It requires an exact live acknowledgement plus a one-hour, one-pool, one-use version-2 permit bound to policy, Python build, account fingerprint and exact selected symbol. Fields: `permit_version`, `permit_id`, `approved_at`, `expires_at`, `maximum_active_pools`, `policy_sha256`, `build_sha256`, `account_fingerprint`, `symbols`. Use the digest helpers in `activation.py`; never create a permit before owner approval. Expiry bounds the run, not just startup. Confirm the account/contract supports standard same-outcome SELL and is not using the incompatible collateral-return reduction mode.

## Stage 4 — bounded two-sided canary

Use one pool, two 10-contract legs, and the full fill/timed-exit path. Confirm no blind retries, opposite-leg cancellation after fill, exact position reconciliation, reward-summary evidence, fees/rebates, and clean shutdown.

Tooling status: implemented as the same script with `--mode two-sided`; not run and not authorized.

## Stage 5 — breadth increases

Increase one reviewed step at a time. The 30-pending-order cap means at most 15 flat two-sided contracts before fills/cancels and safety headroom. Active-pool policy may be smaller. Never infer that all 30 slots should be consumed.
