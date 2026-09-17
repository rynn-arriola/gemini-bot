# Fresh pool-breadth selection audit

Snapshot: September 16, 2026, 22:15:54 ET; rank report 22:15:44 ET.
Read-only authenticated REST checked every working order against exact canonical
client/server identity, symbol, side, outcome, quantity and price. There were 40
owned size-15 orders across 20 selected markets, zero positions, and no selected
owner missing from the rank report. All 20 had two working orders, no concrete
rank rejection and positive leave-one-out marginal net estimates. Their modeled
worst midpoint distance was 2.5 cents, within the 10-cent reward bound. Aggregate
BUY reservation was $285, below the unchanged $300 cap; no cash balance is reported.

Discovery v2 was live and build/policy/permit matched. SQLite integrity passed;
five public shards were connected with no read/stream hold, logged warning/error
or supervisor retry. Exactly one authorized selection-reset audit exists. Nine
markets differ from the old 20-owner cohort; eleven independently reselected.
All selected contracts belong to 12 single-event pools. This demonstrates the
new discovery preference in the chosen cohort, not a ban on broad pools or proof
of profit, future continuity, complete pool competition coverage or qualification.

## Every selected market

Ordered by current modeled leave-one-out marginal net per reserved capital-hour
among resting pairs. This is a reconstructed economic ranking, not actual dollars
per hour, the historical admission sequence or the old `priority_score` field
which can be zero because the reporting-only local 50% estimate is infeasible.
Repeated pool budgets below are the same shared budget, not additive amounts.

| Rank | Contract | Pool USD/day | Sharing events | Working orders | Marginal efficiency proxy |
|---|---|---:|---:|---:|---:|
| 1 | GEMI-FED261030-HIKE25 | 150.00 | 1 | 2 | 0.004870 |
| 2 | GEMI-FED261030-MAINTAIN | 150.00 | 1 | 2 | 0.004636 |
| 3 | GEMI-NEXTPRESS29-ALINAHABBA | 150.00 | 1 | 2 | 0.001748 |
| 4 | GEMI-BESTAI26-CHATGPT | 250.00 | 1 | 2 | 0.001571 |
| 5 | GEMI-BALLONDOR26-MBAPPE | 250.00 | 1 | 2 | 0.001302 |
| 6 | GEMI-NEXTPRESS29-ANNAKELLY | 150.00 | 1 | 2 | 0.001299 |
| 7 | GEMI-BALLONDOR26-KANE | 250.00 | 1 | 2 | 0.001129 |
| 8 | GEMI-BRPRES2026-FBOLSO | 150.00 | 1 | 2 | 0.000817 |
| 9 | GEMI-DEMVP28-BESHEAR | 150.00 | 1 | 2 | 0.000799 |
| 10 | GEMI-DEMVP28-BUTTIGIEG | 150.00 | 1 | 2 | 0.000772 |
| 11 | GEMI-BESTAI26-CLAUDE | 250.00 | 1 | 2 | 0.000750 |
| 12 | GEMI-BRPRES2026-LULA | 150.00 | 1 | 2 | 0.000726 |
| 13 | GEMI-REPNOM2028-REPNOM2028DONALDJTRUMPJR | 250.00 | 1 | 2 | 0.000510 |
| 14 | GEMI-FRA2027-JEANLUC | 150.00 | 1 | 2 | 0.000352 |
| 15 | GEMI-CTRLUSHOU-GOP | 250.00 | 1 | 2 | 0.000346 |
| 16 | GEMI-CTRLUSHOU-DEM | 250.00 | 1 | 2 | 0.000307 |
| 17 | GEMI-FRA2027-EDOUARD | 150.00 | 1 | 2 | 0.000284 |
| 18 | GEMI-DEMNOM2028-DEMNOM28BESHEA | 250.00 | 1 | 2 | 0.000282 |
| 19 | GEMI-PRES2028-BESHEAR | 150.00 | 1 | 2 | 0.000275 |
| 20 | GEMI-CTRLUSSEN-GOP | 250.00 | 1 | 2 | 0.000165 |

## Top-five explanations and uncertainty

1. Fed October decision, hike 25 basis points: $150 single-event pool with five
   active contracts. Highest modeled capital-hour efficiency at the snapshot;
   slightly lower modeled execution cost than the maintain-rate contract.
2. Fed October decision, maintain rate: shares that same $150, not another $150.
   Similar score/capital characteristics and slightly lower marginal net then.
3. Alina Habba, Trump's next Press Secretary: $150 single-event pool/six active
   contracts; lower modeled execution cost than Anna Kelly and positive marginal
   contribution after accounting for both selected members of this pool.
4. ChatGPT, best AI at end of 2026: $250 single-event pool/seven active contracts;
   lower modeled execution cost than Claude, despite more observed pool pressure.
5. Kylian Mbappe, Ballon d'Or: $250 single-event pool/31 active contracts. Greater
   breadth and uncertainty, but still positive modeled marginal economics and
   lower modeled execution costs than Harry Kane at the audit snapshot. Current
   venue event start was October 26 at 19:00 UTC, so normal sports timing applies.

Event names and active-contract counts were rechecked through public REST after
the order audit. None of these pools is assumed competition-free. Fed's public
qualifying-maker count of zero is historical metadata, not zero live liquidity.

Important sensitivity: the existing model uses roughly 209.775 planned scoring
minutes for Fed versus 1080 for the other top-five events. That shorter estimated
window materially boosts Fed's capital-hour ranking. These window assumptions
and the unseen-competition prior remain uncalibrated; this audit does not prove
Fed has the largest dollar payout or superior realized profitability. Rankings
can move while healthy selected markets remain sticky. No audit-driven rotation
or pricing/risk change was made.
