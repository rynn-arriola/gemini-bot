# Pair-send and telemetry release

Source commit `87fab92`. Owner authorized restart September 16, 2026.

## Change and verification

The second maker leg now carries an in-memory guard through socket admission.
It requires the exact original first client/server order, acknowledged intent,
unchanged symbol/outcome/side/price/full quantity, no fills and unchanged public
and private connection generations. A failed final check terminalizes only the
definitely-unsent second intent and returns to existing symbol cleanup. The
guard is removed after send or failure. Attempted sends remain unknown until
reconciled, without replay. Existing locks keep placement serialized; urgent
cancel/fill workers continue independently.

Adverse-cancel reasons distinguish missing contract/book, unsynchronized book,
stale stream, one-sided public book, a missing external side after subtracting
own orders, and invalid two-sided target prices. Existing cancellation policy
is unchanged. Additional book/price/sequence diagnostic context remains a
measurement follow-up if needed.

The previously approved account callback repair is integrated: shared-journal
preparation preserves event-driven continuity and NEW/CANCELED counters. Its
regression drives real preparation and the reducer through a sub-250ms gap.

Validation: **454 production + 28 script tests = 482 passed**, including 18 new
guard/diagnostic tests. Cases include terminal cancellation at second admission,
changed/replaced first identity, quantity/price changes, partial/full fills with
preserved deadlines, both stream generations, healthy pairs, unrelated-market
continuity and attempted-send ambiguity. Compilation, whitespace, PowerShell
parsing and offline Windows policy/storage checks passed.

The new tests first exposed eight behavioral assertion failures on the old
source (plus missing new guard/diagnostic APIs). After integration, the full
suite passed. The active checkout and tested worktree have identical normalized
source; raw build digests differ due solely to LF/CRLF checkout normalization.
All digest-input files were compared after newline normalization and the 18
new tests passed again from the deployment checkout. Fresh authority binds the
actual deployed bytes, not the worktree digest.

## Deployment evidence

V10 reached authenticated `stopped_flat` at **00:19:17.642250 ET**; supervisor
then reached STOPPED and all three old processes were absent. Source was
fast-forwarded only after the old processes ended. Source publication on main
and the release branch was verified against GitHub.

The shared journal passed integrity/flat/order-free checks. Fourteen sticky
identities, realized-risk history, fill history and deadline digests matched
before and after fresh authenticated preparation. Preparation completed at
**00:20:23 ET**, with 21 tracked symbols, 19 ranked and 14 eligible candidates.
A fresh v7 permit authorizes continuous service, no session expiry and **no
uptime override**. Native Windows supervision started in
`data/production-20260916-churn-v11` and repeats startup reconciliation before
activation. Final live acceptance is recorded in STATUS.md and FINDINGS.md.

Activation completed at **00:21:28.458927 ET**. At 00:22:16 ET the new run had
28 pending orders, zero positions/holds/dropped logs, no warning/error/critical
or fill events and zero supervisor restarts. Its source digest matched its
permit. Telemetry counted 40 NEW events (matching raw logs), 12 CANCELED events
and 14 observed symbols. Last old cancellation to first new quote measured
131.710 seconds; individual market restoration takes additional time. Natural
long-run/fill/recovery acceptance remains pending.

## 50% impact and limits

Deployment deliberately drains resting orders and creates an unquoted interval;
lost time receives no reward credit. The new guard may reduce avoidable orphan
orders and cancel/replacement traffic after activation, but cannot prevent
genuine adverse moves, fills or post-send races. It changes no price, quantity,
risk, exposure, deadline, pool-selection or 50% admission policy. Preserve
unknown-execution, ownership, accounting and HARD-stop boundaries. No induced
live fault/fill was used, and passing tests/initial quoting do not prove a
sustained churn reduction or Gemini reward qualification.
