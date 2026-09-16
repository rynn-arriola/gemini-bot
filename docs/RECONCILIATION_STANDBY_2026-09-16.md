# Reconciliation standby and history comparison — September 16

## Evidence and remaining blocker

Fresh read-only Gemini checks still found one position and one external active order. The order is a same-outcome SELL whose remaining quantity does not exceed the held position. It is not bot-owned and was not changed. A fresh targeted trade-history replay found three executions, zero missing journal trade IDs and zero changed identities/quantities/prices/fees. History repair is already complete for those executions; repeating it cannot explain the remaining sub-cent cash discrepancy.

The position's average-price basis matches recorded notional, but the separately derived valuation basis does not exactly explain the cash difference. No valuation-based accounting adjustment was made. Gemini's documented [transaction-history endpoint](https://developer.gemini.com/trading/rest-api/fund-management/get-transaction-history) requires master account scope; the current key received HTTP 400 `InvalidApiKey` there. Other authenticated reads succeeded. No replacement credential or wider access was attempted. A transaction statement or authoritative ledger evidence is still needed; no rounding rule is established.

## Polymarket reference (source inspection only)

Read `C:/Users/rynna/Bots/liquidity-bot` without operating or modifying it:

- `rest_fallback.py::_fetch_execution_history` requests coverage with known execution IDs and bounded overlapping pages; `_rest_reconcile` applies history then requires fresh current account proof.
- `cash_activity.py::fetch_nontrading_cash_changes` independently classifies completed non-trading cash and requires explicit pagination completion. Missing/unknown activity is not silently ignored.
- `live_commissioning.py::_assert_runtime_scope` explicitly rejects unowned working orders. It does not provide authority to adopt the Gemini external SELL.

These patterns support exact history plus current account proof, not matching a balance by tolerance. No assertion is made about the current Polymarket process or account.

## Implemented startup-only availability repair

The v7 CLI now catches narrow typed evidence blockers before activation and remains alive with a five-second `waiting_reconciliation` heartbeat. It waits at least a minute, then performs a read-only preparation with cancel-on-disconnect disabled. Each attempt closes its streams/journal/owner lock. Longer explicit cooldowns are retained. STOP is checked every second during the delay and again after preparation before activation.

A successful read-only check starts a new full production preparation followed by unchanged build/account/policy/session permit validation. Neither waiting nor a successful probe consumes a permit. Auth failures, malformed history, unknown software errors and post-activation failures retain their original fail-closed behavior. Legacy permits do not gain this wait path. `prepare-live` remains a bounded one-shot check.

The service can therefore be started into safe standby after normal release/authority checks, but this does **not** mean the current account is trading-ready or that missing evidence will fix itself. External reductions still require the existing explicit reconciliation path. This release does not cancel, exit, start trading or issue authority.

## Verification and impact

- Fifteen focused offline cases cover success, repeated blockers, read-only construction, cooldown, cancellation/cleanup, legacy behavior, STOP races, malformed evidence, post-activation isolation and unchanged sub-cent cash.
- 522 production and 28 script tests passed, plus compilation and offline native-Windows release validation. An in-memory reversal enabling mutations on retry constructors was caught by the read-only test; original source was unchanged/restored. Final review covered startup/STOP races, legacy authority, post-activation errors, malformed evidence, cooldowns, cleanup and absence of accounting/ownership bypass.
- Authenticated read-only preparation produced the typed external-order hold, with zero new intents and no activation consumed.
- 50% impact: no strategy, selection, sticky ownership or denominator change. Standby earns no resting-time credit. This prevents repeated process-start failure, not the current trading gap; exact safety proof still takes precedence over rewards.
