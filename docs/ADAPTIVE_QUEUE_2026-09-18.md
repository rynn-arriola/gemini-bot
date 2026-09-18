# Adaptive best-bid candidate — September 18, 2026

## Scope and deployment boundary

Owner approved reusable adaptive construction after identifying the problem with
manually calibrating each short-lived market. Source is on isolated branch
`feature/adaptive-queue-policy`, based on `c559ff2`. Production main `840813b`,
its policy, credentials, permits, authority, processes and orders were not changed.
No live restart, pilot activation, scheduled review or automatic broad rollout.

Policy v2 remains one tick and size 15; all 40-order/20-owner/32-watched,
monetary, timing, five-public/one-private/shared-rate and HARD limits are unchanged.
V3 fixed calibration remains supported. V4 uses the same real execution path
with shared, explicit, expiring parameters instead of four per-side tables.
V4 requires one-tick fallback and exactly two existing permitted owner symbols.

The algorithm is reusable across symbols; the pilot permission is not. If a
pilot market ends, normal lifecycle/selection rules operate, and any replacement
continues the normal strategy. It does **not** inherit pilot scope or evidence.
Authorizing wider/adaptive replacement scope remains a later rollout decision.

## Adaptive scenarios

`adaptive_queue.py` owns no transport/account/selection authority. It reuses exact
own-order-adjusted books, real-send anchors, validated side-specific trades and
public/private generation isolation from `QueuePilot`.

For each side in each healthy generation it retains:

- Peak observed rolling 60-second selling quantity.
- Largest observed same-price external-depth decrease between book updates.
- Continuous observation duration and stable-eligibility duration.

The warning scenario is `buffer + burst_multiplier * (peak_flow + peak_drop)`.
Required external depth is `warning * depth_multiplier`. Requeue benefit and
cooldown remain shared explicit parameters. All fields must be positive/finite,
the depth multiplier exceeds one, and no live numeric defaults are supplied.
The complete observed 60-second burst is reserved even for a shorter cancel
budget: it is **not** divided by 60 to assume evenly paced selling. Flow and depth
may double-count executions deliberately; these are scenarios, not mathematical
future-loss bounds. Quiet markets use the reviewed buffer, never a claim of zero
future selling. Test constants are synthetic and not calibrated recommendations.

Peaks never decay during a healthy generation or replenish an existing order's
queue estimate when new depth appears. Generation changes discard local evidence
and require new warmup; they cannot transfer one market's measurements to another.
State is bounded by two markets and the existing 4,096-trade-per-market deque.
Trade-clock uncertainty/overflow, stale account/book/generation, local evidence
errors, expired policy and insufficient depth remove best-bid permission.

The common cancellation budget is a reviewed maximum acceptable observed local
call-to-terminal-confirmation duration (including pacing/proof), not a promise
about exchange latency. A failed/interrupted cancellation or observed over-budget
delay latches adaptive best-bid off. The latch persists under a canonical hash of
the envelope and symbols in the existing journal; same-envelope restarts cannot
erase it. A new reviewed envelope and fresh policy/build authority are required
to change that identity. Failed latch persistence halts and propagates the
storage failure, rather than reporting an ordinary recoverable finish.

## Mode and execution boundaries

Initially each pilot-scoped market uses the normal one-tick policy. Continuous
healthy evidence and promotion hysteresis can request best-bid mode. Only the
orchestrator may commit a mode, after exact pair cancellation, terminal/flat
account proof, no unresolved intents, no inventory/exit/fill hold, unchanged
generations and the ordinary entry/scope/risk gates. A later public sequence and
cooldown are mandatory after cancellation. It waits for cooldown/depth **before**
requesting a short-lived account proof, avoiding a guaranteed expiry/wait cycle.

After a mode change it reranks at the new offset; it cannot reuse an old candidate
or switch modes between the two maker legs. Final admission after shared pacing
rechecks the mode, proof and both sides. A seven-of-fifteen fill follows the
existing fill/exit/deadline path, never a best-bid or fallback replacement.

Demotion uses the same barriers. Once safely flat, a market can resume one-tick
quoting without waiting for its adaptive evidence to recover. A local adaptive
observer failure does not disable normal safe quoting after reconciliation.
Unknown cancel/ownership/accounting, pending exits and HARD remain fail-closed.
Unrelated markets retain their original behavior and shared admission authority.

## Offline review and actual measurements

`review_adaptive_queue.py` reads only bounded `queue-shadow.jsonl` rotation files,
deduplicates timestamps, supports an aware `--since`, excludes incomplete/uncertain
windows, and reports coverage, selling, archive-interval depletion and cancellation
tails. No credentials, venue calls, files written, policy, permit or process action.

