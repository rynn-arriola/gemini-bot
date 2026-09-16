# Fill continuity — September 15, 2026

## Release boundary

Owner approved the [audit recommendations](FILL_FLOW_AUDIT_2026-09-15.md), including
additional paced exit rounds after positive terminal/residual proof. **Source
only, no restart or deployment.** No live process, order, permit, shared journal
or Polymarket file was changed. The running v6 remains on its existing loaded
build and original finite authority; a future deployment requires new explicit
authorization and a fresh build-bound permit. New scoped exit service is v7-only.

## Behavior and tradeoffs

- One independently scheduled exit worker per market; at most configured owner
  capacity. Slow REST/IOC confirmation no longer occupies the entry lifecycle
  lock or the safety clock. All workers use the same account, journal, transports
  and shared rate governor, never independent mutation authority.
- Fill-priority cancellation bypasses unrelated lifecycle/exit waits and other
  symbols' confirmation waits. Simultaneous callers share the existing per-order
  cancellation task. Unknown cancellation still propagates to fail-closed safety.
- V7 stale/thin exit books, known-terminal exhausted rounds and typed transient
  account-read failures wait for 5, 10, 20, 40 then 60 seconds. Longer server
  cooldowns win. Each new round performs fresh positions/active-order reads,
  checks WS/account classification, ownership, terminal intent state and exact
  same-sign non-increasing residual. A round retains at most three attempts in
  30 seconds, with a final post-pacing send deadline. No timer/loss/slot reset.
- Normal entry gates remain. Unrelated new entries during a known-terminal wait
  require proof no older than 60 seconds, unchanged residual/fill version/stream
  generations, fresh classified WS state and no active exit worker or unresolved
  intent. Stale/mismatched account proof cannot grant this exception. Existing
  safe resting pairs remain governed by the separate quote-retention rules.
- Net-flat opposite fills and flat-during-cancel paths now complete both outcome
  deadlines only after fresh REST/WS flat and terminal-order proof. The cleanup
  transaction clears only the fill hold, not independent inactive/reward holds.
  Saved deadlines reconstruct fill holds at startup. No manual cash/fill adoption.
- A same-symbol REST position publication lag gets a two-second recheck cadence
  within a ten-second convergence budget, with no new proof or mutation during
  disagreement. Persistent or unrelated mismatch remains fatal, and foreign-order
  evidence cannot be hidden by that wait. Obsolete fill notifications cannot
  cancel a newly eligible pair after proven lifecycle completion.
- STOP/HARD stops scheduling and waits for already-running workers before the
  existing bounded force-drain path. No concurrent duplicate exit. Rejected exits
  now remain latched against automatic retry by that forced path too.
- Status includes exit wait reason, round count and retry delay, without cash or
  position quantities. Known waits log once per paced round. Completed lifecycle
  cleanup is audited; maps release on flatness and task ownership is bounded.

The purpose is to preserve healthy markets' qualifying resting time while an
affected market is safely reducing. It does not restore missed snapshots or
change the 50% threshold, midnight override cutoff, reward ranking, quote size,
breadth/exposure, original five-minute/sports deadlines or $30 six-hour HARD.
Additional reducing rounds may incur more execution fees than abandoning an
exit; actual matched losses/fees still feed the unchanged risk authority.

## Review and limitations

Reviewed startup reconstruction, normal full/partial/opposite fills, concurrent
market cancellation/exit, terminal/unfilled/partial IOC outcomes, proof races,
server pacing, round exhaustion, stale depth, STOP/HARD, shutdown and rollback.
Transient read waits retain the underlying REST client's lock/timeout/retry
discipline rather than canceling its thread-backed I/O merely for scheduling.
Each round adds bounded proof reads through existing quotas; this is not a new
high-frequency REST replacement for WS. Sustained live load remains unmeasured.

Unknown acknowledgements or mutation outcomes, rejection, missing deadlines,
inconsistent/unknown ownership, malformed accounting or software/storage faults
still stop trading for attention. A contract lifecycle change with held inventory
retains its existing global fail-closed behavior; settlement-wait handling is not
implemented. An unavailable exchange can prevent timely flattening. This release
does not promise that the process can never stop, guarantee a reward, clear HARD,
or self-modify code. Legacy v2–v6 and forced drain keep finite exit sequences.

Official [Gemini lifecycle documentation](https://developer.gemini.com/prediction-markets/order-lifecycle)
was rechecked September 15: order terminal state and actual position/fill truth
remain separate; cancellation alone is not flat proof. No wire protocol changed.
The existing Polymarket reference informed scoped owner/proof design, not Gemini
timing, passive-exit or strategy policy.

## Verification

New offline suite includes 45 cases: both reproduced cleanup defects, independent
holds, read/generation/fill races, missing deadline, full/partial/NO exits,
three-terminal-unfilled then successful later round, cooldown, residual increase/
reversal, missing ACK/no duplicate, rejected/no forced retry, unrelated-entry
proof expiry, original-deadline preservation, concurrent cancellation/workers,
stop/drain serialization, bounded REST publication lag, stale fill notifications,
foreign-order precedence, full-width 30-pending-entry flow (one affected sibling,
29 untouched unrelated entries), transactional rollback and legacy limits.

Three in-memory reversals independently failed as intended: remove flat cleanup,
restore lifecycle-locked cancellation, and restore lifetime-bounded exit handling.
These used isolated test fixtures, never live account data or network mutations.
Final verification: **394 production tests + 28 script tests = 422 passed**.
Production suite completed in 48.892 seconds. Compilation, native Windows offline
policy/storage release validation and whitespace checks passed. Three reversal
checks were rerun successfully against the final source. A local read-only check
at 22:26:28 Eastern found the same supervisor 19180 / runner 34008, restart count
zero and fresh quoting heartbeats; it is not venue-state proof. Live acceptance
remains pending separate authority. GitHub commit verification is reported in
the handoff; no active permit is part of the committed tree.
