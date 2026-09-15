# Gemini Predictions liquidity-rewards bot

**Repaired offline; live remains unapproved.** All 15 review regressions now pass, with 106 production tests and 25 observer tests passing. See the [repair evidence and limits](docs/REPAIR_REPORT_2026-09-15.md) and [next commissioning steps](NEXT.md). No bot was started and no live permit exists.

This is a native-Windows live-trading build. The production runtime is implemented under `src/gemini_bot`, but no production preparation or maker canary has been run and no live activation permit exists. Construction, tests, release validation, and process startup cannot grant trading approval. The earlier public-data paper observer remains research evidence, not the execution engine.

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
- Six-hour Eastern loss windows that progressively reduce the pending-order cap as the approved loss budget is consumed.
- Pool-deduplicated reward ranking with explicit uncertainty around Gemini's private scoring curve.
- Durable SQLite order-intent and inventory-deadline journal plus a native Windows single-owner lock.
- Durable account snapshots, fill IDs, local reward-uptime samples and six-hour loss high-water marks across restart.
- A 900-second exact inventory exit, five-minute pre-expiry override, and exactly one monetary stop: $25 per six-hour Eastern window.
- Pool-deduplicated `rank-report.json`, atomic `status.json`, redacted rotating JSONL logs, and graceful `STOP` drain.
- Separate read-only preparation and live activation commands. Live activation requires the exact acknowledgement and a version-2 short-lived, one-use permit bound to account, Python build, policy and exact symbols, created only after owner approval.

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

The Windows launcher is `scripts\start_production.ps1`; `-Mode Prepare` remains non-ordering and `-Mode Live` requires the permit. `scripts\stop_production.ps1` writes a graceful drain request. Confirm `status.json` reaches `stopped_flat`; do not kill the process first.

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
