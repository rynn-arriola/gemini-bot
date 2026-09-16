# Reward window, recovery and logging — September 15, 2026

## Scope and evidence

Owner approved conservative reward treatment, added the stopped bot investigation, requested useful logs and recovery resembling Polymarket. The initial implementation made no live API call. The restart-readiness follow-up adds authenticated read-only preflight; **no trading restart**, order mutation, risk reset or permit extension.

Runner and supervisor are absent. Last runner heartbeat: 18:29:54 ET; supervisor status was older. Runtime logs and runner stdout/stderr were empty; `runner-result.json` absent. Selected Windows event queries supplied no causal termination proof. External termination is possible, not established. Do not attribute this incident to independently repaired code defects.

## Corrections

- Venue denominator estimate: Eastern calendar day intersected with available reward/trade-start and reward-end/contract-expiry bounds. Crypto pre-expiry and sports pre-start cutoffs only reduce local earning opportunities. Current-minute samples are not also future minutes; missed/inactive observations do not forgive elapsed time. Invalid windows block admission. Missing sports start/live status cannot admit entries.
- The 75% local target and 50% venue threshold remain distinct. Feasible rank estimates cap contribution at the reachable fraction. Infeasible candidates retain labeled counterfactual potential only for the existing separately approved uptime-test override. No new override or reward guarantee.
- Exchange counts remain event-ID/date matched reports with freshness/availability, separate from local estimates. Missing rows are unavailable, not zero. No random-snapshot claim, fixed payout-time reset, exact-phase prediction or maintenance deduction.
- Both maker legs' immediate fills, including partial second-leg fills, use same-symbol cancellation and original fill safety rather than unnecessary global drain. True rejections/ambiguous acknowledgements remain fatal.
- Periodic REST risk checks defer to the independent ten-second cash/position grace and still reject incomplete realized-fill history. A fill racing the REST bracket invalidates that mark instead of persisting a false stop.
- Fill identity, expected cash/position and exit deadline commit together. Fault rollback restores the affected order reducer so replay persists the same fill once. No baseline/risk-state migration.
- Explicit manual-flat preparation reaches recovery despite old expected state, only read-only after flat/order-free REST/WS proof. Adoption still needs terminal owned intents and authenticated closing evidence; ordinary startup remains strict.
- Existing-owner refresh updates reward budgets, maker counts and timing for ranking. Identity/rule/expiry changes are blocked, not substituted. Earlier sports cutoffs also shorten exit service. New recurring owners remain separate work.

## Recovery boundary

Reviewed Polymarket's typed transient classification, capped backoff and recovery/exit-priority history as reference only; no Polymarket files/runtime changed. Gemini now handles transient **periodic account-audit and reward-metadata read** failures in process: hold entries/cancel risk-adding quotes, keep account WS and position-safety workers, retry at 5/10/20/40/60-second capped backoff through existing rate governance, and clear only a successfully proved component's hold. Final sends recheck the hold after pacing. No mutation retry is added.

Fresh, exactly classified account WS is required. Auth/validation errors, malformed successful replies, unknown orders, exit failure and storage failure remain fail-closed. Stream loss/stale account state invokes the bounded proof below, not unconditional permission to resume. Stop, permit expiry and original deadlines remain active. Supervisor recovery still requires durable flat proof and the same approved session/build/policy/account/restart limits.

Public/private reconnect is now implemented: at most three connection attempts per affected stream, 15-second attempt bounds with backoff, and 60 seconds overall. Transport reads are isolated from reducer/storage processing so a disk error cannot masquerade as reconnectable network failure. Concurrent EOF/ping faults share generation-aware recovery; old buffered depth cannot seed new books. Each connection relearns `conninfo` and subscriptions through the shared governor.

Entries are held at admission and final send. REST active-order evidence precedes cancellation because Gemini may already have cancelled on disconnect. Every cached working order from a private disconnect needs exact terminal identity, original quantity and already-recorded filled quantity. Missing fills are never fabricated from position/cash deltas. Fresh account snapshots must match expected cash/positions plus REST position brackets, and all owned intents must be terminal. Safe recovery preserves sticky pools, risk high-water and original deadlines. Private recovery defers exits until account proof; public recovery can service due exits when that book and private state are healthy. It cannot guarantee on-time exits during a venue outage.

