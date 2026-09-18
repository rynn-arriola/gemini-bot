# Missing public reward event: startup and runtime isolation

## Incident and boundary

The user-run v22 preparation failed at 20:47:31 ET on September 17 with an
ordinary public event HTTP 404, before permit creation/live launch. At 20:51 ET
authenticated read-only account inspection found zero orders and positions.
The exact event identity was not captured by the old failure diagnostic; do not
guess it or attribute the failure to the one-tick setting.

The official [Get Event reference](https://developer.gemini.com/rest-api/prediction-markets/events/get-event)
was rechecked September 17 and documents a 404 response for the per-event GET.
The endpoint-specific isolation below is our engineering policy, not a claim
that every 404 proves a closed or settled market.

## Repair

- Shared event-read adapter maps only `ApiError(status=404, retry_after=None)`
  from `rest.event(ticker)` to absent metadata. No global REST retry classifier,
  account route, catalog route or mutation handling changed.
- Discovery logs the public event ticker/status and continues other candidates,
  including siblings in the same reward pool. Missing saved owners retain their
  identities and watched capacity, and are preferred on subsequent discovery.
  No negative cache survives the next load. All-missing startup remains
  `NoMarketsAvailable`, never fabricated readiness.
- Runtime refresh blocks every affected event sibling before cancellation yields.
  It confirms cancellation through the existing symbol-specific owner, marks
  that metadata unusable, and continues healthy events. Unknown cancellation
  still propagates. Missing events are read once per refresh even with siblings.
- Recovery requires new metadata and the existing terminal-flat/account/book
  proof. Other holds, held positions, original deadlines, exposure and HARD stay
  intact. A 404 never authorizes a sale, settlement, history reset or stale quote.

## Continuity and verification

The affected event can lose resting time while metadata is unavailable; it gets
no snapshot credit. Unrelated markets no longer need a global stop solely for
this 404. Missing metadata alone does not immediately rotate a sticky owner;
existing proven lifecycle and sustained-failure retirement rules are unchanged.
One-tick, size 15, 40 pending/20 owners/32 watched, all monetary/time/HARD limits,
five public plus one private socket, and disabled best-bid pilot are unchanged.

Verification: 797 production tests passed in the full run; one subsequently added
capacity-reservation test passed separately (798 production cases total), and
28 script tests passed. Compilation, whitespace and offline release validation
passed. Eighteen new cases include real preparation, no-market and held-inventory
paths, discovery/runtime isolation, siblings, recovery, unavailable-slot capacity,
and propagation of authentication/cooldown/unknown-cancel errors. Reverting the
event-read behavior in memory made all three selected startup/discovery/runtime
regressions fail as expected; no source or live venue fault was induced.

At 20:57:21 ET the repaired read-only preparation passed with zero positions/orders,
no unresolved intents, healthy integrity, complete non-HARD risk, unchanged
economic history and all 20 saved owner identities. There were 32 watched,
30 ranked and 12 actionable markets. Automatic backup succeeded. That fresh run
did not encounter another event 404; isolation evidence is deterministic offline
coverage, not an induced live fault. It disconnected without live activation or
permit creation. Re-run the previously provided prepare-then-launch command as
the user; it creates fresh exact-build authority. Old permits remain invalid.
