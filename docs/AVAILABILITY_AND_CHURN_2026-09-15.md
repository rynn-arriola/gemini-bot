# Availability and quote-churn repair

September 15, 2026. Source-only: no restart, deployment, new permit, live order,
authenticated probe, induced fill/outage, or Polymarket change.

## Priority repairs

1. **Bounded shutdown/cancellation:** cancellation's deadline includes admission
   and send. Exit workers have a 45-second overall budget in addition to the
   existing three-attempt/30-second reducing-send round. Drain starts cancellation
   of known, durably owned entries immediately while exit owners finish; it joins
   those owners for at most 50 seconds and has a 90-second overall budget. Worker
   failures propagate rather than disappearing before forced retry. Timeout is
   attention, never proof of flatness or permission to replay an unknown order.
2. **Correlated WebSocket recovery:** a 256-request, connection-specific tracker
   distinguishes reads from mutations. Known read 429/500/502/503/504 replies
   enter existing recovery; longer server cooldowns remain mandatory. Auth, bans,
   malformed/uncorrelated replies and mutation errors stay fail-closed. Only an
   account-subscription ACK establishes readiness. Old streams retain their own
   tracker until discarded. REST's 60-second operation budget includes lock,
   admission and retries; timeouts do not erase cooldowns.
3. **Recovered-market holds:** lifecycle/reward reasons are separate from fills
   and other holds. Active alone is insufficient: current valid metadata plus
   fresh classified terminal/flat REST/WS proof and unchanged lifecycle version
   may release only the recovered reasons. Races/unknown state retain the hold.
   Temporarily unrewarded unexpired owners remain tracked so recovery is possible;
   expired owners still retire safely. This reserves a tracked slot during pause.
4. **Disconnect/send races:** socket identity is checked after admission. An old
   request cannot silently use a replacement connection. Proven-unsent intents
   are terminalized; attempted sends remain unknown. V7 unsent maker transport
   failures enter existing recovery without replay. Close detaches first; an old
   close cannot erase a new connection. Terminal cancellations are rechecked both
   in the inner cancellation task and after socket admission. Failed/unknown
   cancellations remain latched.

## Churn and snapshot impact

The owner's 280 cancellations/280 replacements describes an earlier window.
A separate current-log capture for **22:14:01–22:44:01 ET** contained **306
CANCELED and 332 NEW events**, plus two stream-recovery starts/completions and no
catalog-owner-refresh event. These are lifecycle event counts, not reconstructed
replacement pairs. Old-build logs do not prove every cancellation was tick-driven;
the earlier 280/280 window was not exactly reproduced.

The running build `7509656` (v6) predates the source-only v7 retention logic.
Its churn is not a live measurement of that newer behavior.

- Ranking/orchestration now share the resting price/size/tick safety predicate.
  Healthy farther-away pairs remain if actual resting prices pass reward distance,
  minimum payout, positive net and current eligibility. Twenty repeated synthetic
  maintenance checks preserved a safe pair without cancellation.
- Removed retention's redundant raw-book midpoint precheck. Full reward/economic
  evaluation still uses the existing external-book calculation, subtracting own
  orders; no distance/economic limit is relaxed.
- Maintenance/adverse-guard decisions have reason-coded counters/logs: unsafe
  two-tick distance, book unavailable, incomplete/filled pair, account unverified,
  invalid price, reward-distance/economics, or legacy target change. These are
  quote-decision counters, not every account-wide cancellation.
- Continuity updates on order transitions as well as the 250ms clock. Sub-clock
  gaps are captured. Separate bounded views measure either-side qualification
  and two-sided bonus continuity, with gap counts/current/last/longest/cumulative
  completed-gap estimates and NEW/CANCELED event counts. Replay can inflate event
  counts, so they are not labeled unique replacements.
- Estimates are per-run local observations, not venue snapshot counts. Gaps in
  observation over three seconds receive no uptime credit. One missing side is
  not falsely counted as loss of all possible reward qualification.

**Safety still wins:** movement inside the approved two-tick distance still
requires cancellation. Removing that rule would change owner policy and is not
part of this repair. New quotes remain exactly two ticks behind touch. Necessary
cancels, inventory management and recovery still create gaps; wider retained
quotes can score less. No reward or 50% qualification is guaranteed. Midnight's
exception is not extended, and sticky identity is not permission to retain an
unsafe/unprofitable quote.

## Verification and limits

- **435 production + 28 script tests = 463 passed**, including **41 new tests**
  in `tests/test_availability_review.py`. Final production run: 55.749 seconds.
- Three isolated in-memory reversals (generic WS failure, disabled hold recovery,
  disabled retention) were independently detected by their tests.
- Compilation, whitespace and native Windows offline policy/storage checks pass.
  Script verification caught a tracker-name collision with the read-only preflight
  request counter; the private tracker name fixes it and that suite now passes.
- Existing midnight, exact-fill/exit, unknown-ACK, risk and supervisor cases pass.
  No live trading smoke, induced fault or long-run acceptance is claimed.

Natural fills/maintenance/churn require observation after separately authorized
deployment. Held-inventory lifecycle changes, unknown ownership/accounting/send,
rejected exits, storage failure and HARD still require attention. Finite wait
budgets do not guarantee venue execution or recovery from OS termination. A
shutdown failure can invoke another bounded emergency-drain attempt, but neither
attempt may replay an unknown mutation or report unproved flatness.

Official protocol rechecked:
[Gemini response/error format](https://developer.gemini.com/prediction-markets/websocket/message-format)
and [order/contract streams](https://developer.gemini.com/prediction-markets/websocket/streams).
