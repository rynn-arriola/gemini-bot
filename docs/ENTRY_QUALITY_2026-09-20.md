# Entry-quality policy v8 — September 20, 2026

## Scope and formula

The owner approved an entry-only check after a near-expiry Zcash quote filled before the exit bid fell sharply. For every proposed two-sided maker pair, evaluate both possible 30-contract fills against a current synchronized L2 book after removing our own acknowledged working orders. Halve each displayed exit level. YES hypothetically sells into YES bids; NO hypothetically closes by BUY YES from asks and receives collateral return of `1 - YES purchase price` per contract. Charge the published standard taker fee on each consumed level, rounded upward, and standard maker entry fee without assuming a rebate or promotion. Uncovered quantity has zero exit value.

Each side must have `haircut-fillable quantity / proposed quantity >= 0.70` **and** `hypothetical net exit value / (entry notional + conservative entry fee) >= 0.70`. Both sides must pass. Freshness, timing, venue, capital, ownership, economics and HARD remain independent gates. The 70% number is not a stop loss and never blocks an exact-position reducer. For Crypto, new entry ends 900 seconds before contract expiry; existing 300-second pre-expiry force exit remains unchanged.

Policy v8 binds the parameters to the exact build/account permit. Version 7 and older have no entry-quality rule. No active process, permit or order is changed by source preparation.

## Availability and limits

The gate may cancel an affected working pair or prevent re-entry, losing real resting time and potentially Gemini qualifying snapshots. A market that recovers may rejoin through the existing terminal-cancel, flat-proof, cooldown and sticky-owner paths; unrelated markets remain available. It does not infer or improve the unpublished 50% scoring-window start and cannot guarantee prevention of a later price collapse. Full-depth live counts are exposed only by the v8 rank report after controlled deployment.

At 18:54 ET, the existing v34 archive showed 29 ranked markets. A conservative *top-level-only* calculation passed 28; among 14 markets with otherwise positive current economics (ignoring the reporting-only uptime proxy), 13 passed. The one failure had complete aggregate depth according to a separate ratio, but the archive lacks its deeper price levels. Therefore the exact full-book pass count was 28–29 ranked and 13–14 otherwise actionable at that instant. The active bot remained v34; no live policy activation or restart was performed to obtain this estimate.

At 19:11 ET, a newer archive showed 21 of 22 ranked and 14 of 15 otherwise actionable passing on the best level alone. One separately paced read-only public full-book request for the remaining BTC contract covered all 30 NO contracts after haircut and retained 95.90% of modeled entry value. The combined near-contemporaneous check therefore supports 22/22 ranked and 15/15 otherwise actionable, but it is not one atomic snapshot and cannot promise that breadth later.

## Verification and deployment boundary

Offline tests cover both outcome formulas, shallow depth, price collapse despite ample quantity, own-order subtraction, missing book, legacy-policy exclusion, entry timing, final pre-send recheck, market-local cancellation and reducer independence. The final 1,074-test production suite and 28 script tests, compilation, native PowerShell parsing and offline Windows release validation pass. Deployment requires terminal-flat old owner, fresh authenticated account/journal proof, new build/policy/account-bound v7 permit and an explicit owner decision to activate. No old v34 permit may authorize policy v8.

Gemini source: [liquidity rewards and 50% snapshot requirement](https://developer.gemini.com/prediction-markets/liquidity-rewards-program); [public depth streams](https://developer.gemini.com/trading/websocket/streams).
