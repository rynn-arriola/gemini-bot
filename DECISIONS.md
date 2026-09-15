# Active decisions

These are current account-owner decisions. Changing one requires a new explicit owner decision and a dated update.

## Approved

- September 15, 2026: owner approved the follow-up review fixes and WebSocket improvements: 100 ms differential depth, connection-reported `conninfo` rate control, and authenticated order-stream cancellation confirmation before bounded REST fallback. Trading remains stopped until the owner explicitly requests restart. Rate and price corrections do not change the 30-slot/$30 six-hour/10-contract business policy.

- September 15, 2026: every code or policy change must include an explicit impact check against Gemini's 50% qualifying-snapshot requirement. Preserve sticky pool uptime when safe; safety, exact exits, exposure controls, venue validity and reconciled ownership always override reward uptime.
- September 15, 2026: keep a fixed ceiling of 30 pending entry orders until the single hard stop. Open positions remain excluded from the order count and unrealized marks do not reduce quoting breadth. Replace the tiered $25 marked-equity control with one $30 per-six-hour Eastern hard stop calculated only from completed, matched bot fill lifecycles including their recorded fees. At HARD, cancel entries and flatten; no separate money-loss stop is added. This $30 decision supersedes the briefly proposed $90 value before restart.
- September 15, 2026: add an event-driven adverse-move guard. A depth update cancels an owned entry pair when either resting leg has become closer than the approved two-tick distance; normal sticky maintenance may replace it only after terminal cancellation. A quote that merely moved farther from touch is not churned by this fast guard.
- September 15, 2026: manual position closes should recover like the Polymarket workflow. Only an explicit operator reconciliation may adopt the new baseline, and only after fresh authenticated REST/WebSocket proof of zero positions, zero active orders, and terminal bot intents. It must preserve the current six-hour loss window and must not invent bot fills. Exit retries are allowed only after the prior IOC is proven terminal and the exact residual agrees across authenticated sources.
- September 15, 2026: owner approved a bounded supervised session patterned after Polymarket's fail-closed boundaries. Version-5 approval permits at most two recovery restarts, only in the same supervisor session and only after the prior runner durably proves flat. Use 10/20-second backoff and stop after three fast failures. Nonflat, ambiguous, exhausted, cross-session, startup, or integrity failures enter `ATTENTION_REQUIRED` and cannot reactivate.
- September 15, 2026: use profitability ranking v2 directly, without a shadow phase. Keep the approved two-tick/10-contract quote policy unchanged. Rank unique reward pools by conservative expected liquidity reward plus applicable maker rebate, less fee/adverse-selection/exit-depth costs, normalized by capital-hours. Keep healthy selected pools sticky so short-term telemetry cannot destroy reward uptime. The owner approved a graceful restart after validation.
- Product is intended for live Gemini Predictions trading, not paper execution.
- Native Windows is the only supported production platform.
- WebSocket is primary for market data, order mutations, order events, balances, and positions.
- REST is used only where WebSocket lacks the information, for discovery/config/reward metadata, startup state brackets, and bounded reconciliation/recovery.
- Base reward quote quantity is 10 contracts per outcome leg.
- Maximum pending/open order count is 30. Filled positions do not consume this order-count limit; they remain governed by independent position and gross-exposure limits.
- Maximum gross reservation/exposure is $300; maximum exposure attributable to one market is $90.
- Maximum tracked market owners is 32, but no more than 15 pools may be active at once because each consumes or reserves two of the 30 planned quote slots. A pool with an open position stays locked in that 15-pool allocation until flat even though the position is not falsely reported as a pending exchange order.
- The $40 stressed-exit value is an entry-admission filter, not a stop loss.
- Use a 300-second inventory exit and a 300-second pre-expiry override. These are time exits, not monetary stop losses, and neither may refuse an exact exit because of its loss amount. The owner shortened the inventory limit from 15 minutes to five minutes on September 15, 2026.
- Exactly one monetary stop-loss authority exists: $30 per six-hour window beginning at 00:00, 06:00, 12:00, and 18:00 America/New_York. The pending-order ceiling remains 30 below HARD and becomes 0 at HARD. Only completed matched bot fill lifecycles, including their recorded fees, enter this realized-P&L meter; open-position marks do not. No separate daily, session, per-market, or per-position money-loss stop may be added without a new owner decision.
- Quote two ticks behind the corresponding touch initially and target 75% locally measured qualifying uptime against Gemini's published 50% requirement.
- Sports markets must stop new entry and force any position flat 20 minutes before their published game start, even if Gemini permits in-play trading. Crypto interval markets remain on the separate five-minute pre-expiry boundary and are governed by their contract effective/expiry interval rather than a blanket `isLive` exclusion.
- AI manages implementation and operation; the owner retains business/risk and activation decisions.
- The Polymarket bot is reference-only and must not be modified or controlled as part of Gemini work.

