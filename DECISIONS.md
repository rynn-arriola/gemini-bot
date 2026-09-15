# Active decisions

These are current account-owner decisions. Changing one requires a new explicit owner decision and a dated update.

## Approved

- Product is intended for live Gemini Predictions trading, not paper execution.
- Native Windows is the only supported production platform.
- WebSocket is primary for market data, order mutations, order events, balances, and positions.
- REST is used only where WebSocket lacks the information, for discovery/config/reward metadata, startup state brackets, and bounded reconciliation/recovery.
- Base reward quote quantity is 10 contracts per outcome leg.
- Maximum pending/open order count is 30. Filled positions do not consume this order-count limit; they remain governed by independent position and gross-exposure limits.
- Maximum gross reservation/exposure is $300; maximum exposure attributable to one market is $90.
- Maximum tracked market owners is 32, but no more than 15 contracts may be quoted two-sided at once because each consumes two of the 30 pending-order slots.
- The $40 stressed-exit value is an entry-admission filter, not a stop loss.
- Use a 900-second inventory exit and a 300-second pre-expiry override. These are time exits, not monetary stop losses, and neither may refuse an exact exit because of its loss amount.
- Exactly one monetary stop-loss authority exists: $25 per six-hour window beginning at 00:00, 06:00, 12:00, and 18:00 America/New_York. The pending-order ceiling ratchets 30/25/20/15/10/0 as remaining budget moves through 100/80/60/40/20/0%. No separate daily, session, per-market, or per-position money-loss stop may be added without a new owner decision.
- Quote two ticks behind the corresponding touch initially and target 75% locally measured qualifying uptime against Gemini's published 50% requirement.
- AI manages implementation and operation; the owner retains business/risk and activation decisions.
- The Polymarket bot is reference-only and must not be modified or controlled as part of Gemini work.

## Required before first activation

- Complete production implementation and every commissioning stage below the first mutation.
- Show the owner the selected first-live pool, exact proposed order(s), maximum reservation/loss path, current reconciled account state, and pass/fail evidence.
- Obtain explicit first-live activation after that commissioning evidence is reviewed. Approval of this policy is not activation.

## One-time commissioning authorization

- September 15, 2026: owner authorized one five-minute read-only preflight. It may authenticate, observe streams, and perform paced account/market reconciliation. It may not place/cancel orders, consume an activation permit, change account settings, or start continuous trading. The diagnostic connection disables cancel-on-disconnect.

- September 15, 2026: the owner authorized up to $10 for live entry/exit testing and then selected aggressive limit fills rather than a resting maker test. This authority covered only the bounded YES and NO round-trip canaries; it does not activate the continuous reward bot or raise any standing limit.
# September 15, 2026 — repeat read-only preflight authorized

- The owner authorized one repeat five-minute preflight after the market-data readiness fixes.
- This authorization is read-only only: no order placement, cancellation, activation permit, or account-setting changes.
