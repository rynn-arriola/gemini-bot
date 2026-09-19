# Fractional fill precision and NO exit repair — September 19, 2026

## Incident truth

The production runner halted after an authenticated maker partial fill. Stable read-only venue checks showed exactly one 5.93-contract NO position and zero active orders. The fill was BUY NO at 0.68 with authenticated fee zero. Three later exit intents were terminal and had no server order identity: the final local mutation guard rejected them before transport.

The raw notional is 4.0324. The contract metadata reports `quoteAssetPrecision: 2`, and Gemini's private authenticated cash update reflected a 4.04 debit. The prior ledger stored raw notional, so cash classification became incomplete. The independent safety clock correctly halted after its reconciliation grace. A concurrent risk read then observed the changed gate state and persisted an incomplete window. Emergency drain attempted exits, but the same incomplete-account guard blocked each before send and the loop incorrectly counted those local blocks as three venue attempts.

The old negative-position exit was also incompatible with Gemini collateral return. Gemini documents reducing NO inventory by BUY YES, not direct SELL NO, for accounts using that feature.

## Repair

- Parse and validate each contract's `quoteAssetPrecision`; malformed or missing metadata remains isolated and untradeable.
- Round BUY debits up and SELL credits down at quote precision, subtract authenticated fees, and persist the exact cash delta with each fill.
- Account for BUY YES negative-position exits as the complementary net collateral credit. Such a BUY is an exit, never a new entry: it creates no new deadline, sibling cancellation or safety action.
- Persist historical symbol precision. A startup repair considers only blank fills at or after the durable cash watermark and commits only when the entire candidate correction exactly equals the fresh authenticated private-stream cash balance. It is atomic and idempotent. Older exception-covered history is untouched.
- Build and validate negative exact exits as BUY YES against fresh synchronized asks. Positive inventory remains SELL YES against bids.
- Identify entry versus exit from the durable intent, not from BUY/SELL alone. Unknown purpose remains blocked.
- Count an IOC retry only after the order reaches transport. Repeated local pre-send blocks wait within the same 30-second proof window and do not spend three venue attempts.
- Capture whether a risk mark started while quoting so a simultaneous fill-triggered halt cannot persist a false incomplete risk window.

## Safety boundaries

This does not tolerate unexplained cash, guess missing precision, replay an ambiguous mutation, extend the original inventory deadline, bypass HARD, or force an exit without exact synchronized depth. No-buyer/no-depth remains isolated to the held market; it consumes position exposure and an owner allocation but no pending-order slot. Unrelated fully reconciled markets may continue.

## Verification and deployment state

The complete offline verification passed: **1,023 production tests plus 28 script tests (1,051 total)**, Python compilation, all PowerShell parse checks, whitespace checks and offline Windows policy/durable-storage release validation. Dedicated regressions cover both observed fractional-fill examples, exact complementary exit cash, BUY-exit lifecycle isolation, fresh-balance-matched/idempotent legacy repair, durable exit intent classification, the risk-mark race and pre-send retry accounting. The bot has not been restarted by this repair. Controlled deployment must preserve the canonical journal, prove the current venue bracket, create a verified backup, apply the exact balance-matched legacy correction, and use fresh build/policy/account authority before activation.

Official references:

- [Prediction Markets market schema](https://developer.gemini.com/prediction-markets-spec/markets)
- [Collateral Return](https://developer.gemini.com/prediction-markets/collateral-return)
- [Maker Fee-Free Trading Promo](https://developer.gemini.com/prediction-markets/maker-fee-free-trading-promo)
