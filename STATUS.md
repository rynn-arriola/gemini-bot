# Gemini current status

Updated September 17, 2026. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **Latest checkpoint: v21 is restart-prepared for two-tick measurements, not a completed best-bid rollout.** At 20:26:55 ET fresh read-only preparation passed flat/order-free, complete non-HARD risk, unchanged economic history and all 20 sticky identities (32 watched, 30 ranked, 11 actionable). Automatic backup passed without errors. A fresh unconsumed v7 permit matches source `771d524`, the account and unchanged policy. Runner/supervisor remain absent and the persistent task is Ready; no trading restart occurred.

- **Stopped, not restarted.** V18 was deliberately drained flat at 19:58:24 ET after the independent audit found mixed AppData database views. V19 read-only preparation finished at 20:07:38 ET: flat/order-free, all 20 sticky owners and economic history unchanged, 32 watched/30 ranked/12 actionable, permit validated but unconsumed. No runner or supervisor was present at the subsequent check; the persistent task was Ready.
- **The native ledger was healthy.** Every table was migrated to `%USERPROFILE%\GeminiRewardBot\control`; shared file identity and native/packaged owner-lock rejection passed. Never operate from the legacy AppData/package-cache view. [Evidence](docs/CONTROL_LOCATION_2026-09-17.md).
- **Code completed and verified:** real two-market best-bid execution integration, failed-worker error propagation and control-path repair were pushed as `5166001`. Automatic verified backup rotation and read-only pre-write startup integrity checks now pass **771 production + 28 script tests (799 total)**, compilation, PowerShell parsing and offline release validation. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- **Fresh backup-build readiness passed at 20:23:10 ET:** `data/production-20260917-backup-readiness-v20` reconciled read-only and disconnected, with zero positions/orders, 32 watched/30 ranked/8 locally eligible, all 20 owners and healthy integrity. Its automatic 6,275,072-byte backup succeeded without errors. No activation permit was created or consumed; the bot remains stopped. Candidate counts are time-bound, not a promise of resting breadth.
- **Best-bid is not enabled.** Production policy remains version 2: size 15, two ticks, at most 40 pending orders/20 owners/32 watched, five public plus one private socket, all monetary/time/HARD limits unchanged. Live depth/flow and natural cancellation calibration plus exact two incumbent symbols are prerequisites, not synthetic defaults. [Execution boundaries](docs/QUEUE_PILOT_EXECUTION_2026-09-17.md).
- **No crash-free or reward guarantee.** A backup cannot prove current venue flatness or authorize restoration. Known scoped recovery remains automatic; unknown mutations/accounting, integrity faults and HARD stay fail-closed.

## Operator route

Latest read-only readiness: `scripts/check_runtime.ps1 -RuntimeDirectory data/production-20260917-restart-ready-v21`.
Prepared authority: `data/production-20260917-restart-ready-v21/activation-permit-queue-measurement.json`, v7, null expiry/uptime override, two same-supervisor recovery bursts, unconsumed. V20 created no permit; v19 authority is obsolete for this source.
Only after an explicit restart instruction, use `scripts/start_production.ps1 -Mode Live -RuntimeDirectory data/production-20260917-restart-ready-v21 -ActivationPermit data/production-20260917-restart-ready-v21/activation-permit-queue-measurement.json`. Normal fresh startup reconciliation remains mandatory, followed by exact owned resting-order acceptance. Any source/policy change requires new authority. Best-bid is still disabled pending the separate calibration/pilot work.
Never reuse consumed/obsolete permits, start a second owner, reset the cohort, reinterpret history or give downtime snapshot credit.

[NEXT.md](NEXT.md) lists the remaining evidence gates.
[Archived checkpoints](docs/history/STATUS_BEFORE_BACKUP_RELEASE_2026-09-17.md) are historical only.
