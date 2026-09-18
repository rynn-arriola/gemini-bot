# Shared adaptive settings proposal — September 18, 2026

Status: **research proposal, not owner-approved live policy**. No active source,
configuration, orders, permits or processes were changed. No restart or activation.

## Corrected scope

The owner clarified that two markets are the research sample, **not the intended
permanent scope**. The finished strategy is one common adaptive rule for all
selected markets, including newly selected replacements, within the existing
40-order/20-owner limits. Each market/outcome supplies its own fresh observations;
no separate manual per-market threshold table or research trial is required.

The current `d955f4e` implementation still enforces exactly two named symbols.
Do not claim it is already an all-market release. Before production-wide rollout,
generalize policy scope into a bounded all-selected mode plus a two-symbol research
mode; remove the two-symbol assumptions in policy validation/planning/observation,
keep per-symbol/generation state and cold-start fallback, and verify full-capacity
CPU/memory/shared-quota/failure isolation. A newly selected contract starts one
tick back and cannot inherit another symbol's evidence, anchors or uptime.
This is scope integration work, not twenty manual calibrations.

## Proposed common envelope

| Parameter | Proposal | Rationale / limitation |
| --- | ---: | --- |
| Continuous warmup | 600 seconds | Observe ten complete one-minute windows while normal one-tick quoting remains available; engineering choice, not a statistical confidence threshold. |
| Stable promotion | 60 seconds | Avoid promotion from a brief improvement; not a guarantee against a later shock. |
| Maximum observation gap | 3 seconds | Existing continuity boundary; a gap restarts warmup. |
| Base warning buffer | 250 contracts | Substantially above the research samples' largest observed 60-second sell quantities of 136 and 117; also protects cold starts better than the synthetic 15/30-contract examples. It is **not** guaranteed quantity ahead. |
| Burst multiplier | 1.0 | Reserve the entire observed 60-second selling peak **plus** peak depth loss, which can already double-count executions; never divide by 60 to assume smooth selling. |
| Entry depth multiplier | 1.5 | Require 50% more external depth than the current warning scenario. This is an explicit model margin, not a proven loss bound. |
| Minimum requeue benefit | 250 contracts | Avoid moving to the back for small apparent gains; aggregate depth does not establish exact queue rank. |
| Post-cancel cooldown | 10 seconds | Reduces repeated refreshes; fresh terminal/flat proof and later depth are still required and can take longer. |
| Cancellation budget | 10 seconds | Roughly twice the observed 4.9488708-second call-to-terminal tail. It is a breach threshold, **not** a promise or an instruction to wait ten seconds before canceling. |
| Research expiry | September 19, 2026, 05:45 ET | Best-bid becomes unavailable and falls back through normal guards; no automatic renewal, all-market enablement, or scheduled reminder. Recheck if deployment is delayed. |

For each side:

`warning = 250 + peak_sell_quantity_60s + peak_same_price_depth_drop`

`minimum_external_depth = 1.5 * warning`

The runtime derives peaks from its current healthy generation, not from seeded
archive maxima. The base buffer therefore matters at restart/new selection.
Archive interval depletion used below is a separate stronger stress scenario,
not a claim that the exact runtime will reproduce those thresholds.

These numbers are a testable engineering proposal. They are not statistically
calibrated future-risk guarantees or settings justified for immediate account-wide
best-bid activation. Ten minutes of warmup is not ten minutes without orders.

## Research sample and evidence

Read-only canonical ownership confirmed both proposed symbols were current
incumbents. Authenticated read-only verification during this review found 40 owned
size-15 orders on 20 markets, zero positions, healthy integrity, matching current
main build/policy, and no shadow/archive errors. Pilot remained disabled.

Research sample:

- `GEMI-CTRLUSHOU-GOP` — House GOP control contract.
- `GEMI-CTRLUSSEN-GOP` — Senate GOP control contract.

They belong to distinct event pools but are politically correlated; this is not
a diversification or category-generalization claim. Selection is for execution
research among current owners, not a prediction about politics or a highest-payout
ranking. Keep the normal economics, lifecycle, exposure and HARD gates.

Frozen measurement cut: **522 minute reports**, September 17 21:04:23 ET through
September 18 05:45:17 ET. Full archive review exceeded the diagnostic tool's
64-historical-symbol bound, so the in-memory review was explicitly restricted to
current owner symbols and then the named comparison group, without altering raw
files or runtime. Counts below are public observation windows, not independent
trades, exchange reward snapshots or actual pilot fills.

