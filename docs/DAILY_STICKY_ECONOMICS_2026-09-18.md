# Daily reservations for economic failures

The owner approved improving sticky retention after the v24 log review found 31 releases/admissions driven by continuously negative modeled economics for ten minutes. Some symbols were admitted again shortly after release. The per-run continuity map is pruned and cannot establish the number of historic rotations; use durable owner events.

## Behavior

For an existing continuous-service owner, the first individual net-profit or shared-pool marginal-profit failure reserves the exact owner through that Eastern calendar day's midnight. This applies to every incumbent: no invented substantial-time threshold and no exchange qualification assumption. The slot remains within the 20-owner limit and its paused orders consume no pending-order slots. Vacant slots retain normal economic ranking. No other owner can borrow the reservation.

Economically invalid orders still cancel and new sends remain blocked. On valid recovery the same incumbent resumes, even when another candidate ranks higher. This changes slot retention only, not profit estimation or cancellation/admission gates. Adaptive scope remains attached to that owner, avoiding a needless removal/readmission warmup cycle.

The original economic hold is persisted with the bounded recovery state under `sticky_recovery_v2`. Existing `sticky_markets_v2` and economic, fill, deadline, reward and uptime history are preserved. If no v2 recovery exists, restore the old v1 recovery state; once v2 exists malformed v2 blocks rather than falling back to v1. Restarts reset continuous-failure evidence but preserve the original reservation deadline. Inventory/shard holds also preserve that deadline.

Midnight is a local retention boundary, not an assertion about Gemini's scoring start. Healthy owners remain sticky across midnight. A continuously failed economic hold is not renewed: require a fresh ten-minute observed failure period and the existing paced terminal-cancel/flat-account/post-proof-ranking checks before releasing it. Expired economic records remain until true recovery/release so a restart cannot renew them. DST uses the existing Eastern day helper.

Expiry, reward/lifecycle changes, unavailable books and invalid quote geometry retain existing safety and retirement behavior. Only the exact two economic failure reasons receive the daily reservation; compound economic-plus-structural reasons do not. Entry cutoffs take precedence over economics. A change in failure reason resets the grace; a structural failure that becomes economic during an awaited release proof receives the new reservation instead of being released using stale classification.

## Continuity tradeoff

This prevents economic churn from discarding accumulated market time, but it does not guarantee continuously resting orders. A paused reserved slot may remain idle for hours and reduce aggregate order count/opportunities elsewhere. Orders resume only when all existing checks pass. No downtime credit, event-to-event transfer, 50% trading gate, cohort reset, exposure increase or HARD change is introduced.

Correction to the preceding operator report: `local_eastern_day_observed_uptime_proxy` divides qualifying local minutes by elapsed estimated minutes. A value above 50% mid-day does not establish 50% of the full scoring denominator already secured. Event-level local counts also cannot establish each contract's individual resting time or exchange credit. Neither the earlier elapsed percentages nor the pruned continuity map is used to authorize this retention policy.

## Verification and deployment

Verification passed: **973 production + 28 script tests = 1,001 total**, compilation, offline Windows policy/storage validation and whitespace checks. The 20 new cases complement the existing 23 sticky-recovery tests. Restoring the old unprotected release behavior in memory makes the long-economic-dip regression fail as expected. A final authenticated read-only v24 check found 32 owned orders/16 markets, no positions or read/stream holds, healthy journal/build binding and a RUNNING supervisor.

New offline cases cover long economic pauses, order cancellation with retained ownership, exact-incumbent recovery, pool-marginal failure, restart/legacy state, midnight/DST, changed failure class, stale proof reclassification, structural/cutoff retirement, all 20 reserved slots, malformed persistence and storage failure. Existing cancellation/account/unknown-execution/HARD tests remain applicable. All account and order adapters in these tests are synthetic.

Source commit `0e8fa67cc7a48360ce1c241dbdb18e4ec20b9355` was fast-forwarded to `main`, pushed and verified against the GitHub remote after v24 reached `stopped_flat`. Fresh v25 preparation proved zero venue positions/orders, no unresolved intents, healthy journal and non-HARD risk, a verified backup, and unchanged sticky/economic history before creating new exact build/policy/account version-7 authority with no uptime override.

V25 launched through the persistent Windows task at approximately 14:43 Eastern. Initial authenticated acceptance found 32 canonically owned size-15 orders across 16 markets, zero positions, five connected public shards, no entry/read/stream holds or stderr errors, and a RUNNING supervisor with zero restarts. Three profitability-only owners were already reserved through Eastern midnight; their paused orders consumed no pending-order slots and were not reported as uptime. This is startup evidence, not proof of long-run recovery, snapshot qualification or rewards. Continue read-only observation of natural pause/recovery, absence of economics-only owner release before expiry, and fresh grace/release behavior after midnight.
