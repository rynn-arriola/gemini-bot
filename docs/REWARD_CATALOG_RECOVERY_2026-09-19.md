# Reward-catalog recovery — September 19, 2026

## Confirmed incident

The v30 process logged `active_runtime_failed` at 10:00:43 ET after the public
reward-event pagination reader exhausted its three bounded attempts and raised
`ApiError(200, "unstable_reward_catalog")`. Periodic read recovery recognized
transport/server errors, but not this locally detected inconsistent catalog.
The runner canceled its entries and completed `stopped_flat` at 10:00:48 ET;
the supervisor correctly required attention for the unclassified failure.
This stop was not caused by the earlier fractional-fill accounting issue.

The old attention-only supervisor was dismissed through the normal STOP route.
No active trading process was edited. Canonical history, sticky ownership,
policy, position deadlines and HARD were preserved.

## Repair and boundaries

- Exhausted duplicate/count-inconsistent pagination now raises the specific
  `RewardCatalogUnstable` type. Partial rows are still discarded, never used
  to retire an owner or select replacements.
- Periodic reward refresh uses the existing same-process read hold and
  5/10/20/40/60-second backoff, preserving safety/exit workers. Fresh classified
  private state remains required. A complete successful refresh clears only
  its own read hold. Other entry restrictions remain authoritative.
- The same typed error permits read-only v7 startup waiting through the
  existing supervisor route, not activation before complete reconciliation.
- Lifecycle refresh requests cannot bypass an outstanding metadata backoff.
  Existing immediate lifecycle blocks/cancellations remain in force.
- Explicit longer server cooldowns take precedence over the local delay.
  Malformed catalog shape/rows/totals, ceiling overflow, ordinary HTTP-200
  errors, authentication, accounting uncertainty and unknown mutations remain
  fail-closed. No mutation retry path or risk/strategy policy changed.

## Snapshot impact

A catalog-wide read hold pauses new entries and cancels working entries through
existing maintenance. This can lose snapshots, but the process can recover
without an operator restart. Saved market identity/history remains intact;
unquoted time is not credited. This is not a promise of uninterrupted quoting
or reward qualification. Single-event metadata failures retain their separate
market-local handling.

## Verification

Targeted regressions passed for exhausted pagination, discarded partial rows,
malformed-data rejection, same-process recovery, unchanged universe, independent
holds, fresh-account requirements, paced refresh requests, longer cooldowns and
read-only startup retry. Full verification passed: **1,031 production tests plus
28 script tests (1,059 total)**, Python compilation, PowerShell parsing,
whitespace checks and offline Windows policy/durable-storage release validation.

Fresh read-only preparation completed at 10:20:37 ET: zero positions/orders,
32 tracked/ranked, 15 eligible, no read/stream holds and no permit consumed.
Independent read-only journal checks found healthy integrity, complete risk
classification and the unchanged non-HARD 40-order cap. Automatic verified
backup succeeded (13,725,696 bytes); normal retention removed one older managed
snapshot after the replacement passed verification. No history was reset.
No new live activation has occurred; restart approval was requested.