This is not complete Polymarket recovery parity. Recurring-universe replacement remains in NEXT.md. No promise of survival of external termination or resumption after unknown execution. A Windows Modern Standby exit at 18:25 predates the 18:29 heartbeat; it is not causal proof for the disappearance.

## Logs and operator check

`scripts/check_runtime.ps1 -RuntimeDirectory data/production-20260915-nontrading-v8 -Logs` makes no API request or process action. It shows stale/missing status, process presence, recovery holds and latest log tails. Local telemetry is not venue proof.

- `logs/gemini-bot.jsonl`: queued JSON lines; UTC timestamp/process/thread, lifecycle/status, minute heartbeat, order identity/symbol/side/outcome/status, recovery and redacted failure trace. No balances, prices, raw frames or auth payloads are newly logged. Rotation: 20 MiB plus five backups; bounded 4096-record queue/drop counter.
- `supervisor.jsonl`: state transitions/minute heartbeat, process/session IDs and failure/restart classification. Rotation: 2 MiB plus three backups. Atomic supervisor status every ten seconds, TTL45 seconds; reader uses runner TTL90 seconds.
- Unknown Python failures persist non-reusable results. Unexpected supervisor exceptions publish attention when storage works. Neither can log after OS kill/power loss: stale heartbeat plus missing result means unclassified termination, never clean-flat exit.

## 50%-uptime and rollback impact

Normal fill-race fixes preserve other markets' quotes. Read-recovery holds can cancel entries and lose snapshots; safety is not bypassed for rewards. Healthy sticky pools are not rotated simply for a better rank. Corrected windows can reject formerly admitted candidates. If the corrected window end changes an old uptime row's key, old rows are retained but not credited to the new estimate: conservative undercredit, not fabricated eligibility or rewritten execution history.

Reporting requests cover today/yesterday together every five minutes, not per-market polling. Market WS and server-derived quotas remain primary. Healthy pending counts avoid redundant REST cap reads. Recovery uses existing governors; no extra socket or busy loop.

## Validation and open acceptance

Final source: **277 production and 28 script tests pass**, compilation, PowerShell parsing and whitespace checks pass. The 300-second read-only preflight finished at 20:04:45 ET: authenticated account flat at both endpoints, all 29 tracked books fresh in all 17 samples, two eligible candidates, both WS quotas verified, no gaps or dropped work/log records, zero order/cancel attempts. Read-only CPU averaged 0.38% of one core. It overlapped final recovery edits; final recovery branches are validated offline and require natural-run observation. The next approved launch must reconcile the final build again.

Offline cases cover denominator/cutoff/DST arithmetic, unknown sports clocks, missed time, immediate second-leg fills, transaction rollback/replay, read holds/resume/fatal errors, metadata refresh, manual-flat preparation, rotation/redaction/heartbeats and stale RUNNING detection. Added transport fault tests cover EOF, concurrent generations, private snapshot reset, attempt/proof bounds, no-replay, storage errors, stop/expiry, original deadlines, exact history, missing fills and cancel-on-disconnect without duplicate cancellation.

No restart or induced live mutation/outage. Stop cause, nonempty-summary calibration and natural recovery/exit timing remain unverified. Read-only preflight results are recorded in STATUS.md; they are point-in-time evidence, not continuing venue monitoring.

Sources rechecked: [liquidity rewards](https://developer.gemini.com/prediction-markets/liquidity-rewards-program), [daily summary](https://developer.gemini.com/rest-api/prediction-markets/rewards/get-liquidity-rewards-daily-summary).

Recovery source: [official WebSocket introduction](https://developer.gemini.com/prediction-markets/websocket/introduction), including `snapshot=-1` and `cancelOnDisconnect` behavior. These transport features do not supply missing execution/fee evidence.
