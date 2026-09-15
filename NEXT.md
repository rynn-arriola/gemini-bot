# Next work

1. Commit and push the reviewed fill-race/supervisor repair.
2. Create a fresh version-5 permit bound to the pushed build and the already approved remainder-of-day limits; run one final permit/config check.
3. Restart under the native-Windows supervisor and verify live status, order cap, positions, logs, and supervisor state. Do not claim success from process creation alone.
4. Observe the next fill: cancel only same-symbol residual entry orders, keep other safe reward quotes, wait five minutes, then exit the exact authenticated position.
5. After the Eastern reward day closes, compare authenticated reward summaries and `total_snapshots` with local uptime/ranking estimates.

Any unknown execution, nonflat reconciliation, incomplete P&L classification, stale account state, policy/build mismatch, or failed drain leaves the bot stopped. Maintenance/status ingestion remains measurement-only in [WATCHLIST.md](WATCHLIST.md).
