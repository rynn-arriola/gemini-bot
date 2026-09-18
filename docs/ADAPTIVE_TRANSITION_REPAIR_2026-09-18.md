# Adaptive cancellation-transition repair

## Scope and diagnosis

Owner first requested a restart-ready repair, then explicitly approved the controlled restart after verification and publication. Candidate branch: `fix/adaptive-transition-continuity`; deployed source commit: `52454db`.

Read-only live inspection found repeated `adaptive_mode_transition` cancellations but no completed `adaptive_queue_mode` audit entries. An offline reproduction established that concurrent adaptive checks treated the bot's own entry-cancellation flag as lost observation continuity. The warmup reset during cancellation, returning desired mode to fallback rather than completing best-bid promotion.

Two related scheduling gaps mattered: the adverse guard returns early without working orders, so quiet-book cooldowns lacked regular evidence checks; completed three-second flat proofs and mode switches did not explicitly wake ranking.

## Repair

- Separate healthy observation from permission to mutate. Owned cancellation does not erase otherwise current book/account evidence, but still blocks entry and flat-proof acceptance. Real fills, stale data, recovery, blocked markets, exits and uncertain accounting still invalidate evidence.
- Refresh only the bounded adaptive membership on the existing safety clock. No new thread, socket, venue poll or rate allowance.
- Wake the existing coalesced dirty-symbol scheduler after accepted flat proof and mode switch; rerank before sending the changed price.
- Preserve terminal cancellation, later public sequence, cooldown, fresh generation/fill-bound account proof, maker-only admission and all existing risk/HARD gates.

Historical tests now explicitly select the nonadaptive policies they were written to exercise instead of accidentally inheriting deployed v5. The active policy itself is unchanged.

## Evidence and limits

Final verification: **953 production tests + 28 script tests = 981 passed**. Compilation, offline Windows policy/durable-storage validation and whitespace checks passed. Tests used the root virtualenv through a candidate-local directory junction and explicitly imported candidate `src`; no active environment/configuration was changed.

Nine new deterministic offline regressions exercise interleaved cancellation/checks, the actual 600/60/10-second timings, quiet-book cooldown, proof/scheduler handoff and real fake-transport best-bid placement; unknown cancellation, a seven-of-fifteen fill and unchanged deadline, unsafe account/stream/book/exit conditions, missed continuity, expiry, depth loss, the safety clock, and independent evidence for 20 members. Restoring the old cancellation-as-unsafe predicate in memory makes the promotion regression fail. No live orders or induced fills/faults were used for tests.

At approximately 08:15 ET, authenticated read-only verification of untouched v23 found 34 canonically owned size-15 orders across 17 markets and no positions. Journal integrity and deployed build/policy binding matched, the supervisor was RUNNING, and reported read/stream holds were empty. This is a time-bound checkpoint, not live acceptance of the new code.

A 1,000-iteration synthetic benchmark of the additional 20-member local check measured median 0.397 ms and p95 0.892 ms, with no venue calls. This isolates local decision cost; it is not a live latency/CPU guarantee or a full-buffer benchmark.

## Continuity and restart handoff

This targets repeated failed-promotion churn without rotating sticky owners or transferring uptime. Genuine mode transitions still create an unquoted cancellation/proof/cooldown interval; neither tests nor resting presence prove Gemini snapshot qualification, exact queue rank, reward or fill avoidance.

Deployment completed in that order: v23 reached `stopped_flat` with zero orders/positions and its supervisor stopped; `main` fast-forwarded to `52454db`; a fresh verified backup and read-only preparation proved flat/order-free state, healthy journal, preserved economic/sticky history and non-HARD capacity; a new v7 exact-build/policy/account permit launched v24 through the persistent Windows task. Initial authenticated acceptance found 34 owned size-15 orders across 17 markets, zero positions, five healthy public shards, no read/stream holds or backup error, and zero supervisor restarts.

No automatic policy renewal: the unchanged adaptive envelope expires September 19 at 05:45 ET, after which normal guarded one-tick fallback applies. Real best-bid promotions, requeues/demotions, natural fills/exits and unaffected resting orders still require long-run observation. The restart interval receives no snapshot credit.
