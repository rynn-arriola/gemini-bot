# Repeat five-minute read-only preflight — September 15, 2026

**Later correction:** this report records the then-current UTC-day proxy. The owner subsequently directed use of an Eastern-calendar-day proxy consistent with Gemini's authenticated daily-summary dates. A new live read-only preparation found 13 feasible candidates. The measurements below remain historical evidence of the pre-correction behavior.

## Verdict

The repaired live data path passed its five-minute read-only check. The run completed **300.447 seconds**, from 10:46:09 to 10:51:09 America/New_York. All 32 selected books synchronized, the shared public WebSocket remained current and gap-free, and 25–27 books were two-sided across the samples. The one-sided/quiet-book failures from the first preflight did not recur.

The overall diagnostic result remained `blocked`, for a different and expected reason: the run used a new local uptime database and began at 14:46 UTC, leaving at most **38.33%** of the local UTC-day proxy. It could not accumulate the conservative 50% qualifying-uptime threshold from zero before that proxy day ended. This demonstrates the scheduling gate working; it is not evidence of a WebSocket or account-state failure.

This local UTC-day calculation is an operational proxy because the public reward row does not expose a certified score-day start. It must not be represented as proof of Gemini's actual eligibility denominator or expected payout.

## Safety and account consistency

- The diagnostic transport was read-only and used `cancelOnDisconnect=false`.
- Orders submitted: 0. Cancels submitted: 0. Blocked mutation attempts: 0. Activation permits consumed: 0.
- Account WebSocket state matched periodic REST reconciliation throughout. The account was flat at the recorded start and final checks; no balances are published here.
- Both WebSockets closed cleanly, and the final process check found no matching Gemini Python process.
- Standard SELL compatibility was not exercised by this read-only observation.

## Measured results

- Public WebSocket: 615 depth frames, 22 contract-status frames, three subscription responses, zero detected sequence gaps.
- Private WebSocket: 249 balance snapshots, 249 position snapshots, one order snapshot, one subscription response.
- REST: 39 public and 14 private requests, including startup/final state and three periodic account reconciliations. REST did not replace the streams.
- Books: 32 synchronized and fresh under shared-stream continuity; 25–27 two-sided across the observation window; 26 two-sided at the final sample.
- Runtime health: zero dropped dirty work, zero dropped log records, and **0.411%** average utilization of one logical CPU core.
- Candidate gate: zero eligible candidates. Twenty-six passed the entry-time window, but the local 50% uptime proxy was no longer reachable; ten candidate rows also had quote prices invalid at the configured two-tick offset.

## Operational consequence

The local proxy rolls at 00:00 UTC—20:00 America/New_York on September 15, 2026—and would reach 50% after roughly 12 continuously qualifying hours and the configured 75% target after roughly 18. This is not a documented Gemini account reset or proven score-day boundary. Gemini documents daily per-minute scoring and Eastern dates for the daily-summary API but does not publish the boundary used for the eligible-snapshot denominator.

No live activation is authorized by this result. A maker-mechanics canary may be engineered to ignore only this uncertain uptime proxy because it does not claim a daily reward; all other safety and exact-order approval gates must remain. Continuous reward operation still requires a defensible denominator. Owner review of the exact pool, contract, prices, reservation, account state, risk window, and rank report remains mandatory before any permit or order mutation.

Machine-readable evidence: `data/preflight-20260915-five-minute-postfix/preflight-report.json`.
