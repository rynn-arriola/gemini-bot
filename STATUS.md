# Gemini current status

Updated September 17, 2026. This is a checkpoint, not a perpetual venue-state guarantee.

## Operational truth

- **Latest release: one-tick normal quoting, separate observation-only queue measurements.** Owner changed the measurement run from two ticks to one and declined scheduling. Quantity stays 15; best-bid/back-of-queue execution remains disabled. **780 production + 28 script tests (808 total)** and offline release validation pass. Bot has not been started; live activation is a user-operated step. No reminder/review automation was created. [Scope and tests](docs/ONE_TICK_MEASUREMENT_2026-09-17.md).
- **V22 read-only readiness passed at 20:43:17 ET:** zero positions/orders, complete non-HARD risk and healthy journal; all economic history and 20 saved owners unchanged. 32 watched/30 ranked/13 actionable at that checkpoint; automatic backup successful. Preparation disconnected without activation. These counts are not promised resting breadth.

- **Stopped, not restarted.** V18 was deliberately drained flat at 19:58:24 ET after the independent audit found mixed AppData database views. V19 read-only preparation finished at 20:07:38 ET: flat/order-free, all 20 sticky owners and economic history unchanged, 32 watched/30 ranked/12 actionable, permit validated but unconsumed. No runner or supervisor was present at the subsequent check; the persistent task was Ready.
- **The native ledger was healthy.** Every table was migrated to `%USERPROFILE%\GeminiRewardBot\control`; shared file identity and native/packaged owner-lock rejection passed. Never operate from the legacy AppData/package-cache view. [Evidence](docs/CONTROL_LOCATION_2026-09-17.md).
- **Code completed and verified:** real two-market best-bid execution integration, failed-worker error propagation and control-path repair were pushed as `5166001`. Automatic verified backup rotation and read-only pre-write startup integrity checks now pass **771 production + 28 script tests (799 total)**, compilation, PowerShell parsing and offline release validation. [Backup design](docs/JOURNAL_BACKUPS_2026-09-17.md).
- **Fresh backup-build readiness passed at 20:23:10 ET:** `data/production-20260917-backup-readiness-v20` reconciled read-only and disconnected, with zero positions/orders, 32 watched/30 ranked/8 locally eligible, all 20 owners and healthy integrity. Its automatic 6,275,072-byte backup succeeded without errors. No activation permit was created or consumed; the bot remains stopped. Candidate counts are time-bound, not a promise of resting breadth.
- **Best-bid is not enabled.** Production policy remains version 2: size 15, **one tick**, at most 40 pending orders/20 owners/32 watched, five public plus one private socket, all monetary/time/HARD limits unchanged. Live depth/flow and natural cancellation calibration plus exact two incumbent symbols are prerequisites, not synthetic defaults. [Execution boundaries](docs/QUEUE_PILOT_EXECUTION_2026-09-17.md).
- **No crash-free or reward guarantee.** A backup cannot prove current venue flatness or authorize restoration. Known scoped recovery remains automatic; unknown mutations/accounting, integrity faults and HARD stay fail-closed.

## Operator route

One-tick read-only preparation directory: `data/production-20260917-one-tick-measurement-v22`. No v22 activation permit has been created or consumed. V21 and older permits are obsolete for the new source/policy; never launch with them.
Live startup still requires fresh exact-build/account/policy authority and normal reconciliation, followed by owned resting-order acceptance. User must initiate live trading. Best-bid remains disabled pending the separate calibration/pilot work; the next-day review is user-initiated, not scheduled.
Never reuse consumed/obsolete permits, start a second owner, reset the cohort, reinterpret history or give downtime snapshot credit.

[NEXT.md](NEXT.md) lists the remaining evidence gates.
[Archived checkpoints](docs/history/STATUS_BEFORE_BACKUP_RELEASE_2026-09-17.md) are historical only.