| Research contract | Complete / unavailable rows | YES / NO selling windows | Full-sample stress depth requirement YES / NO | Complete rows passing both depths |
| --- | ---: | ---: | ---: | ---: |
| House GOP | 496 / 26 | 1 / 5 | 780 / 954 | 496 / 496 |
| Senate GOP | 520 / 2 | 1 / 1 | 1,335 / 853.5 | 516 / 520 |

Inputs to this stress screen:

- House peak selling: YES 5, NO 136; peak archived-interval depletion: YES 265,
  NO 250. Minimum observed complete-window depth: YES 1,067, NO 1,276.
- Senate peak selling: YES 117, NO 43; peak archived-interval depletion: YES 523,
  NO 276. Minimum observed complete-window depth: YES 1,220, NO 1,913.
- At the frozen last observation, Senate YES depth was 1,373 versus the 1,335
  stress screen. That narrow admission margin is a reason to retain immediate
  fallback and not force it to stay at best bid.

With the same 250 buffer and 1.5 depth multiplier, increasing the burst multiplier
to 1.25 left House at 496/496 but reduced Senate to 157/520. With burst 1 and depth
multiplier 2, Senate passed 0/520. This is material sensitivity, **not** proof that
the proposed values are optimal. More restrictive common settings remain a valid
choice if the owner prefers less best-bid participation.

The Tampa Bay-Cleveland contract had more selling windows (13 YES / 20 NO), but
passed only 12/271 under the proposed full-sample depth stress, and zero under the
1.25-burst variant. It is not the preferred first execution sample simply because
it traded more. Buttigieg passed 488/488 under the proposed stress but had zero
selling windows on both sides; that makes it less informative for this research.

### Forward-only sample split, with important limits

Freeze observed peaks before September 18 **01:30 ET**, then screen later depth
against those fixed thresholds (without future peak values):

- House: 263 complete training rows; thresholds 772.5 / 954; 233/233 later complete
  rows passed depth. One later YES-selling window exceeded the training peak.
- Senate: 264 complete training rows; thresholds 1,030.5 / 814.5; 256/256 later
  complete rows passed depth. No later selling window exceeded either trained peak.

This split is a descriptive robustness check, not independent validation: the
common parameter proposals were considered with knowledge of the whole sample.
It omits within-minute queue paths, send/cancel timing, fills and execution costs.
Do not equate these ratios with predicted best-bid uptime, safety or rewards.

### Cancellation and continuity

Across the archive, the maximum local call-to-terminal confirmation time was
4.948870799969882 seconds, first visible in the 03:00:06 ET report; the largest
failed/interrupted-call count was zero. Recent rolling maxima near 0.6 seconds
must not erase that historical tail. The archive does not identify its exact cause.

Current-run logs showed House 46 NEW / 44 CANCELED order transitions and Senate
2 NEW / 0 CANCELED. House had 21 selection-unquotable economic cancellations and
one reward-distance/economics replacement; these were not best-bid refreshes.
Adding a ten-second post-cancel delay to 22 historical pair withdrawals would be
about 220 seconds of possible additional waiting before overlap with existing
holds/proofs. This is an illustration, **not** a forecast of pilot churn or gaps.
Safety cancellations remain immediate; cooldown controls replacement only.

## Research acceptance and all-market rollout

1. Owner reviews the common settings. Approval is not inferred from this report.
2. Stage/deploy only through the normal exact-build/policy/account workflow;
   user launches trading. Preserve every current owner, all history and all limits.
3. Verify normal warmup orders, genuine best-bid joins, at least one naturally
   observed cancel/prove/rejoin or fallback path, no duplicate/ambiguous replacement,
   no lost fill/deadline, and unaffected other markets. If an event is not observed,
   report that missing coverage instead of inducing it or claiming success.
4. Inspect natural cancellation tails, observed gaps/churn, and authenticated fills,
   fees and reward results. An over-budget cancel disables best-bid under the
   existing durable latch; unknown outcomes retain account-wide safety handling.
5. Integrate and verify bounded all-selected scope, then apply the **same common
   rule** across selected markets. Expand in controlled groups for performance and
   rate-load checks—not to require new manual per-market settings. Unsuitable/new
   markets quote normally one tick back until their own fresh evidence qualifies.

No promise of a particular payout, fill avoidance, exact queue position or Gemini
50% qualification. Deployment/mode-transition gaps earn no snapshot credit.

## Venue cross-check

Gemini's current [stream reference](https://developer.gemini.com/trading/websocket/streams)
documents price-level L2 quantities, the buyer-maker trade flag and order events.
The [order reference](https://developer.gemini.com/rest-api/prediction-markets/order-management/place-order)
maps WebSocket maker-only orders to `timeInForce: MOC`. Post-only prevents an order
from immediately taking liquidity; it does not prevent a later resting fill.
These venue facts do not validate our chosen buffer/multipliers or reward outcome.
