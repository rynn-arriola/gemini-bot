# Priced order exposure — September 16, 2026

The owner retained 20 contracts per leg and the 30-order target, explicitly rejecting a return to 10 contracts and the blanket $1-per-contract order reserve. The configured dollar limits and $30 six-hour realized-loss HARD remain unchanged.

The observed eighth-market churn had one accounting cause: pair admission reserved the two limit-order costs, while live exposure/final-send validation charged every contract at $1. At seven size-20 pairs this became 280; the eighth first leg passed price-weighted admission, its second failed the full-contract cap, and the first was canceled. The seven complete pairs remained working, but repeated first-leg attempts consumed venue capacity.

The repair changes only working BUY and proposed BUY reservation to price times remaining/submitted quantity. Held-position reservation remains unchanged, and working SELLs do not reduce it. Both outcomes count independently; no offset or future exit is assumed. Complete-pair admission still checks available funds, gross/market limits and both order slots before sending. The final send rechecks current account/exposure and all generation, first-leg identity, fill, lifecycle, deadline, HARD and ownership guards.

Offline regressions run the real pair/transport/reducer/guard path with fake transport. Tests for the eighth market and thirtieth size-20 order fail against the prior source and pass with the correction. Additional cases cover repeated over-cost admission (no first leg or cancellation), exact $300 boundary, remaining quantity, retained inventory despite an active SELL, insufficient funds, market cap, genuine second-leg exposure changes and zero pending allowance at HARD.

Snapshot impact: eliminates avoidable capacity-edge placement/cancellation and removes the artificial seven-market ceiling. Healthy resting pairs need not rotate. Concrete price, book, economics, funds and exposure gates may still leave slots unused; local presence never proves Gemini snapshots or payout. Live deployment requires the controlled stop/account proof and a new exact-build permit; do not hot-edit the running source.

Release verification: 578 production tests plus 28 script tests passed (606 total). Python compilation, all PowerShell parser checks, whitespace and offline Windows policy/storage validation passed. All testing used an isolated worktree; the old live checkout remained untouched during verification.

## Live acceptance

The old runner drained to authenticated `stopped_flat` at 17:54:42 ET before source replacement. Commit `ad6d827` was fast-forwarded into the live checkout and verified on `origin/main`; 29 focused tests and offline release validation passed there again. Fresh exact-account/build/policy v7 authority retains 15 pools, two same-session recoveries, null expiry and no uptime override. The persistent task started supervisor 16736 / runtime process 6672 at 17:55:51 ET and first NEW arrived at 17:58:07 ET after fresh preparation.

At 17:58:59 authenticated REST found 28 active orders across 14 markets, all original quantities 20 and all exact client/server identities owned by the canonical journal. Zero positions, SQLite `quick_check=ok`, five healthy public shards, no entry/read recovery holds, no supervisor restart and no new warning/error/critical logs. No post-launch pre-send exposure-change audit was recorded. Natural adverse-price cancellations remain active. The report contains 18 ranked candidates; four fail concrete quote-price/payout/net gates, so 28 orders is not an imposed cap. The 205.935-second last-old-cancel to first-new interval is a real deployment gap, not earned reward uptime. Long-run acceptance remains pending.
