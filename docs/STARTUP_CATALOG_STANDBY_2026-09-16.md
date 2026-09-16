# Held-inventory catalog standby — September 16, 2026

## Observed launch

The owner started the newly persistent Live task at approximately 10:27 ET. Task Scheduler and the PowerShell supervisor remained alive with zero restarts, proving the launcher boundary worked. The runner stayed pre-activation and failed at 10:28:19 because its authenticated REST position was absent from that attempt's 20-symbol reward/subscription universe. The supervisor correctly refused to reactivate and entered attention. The v7 permit remained unconsumed and Gemini showed one known position and zero active orders.

A later public discovery returned 32 markets and included the same held ZEC symbol. The durable sticky identity also exactly named it. This identifies a transient catalog omission; it does not authorize assuming the market exists when current discovery cannot load its exact contract metadata.

## Repair boundary

`InventoryMarketUnavailable` is a typed `ReconciliationRequired` condition raised only before connections, permit consumption or activation when exact authenticated inventory is missing from the current subscribable universe. V7 uses the existing reconciliation standby:

- nonactivated status heartbeats and STOP responsiveness;
- at least 60 seconds between complete read-only checks;
- new read-only runtime and cancel-on-disconnect disabled on every check;
- complete fresh Live preparation and normal build/account/policy permit validation after a check passes.

The path does not synthesize contract rules, subscribe stale books, submit an exit, alter the position/deadline, credit missed uptime, or relax any ownership/accounting evidence. Legacy permits and all post-activation failures retain their previous behavior. Unknown/extra positions, malformed/auth failures, cash/integrity mismatch and ambiguous executions remain fatal or operator-blocked as before.

## Verification and reward impact

Offline regression runs a real preparation with exact inventory outside the loaded market and requires the typed blocker. A second regression proves v7 routes it through read-only standby and repeats the unchanged full Live attempt only after the check passes. Unknown errors remain nonretryable and typed blockers remain outside generic transport retry classification.

Full verification passed 553 production and 28 script tests, Python compilation, every PowerShell parse, whitespace checks and offline Windows policy/durable-storage validation. The change avoids a manual-restart gap when the catalog is briefly incomplete. It never credits the wait as a Gemini snapshot and cannot bypass normal 50% feasibility, profitability, market, stream, position, exit or risk gates.
