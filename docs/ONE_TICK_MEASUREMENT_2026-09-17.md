# One-tick live strategy with separate queue measurement

The owner approved starting the bot, changed normal quoting from two ticks to
one, and clarified that this is separate from best-bid/back-of-queue research.
They declined scheduling and will request the review tomorrow themselves.

`config/live_policy.json` remains policy v2, with offset 1 and quantity 15.
Discovery, profitability/ranking, current target construction, resting retention,
post-admission maker guards and adverse-move cancellation already share the
configured offset. Diagnostic labels now say configured distance, not two ticks.
A book with YES bid .48 / ask .52 and a .01 tick produces YES/NO BUY .47/.47.
This is one tick behind each external best bid, not at the best bid. Existing
safe farther quotes may remain sticky under the normal economic checks.

The separate queue observer still records depth, 60-second side-specific sell
flow, hypothetical depletion and natural cancel-to-terminal timings. It does
not alter orders. The zero-offset queue pilot stays unconfigured; its original
v3 two-tick nonpilot configuration restriction remains a separate future review
item before any one-tick-nonpilot pilot rollout. Do not enable it merely because
the measurement run succeeds or assume aggregated depth is exact queue position.

All pending/exposure/size/exit/HARD limits and sticky identities are preserved.
One tick is closer to execution and may increase fills or change eligibility;
no payout benefit is guaranteed. New quotes use one tick while measurement runs
separately. No forced churn, induced fill, selection reset or schedule was added.
The local 50% calculation remains reporting-only; downtime earns no snapshot credit.

Offline tests cover exact one-tick maker pairs, retention, adverse moves to touch,
second-leg pacing races, seven-of-fifteen fill, HARD, capacity and full-width
priced exposure. Historical incident fixtures explicitly retain their original
two-tick prices. New policy/build requires fresh unconsumed authority; v21 is not
valid for this release. Final offline verification: 780 production tests and
28 script tests passed (808 total), plus offline release validation. No live
activation occurred; user must initiate live trading. No v22 permit or schedule
was created. Read-only readiness evidence is recorded in STATUS/FINDINGS.
