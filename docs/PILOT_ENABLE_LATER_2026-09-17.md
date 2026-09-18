# Build now, configure and enable only after evidence review

## Delivered source, not an active deployment

The isolated `feature/pilot-enable-ready` branch supports the already-built
two-market execution path with **one tick for all nonpilot markets**. Legacy
two-tick nonpilot configurations remain valid. Neither zero-tick account-wide
quoting nor a quantity other than 15 is admitted for this pilot. The checked-in
`config/live_policy.json` is unchanged v2: one tick, pilot disabled.

All existing real-send anchors, side-specific flow warmup, immutable expiring
calibration, terminal cancellation, new depth, cooldown, fresh classified-flat
proof, partial-fill/exit, ownership, rate and HARD guards remain. Runtime still
checks the exact two current permitted incumbents and calibration expiry before
consuming activation authority. A disabled or unhealthy pilot cannot change
nonpilot quote targets. Reports now call that path the configured-offset policy.

No protocol, reward formula, fee, monetary limit, market selection policy,
connection count or automatic-enable mechanism changes in this release.

## Offline staging tool

Copy `config/queue_pilot_review.template.json` to a private review file outside
active configuration. Its null values are intentionally invalid; there are no
recommended/default thresholds to accidentally deploy. After reviewing actual
measurement coverage, provide:

- An aware `reviewed_at` timestamp, no later than the validation time.
- A snapshot list of current incumbent symbols (2–20 distinct canonical symbols).
- Exactly two of those symbols, each with independent YES and NO positive depth,
  warning-ahead, requeue-gain, sell-flow ceiling and refresh-cooldown values.
- Future aware calibration expiry timestamps and nonempty evidence references
  identifying the reviewed measurements and rationale.

Then, in the **isolated checkout**, run the offline builder:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m gemini_bot.stage_queue_pilot `
  --review data\reviewed-pilot.json --draft-name reviewed-pilot
```

It writes a new `data/queue-pilot-drafts/reviewed-pilot.json`, never replaces an
existing file, and leaves active configuration untouched. It rejects duplicate
JSON fields, oversized inputs, traversal-like draft names, invalid scopes,
expired/incomplete calibration and changed base schemas. Apart from adding
`queue_pilot` and changing `policy_version` to 3, the candidate exactly preserves
the supplied base. The output explicitly states that evidence sufficiency has
**not** been verified and live activation has **not** been authorized.

The tool loads no credentials and has no network, account, order, permit, process
or scheduler interface. The supplied owner list is only a review snapshot;
runtime independently checks fresh reconciled ownership before activation.

## Later operator steps

1. Review natural cancellation tails/failures, depth and outcome-side selling
   on the two specific incumbents. Do not count repeated quiet snapshots as
   independent busy-market evidence or successful cancels as a worst-case bound.
2. Fill the review file, generate/review the candidate and obtain owner approval.
3. Preserve the current cohort/history; use the normal controlled deployment
   after proven terminal/flat/account state. Integrate this published source and
   explicitly promote the reviewed policy; verify again. No hot config edits.
4. Create fresh exact-build/account/policy authority, user-start live trading,
   then verify both pilot and unchanged one-tick nonpilot behavior on the venue.

This is designed to leave configuration, review and deployment work later—not
another known compatibility implementation. Unexpected evidence may still require
a code change. There is no promise of exact queue position, fill avoidance,
profitability, reward qualification or a zero-gap deployment. Current isolated
work creates no resting gap; future pilot cancellation/deployment gaps earn no
snapshot credit and must be measured.

## Verification

827 production and 28 script tests passed (855 total), plus compilation,
whitespace and offline release validation. The existing pilot execution suite
runs for both legacy two-tick and new one-tick nonpilots, covering real fake-wire
MOC sends, first-leg fills, second-send races, canceled/changed anchors, cooldown,
terminal-flat recovery, failed cancels and HARD. A separate mixed-market test
places/retains one-tick nonpilot orders while pilot evidence is missing. Twelve
staging tests cover unchanged base limits, strict/expiring inputs, current-owner
snapshot scope, safe disabled defaults, no overwrite/traversal, bounded/unique
JSON and explicit nonauthority CLI output. The blank template also failed a real
offline CLI invocation without creating a candidate. No live fault/fill induced.
