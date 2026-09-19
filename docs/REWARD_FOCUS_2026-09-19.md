# Realized reward focus and size-30 deployment

The owner approved a 70% live-model / 30% historical preference and a controlled restart together with the already verified 30-contract maker size. Existing healthy owners remain sticky. The calibration orders new/released slots within the existing watched universe; it does not reset the cohort or expand discovery/rate limits.

## Evidence and formula

Four authenticated reports are retained: September 16 and 17 below the payout threshold, and September 18/19 paid. The paid totals are 1.44 and 3.25. The latest three leading events (Senate, House, JAX–DEN) contribute 3.10 of the latest 3.25. Event amounts are rounded, so they never replace account totals.

Use only paid rows with a valid nonfuture payment timestamp, plus earlier-date BELOW_THRESHOLD/ZERO_AMOUNT rows. Pending, unknown, missing amounts, zero denominators and future rows supply no preference. Evidence is bounded to 14 dates; an event last observed more than seven days ago becomes neutral. Duplicate/invalid data invalidates the calibration atomically and falls back to the live model.

For each exact numeric event ID, compute the weighted mean reward among reports with at least 50% of reported snapshots, and multiply it by weighted qualification reliability. Weight each date by `1 / (1 + age_in_days)`; pre-September-18 regimes receive an additional 0.25 factor. Confidence is `min(1, qualified_weight / 2)`. No qualifying observations means unknown quality, not a zero-reward ban.

Each vacant-slot allocation recomputes positive marginal live profit per capital-hour and normalizes it by the largest current candidate value. Historical preferences are normalized by the largest historical event preference, then shrunk toward that candidate's model preference using confidence. The final ordering is `0.70 * model + 0.30 * shrunk_history`. Missing evidence is neutral. A second contract from an already-selected event cannot repeat a positive historical uplift. Conflicting current mappings of an event ID to different event/pool identities get no historical adjustment.

These are dimensionless ordering preferences. Gemini provides event totals without exact contract/capital-hour attribution, so pool dollar forecasts remain labeled proxies and historical mean event rewards are displayed separately. Increasing size does not multiply historical rewards. Generic new sports events do not inherit another matchup's reward; their current book, pool and economics determine entry until evidence exists.

## Runtime and continuity

Reuse the existing authenticated five-minute reporting refresh and persisted history; no extra per-market requests or scheduled job. Compile the evidence once per new history object/date, with bounded per-candidate scoring. Legacy sessions retain the original selector. Continuous sessions use historical preference only for vacant/released slots. All live/marginal profit checks, sibling economics, maker sizing/pricing, sticky reservations, fill/exit/accounting/HARD, shared pools and rate budgets remain authoritative.

The size-30 release changes quote quantity while preserving the $300 gross/$90 market/$40 stressed-entry and $30 six-hour HARD limits, 40-order/20-owner ceilings, best bid and optional 300-second/500-contract refresh controls. Approximately ten pairs can fit at ordinary prices. This can leave some retained owners without funded orders; ownership alone earns no snapshots. Neither a report nor a restart changes the reward-day clock or gives downtime credit.

Official documentation rechecked September 19: [liquidity scoring](https://developer.gemini.com/prediction-markets/liquidity-rewards-program) and [authenticated daily summary](https://developer.gemini.com/rest-api/prediction-markets/rewards/get-liquidity-rewards-daily-summary). The summary remains dated evidence; its publication time is not an asserted scoring reset.

## Verification and deployment

Deterministic evidence/selection/runtime cases cover stale, missing, pending, malformed, duplicate and future evidence; exact event identity; confidence/recency; no repeated event uplift; sticky/locked ownership; negative-profit refusal; pool forecast separation; and restored runtime integration. Full regression and live acceptance results are recorded below after completion.

Full verification passed 1,060 production tests and 28 script tests (1,088 total), plus Python compilation, native PowerShell parsing, whitespace checks and offline Windows policy/storage validation. The final reporting addition was checked again with the reward-focus and continuous-service suites. Read-only predeployment proof found 22 owned size-15 orders, zero positions, 20 saved owners and healthy journal integrity; the active checkout stayed unchanged during construction.
