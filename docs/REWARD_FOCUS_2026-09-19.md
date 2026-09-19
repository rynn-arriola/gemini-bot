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

### V34 deployment acceptance

Old v33 reached terminal-flat at 18:27:58 ET on September 19. The single-owner locked backup passed integrity and exact table/history comparison. Main and GitHub matched code `57e7036`. Post-merge 30 reward-focus/size tests and offline release validation passed. Fresh read-only preparation proved zero orders/positions, preserved economic/sticky history and complete non-HARD risk, with 32 watched/ranked and 11 actionable candidates. Calibration loaded 18 events without error. New exact v7 build/policy/account authority has no uptime override.

The hypothetical empty-cohort preview produced the same first-five displayed symbols with and without history (Mbappe, Kane, Claude, Senate GOP, Senate DEM); that diagnostic did not reset or persist a new cohort. Historical preferences do not guarantee different symbols under current economic gates. The existing 20 owners were preserved for the actual launch.

The persistent task launched `data/production-20260919-reward-focus-v34` and reached quoting around 18:32:35 ET. The first acceptance attempt preceded the first calibration-bearing rank report and correctly refused to claim full acceptance; the refreshed report populated normally without intervention. Authenticated acceptance at 18:33:08 ET then proved 20 owned size-30 orders across ten markets, zero positions, unchanged gross cap respected, all 20 prior owners retained, five connected/unheld public shards, no entry/stream holds and zero restarts. Build/policy matched the permit. Logs contained no warnings/errors, stderr was empty, reward program was not paused and automatic backup succeeded.

A second authenticated check at 18:35:44 ET again found 20 owned size-30 orders/ten markets, zero positions, all 20 prior owners, 18 calibrated events, five healthy shards and no holds/restarts. The build and policy still matched authority after documentation-only edits.

This is initial deployment acceptance. Future reward outcomes, actual newly allocated slots, natural fills/exits and uninterrupted presence remain observational evidence, not guarantees. No live fault or fill was induced and no runtime source was hot-edited.
