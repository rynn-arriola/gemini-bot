# XRP/ZEC churn investigation

September 16, 2026. Diagnosis only; live source, permit, policy and processes
unchanged. No venue requests, induced fills, reconnects or restarts. Source
reviewed from main `1ec2edc`; deployed runtime is v10.

## Findings and priority

**P2: second maker leg can outlive its canceled first leg.**
`_quote_pair_current` checks the first YES order before calling `place_maker`
for NO. Transport admission may then wait. `_validate_send` rechecks current
price/account/risk state and whether a symbol cancellation is still active, but
does not bind NO to the exact first YES order and recheck that it remains intact
at final admission. If cancellation completes during that wait and the book
again permits the original NO price, NO may still be sent. Maintenance then
cancels it as `pair_incomplete`, creating avoidable traffic and a restoration gap.

An offline probe using the existing isolated `RuntimeRepairTests` fixture ran
the real `_quote_pair_current`, `LiveWebSocket.place_maker`, transport admission,
final mutation guard and account reducer, with a fake connection/governor only.
The control produced YES NEW -> NO NEW and retained the complete pair. Injecting
an authenticated zero-fill YES CANCELED at second-leg admission produced YES NEW
-> YES CANCELED -> NO NEW; the next maintenance requested cancellation with
`pair_incomplete`. No actual network send or live journal access occurred in
this reproduction. Local probe: `data/churn-presend-probe-20260916.py`.

Recommended repair: associate each second-leg intent with its expected first
order/pair generation and revalidate that exact working, unfilled first leg
immediately before socket send. A failed check must remain definitely unsent,
terminalize only that unsent intent, and let existing symbol cleanup/reranking
continue. Add deterministic cancellation-during-admission, fill, partial-fill,
stream-generation, healthy-pair and unrelated-market coverage. If the second
send was already attempted, preserve existing unknown-outcome reconciliation;
never retroactively classify it as unsent.

**P2: cancellation evidence needs more specific reasons.**
`_current_quote_target` returns None for a missing contract, stale/unavailable
book, an empty book after subtracting own working orders, or invalid two-sided
target prices. The adverse guard labels all these `book_unavailable`. Existing
logs omit the corresponding book/target prices, sequence and freshness evidence.
They cannot distinguish every necessary cancel from an avoidable loop, nor
prove an own-order/public-depth timing mismatch. Add bounded, redacted decision
context and distinct reasons in a future source repair. Keep the separately
prepared account callback fix in the deployment queue.

Do not add a blind delay, relax the two-tick boundary, retain unverified orders,
or add WebSockets as a response to this evidence. This review does not establish
a connection-capacity bottleneck. Safety cancellation still takes priority over
resting time.

## Measured v7 window

Fixed interval: September 15 **23:35:22.066465** through September 16
**00:05:22.066465**, Eastern. Source: v10 structured logs; read-only SQLite
queries were restricted to order-intent price/outcome and named audit events.
No sensitive account values were retrieved or published.

| Observation | XRP | ZEC |
| --- | ---: | ---: |
| CANCELED / NEW order events | 114 / 114 | 27 / 27 |
| Median lifetime of orders canceled in this interval | 6.664 s | 20.954 s |
| Those lifetimes shorter than 1 second | 12 | 5 |
| Time with neither side acknowledged working | 39.812 s | 11.453 s |
| Time with only one side acknowledged working | 23.471 s | 5.151 s |
| Either-side acknowledged presence | 97.788% | 99.364% |
| Two-sided acknowledged presence | 96.484% | 99.078% |

These are log-observed order-presence statistics, **not qualifying uptime or
exchange snapshot counts**. They do not check historical reward distance,
freshness, economics, venue receipt times or the complete scoring day. Order
state was reconstructed from this run's earlier events before integrating the
fixed interval. Completed lifetimes can begin before the interval; live orders
at its end are censored. Do not compare these directly with an older build's
different market window to claim percentage improvement.

The 83 reason-coded decisions comprise 50 `inside_two_tick_distance`, 15
`book_unavailable`, nine `pair_incomplete`, seven `reward_distance_or_economics`
and two `inside_two_tick_distance_or_invalid_price`. Decisions and order events
are different units and may overlap through shared cancellation tasks. All
141 CANCELED/141 NEW events were on XRP/ZEC; no other market had either event
inside this interval. There were no fills or warning/error/critical events.

Two NO NEW events arrived while the local log reconstruction had no working YES
for XRP (00:01:06.642482 and 00:04:16.404679 ET). The first trace was:

| Time ET | Observation |
| --- | --- |
| 00:01:06.255851 | YES NEW |
| 00:01:06.279705 | `book_unavailable` cancellation decision |
| 00:01:06.473485 | YES CANCELED |
| 00:01:06.642482 | NO NEW |
| 00:01:07.653358 | `pair_incomplete` cancellation decision |
| 00:01:07.687724 | NO CANCELED |

The live trace supports the symptom, but lacks socket-send timestamps; it cannot
prove that this particular NO was sent after YES became terminal. The offline
probe independently confirms that the avoidable pre-send race exists. Do not
attribute every incomplete-pair decision or the entire churn rate to it.

## Continuity and limits

Midnight was crossed by the same Python worker (6332), launcher (24252) and
supervisor (22052), with zero supervisor restarts. The uptime override became
false and the reward epoch advanced to September 16 at 00:00:00.273457 ET.
Catalog refresh retired one tracked owner and added one at 00:00:25.854772;
code explicitly invalidates the cached epoch on identity changes, explaining
the repeated epoch event at 00:00:27.003511. That event alone is not a restart
or an uptime-history reset. The available catalog log does not identify which
owner was retired or prove its exact retirement reason.

At 00:03:43 ET, the rank report contained 14 eligible of 19 candidates under
normal day-specific admission. At 00:08:33 ET, local status remained quoting,
28 pending orders, zero positions/holds/dropped logs. No warning/error/critical
or fill events had appeared. This is local telemetry, not a fresh venue audit.

50% impact: the proposed guard removes an avoidable orphan submission and may
reduce cancellation traffic and restoration gaps. It cannot recover missed
snapshots or guarantee qualification. Genuine adverse moves, missing external
depth, safety exits and ineligible economics must still cancel. No production
repair or policy change was made in this investigation; implementation and its
tests remain pending, followed by a separately authorized deployment.
