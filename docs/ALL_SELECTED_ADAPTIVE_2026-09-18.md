# All-selected adaptive release — September 18, 2026

## Delivery boundary

Implemented in isolated `feature/adaptive-queue-policy`. No running checkout,
active policy, order, process, permit or schedule was changed. This is release
preparation, not a restart or verified live best-bid run.

The shared numeric settings remain a proposal pending the explicit settings
question. The reviewed-data rationale is in
[the settings review](ADAPTIVE_SETTINGS_PROPOSAL_2026-09-18.md). Source now supports
both optional two-market research and the finished all-selected strategy; no
additional per-market calibration implementation is required for expansion.

## Implemented behavior

- Policy v5 explicitly selects `all_selected`; v4 retains the optional two-name
  research scope. Existing v2 one-tick and v3 fixed-calibration schemas still work.
  Schema versions cannot silently widen one another's scope.
- Membership follows permitted durable owners, up to 20 markets/40 orders,
  not all 32 watched candidates. Normal sticky selection remains unchanged.
- Replacement and readmitted markets start in one-tick fallback with fresh
  continuous observations. They inherit no previous market's mode, trade window,
  queue anchor, cooldown, account proof or reward time.
- A retiring member with working orders, nonterminal intents, inventory,
  deadlines, uncertain exits or unfinished account-proof work retains its safety
  state. New adaptive membership waits for capacity; normal safe one-tick quoting
  is still possible. Existing owner retirement supplies terminal/flat proof.
- Retired dictionaries and proof-task references are pruned. Market state is
  bounded at 20 members, with at most 4,096 trades per member. Observation cannot
  admit watched/unselected symbols. The cancellation-breach latch is shared and
  survives both membership changes and same-envelope process restart.
- Additional adaptive REST proofs run one at a time, still through the existing
  shared governor. Cancellation priority/server cooldowns and all monetary,
  fill/exit/account/HARD protections remain unchanged.
- Rolling selling totals are maintained incrementally, replacing repeated scans
  of all retained trades. Inclusive 60-second boundaries, duplicate IDs,
  overflow uncertainty and clock reversals have dedicated regression coverage.

Best-bid permission is per market. A local depth/evidence failure does not stop
unrelated markets. A shared cancellation-envelope breach removes best-bid
permission across the strategy but allows normally reconciled one-tick fallback.
Unknown mutations, account ambiguity, integrity failures and HARD remain global;
they are never relabeled as a harmless local issue.

## Verification and limits

Dedicated all-selected tests exercise 20 genuinely sent synthetic maker pairs
through the real transport/last-moment guards: 40 size-15 MOC orders, gross
reservation 288 under the unchanged 300 cap. A separate full-width test uses the
real shared rate governor with synthetic reported limits/account-stream updates,
charging exactly 40 order units and 4,000 conservative request units. No quota
multiplier or per-market governor is introduced.

The full-width partial-fill test executes 7 of 15, terminally cancels that pair,
preserves 38 unrelated orders and the exact original exit deadline during a
no-buyer wait, then sells exactly 7 when synthetic depth returns. Flat cleanup
removes the completed deadline/hold without duplicating an exit. Existing
inter-leg races, unknown cancellation, HARD, expiry and persistence cases run
under all-selected mode as well. Faults/fills are simulated, never induced live.

A 200-batch synthetic decision benchmark with 20 full 4,096-trade buffers measured
median 37.133 ms/p95 57.901 ms before the rolling-total optimization and median
0.190 ms/p95 0.238 ms afterward. This isolates decision computation, not whole
process CPU, network throughput or live cancel latency. Peak counts are scenarios,
not guaranteed queue positions. No reward, fill-avoidance or crash-free guarantee.

The final test totals and current read-only runtime checkpoint are recorded in
[FINDINGS.md](../FINDINGS.md) and [STATUS.md](../STATUS.md).

## Offline policy preparation

`config/adaptive_all_selected_review.template.json` is intentionally invalid until
reviewed values are supplied. The concrete
`docs/adaptive_all_selected_proposal_2026-09-18.json` contains the proposed common
settings but is **not active configuration or proof of owner approval**. Its
empty incumbent list is permitted only because membership is derived from fresh
durable ownership at runtime; it is not permission for an arbitrary symbol list.

The existing offline builder can create a separate non-active v5 draft:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m gemini_bot.stage_queue_pilot `
  --review docs\adaptive_all_selected_proposal_2026-09-18.json `
  --draft-name all-selected-review-20260918
```

It preserves all base fields except policy version and the explicit queue policy;
it never loads credentials, submits requests, overwrites active configuration,
creates a permit or launches a process. Syntactic success is not live approval.

## Restart handoff — separate from this preparation

1. Obtain the explicit common-settings response and the later restart instruction.
   Confirm the intended research/all-selected activation scope. No manual
   thresholds or separate trials for every market are required.
2. Recheck current source/dirty state, venue ownership, positions, accounting,
   integrity and HARD. Do not reuse an old "flat" report. Gracefully stop only
   within the authorized deployment and prove terminal/flat state, or retain
   only the existing fully reconciled held-inventory recovery path.
3. Verify backup and preserve every sticky owner, fill/settlement/cash/risk row,
   original deadline and uptime record. Integrate the published candidate into
   the stopped main checkout and explicitly promote the approved policy. Never
   hot-edit running source/configuration or silently reset selection/history.
4. Run final configuration/read-only preparation and fresh exact-build/policy/
   account v7 authority. The current v22 permit cannot authorize this release.
   The operator's old `data/deploy_queue_release.py` targets v22; its prepare mode
   must not be reused for this rollout. User launches through the persistent
   Windows workflow after successful preparation.
5. Verify actual owned size-15 orders, warmup fallback, best-bid transitions,
   cancel/proof/requeue/fallback, unaffected other markets, fills/exits, pacing
   and gaps. Report unobserved cases instead of inducing them. Two research
   markets validate execution; the same implemented rule supports all selected
   markets and replacements, without permanent two-market restriction.

The proposed envelope expires September 19 at 05:45 ET. Re-review if delayed;
never auto-renew it. Expiry disables best-bid through guarded fallback, not the
24/7 service. No reminder or scheduled review is created.

## Resting-time impact

This isolated preparation creates no live quote gap and rotates no owners.
Deployment and cancel/prove/cooldown transitions can create unquoted intervals;
none earns local or exchange snapshot credit. Warmup permits ordinary one-tick
quotes. Closer quoting can increase fill/exit exposure and may change modeled
economics; neither reward qualification nor maximum resting breadth is promised.
