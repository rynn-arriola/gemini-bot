# Sticky ownership recovery and 40-order release

## Defect and correction

Read-only comparison from 19:38 to 20:49 ET September 16 found six of 15 market identities changed. The 20:27 cluster included Rick Jackson's one-sided external book and Chris Gallant's inside-two-tick cancellation. Other removed markets lacked saved selection reasons. Offline reproduction confirms one missing rank cycle releases a slot in the old selector, even if the original candidate recovers immediately. This is not proof of Gemini qualification or lost payout.

The new production wrapper reserves current incumbent identities while failed quotes remain canceled. Ten minutes of continuously observed failure can make a flat owner replaceable; it does not itself authorize release. Fresh authenticated terminal/flat proof and fresh ranking are mandatory. Recovery keeps the same market. A restart or unknown observation interval resets the grace; it cannot earn failure time or reward time. Inventory, unresolved exits and recovering shards remain protected. One account-wide release-proof attempt per minute limits read load. Hold, recovery, release and admission reasons are durable and logged. Existing safe lifecycle retirement remains separate.

## Owner-approved capacity

40 pending orders, at most 20 active/held two-sided market owners and 32 watched markets. Keep 20 contracts per leg, two ticks, five public sockets plus one private socket, one shared rate/mutation authority, $300 gross/$90 per-market/$40 stressed-entry admission, five-minute exits, sports cutoff and the single $30 six-hour realized-loss HARD. Actual working count may be lower because of economics, depth, exposure or recovery reservations. A held position is not an order but retains exposure and an owner.

Normal persisted risk updates never increase a prior cap. The isolated operator migration requires a nonactivated owner lock, fresh stable flat/order-free REST/WS proof and a complete non-HARD current 30-slot row; it atomically changes only that cap to 40 and audits once. It cannot forgive HARD or rewrite execution, loss, cash or deadline history. There is no startup invocation.

## Verification and deployment

638 production tests plus 28 script tests passed against the isolated source, along with compilation, every PowerShell parse, whitespace checks and offline Windows policy/storage validation. Tests explicitly import this worktree's source because the shared virtual environment's editable install points at the active checkout. Initial tests accidentally imported old source; those results are excluded from release evidence. New regressions cover one-cycle disappearance/recovery, bad prices/economics, shared-pool marginal failure, grace timing/restarts, delayed proof recovery, failed/unknown proof, protected owners, persistence, 20 owners, actual 40th-order transport admission, 41st-order prevention, permit width and irreversible HARD. Offline acceptance is not live acceptance.

Deployment preserves the cohort current at the drain, not an abandoned earlier cohort. Backup canonical SQLite after flat stop; fast-forward only verified published source; prepare against the canonical journal and preserve histories; apply only the explicitly approved cap migration; create fresh exact-build/account/policy v7 authority and start the persistent Windows task. The gap earns no resting-time credit. Long-run recovery, reward qualification and payout profitability remain unproven; automated storage backup is separate pending work.
