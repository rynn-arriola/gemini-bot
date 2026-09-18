# Native/packaged control authority repair

## Confirmed incident

V18 began quoting at approximately 19:55 ET after the queue integration release.
The independent packaged-operator REST audit found 22 active orders in the new
runner's logs but no matching intent rows in its apparent AppData journal. It saw
9,724 intents ending at 23:09:15 UTC. The bot was deliberately drained flat at
19:58:24 ET and the supervisor stopped; no unknown order was adopted.

A no-package Scheduled Task audit of the actual native journal proved integrity,
9,746 intents ending at 23:58:23 UTC, all terminal, 20 sticky owners and fresh
authenticated zero orders/positions. The 22-order difference exactly matches the
new run. The packaged operator saw redirected `production.sqlite-wal` and `-shm`
under `AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/GeminiRewardBot/control`
but no redirected base database. Native checkpointing changed the physical base
while that stale redirected WAL remained. The packaged mixed view subsequently
failed integrity; the native view passed. No physical journal corruption was
inferred from the packaged failure, and it was not "repaired" by discarding rows.

Microsoft documents [AppData file-write virtualization](https://learn.microsoft.com/en-us/windows/msix/desktop/flexible-virtualization).
This is direct evidence for this incident. It is a possible explanation for older
operator-side corruption symptoms, not proof of every historical incident.

## Explicit preservation and migration

The native-only operator audit held the old owner lock, verified complete terminal
intents and fresh venue flatness, then took a consistent SQLite backup at
`data/control-native-before-unvirtualized.sqlite`. All table digests and full
integrity matched. An explicit native migration then held both old and new owner
locks, rechecked that backup against the unchanged source, copied every table to
`C:\Users\rynna\GeminiRewardBot\control\production.sqlite`, verified integrity and
digests, closed the destination connection and renamed the verified temporary.
The first rename attempt refused because its destination connection was still
open; the retry validated the same complete temporary before closing and renaming.
No source or destination contents were overwritten to bypass that failure.

The legacy native files, redirected companions and earlier operator backup are
preserved. The earlier packaged `control-before-queue-v18.sqlite` backup and
19-owner preparation observation were not current native authority and must not
be used to restore production. The authoritative migrated journal has 20 owners.
No balances, fills, fees, settlements, cash adjustments, deadlines, risk rows,
uptime or permit history were reset or reinterpreted.

While the native migration held the new lock, the packaged operator read the
same database SHA-256 and was rejected with `another Gemini mutation process owns
the lock`. This verifies shared file/lock visibility across the two execution
boundaries before any trading restart. The temporary diagnostic task has no
recurring trigger and no trading authority.

## Runtime change

`shared_control_directory()` now uses `%USERPROFILE%\GeminiRewardBot\control`,
independent of project/runtime output folder, API key and AppData virtualization.
It refuses startup when that journal is missing but legacy AppData authority
exists. It never auto-migrates or creates a blank replacement for legacy history.
The production v19 restart requires new exact-build/account/policy authority and
full fresh preparation against the migrated journal. All strategy/risk/HARD limits
and stickiness remain unchanged; the stopped interval earns no reward credit.

New deterministic tests cover stable cross-environment paths, refusal of blank
legacy replacement, and no fallback after migration. Native/packaged lock and
file-identity checks above are real host checks, performed while trading was flat
and stopped. This is not automatic corruption recovery or a host-crash guarantee.
