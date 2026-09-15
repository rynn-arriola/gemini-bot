# First one-leg live maker canary — September 15, 2026

## Two-sided canary: passed

Immediately before the date-bounded continuous observation, the owner approved a 30-second two-sided maker canary on `GEMI-BESTAI26-CHATGPT`: BUY YES 10 @ $0.15 and BUY NO 10 @ $0.80, maximum combined reservation $9.50. Both prices were asserted exactly at activation. Gemini acknowledged working maker orders; the production safety and drain path then authenticated zero positions and zero active orders. No fill occurred.

Two earlier exact-symbol attempts stopped before permit consumption or account mutation because their rapidly moving BTC contract was not the representative selected by the full reconciled reward universe. That fail-closed behavior was correct. A separate transient reward-catalog pagination duplicate exposed an offset-snapshot race; catalog reads now restart up to three bounded attempts instead of accepting a missing event.

## Repeat verdict: passed

After repairing the replacement loop and passing 116 production tests, the owner authorized one repeat with the same fresh exact order: `GEMI-ZEC2609152100-HI1220`, BUY YES 10 @ $0.02, five-second maker window.

The repaired run created exactly one new durable intent and one matching Gemini order-history row. Gemini recorded the order from 15:18:49.750Z through terminal cancellation at 15:18:54.996Z, approximately 5.25 seconds. Its entire quantity of 10 remained, so it did not fill. Durable fill count remained zero. Final authenticated reconciliation proved zero positions and zero active orders, status reached `stopped_flat`, and no process remained. The repeat one-use permit was consumed.

The one-leg maker acknowledgement, resting and cancellation path is now verified live. Because no fill occurred, this result does not independently exercise the production exact-exit path. Continuous activation remains a separate owner decision after the two-sided canary.

## Verdict: flat, but commissioning failed

The owner authorized a bounded live maker-mechanics test on `GEMI-ZEC2609152100-HI1220`: BUY YES, 10 contracts, $0.02 limit, maker-or-cancel, with a five-second commissioning window. The price was asserted exactly at runtime. The uncertain local daily-uptime proxy was ignored only for this one-leg mechanics test; reward eligibility was not claimed.

Gemini authenticated four sequential orders rather than the intended one. Each order remained completely unfilled and was terminally canceled with all 10 contracts remaining. Durable execution-fill history is empty. Final authenticated reconciliation proved zero positions and zero active orders. Runtime status reached `stopped_flat`; no matching process remained. The one-use permit was consumed.

Because the test intended exactly one order, this is not accepted as a commissioning pass even though the account ended flat and no trading loss occurred.

## Root cause and repair

Candidate placement correctly recognized the commissioning-only uptime exception. A separate cleanup loop still tested reward `eligible_now` directly, canceled the working order, and allowed maintenance to replace it. The four creation/cancellation intervals were approximately 1.1, 1.0, 1.0 and 2.3 seconds.

Cleanup now uses the same narrowly scoped actionable-candidate predicate as placement. The exception remains limited to a bounded one-leg canary and only when uptime feasibility is the candidate's sole rejection. Stale data, changed exact price, invalid geometry, exposure, ownership, stream, permit, fill, exit and reconciliation gates remain enforced.

## Verification

- 116 production tests pass, including a regression that retains an actionable mechanics-canary order and cancels a candidate carrying any additional rejection.
- 28 script/observer tests pass.
- Native Windows policy/storage validation and Python compilation pass.
- Live configuration is structurally valid and startup remains paused.

A second live canary requires a fresh read-only selection, a newly displayed exact price, explicit owner approval, and a new one-use permit bound to the repaired build. Continuous trading remains blocked.
