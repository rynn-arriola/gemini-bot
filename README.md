# Gemini Predictions liquidity-rewards bot

**The realized-score reward bot is live under the continuous-service v41 release.** Its persistent on-demand Windows launcher, fresh authenticated preparation and exact-build permit are verified. Current venue/process truth and the launch route are in [STATUS.md](STATUS.md). The bot is configured for up to 20 rewarded owners and 40 pending orders, subject to the unchanged $300 gross, $90 per-market and $30/six-hour Eastern realized-loss HARD plus five-minute inventory and pregame controls.

This is a native-Windows live-trading build. The production runtime is implemented under `src/gemini_bot`. WebSocket is primary and REST is bounded discovery/reconciliation fallback. Reward timing is tracked per event inside Eastern payout days while shared pools remain one budget; the unpublished reward-start boundary remains explicitly non-certified. One- and two-leg maker canaries passed, and a clean zero-fill maker-or-cancel race is now handled locally only after authenticated flat reconciliation. Approval remains build/policy/account/session bound; v7 continuous recovery retains the same-supervisor, exact-reconciliation and no-mutation-replay boundaries. The earlier public-data paper observer remains research evidence, not the execution engine.

- [AI operator entry point](AI_OPERATOR.md)
- [Current status](STATUS.md)
- [Owner decisions](DECISIONS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Safety invariants](docs/SAFETY_INVARIANTS.md)
- [Live commissioning](docs/COMMISSIONING.md)
- [Findings and prior research](FINDINGS.md)
- [API reference](GEMINI_PREDICTIONS_API_REFERENCE.md)
- [API limits and observer traffic](docs/API_LIMITS.md)
- [Rewards research](docs/REWARDS_STRATEGY_REVIEW_2026-09-14.md)
- [Overnight experiment design](docs/OVERNIGHT_TEST.md)

## Production foundation

- WebSocket-first local differential books with gap detection and bounded dirty-symbol work.
- Authenticated WebSocket headers, account subscriptions, real `LIMIT` + `MOC` maker order and cancellation envelopes.
- One shared priority rate governor; REST remains bounded metadata/reconciliation transport.
- Ten contracts per outcome leg, maximum 30 pending orders, $300 gross exposure, $90 per-market exposure, and separate position controls.
- Six-hour Eastern realized-loss windows that keep the pending-order ceiling fixed at 30 below HARD and reduce it to zero at the approved $30 stop.
- Polymarket-style accounting separation: authenticated deposits, withdrawals, liquidity rewards, and maker rebates reconcile cash but never enter fill-only realized trading P&L.
- Pool-deduplicated profitability ranking v2: conservative reward share less fee/rebate, projected fill, adverse-move and exit-depth costs per capital-hour, with sticky cohorts and explicit uncertainty around Gemini's private scoring curve. See [the v2 model](docs/PROFITABILITY_RANKING_V2.md).
- Durable SQLite order-intent and inventory-deadline journal plus a native Windows single-owner lock.
- Durable account snapshots, fill IDs, local reward-uptime samples and six-hour loss high-water marks across restart.
- A 300-second exact inventory exit, five-minute pre-expiry override, and exactly one monetary stop: $30 realized loss per six-hour Eastern window.
- Pool-deduplicated `rank-report.json`, atomic `status.json`, redacted rotating JSONL logs, and graceful `STOP` drain.
- Separate read-only preparation and live activation commands. Live activation requires the exact acknowledgement and a short-lived permit bound to account, Python build and policy; version 4 may authorize capped dynamic reward-catalog scope, while version 5 may additionally authorize at most two same-supervisor recovery restarts after durable flat proof.

Install the package into the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Validate production configuration without connecting or trading:

```powershell
.\.venv\Scripts\python.exe -m gemini_bot validate-config --env .env
```

Risk and strategy values come only from the reviewed `config/live_policy.json`. Validation intentionally fails until the separate live acknowledgement is present. A valid configuration still starts paused.

Offline release validation (no Gemini connection):

