# Next work

1. Owner authorized overnight continuity on September 15. Version-6 midnight handoff is under release verification/deployment; see STATUS.md for actual runtime. Never reuse an old build-bound permit. The override ends at midnight; the session remains bounded to at most 24 hours.
2. Same-process public/private reconnect is implemented and offline-tested; observe a natural recovery during an approved run. Do not deliberately induce a live outage/fill. Proof is bounded to 60 seconds/three reconnect attempts per affected stream; unknown executions still require operator reconciliation. Do not claim complete Polymarket recovery parity.
3. Verify natural v6 recurring-owner rollover after deployment. Bounded discovery retains healthy identities and replaces only expired/inactive flat owners; no live fault/fill injection is authorized. Missing candidates/snapshots can leave slots empty safely.
4. Calibrate local windows against a **nonempty** authenticated daily summary. Live freshness, scored-through time, exact reward activation and shared-pool uptime aggregation remain unconfirmed. No inferred 5pm reset or maintenance subtraction.
5. At a future approved run, verify logs/heartbeat, same-symbol fill handling, exact timed exits and natural transient-read recovery. Synthetic tests are not live acceptance. Do not induce faults/fills without separate approval.

The 18:29 ET disappearance remains unexplained: both processes absent, empty old logs, no runner result. New observability does not repair an unproven external cause. [Repair evidence](docs/RUNTIME_HARDENING_2026-09-15.md).