## Required before first activation

- Complete production implementation and every commissioning stage below the first mutation.
- Show the owner the selected first-live pool, exact proposed order(s), maximum reservation/loss path, current reconciled account state, and pass/fail evidence.
- Obtain explicit first-live activation after that commissioning evidence is reviewed. Approval of this policy is not activation.

## One-time commissioning authorization

- September 15, 2026: owner authorized one five-minute read-only preflight. It may authenticate, observe streams, and perform paced account/market reconciliation. It may not place/cancel orders, consume an activation permit, change account settings, or start continuous trading. The diagnostic connection disables cancel-on-disconnect.

- September 15, 2026: the owner authorized up to $10 for live entry/exit testing and then selected aggressive limit fills rather than a resting maker test. This authority covered only the bounded YES and NO round-trip canaries; it does not activate the continuous reward bot or raise any standing limit.
- September 15, 2026: owner authorized one repeat five-minute preflight after the market-data readiness fixes. It remained read-only: no order placement, cancellation, activation permit, or account-setting changes.
- September 15, 2026: owner authorized the bounded one-market live maker test and acknowledged real-money execution. The displayed test is one BUY YES order for 10 contracts on `GEMI-ZEC2609152100-HI1220` at the fresh two-ticks-behind-touch price of $0.02, resting for five seconds before cancel/drain. Actual initial reservation is $0.20; conservative pair admission remains $9.50 with a $9.30 stressed-exit check. If filled, the production safety path must cancel risk-adding orders and reduce the exact authenticated position. This does not authorize continuous trading.
- September 15, 2026: after the first canary exposed and offline verification repaired the replacement loop, owner explicitly authorized one repeat five-second live canary. Fresh preparation selected the same exact order: `GEMI-ZEC2609152100-HI1220`, BUY YES 10 @ $0.02. This repeat does not authorize continuous trading.
- September 15, 2026: owner directed correction of the unsupported midnight-UTC reward-day assumption. Until Gemini publishes a more exact eligible-snapshot boundary, local feasibility uses Eastern calendar dates consistent with the authenticated daily-summary API and remains labeled a proxy.
- September 15, 2026: owner approved a read-only reward-timing check and shortened the first proposed 30-minute window to 5–10 minutes. The ten-minute diagnostic and post-repair five-minute verification sent no mutations and consumed no permit.
- September 15, 2026: owner authorized a live run for the remainder of the Eastern day, explicitly overriding only the local 50% feasibility gate to observe real positions and trading losses. Owner clarified the account is funded with $300 and expects normal automatic scanning/ranking across rewarded markets. Version-4 permit `reward-catalog-20260915-live-test` therefore authorizes dynamic selection only from Gemini's reconciled reward catalog, capped at 15 simultaneous pools/30 pending orders, and expires with its override at 00:00 America/New_York on September 16. All other approved risk controls remain unchanged. This does not guarantee or claim a September 15 reward.
