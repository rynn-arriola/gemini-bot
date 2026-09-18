# Verified journal backups and startup integrity

The owner requested completing the remaining fixes. No live restart or best-bid
activation is implied by this source release. All strategy, risk, HARD, ownership
and sticky market policies remain unchanged.

## Implementation

- Before startup opens an existing canonical database for schema/account writes,
  a separate read-only connection checks full SQLite integrity and the required
  authority tables. Malformed or empty/incomplete existing authority blocks
  startup. It is preserved, never initialized over or silently restored.
- After canonical selection under the existing mutation-owner lock, one backup
  service starts an immediate snapshot and repeats hourly. It owns only a private
  read-only SQLite connection and a new destination, never the trading connection.
  One sequential I/O worker runs outside the event loop. Shutdown signals it and
  joins it before releasing the owner lock; cancellation never leaves an unjoined
  backup thread. Error text is redacted inside the worker, including late failures.
- Use SQLite's [online backup API](https://www.sqlite.org/backup.html), not a raw
  copy of the base file; committed WAL data is included. Check the destination's
  full integrity, close it, reopen/read-check independently, hash/fsync, then
  publish under a unique name by same-directory rename. Failed copies never
  replace good snapshots. All tables, including permits, unknown intents, fills,
  deadlines, loss high-water, HARD and uptime, are preserved as-is.
- Dedicated directory: `%USERPROFILE%\GeminiRewardBot\control\backups`.
  Keep 24 managed `auto-v1-...sqlite` snapshots; remove only older matching files
  after a new verified copy exists. No recursion or link/junction traversal. Manual
  backups, native migration evidence, legacy files and unrelated files are not
  retention targets. Clock rollback cannot select the just-created copy for removal.
- Each snapshot is limited to 128 MiB; require 384 MiB free before starting. SQLite
  callbacks enforce a cooperative 15-second copy/check budget. OS disk calls are
  not a hard real-time guarantee. Successful managed retention is at most 3 GiB
  (about 144 MiB at the current database size), plus one in-progress copy. If
  interrupted artifacts/deletion failures exceed the directory count budget,
  refuse new copies and report attention rather than grow without bound.
- Backup failures do not interrupt quotes, alter gates or forgive database writes.
  Retry on the next hourly interval and expose error class, last success, in-progress
  state, checksum, bytes and retention errors in runtime status. Actual authority
  integrity/accounting failures retain fail-closed behavior. No exchange API calls.

## Recovery procedure — never automatic

Stop mutation ownership and preserve the incident files. Independently verify a
candidate snapshot and compare complete history with fresh authenticated venue
orders, fills, fees, positions, settlements and cash evidence. Reconstruct missing
history without losing original deadlines, risk high-water, HARD, permit consumption
or uptime gaps. Only explicit verified operator recovery may replace authority;
startup must still perform normal reconciliation under fresh valid activation.
A backup may contain nonterminal intents and is not a flatness certificate.
Local copies do not protect against total disk loss and are never pushed to GitHub.

## Evidence and continuity impact

Locked read-only acceptance on the real stopped journal created a 6,275,072-byte
verified snapshot. All 14 tables matched the source before/after; zero venue calls
and no source-history changes. Full integrity took 16.47 ms and backup/verify/hash
took 70.58 ms on this laptop. This supports off-loop I/O rather than blocking the
250-ms safety clock; it is one measurement, not a worst-case latency bound.

Offline tests cover WAL/uncommitted transactions, complete authority preservation,
failed verification/rename, disk/size/time bounds, redaction, shutdown joins,
rotation failure, clock rollback, malformed/missing/empty authority and real
orchestrator startup refusing corruption before network/schema writes.
Final verification passed 771 production plus 28 script tests (799 total),
compilation, PowerShell parsing, whitespace and offline release validation.
Fresh read-only preparation ended at 20:23:10 ET with zero orders/positions,
all 20 owners intact and no permit created/consumed. Its integrated automatic
startup backup succeeded, with no backup/rotation errors. This also verifies
the runtime health field, not merely the standalone backup function.

No policy/cohort/quote change is made. A later authorized source deployment still
causes an uncredited resting gap; best-bid remains calibration-gated. These backups
do not establish Gemini qualification or guarantee crash-free operation.
