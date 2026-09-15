# Five-minute read-only preflight — September 15, 2026

## Verdict: live startup blocked

The owner authorized a five-minute non-ordering preflight. The completed run lasted **300.516 seconds**, from 10:29:42 to 10:34:43 America/New_York, including startup and final reconciliation. Account streams ran for approximately 253 seconds after discovery/subscription. An earlier attempt stopped after 70.626 seconds on the same readiness timeout; it is preserved separately and is not counted as the completed five-minute run.

Production trading was never activated. No order placement, cancellation, permit consumption, or account setting change occurred. The dedicated diagnostic adapter blocks all order commands and explicitly uses `cancelOnDisconnect=false`. Both WebSockets closed at completion; the final process check found no matching Gemini Python process.

## Measured results

- Authenticated account streams delivered 253 balance snapshots, 253 position snapshots, one order snapshot and one subscription response. Account freshness and cash/position matching passed all 15 observation samples and the final check.
- Startup, three periodic REST reconciliations, and the final REST/WS checks found consistent account state. The account was flat with no active orders at both recorded endpoints. No account amounts or credentials are included here.
- Public streaming delivered 531 depth frames, ten contract-status messages and three responses. All 32 tracked books were synchronized; zero sequence gaps, dropped dirty work or dropped log records were observed.
- Traffic: 39 public REST requests, 14 private REST requests, three public WS subscription requests and one private WS subscription request. No throttling failure was observed. REST remained paced; it did not replace streaming.
- CPU averaged **0.411% of one logical core**, including discovery and reporting. This measures a read-only preflight, not live order-processing performance.
- Final public snapshot: 32 synchronized books, 28 two-sided books, three passing the three-second freshness check. Across 15 observations, fresh-book counts ranged from two to five (mean 3.53).

## Confirmed startup blocker

`ProductionOrchestrator._wait_ready()` requires a two-sided book for **every** selected market. Four selected contracts remained one-sided throughout the diagnostic:

| Contract | Bid levels | Ask levels |
| --- | ---: | ---: |
| `GEMI-XAU2609182100-HI4200` | 1 | 0 |
| `GEMI-WXHIGH-TUS-2609170359-84TO85` | 0 | 1 |
| `GEMI-NFL-2609220015-NYG-LAR-TT-LARO10` | 1 | 0 |
| `GEMI-NFL-2609150015-DEN-KC-PPPASSTD-NIXO0` | 2 | 0 |

These books have received snapshots; waiting longer did not create the missing side. They block startup of the entire 32-market cohort, including usable markets. Discovery selects contracts marked active/open without proving their live books satisfy the strategy.

## Freshness finding requiring engineering review

The runtime uses the time of the last applied depth change as book freshness. Most books failed its three-second rule while account streaming and public traffic continued. Quiet/one-sided books are not evidence that every connection is broken. Conversely, a live connection alone does not prove every market is current.

Before live trading, distinguish unchanged-but-confirmed market state from genuinely stale/unconfirmed depth, using bounded WS verification where needed. Do not simply remove the freshness guard or extend it arbitrarily. Isolate unusable contracts instead of allowing them to block all otherwise usable markets, while keeping ownership, expiry, pricing and approval gates intact.

Because production preparation failed, normal uptime initialization/ranking was not completed. The diagnostic's two-to-five locally eligible candidates reflect price/size/freshness checks without a completed uptime estimate. They are **not validated live candidates or proof of reward eligibility**.

## Next

1. Obtain authority to repair the startup-selection and data-freshness handling; add realistic one-sided/quiet-market tests.
2. Retest offline and repeat an approved read-only preflight before proposing a live maker canary.
3. Still verify standard same-outcome SELL compatibility and then obtain a fresh exact-symbol live permit. Read-only observation does not test order acceptance, fills, exits, fee application or reward payout.

Evidence: `data/preflight-20260915-five-minute-diagnostic/preflight-report.json` and its local logs. Earlier stopped attempt: `data/preflight-20260915-five-minute/preflight-report.json`. Diagnostic implementation: `scripts/preflight_read_only.py`; explicit mutation-block tests: `scripts/test_preflight_read_only.py`. Production trading modules were not changed during this preflight.

Read-only connection behavior checked against [Gemini connection parameters](https://developer.gemini.com/websocket/introduction).
