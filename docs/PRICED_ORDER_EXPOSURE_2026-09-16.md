# Priced order exposure — September 16, 2026

The owner retained 20 contracts per leg and the 30-order target, explicitly rejecting a return to 10 contracts and the blanket $1-per-contract order reserve. The configured dollar limits and $30 six-hour realized-loss HARD remain unchanged.

The observed eighth-market churn had one accounting cause: pair admission reserved the two limit-order costs, while live exposure/final-send validation charged every contract at $1. At seven size-20 pairs this became 280; the eighth first leg passed price-weighted admission, its second failed the full-contract cap, and the first was canceled. The seven complete pairs remained working, but repeated first-leg attempts consumed venue capacity.

The repair changes only working BUY and proposed BUY reservation to price times remaining/submitted quantity. Held-position reservation remains unchanged, and working SELLs do not reduce it. Both outcomes count independently; no offset or future exit is assumed. Complete-pair admission still checks available funds, gross/market limits and both order slots before sending. The final send rechecks current account/exposure and all generation, first-leg identity, fill, lifecycle, deadline, HARD and ownership guards.

Offline regressions run the real pair/transport/reducer/guard path with fake transport. Tests for the eighth market and thirtieth size-20 order fail against the prior source and pass with the correction. Additional cases cover repeated over-cost admission (no first leg or cancellation), exact $300 boundary, remaining quantity, retained inventory despite an active SELL, insufficient funds, market cap, genuine second-leg exposure changes and zero pending allowance at HARD.

Snapshot impact: eliminates avoidable capacity-edge placement/cancellation and removes the artificial seven-market ceiling. Healthy resting pairs need not rotate. Concrete price, book, economics, funds and exposure gates may still leave slots unused; local presence never proves Gemini snapshots or payout. Live deployment requires the controlled stop/account proof and a new exact-build permit; do not hot-edit the running source.

Release verification: 578 production tests plus 28 script tests passed (606 total). Python compilation, all PowerShell parser checks, whitespace and offline Windows policy/storage validation passed. All testing used an isolated worktree; the old live checkout remained untouched during verification.
