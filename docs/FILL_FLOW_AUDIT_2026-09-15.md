# Fill-flow audit — September 15, 2026

Follow-up: owner subsequently approved the recommendations. [Fill continuity implementation](FILL_CONTINUITY_2026-09-15.md) records the source-only repairs and verification. The findings below describe the audited pre-repair build and remain historical evidence, not the latest implementation state.

## Scope and conclusion

Owner requested investigation of stops after fills, with Polymarket as a
read-only reference, and prohibited restarting Gemini. This follow-up is an
audit, not deployment or authority to enlarge exit retry limits. No live orders,
permits, processes or shared journal were changed; Polymarket was not operated.

Normal owned fills should cancel only their symbol's entries, retain the original
five-minute/earlier-cutoff deadline, exit exact inventory, and allow other healthy
markets to continue. Today's earlier terminal-IOC, exit-convergence and maker-leg
fill races have existing repairs/tests. However, market isolation through every
exit failure is incomplete. The prepared continuous-service release `accca2a`
does not resolve the gaps below. The 18:29 process disappearance remains unexplained.

Local read-only check at approximately 21:58 Eastern: overnight-v9 had fresh
quoting heartbeats, present runner/supervisor and no read/stream holds. Its current
JSONL file contained zero new-fill events and zero ERROR/CRITICAL events. That is
not venue exposure proof or natural live post-fill acceptance.

## Remaining gaps, in priority order

### P1: one market's exit failure can still drain unrelated markets

`orchestrator.py::_exit_due_positions_locked` raises on stale/insufficient exit
depth, exhausted retries, rejected exits and missing confirmation. These can
propagate to account-wide failure/drain. The existing regression
`test_terminal_unfilled_ioc_stops_after_three_attempts` verifies that three
positively terminal unfilled IOCs raise after their bound. A known terminal
residual is different from an unknown send, but currently neither offers complete
market-scoped continuity.

Recommendation: independently scheduled per-market exit states for pending proof,
known residual, unavailable depth and flat. Keep monitoring alive and retain the
original deadline, position risk and owner slot. Never duplicate an unknown IOC;
unrelated entries need all whole-account accounting/risk/health gates. Further
IOC rounds beyond the approved three attempts/30 seconds require an explicit
policy decision, not an incidental bug fix. Do not copy Polymarket's passive
exit or 900-second inventory policy.

### P1: an exit can delay another market's fill cancellation

`_safety_loop` and the complete `_exit_due_positions` operation share the lifecycle
lock. Exit confirmation, REST reads and retry backoff hold it. Another symbol's
fill blocks new entries immediately, but cancellation of its already-resting
sibling can wait behind the unrelated exit. The adverse-price worker bypasses
this lock; it does not guarantee cancellation on every fill. This is source
evidence, not a measured live delay.

Recommendation: use the existing per-order deduplicated cancel owner for
fill-priority cancellation independently of unrelated exit waits, while retaining
same-symbol terminal proof and exact-exit sequencing. Test concurrent fills,
shutdown, timeouts and server cooldowns.

### P2: net-flat fill paths can leave a market permanently blocked

Two synthetic offline characterizations reproduced:

- Equal owned YES/NO fills with matching synthetic cash and flat positions leave
  both deadlines and `blocked_entries` behind: the exit loop has no position to visit.
- Flatness reached during entry cancellation takes the subsequent `if not signed:
  continue` branch, bypassing deadline/block cleanup.

The global gate remains open, but that market cannot quote again. Stale deadlines
also obstruct owner retirement or contaminate a future inventory lifecycle.
Repair must require fresh classified account truth, owned terminal orders, no
unknown/pending mutation or exit, and preserve independent inactive/recovery holds.
Only completed lifecycle state may clear; normal entry eligibility still applies.

Reproduction: local ignored `data/fill-flow-audit-20260915/probe.py`, using existing
test fixtures, temporary journals and mocked transport, with no production data
or socket access. Both assertions reproduced current defects. No repair is claimed.

## Polymarket reference and verification

Read-only source: `C:/Users/rynna/Bots/liquidity-bot`:

- `live_commissioning.py::_reconcile_pending_hard_exit` separates pending, flat
  and known-terminal residual evidence.
- `live_commissioning.py::_retire_market` bars the affected market's entries while
  continuing monitoring/reduction, retaining sent-IOC uncertainty until proven.
- `rest_fallback.py::_scoped_reducing_mutation_proof_current` binds retry proof to
  one owner, schedule token, freshness and lack of order ambiguity; it cannot
  restore unrelated entry authority.
- `docs/history/REST_BULK_CONTINUITY_2026-09-11.md` records a prior defect where
  redundant refresh exhausted an IOC retry window despite terminal proof. Its
  full-width rehearsal includes unrelated entries, cancellation and unfilled IOCs.

These are architecture references, not Gemini API rules or current Polymarket
live-state proof. Gemini keeps its own timing, accounting, exposure and protocol.

Fresh complete Gemini production suite: **349 tests passed** in 42.885 seconds.
The two supplemental characterizations also passed assertions that the defects
exist. Existing passing tests do not resolve them. Prior continuous-service
verification included 28 script tests; those were not rerun for this docs-only audit.

Audit reward impact: no behavior changes. Scoped recovery and safe post-flat
cleanup could preserve resting time, but the affected pair necessarily loses
time during inventory management. No missed snapshots are forgiven, no 50%
exception extended, and no uninterrupted execution or reward guarantee is made.