At the September 18 05:33 ET checkpoint, filtering from the actual 21:03 ET
launch yielded **510 minute reports**, from 21:04:23 ET through 05:33:11 ET.
Maximum archived local cancellation duration remained **4.948870799969882 s**,
first present in the 03:00:06 ET archive; failed/interrupted calls maximum was zero.
That identifies its first archive interval, not the exact request or cause.

A deliberately explicit **sensitivity experiment only** used buffer 15,
burst multiplier 1, depth multiplier 2 and cancel budget 5 seconds. This is not
a live recommendation: 5 seconds barely exceeds the measured tail and provides
no proof of adequate future allowance. Using full-sample selling peaks plus the
largest archived interval's cumulative depletion, both-side depth passed:

| Observed symbol | Complete rows | YES/NO selling windows | Stress depth-pass rows |
| --- | ---: | ---: | ---: |
| GEMI-CTRLUSHOU-GOP | 484 | 1 / 5 | 484 |
| GEMI-CTRLUSSEN-GOP | 508 | 1 / 1 | 505 |
| GEMI-NFL-2609201700-CLE-TB-M-TB | 259 | 9 / 15 | 0 |

This does **not** select pilot markets or forecast rewards. The largest
archive-interval cumulative depletion is not the runtime's largest single-update
drop. Full-sample peaks include future information; counts are a stress screen,
not causal/out-of-sample backtesting or predicted quoting uptime. Overlapping
selling windows are not independent trades. Minute snapshots cannot reproduce
intra-minute cancel/add order, actual queue identities, fills or subsecond races.

Example read-only command (experiment values, not a deployment):

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m gemini_bot.review_adaptive_queue `
  --runtime C:/Users/rynna/Bots/gemini-bot/data/production-20260917-one-tick-measurement-v22 `
  --since 2026-09-18T01:03:23+00:00 `
  --buffer 15 --burst-multiplier 1 --depth-multiplier 2 --cancel-budget 5
```

## Staging and acceptance still required

The intentionally invalid `config/adaptive_queue_review.template.json` contains
no chosen markets or recommended constants. After shared-envelope review, the
existing offline `stage_queue_pilot` builder recognizes the adaptive schema and
produces a v4 draft while preserving every other base-policy field. It remains
non-overwriting/non-activating, requires two supplied incumbents and an unexpired
envelope, and explicitly states that evidence sufficiency is **not** verified.

Before deployment:

1. Review shared buffer/multipliers, cancellation allowance, continuous warmup,
   promotion, cooldown and expiry. Document model uncertainty and sensitivity;
   do not promote synthetic test inputs to live values or call elapsed time proof.
2. Select two **current** incumbent symbols and obtain the owner's pilot decision.
   Current ownership must be proved again at activation, not inferred from archives.
3. Integrate the verified source and reviewed policy through controlled deployment,
   preserving cohort/history and using fresh exact-build/account/policy authority.
   User initiates trading. No hot edits or reuse of the current permit.
4. Observe mode transitions, actual order prices/ownership, fill/exit handling,
   cancellation tails, quote gaps/churn and unaffected one-tick markets. Compare
   authenticated rewards and economic results; no induced fill/fault or guarantee.
5. Only then consider a separately approved broader scope. No silent expansion
   or promise that every market should qualify for best-bid.

## Continuity impact

Isolated construction caused no production resting gap. Future promotion/demotion
and deployment create unquoted intervals; they earn no snapshot credit. Hysteresis,
unchanged-market retention and proof-after-cooldown reduce avoidable churn but
cannot promise Gemini qualification or preserve unsafe orders for uptime.
The local 50% proxy remains reporting-only, and sticky owner identities are not
rotated by an adaptive signal.

## Verification

The synthetic adaptive suite covers initial one-tick orders, continuous warmup,
clock/generation resets, asymmetric bursts, depth loss/replenishment, duplicate
trades, latency-tail latch, expiry, strict schema/scope, mode promotion/demotion,
real fake-wire sends, exact cancellation, new depth, cooldown/proof freshness,
partial fills, inter-leg races, unknown cancellation, HARD, nonpilot isolation,
durable latch restart and persistence failure. It repeats the existing complete
one-tick pilot execution suite. Archive tests cover chronology, rotations,
duplicates/partial tails, incomplete windows and explicit nonauthority outputs.
Final verification: **890 production + 28 script tests = 918 passed**, compilation,
offline Windows release validation and whitespace checks passed. The blank
adaptive template was rejected by the real staging CLI without writing a draft.
A synthetic two-market evaluation with both 4,096-trade buffers full, repeated
200 times, measured median 2.572 ms/p95 3.866 ms. This is not full-pipeline live
profiling or a performance guarantee for a future wider rollout.
