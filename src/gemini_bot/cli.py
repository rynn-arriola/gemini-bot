from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from .config import ConfigurationError, load_live_config, load_policy, load_preflight_config
from .orchestrator import ProductionOrchestrator
from .runtime_files import atomic_write_json
from .storage import SQLiteJournal


class LiveRunnerExit(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _write_live_runner_result(
    directory: Path, *, session_id: str | None, exit_code: int, outcome: str,
) -> None:
    """Persist a same-session result for the native Windows supervisor."""
    atomic_write_json(directory / "runner-result.json", {
        "session_id": session_id or "",
        "exit_code": exit_code,
        "outcome": outcome,
    })


async def _prepare(config_path: Path, directory: Path) -> None:
    config = load_preflight_config(config_path)
    runtime = ProductionOrchestrator(config, directory, read_only=True)
    try:
        state = await runtime.prepare()
        print(
            "Read-only preparation completed and disconnected; no orders were submitted. "
            f"Tracked={len(state.symbols)}, ranked={state.ranked_count}, eligible={state.eligible_count}."
        )
    finally:
        await runtime.close()


async def _run_live(
    config_path: Path,
    directory: Path,
    permit: Path,
    *,
    ignore_uptime_proxy: bool,
    supervisor_session_id: str | None,
) -> None:
    config = load_live_config(config_path)
    runtime = ProductionOrchestrator(config, directory)
    try:
        try:
            await runtime.prepare()
            await runtime.activate(
                permit,
                ignore_uptime_proxy=ignore_uptime_proxy,
                supervisor_session_id=supervisor_session_id,
            )
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            if runtime._activation_consumed:
                phase = "unknown"
                try:
                    phase = str(json.loads(
                        (directory / "status.json").read_text(encoding="utf-8")
                    ).get("phase", "unknown"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pass
                raise LiveRunnerExit(
                    5 if phase == "stopped_flat" else 3, str(exc),
                ) from exc
            raise
    finally:
        await runtime.close()


async def _reconcile_manual_flat(config_path: Path, directory: Path) -> None:
    config = load_preflight_config(config_path)
    runtime = ProductionOrchestrator(config, directory, read_only=True)
    try:
        await runtime.prepare(manual_flat_repair=True)
        imported = await runtime.reconcile_verified_manual_flat()
        print(
            "Authenticated Gemini state is flat and order-free; the manual close was "
            "adopted without resetting the six-hour risk window. "
            f"Imported authenticated close orders={imported}. No orders were submitted."
        )
    finally:
        await runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini Predictions live bot operator utility")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-config", help="validate live configuration without connecting")
    validate.add_argument("--env", type=Path, default=Path(".env"))
    release = sub.add_parser("validate-release", help="run offline platform/policy/storage checks")
    release.add_argument("--directory", type=Path, default=Path("data/production-validation"))
    prepare = sub.add_parser("prepare-live", help="read-only reconcile/rank, then disconnect")
    prepare.add_argument("--env", type=Path, default=Path(".env"))
    prepare.add_argument("--directory", type=Path, default=Path("data/production"))
    manual_flat = sub.add_parser(
        "reconcile-manual-flat",
        help="adopt a verified manual flatten without submitting orders",
    )
    manual_flat.add_argument("--env", type=Path, default=Path(".env"))
    manual_flat.add_argument("--directory", type=Path, default=Path("data/production"))
    manual_flat.add_argument(
        "--acknowledgement", required=True,
        choices=("I_CONFIRM_MANUAL_FLAT",),
        help="explicit operator acknowledgement required",
    )
    live = sub.add_parser("run-live", help="reconcile then activate with a bounded permit")
    live.add_argument("--env", type=Path, default=Path(".env"))
    live.add_argument("--directory", type=Path, default=Path("data/production"))
    live.add_argument("--activation-permit", type=Path, required=True)
    live.add_argument("--supervisor-session-id")
    live.add_argument(
        "--ignore-uptime-proxy", action="store_true",
        help="requires a permit-bound expiry; does not claim Gemini reward eligibility",
    )
    args = parser.parse_args()
    if args.command == "validate-config":
        try:
            config = load_live_config(args.env)
        except ConfigurationError as exc:
            parser.error(str(exc))
        print(
            "Live configuration is structurally valid; startup remains paused. "
            f"Base quantity={config.strategy.quote_quantity}, "
            f"pending-order cap={config.risk.max_pending_orders}, "
            f"gross cap=${config.risk.max_gross_reservation_usd}, "
            f"market cap=${config.risk.max_market_exposure_usd}."
        )
        return 0
    if args.command == "validate-release":
        if os.name != "nt":
            parser.error("production is supported only on native Windows")
        load_policy()
        journal = SQLiteJournal(args.directory / "validation.sqlite")
        try:
            if journal.quick_check() != "ok":
                parser.error("SQLite quick-check failed")
        finally:
            journal.close()
        print("Offline Windows policy and durable-storage checks passed; no network or orders used.")
        return 0
    if args.command == "prepare-live":
        try:
            asyncio.run(_prepare(args.env, args.directory))
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "reconcile-manual-flat":
        try:
            asyncio.run(_reconcile_manual_flat(args.env, args.directory))
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "run-live":
        try:
            asyncio.run(_run_live(
                args.env, args.directory, args.activation_permit,
                ignore_uptime_proxy=args.ignore_uptime_proxy,
                supervisor_session_id=args.supervisor_session_id,
            ))
        except LiveRunnerExit as exc:
            _write_live_runner_result(
                args.directory, session_id=args.supervisor_session_id,
                exit_code=exc.code,
                outcome="recoverable_flat" if exc.code == 5 else "failed",
            )
            print(f"live runner stopped: {exc}", file=sys.stderr)
            return exc.code
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            _write_live_runner_result(
                args.directory, session_id=args.supervisor_session_id,
                exit_code=2, outcome="startup_or_unclassified_failure",
            )
            parser.error(str(exc))
        except Exception as exc:
            # Unknown failures must never authorize a supervised live restart.
            # No raw exception payload (potential HTTP/auth data) in stderr.
            _write_live_runner_result(
                args.directory, session_id=args.supervisor_session_id,
                exit_code=3, outcome="startup_or_unclassified_failure",
            )
            print(f"unclassified live runner failure: {type(exc).__name__}; inspect redacted logs", file=sys.stderr)
            return 3
        _write_live_runner_result(
            args.directory, session_id=args.supervisor_session_id,
            exit_code=0, outcome="closed",
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
