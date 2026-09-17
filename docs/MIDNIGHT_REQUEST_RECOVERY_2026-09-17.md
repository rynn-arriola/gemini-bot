# Midnight request failure and recovery

At 00:00:01 ET September 17, v16's private reader raised the generic correlated WebSocket rejection error. The active maker acknowledgement wait timed out at 00:00:10; emergency cancellation confirmation then timed out at 00:00:55. The reader was dead but no transient stream hold existed, so drain skipped reconnection and queued per-order REST fallback under one private-read lock.

The preceding ZEC YES 15 order is authenticated in history as created at 04:00:00.900 UTC, canceled at 04:00:55.957 UTC, filled quantity zero. That does **not** prove which request/error caused the reader failure. The old logger discarded method/status/code; its original cause cannot be reconstructed from available logs. Reward-day rollover was logged successfully, and there was no fill. Supervisor read-only recovery reached ready-paused at 00:02:31 but appropriately did not invent restart permission for the unknown failure.

At investigation, authenticated REST returned zero orders/positions and canonical SQLite integrity passed. The attention-only supervisor was dismissed via the normal stop mechanism before source edits; no active runner was hot-edited.

## Repair boundaries

- Detect completed/failed readers before drain and reconnect solely for cancellation, execution/account proof and reduction.
- Preserve safe allowlisted method and numeric status/code in rejection diagnostics; discard free-form venue text/data.
- Resolve a lost maker acknowledgement only from one exact terminal zero-fill history match. No missing-history inference, external order adoption, fill erasure or request replay.
- For owner-approved v7, only correlated service/rate codes may qualify for the existing same-supervisor bounded restart after fresh classified terminal/flat proof and no HARD. The startup checks repeat; permanent/auth/unidentified errors remain attention-required.
- Keep five public plus one private connections, shared quota, size 15, 40 orders/20 owners, current sticky identities, five-minute exits and the unchanged $30 six-hour HARD.

This improves shutdown/recovery; it does not prove the unidentified original rejection will be automatically recoverable. The account-wide reader requires account-wide proof. Restart/downtime is not credited toward Gemini snapshots; no market-selection reset or uptime override is authorized.

Official [message format](https://developer.gemini.com/prediction-markets/websocket/message-format) rechecked September 17 documents response correlation, numeric status/error codes, -1000 service, -1003 rate and -2010 rejection. [Connection introduction](https://developer.gemini.com/prediction-markets/websocket/introduction) documents cancel-on-disconnect; authenticated terminal proof remains mandatory.

## Verification

670 production tests and 28 script tests pass (698 total), including 15 new focused regressions. Compilation, all PowerShell parse checks, whitespace checks and offline Windows policy/storage validation pass. Replacing the new drain condition in memory with the old one makes the dead-reader regression fail as expected. Fresh authenticated preparation reached ready-paused with zero positions/orders, unchanged historical ledger rows and all 20 sticky identities preserved. The current new six-hour risk window has no stored row yet; exact classified history is checked without manufacturing/resetting one. Activation performs the ordinary current-window update.

## Deployment acceptance

Source `2a1efdb98cce3d180a5afe73f66c5dcb9ccc66e7` exactly matched `origin/main`. Final preparation found 32 watched/30 ranked/16 actionable candidates, zero orders/positions, preserved history and all 20 owners, complete current-window classification and no HARD. Fresh v7 authority is `data/production-20260917-request-recovery-v17/activation-permit-request-recovery.json`: exact build/account/policy, 20 owners, two same-supervisor bounded recovery restarts, null expiry and no uptime override.

Persistent supervisor 29360 / runner launcher 26640 started through the existing on-demand task. First NEW was 06:56:31 ET September 17. Authenticated REST at 06:56:40 confirmed **30 canonically owned size-15 orders across 15 markets, zero positions**. All 20 previous owners remain. Five public shards connected, no read/stream holds, zero supervisor restarts and no WARNING/ERROR/CRITICAL log events; SQLite integrity and permit/build/policy agreement passed. The 40-order ceiling is unchanged; this is an initial checkpoint, not sustained uptime/reward or injected-fault recovery proof.

Backup: `data/control-before-midnight-recovery.sqlite`. Preserve v17 and do not reuse the consumed v16 permit. The original unidentified rejection may still require attention; this release proves recovery only for the explicitly classified service-error cases and complete proof boundaries described above.
