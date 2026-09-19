# Fractional fill precision and NO exit repair — September 19, 2026

## Incident truth

The production runner halted after an authenticated maker partial fill. Stable read-only venue checks showed exactly one 5.93-contract NO position and zero active orders. The fill was BUY NO at 0.68 with authenticated fee zero. Three later exit intents were terminal and had no server order identity: the final local mutation guard rejected them before transport.

The raw notional is 4.0324. The contract metadata reports `quoteAssetPrecision: 2`, and Gemini's private authenticated cash update reflected a 4.04 debit. The prior ledger stored raw notional, so cash classification became incomplete. The independent safety clock correctly halted after its reconciliation grace. A concurrent risk read then observed the changed gate state and persisted an incomplete window. Emergency drain attempted exits, but the same incomplete-account guard blocked each before send and the loop incorrectly counted those local blocks as three venue attempts.

The old negative-position exit was also incompatible with Gemini collateral return. Gemini documents reducing NO inventory by BUY YES, not direct SELL NO, for accounts using that feature.

The first controlled activation proved that route on the live account. The bot
bought 5.93 YES at 0.32, Gemini filled the IOC, and authenticated position proof
became flat. The fill reported a 0.07 fee. Gemini credited 3.97: the raw
complementary credit 4.0324 rounds **up** to 4.04 at quote precision before the
exact fee is subtracted. The first implementation rounded that credit down to
4.03 and expected 3.96, leaving an exact one-cent mismatch. The safety clock
correctly halted again rather than adopting the unexplained cent.

## Repair

- Parse and validate each contract's `quoteAssetPrecision`; malformed or missing metadata remains isolated and untradeable.
- Round BUY debits up and SELL credits down at quote precision, subtract authenticated fees, and persist the exact cash delta with each fill.
- Account for BUY YES negative-position exits as the complementary collateral credit rounded up at quote precision, then subtract the authenticated fee. Such a BUY is an exit, never a new entry: it creates no new deadline, sibling cancellation or safety action.
- Persist historical symbol precision. A startup repair considers only blank fills at or after the durable cash watermark and commits only when the entire candidate correction exactly equals the fresh authenticated private-stream cash balance. It is atomic and idempotent. Older exception-covered history is untouched.
- Repair the earlier down-rounded collateral exit only when the complete post-watermark correction exactly matches fresh authenticated cash and positions. The migration is atomic, fill-identity guarded and idempotent; it cannot reset the cash baseline.
- Build and validate negative exact exits as BUY YES against fresh synchronized asks. Positive inventory remains SELL YES against bids.
- Identify entry versus exit from the durable intent, not from BUY/SELL alone. Unknown purpose remains blocked.
- Count an IOC retry only after the order reaches transport. Repeated local pre-send blocks wait within the same 30-second proof window and do not spend three venue attempts.
- Capture whether a risk mark started while quoting so a simultaneous fill-triggered halt cannot persist a false incomplete risk window.

## Safety boundaries

This does not tolerate unexplained cash, guess missing precision, replay an ambiguous mutation, extend the original inventory deadline, bypass HARD, or force an exit without exact synchronized depth. No-buyer/no-depth remains isolated to the held market; it consumes position exposure and an owner allocation but no pending-order slot. Unrelated fully reconciled markets may continue.

## Verification and deployment state

The complete follow-up verification passed: **1,025 production tests plus 28 script tests (1,053 total)**, Python compilation, all PowerShell parse checks, whitespace checks and offline Windows policy/durable-storage release validation. Dedicated regressions cover both observed fractional-fill examples, the live fee-bearing complementary exit, BUY-exit lifecycle isolation, both fresh-balance-matched/idempotent legacy repairs, durable exit intent classification, the risk-mark race and pre-send retry accounting. The first controlled activation closed the position and then halted on the one-cent accounting discrepancy; it did not resume unrelated quoting.

The second controlled deployment completed from remote-matched `main` commit
`cf36373`. Fresh read-only preparation proved flat/order-free state, applied the
one-cent correction exactly once, completed the non-HARD risk window, tracked
32 markets and found 12 eligible at that checkpoint. A verified post-migration
backup preceded fresh exact build/policy/account v7 authority with no uptime
override. Initial live acceptance at 08:58 ET found 24 canonically owned orders
across 12 markets, zero positions, all five public shards connected, no
read/stream holds, backup error, current-process warning/error or supervisor
restart. This is initial acceptance, not a guarantee of future fills, rewards
or uninterrupted venue availability.

Official references:

- [Prediction Markets market schema](https://developer.gemini.com/prediction-markets-spec/markets)
- [Collateral Return](https://developer.gemini.com/prediction-markets/collateral-return)
- [Maker Fee-Free Trading Promo](https://developer.gemini.com/prediction-markets/maker-fee-free-trading-promo)
