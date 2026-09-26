# Shielded placement and the 50% uptime ledger — September 25, 2026

Policy v9 (`config/live_policy.json`). Source-only; the bot is stopped and nothing here is deployed or activated.

## Why

Sept 15–22 results from the canonical journal: $17.88 liquidity rewards and $0.23 maker rebates, against −$38.79 realized trading P&L. Senate and House control produced 87% of the rewards; they were held all day at 98–99% uptime with almost no fills. Crypto markets never earned a reward cent and lost $30.49, mostly in three ZEC fills. Before each ZEC fill, the queue ahead of our order held steady at 100–208 contracts with zero trades, until a single sweep took the whole level.

Uptime was lost in four ways:
- The bot counted midnight-to-midnight ET windows, while Gemini's scoring day evidently closes at 5 pm ET.
- The bot paused held events 4,765 times on its own profit estimate.
- Restart days hurt: on 9/22 only 1 of 10 events qualified.
- Game markets rotated in and out, so none of them reached 50%.

The rewarded universe also changed after 9/22. The public catalog on 9/25 held six category pools ($5,500/day): College Football at $5,000 across 251 events, 249 of them with zero qualifying makers, plus small crypto pools. The event-override pools that paid us were gone.

## Placement: rest behind other traders' volume

A BUY leg rests at the price closest to the midpoint where the external (own-order-adjusted) volume at that price or better is at least the **entry requirement**. The requirement is 500 contracts, or 3 × the last 60 seconds of same-side selling if that is higher. Placement never improves the external best bid and never goes further than 10¢ from the midpoint. NO bids are read from YES asks at the complementary price. `src/gemini_bot/shield.py` holds the pure logic.

Each resting order's protection is the external volume at strictly better prices plus the estimated queue ahead at its own price. That estimate is the existing queue-scenario anchor, re-anchored at the actual send price.

- **Reposition:** when protection falls below the **keep level** (60% of the requirement), that leg is canceled. After exact terminal proof, a later public depth sequence and fresh REST/WS proof (flat symbol, only the sibling active, intents consistent), one fresh leg is placed at the current shielded price. The other leg keeps resting, so the event keeps a qualifying order for its snapshots. A fill during the cancel hands control to normal fill safety, with the full pair barrier.
- **Upgrade:** a leg may move closer to the midpoint at most once per 300 seconds per market. A cancel over 10 seconds switches upgrades off; near-fill repositioning is never switched off.
- A shield reduces fill exposure. It cannot guarantee zero fills: a sweep larger than the volume in front of the order still reaches it.

## 50% uptime ledger

- **Scoring window: 17:00 → 17:00 ET.** This is inferred, because on all 7 recorded days Gemini published the complete 1440-snapshot summary the same evening (18:20–20:18 ET). The rank report compares our count for each closed window with the venue's count and flags differences over 3%. That comparison confirms or corrects the boundary.
- **Per-event target:** ceil(50% × window minutes) + 30 minutes. The window is clipped to the reward end and expiry. Game in-play minutes are counted conservatively as part of the window.

| Status | Meaning | Behavior |
|---|---|---|
| LOCKED | target reached | quote normally |
| ON_TRACK | ≥120 spare minutes | normal shield |
| AT_RISK | <120 spare minutes | entry requirement lowered to the keep level; no upgrades |
| LOST | cannot reach 50% | stop quoting until the next window ($0 reward, only fill risk) |

- **New events** are admitted only with at least 60 spare minutes in the current window.
- **Held events are not paused** mid-window for profit estimates. Only safety, shield, entry-quality and lifecycle reasons pause them. Economics still decide admission and the 5 pm rotation.

## Selection

- Crypto categories are excluded, for both reward-row and event categories.
- Discovery reads up to 24 events per shared pool. It prefers the fewest qualifying makers (zero first) and the longest remaining reward life.
- When an event's latest snapshot had zero qualifying makers, its visible depth is treated as non-qualifying (house) liquidity in the competition estimate. The fixed unseen-contract prior still applies.

## Unchanged limits

The owner changed only two limits: $400 gross (from $300) and 50 contracts per leg (from 30). Everything else is unchanged:
- $90 per market; at 50 per leg this blocks legs priced above $0.80.
- $40 stressed exit.
- The $30 six-hour HARD stop.
- 40 orders and 20 owners.
- Exit rules, accounting and recovery.

## Pair netting accounting (prerequisite)

A BUY that nets against opposite-outcome inventory on the same symbol is now booked as a collateral-return credit of qty × (1 − price), rounded up at quote precision. It no longer counts as a second purchase debit. It writes no new inventory deadline for the netted part. The realized-P&L replay closes the old lifecycle at a sign flip instead of marking it unclassifiable.

The rounding of BUY NO against YES has not been checked against real venue data. A mismatch still fails closed through cash reconciliation.

## Verification

- 1,137 production tests and 28 script tests pass, plus offline release validation. The new suites are `test_pair_netting.py`, `test_shielded_placement.py` and `test_policy_v9.py`. The last includes a transport-level test in which only the near-fill leg moves one level deeper while its sibling keeps resting.
- Legacy suites run against an exact copy of the v8 policy (`tests/fixtures_live_policy_v8.json`), because v8 remains loadable.
- The queue-shadow archives record only top-of-book depth, so they cannot replay full-depth shields. The ZEC case, with 208 contracts ahead, is covered as a refusal at 500.

## Not claimed

Nothing here claims fill avoidance, reward qualification, exact queue position, the category-pool split, or profitability. The first day of quoting a zero-maker game event will show whether the College Football $5,000 pool is budgeted per event (normalized score ≈ 1) or shared.
