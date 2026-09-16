# Continuous service and resting-time protection — September 15, 2026

## Deployment boundary

**Source only. Not activated.** The owner asked for prioritized implementation, then explicitly said not to restart the bot. No stop/restart, live fault injection, new permit, order mutation, or shared-journal migration is part of this release. Native Windows only; no host power/startup changes. Existing v6 remains active under its original bounded permit and midnight behavior.

A read-only local status check at 21:39:55 ET showed fresh quoting heartbeats, supervisor 19180, runner 34008 and restart count zero. This does not independently prove venue state or uninterrupted reward qualification. Changed source cannot reuse the old build-bound permit on a future re-launch; a separately authorized deployment must issue fresh authority.

## Priority 1: qualifying resting time

- Keep the existing midnight day/override transition in-process, persistent sticky pool/event/symbol identities, and flat-proof recurring-owner rollover.
- V7 can retain both existing quote legs when intact, unfilled, correctly sized, fresh, at least two ticks behind external touch and within the existing inclusive local 10-cent midpoint bound. New orders still use the exact two-tick target; the fast adverse-move guard is unchanged.
- Evaluate retained prices through the same conservative reward-share and execution-cost model. The minimum payout and positive-net gates still apply. More resting time can trade off against lower spread score; neither improvement in earnings nor exact exchange qualification is asserted.
- Per-run operational telemetry distinguishes observed qualifying seconds, unobserved seconds, current/longest gaps, retained-pair checks and replacements. It is bounded to current owners and separate from durable daily minute proxies and authenticated exchange summaries. Missed loop/restart time is not credited.

Gemini's [liquidity rewards documentation](https://developer.gemini.com/prediction-markets/liquidity-rewards-program), rechecked September 15, describes minute snapshots, resting-order scoring, a 50% threshold, minimum size and maximum distance. It does not make process uptime equivalent to qualification or guarantee our local estimates.

## Priority 2: continuous standby and recovery

- V7 requires an explicitly null session expiry; v2–v6 keep their original finite behavior. Build, account, policy, catalog scope, single mutation owner and supervisor identity remain mandatory. A one-day uptime exception cannot become indefinite.
- Disabled rewards pause entries/cancel affected working entries while exit safety remains active. Successful complete metadata refresh is required to resume. No eligible markets leaves a healthy standby service; empty/disabled startup catalogs use read-only supervised retry.
- Known transport outages use 15-second attempts and exponential backoff up to 60 seconds, with server cooldowns taking precedence. A public outage can quarantine/cancel owned entries while private truth is available, then wait without repeated REST account polling. No data freshness, exact quantity or original deadline is invented.
- Connected streams must prove account/order/cash/ownership state before entry holds clear. The 60-second proof budget remains for unresolved evidence after connectivity; explicitly typed transient REST outages may defer it. Unknown execution evidence is not synthesized or blindly retried.
- Supervisor startup-wait and proven-flat transient recovery are separate from fatal failures. Server retry deadlines persist across child processes. Repeated fast live failures stop auto-reactivation; normal hard-stop drains, unknown result, accounting/integrity or session mismatch stay online in attention. Logs/status remain bounded and fresh during backoff/reconciliation.

## Unchanged safety / limitations

30 pending entries, 15 active pools, 10-contract legs, all existing exposure filters, one $30 six-hour realized-loss stop and original five-minute/sports exit deadlines remain. A hard stop is not cleared by keeping the service alive. Unknown fills/accounting still require review. This is operational self-recovery, not live self-modifying code.

An exchange outage can prevent cancellations or exits. The service must wait for executable, authenticated truth and cannot promise zero downtime. Private cancel-on-disconnect may remove resting orders; safety takes priority. No supervisor/host-crash auto-launch or single-symbol stream-gap isolation is claimed. Entire shared-stream failure still affects its dependent markets. Sustained live performance and natural recovery remain unobserved for this build.

## Verification

Deterministic offline cases cover legacy/v7 permit boundaries, repeated same-session recovery, no active/closed/attention replay, account/build/policy binding, safe quote retention, unsafe/partial/stale/uneconomic rejection, disabled/empty rewards, long reconnects, unknown mutation refusal, original deadline preservation, quarantine polling bounds, local gap accounting, and native PowerShell supervision with every child launch mocked. No real trading child is launched by the supervisor harness.

Final verification passed: **349 production tests + 28 script tests = 377 tests**, including 47 new continuous-service cases. Python compilation, native PowerShell parsing, Windows policy/storage release validation and whitespace checks passed. Deployment and live acceptance remain separate, unperformed steps.

Commands: `.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q`, the equivalent discovery under `scripts`, `compileall -q src tests`, PowerShell AST parsing, and `python -m gemini_bot validate-release --directory data/validation-continuous-service-20260915`. All generated evidence/data stays ignored; no credential or active permit is committed.
