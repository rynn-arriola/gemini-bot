# Cancellation rotation recovery

## Incident

At approximately 14:00 Eastern on September 16, five public shard recoveries converged on shared entry cancellation while the authenticated private socket rotated. The transport raised `TransportChangedBeforeSend`, which proves the affected cancellation frame was not sent on the obsolete connection. The shared cancellation owner nevertheless latched that outcome as fatal, causing account-wide drain. Drain canceled the entry cohort but could not sell the known ZEC position because no executable buyer existed.

## Repair boundary

Commit `694f593` allows one retry only after a conclusively pre-send cancellation failure. It reconnects, obtains fresh authenticated active-order evidence, verifies the exact locally owned order identity and then reuses the single cancellation owner. Any attempted/ambiguous send, identity mismatch, missing order evidence, fill/account disagreement or incomplete execution history remains fail-closed. The known no-buyer position stays market-local; it never authorizes mutation replay or suppresses global integrity checks.

## Verification and follow-up

The repair passed 559 production and 28 script tests, compilation, all PowerShell parses, whitespace and offline Windows policy/storage validation. A regression runs real private recovery concurrently with two cancellation callers and proves one owner resumes without duplicate attempted cancellation; a disagreement control refuses retry. Source `694f593` was pushed, deployed and naturally ran without a supervisor restart before the later owner-authorized sticky-policy deployment. Natural recurrence remains the only approved live follow-up; no disconnect or fill should be induced.
