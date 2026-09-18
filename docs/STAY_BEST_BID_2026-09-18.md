# Stay at best bid — September 18, 2026

## Owner decision and scope

The owner accepts fills when queue protection is thin or depleted: remain at best bid, reposition when useful, otherwise leave the orders resting. Explicit policy version 6 sets `queue_pilot.placement_mode=stay_best`, `scope=all_selected` and zero-tick placement. Legacy v3/v4/v5 behavior remains explicit and unchanged.

This is isolated implementation in `feature/stay-best-bid`, not deployed trading behavior. Running main/v25 was not edited or restarted. A read-only check at 18:13 ET found fresh quoting/supervisor heartbeats, RUNNING task and no read/stream holds; it did not independently verify venue orders.

## Behavior

- Every permitted selected owner starts at the external best bid on each outcome leg, after fresh account/terminal proof. Own quantity is removed from public depth before calculating the external touch. No thin-depth, flow, warmup or expired-optimization fallback to one tick.
- Track price changes using the existing affected-symbol cancellation/proof/rejoin path. Recheck both maker legs after pacing; never knowingly cross the book.
- Same-price queue refresh is optional. Under the existing unexpired settings, refresh only with a matching actual-order queue estimate, completed warmup/cooldown, low estimated quantity ahead and sufficient estimated new depth to gain by rejoining. Otherwise keep the pair resting, accepting fill risk. Aggregate depth cannot prove actual queue position or guarantee back-of-queue execution.
- The September 19 05:45 ET settings expiry, or a persisted cancel-envelope breach, disables optional same-price optimization, not v6 best-bid placement or mandatory price tracking. No expiry extension or numeric calibration change is included.
- Unknown cancellation, partial/full fill, stale canonical book/account evidence, inventory/exit work, invalid orders, adverse economics and HARD keep existing safety behavior. A partial seven-contract fill is not a clean eight-contract quote eligible for refresh; exact fill accounting and exit recovery own that lifecycle.

Size 15, 40 pending orders, 20 owners, 32 watched markets, all monetary limits and five-minute exit deadlines remain unchanged. Five public shards, one private socket, one mutation owner, one shared governor and serialized extra proofs remain. No cohort reset, loss/deadline reset or manufactured uptime.

## Uptime tradeoff

Staying at the touch removes deliberate one-tick fallback and depth-driven withdrawal, but can increase actual fills and exit downtime. Price tracking and optional refresh create cancellation/reconciliation gaps; deployment also creates a gap. None earns snapshot credit. Existing sticky/economic reservations retain owners rather than deliberately rotating them. Best-bid presence, continuous coverage and reward qualification cannot be guaranteed.

## Verification and deployment

Final verification: **1,013 production tests plus 28 script tests (1,041 total) passed**, including 40 stay-best cases. Compilation, offline Windows policy/storage validation and whitespace checks passed. Legacy fixtures explicitly pin their old placement/pilot settings so they test the intended old behavior rather than accidentally inheriting the new default. The worktree uses the existing Python environment through an ignored directory junction; native supervisor/launcher tests require that path. No supervisor or launcher production code was changed.

Deterministic tests exercise cold/thin placement, depleted-queue retention, optional useful refresh, cooldown, expired settings, existing cancellation breaches, missing estimates, price movement, fresh membership/proof, private-generation changes, partial fills, unknown cancels, post-pacing races and HARD. Full-width fake execution covers 40 size-15 maker orders, shared rate charging and a seven-contract/no-buyer/exact-exit lifecycle preserving 38 unrelated orders. These are offline tests, not live queue/profitability acceptance.

The operator must promote the verified commit only during an approved controlled deployment, gracefully drain v25, prepare with preserved canonical history and verified backup, create fresh exact-build/policy/account authority and launch through the persistent task. Never reuse the v25 permit. After activation verify actual owned prices, quantities, holds, private/public health and natural recovery; tests alone cannot prove long-run behavior.

Protocol remains unchanged. Official references rechecked September 18: [maker-only order placement](https://developer.gemini.com/rest-api/prediction-markets/order-management/place-order) and [order cancellation](https://developer.gemini.com/rest-api/prediction-markets/order-management/cancel-order).