```powershell
.\.venv\Scripts\python.exe -m gemini_bot validate-release --directory data\production-validation
```

The following commands are documented for a later approved commissioning session. **Do not run them merely because they exist.** `prepare-live` connects and reconciles but cannot submit orders. `run-live` can submit real orders only after preparation and only with an unexpired, unconsumed approval permit matching the exact policy file.

```powershell
# Later, after explicit approval to connect:
.\.venv\Scripts\python.exe -m gemini_bot prepare-live --env .env --directory data\production

# Later, after the separate first-live approval and permit creation:
.\.venv\Scripts\python.exe -m gemini_bot run-live --env .env --directory data\production `
  --activation-permit data\production\activation-permit.json
```

The local 50% reachability estimate is reporting-only because Gemini's exact
snapshot phase/denominator is unavailable. `--ignore-uptime-proxy` remains only
for legacy/bounded permit compatibility and is not required by the current
continuous sticky-market policy. `universe_scope=reward_catalog` allows capped
replacement of a concretely unusable incumbent from the reconciled reward
catalog. None of these settings asserts that Gemini's 50% threshold was met.

The Windows launcher is `scripts\start_production.ps1`; `-Mode Prepare` remains non-ordering and `-Mode Live` requires the permit. Live mode registers and starts one project-scoped, on-demand Scheduled Task with no boot/logon trigger and unlimited execution time; Task Scheduler then owns `scripts\supervise_production.ps1` instead of the calling terminal. The launcher refuses an existing running task/supervisor and requires a fresh process-backed supervisor heartbeat before reporting success. A version-5 bounded session may restart its runner at most twice, with backoff, only after the runner durably proves flat; otherwise the supervisor performs one read-only reconciliation and enters `ATTENTION_REQUIRED`. `scripts\stop_production.ps1` disables supervisor recovery and writes a graceful drain request. Confirm `status.json` reaches `stopped_flat`; do not kill the process first.

Version 6 keeps those supervision bounds and the 24-hour permit maximum. At midnight the runner refreshes day-specific reporting without clearing healthy sticky identities; session expiry still drains. Persisted market identities and flat-only recurring rollover preserve continuity. See [overnight continuity](docs/OVERNIGHT_CONTINUITY_2026-09-15.md).

After the owner manually closes all bot inventory, adopt the new baseline only with fresh authenticated proof:

```powershell
.\.venv\Scripts\python.exe -m gemini_bot reconcile-manual-flat --env .env `
  --directory data\manual-flat-reconcile --acknowledgement I_CONFIRM_MANUAL_FLAT
```

This command is venue-read-only. It refuses any position, active order, nonterminal bot intent, stale account snapshot, or unknown order ownership, and it never resets the six-hour loss window.

## Check status and logs

Local status/log check, without API calls or restart:

```powershell
.\scripts\check_runtime.ps1 -RuntimeDirectory data\production-20260915-nontrading-v8 -Logs
```

Use the applicable run directory. Stale RUNNING files and recovery holds are identified; the reader tails rotated runner/supervisor logs but cannot prove venue flatness. [Logging/recovery boundaries](docs/RUNTIME_HARDENING_2026-09-15.md).

## Run a bounded observation

Use the isolated `.venv` with `requirements.txt`. Example paths are relative to this folder:

```powershell
.\.venv\Scripts\python.exe scripts\overnight_observer.py --until 2026-09-15T13:00:00Z --directory data\new-observation-run --markets 50
```

The directory must be under `data/` and new. A file lock rejects a concurrent writer; an existing run is preserved. The stop timestamp must be within the next 24 hours. A `STOP` file in that run directory requests an early stop; allow one sample cycle for it to be observed.

Generate a report from an existing run without contacting Gemini:

```powershell
.\.venv\Scripts\python.exe scripts\overnight_observer.py --report --directory data\overnight-hardened-20260914
```

All tests:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p 'test_*.py' -v
```

Credentials, local environments and all runtime data remain Git-ignored. Never put credentials in documentation, command output or reports.
