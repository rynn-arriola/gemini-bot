# Watchlist

Measurement-only items; none may alter live behavior without a new decision and tested release.

- Compare touch, one-tick, and two-tick distance using actual fills, adverse selection, achieved daily reward scores, and net P&L.
- Measure whether the 75% internal uptime target gives useful recovery margin over Gemini's 50% threshold.
- Confirm the Eastern reward-day proxy and exact shared-pool uptime/size aggregation from authenticated daily-summary snapshot totals.
- Calibrate the local distance proxy against actual score ratios; do not reverse-engineer one day into a permanent curve.
- Measure queue position and maker competition separately from public maker count.
- Compare passive exits against the 300-second timed exit and pre-expiry override.
- Profile parser, book reducer, rank scheduler, storage batches, memory, and CPU at each breadth step.
- Monitor Gemini Statuspage upcoming/active maintenance and incidents as read-only operational annotations. Do not manually subtract maintenance time from reward uptime until the authenticated daily summary proves it was excluded from `total_snapshots`.
