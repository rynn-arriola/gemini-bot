from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .config import ConfigurationError, load_live_config, load_policy, load_preflight_config
from .orchestrator import ProductionOrchestrator
from .storage import SQLiteJournal


async def _prepare(config_path: Path, directory: Path) -> None:
    config = load_preflight_config(config_path)
    runtime = ProductionOrchestrator(config, directory)
    try:
        state = await runtime.prepare()
        print(
            "Read-only preparation completed and disconnected; no orders were submitted. "
            f"Tracked={len(state.symbols)}, ranked={state.ranked_count}, eligible={state.eligible_count}."
        )
    finally:
        await runtime.close()


async def _run_live(config_path: Path, directory: Path, permit: Path) -> None:
    config = load_live_config(config_path)
    runtime = ProductionOrchestrator(config, directory)
    try:
        await runtime.prepare()
        await runtime.activate(permit)
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
    live = sub.add_parser("run-live", help="reconcile then activate with a one-use permit")
    live.add_argument("--env", type=Path, default=Path(".env"))
    live.add_argument("--directory", type=Path, default=Path("data/production"))
    live.add_argument("--activation-permit", type=Path, required=True)
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
    if args.command == "run-live":
        try:
            asyncio.run(_run_live(args.env, args.directory, args.activation_permit))
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            parser.error(str(exc))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
