# Next work

1. Monitor `data/production-20260915-nontrading-v8`; latest verified state is live with 30 pending orders and zero positions.
2. Observe the next fill through same-symbol cancellation and the five-minute exact exit. Confirm unrelated reward quotes remain active.
3. After the Eastern reward day closes, compare authenticated reward summaries and `total_snapshots` with local uptime/ranking estimates.

Any unknown execution, nonflat reconciliation, incomplete P&L classification, stale account state, policy/build mismatch, or failed drain must leave the bot stopped. Maintenance/status ingestion remains measurement-only in [WATCHLIST.md](WATCHLIST.md).
