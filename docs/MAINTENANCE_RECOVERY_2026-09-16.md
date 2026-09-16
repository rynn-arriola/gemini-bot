# Maintenance and execution recovery

Owner authorized repairs, restart, and exact force exit on September 16.

At 02:02:43 ET both streams received close 1012, service restart. The private
reconnect encountered an HTTP EOF wrapped by websockets as `InvalidMessage`.
The retry classifier omitted this wrapper; the runner halted at 02:02:49.
Emergency cancellation then used the disconnected transport and failed before
send. The supervisor remained in attention. Its read-only preparation actually
succeeded with 28 orders, but the PowerShell child ExitCode observation produced
a misleading failure reason. Fresh later reads found 27 orders and two positions.

Repairs:

- Retry only incomplete handshakes with a typed EOF/connection/timeout cause;
  preserve paced attempts, cooldowns, STOP and malformed/authentication stops.
- Restore drain transports inside the existing 90-second overall budget,
  reconstruct subscriptions and account proof, then cancel/reduce. Never use
  reconnect as permission to resend an unknown mutation.
- Recover exact executions using authenticated trade IDs and fees joined to
  terminal order history. Persist all changes atomically; deduplicate late
  messages and retain original deadlines. A partial terminal update without
  trade identity enters reconciliation rather than killing the account pump.
- Explicit operator execution reconciliation may repair a proven external
  reduction, including while another known position remains. It does not reset
  cash, positions or the loss meter. Automatic recovery imports owned orders only.
- Bind preparation results to an attempt token so the supervisor reports the
  actual read-only result without treating it as restart authority.
- Verify the no-buyer flow: keep an overdue position entry-blocked, wait with
  bounded backoff, re-prove residual/depth, and exit exactly when bids return.
  A missing bid never proves flat and cannot be solved by repeating sell requests.

The incident's 27 surviving entry orders were canceled and authenticated REST
confirmed zero active orders. A manual XRP close and the fractional ZEC fill
were identified from exact authenticated trade history. A temporary forced-exit
helper observed no NO-side bids and sent no exit; it was stopped with zero
unresolved exit intents for controlled handoff. Current deployment/reconciliation
status belongs in STATUS.md, not this historical narrative.

Offline coverage includes simultaneous stream failures, repeated handshake EOF,
unknown-send preservation, emergency reconnect budgets, missed partial fills,
exact fees, duplicate/late events, external reduction versus reversal, partial
external-close reconciliation without a baseline reset, native-supervisor result
binding, and no bids followed by executable liquidity in the same runner.

Final offline verification: 479 production tests plus 28 script tests passed;
compilation and native Windows offline release checks passed. A fractional-fill
regression explicitly proves that cent-rounded cash cannot bypass classification.
At 07:17 ET exact recovered positions matched, but cash retained an unexplained
sub-cent difference. No rounding correction or baseline reset was applied. The
07:23:01 ET authenticated read found one position and one active order; a fresh
cash-out-history query supplied no corroborating cash-out rows. No restart was
performed. Source verification is not live acceptance.

50% impact: all safety cancellation/reconciliation gaps remain uncredited.
Recovery preserves sticky identity, prior uptime, inventory deadlines and losses;
it does not renew an override or relax the two-tick, size, breadth or economic
filters. Necessary adverse-move cancellations remain. Offline tests do not prove
future venue availability, reward qualification or survival of host termination.
