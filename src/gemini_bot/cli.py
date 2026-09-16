from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone

from .config import ConfigurationError, load_live_config, load_policy, load_preflight_config
from .orchestrator import ProductionOrchestrator
from .runtime_files import atomic_write_json, runtime_status
from .storage import SQLiteJournal
from .recovery_policy import transient_failure, retry_after_seconds, NoMarketsAvailable
from .reconciliation_errors import ReconciliationRequired


class LiveRunnerExit(RuntimeError):
    def __init__(self, code: int, message: str, *, retry_after: float = 0) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _write_live_runner_result(
    directory: Path, *, session_id: str | None, exit_code: int, outcome: str, retry_after: float = 0,
) -> None:
    """Persist a same-session result for the native Windows supervisor."""
    atomic_write_json(directory / "runner-result.json", {
        "session_id": session_id or "",
        "exit_code": exit_code,
        "outcome": outcome,
        **({"retry_not_before": (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat()}
           if retry_after > 0 else {}),
    })


async def _prepare(config_path: Path, directory: Path, *, result_token: str | None = None) -> None:
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
    if result_token is not None:
        atomic_write_json(directory / 'prepare-result.json', {
            'token': result_token, 'outcome': 'reconciled_read_only',
            'open_positions': len(state.account.positions),
            'pending_orders': len(state.account.active_orders),
        })


def _startup_stop_requested(directory: Path) -> bool:
    return any((directory / name).exists() for name in ('STOP', 'SUPERVISOR_STOP'))


async def _wait_reconciliation_delay(directory: Path, reason: str, delay: float) -> bool:
    """Keep a heartbeat and respond to STOP without claiming flatness."""
    deadline = asyncio.get_running_loop().time() + max(60.0, delay)
    next_heartbeat = 0.0
    while not _startup_stop_requested(directory):
        now = asyncio.get_running_loop().time()
        remaining = deadline - now
        if now >= next_heartbeat:
            atomic_write_json(directory / 'status.json', runtime_status(
                phase='waiting_reconciliation', activated=False, reason=reason,
                counters={'retry_in_seconds': max(0, int(remaining)), 'trading_enabled': 0},
            ))
            next_heartbeat = now + 5.0
        if remaining <= 0:
            return True
        await asyncio.sleep(min(1.0, remaining))
    atomic_write_json(directory / 'status.json', runtime_status(
        phase='stopped_before_activation', activated=False,
        reason='operator stop; account flatness is not asserted',
    ))
    return False


async def _wait_for_reconciliation(config_path: Path, directory: Path,
                                   blocker: ReconciliationRequired) -> bool:
    """Recheck evidence only: no adoption, mutation, permit or rounding."""
    reason, delay = type(blocker).__name__, 60.0
    while await _wait_reconciliation_delay(directory, reason, delay):
        runtime = ProductionOrchestrator(load_preflight_config(config_path), directory, read_only=True)
        try:
            await runtime.prepare()
            if _startup_stop_requested(directory):
                await _wait_reconciliation_delay(directory, reason, 0)
                return False
            return True
        except ReconciliationRequired as exc:
            reason, delay = type(exc).__name__, 60.0
        except Exception as exc:
            if not (transient_failure(exc) or isinstance(exc, NoMarketsAvailable)):
                raise
            reason = type(exc).__name__
            delay = max(60.0, retry_after_seconds(exc))
        finally:
            await runtime.close()
    return False


async def _run_live(config_path: Path, directory: Path, permit: Path, *,
                    ignore_uptime_proxy: bool, supervisor_session_id: str | None) -> None:
    # A v7 hint allows ONLY read-only waiting. Full permit validation still
    # occurs on every activation attempt, against fresh complete preparation.
    continuous_requested = False
    try:
        continuous_requested = json.loads(permit.read_text(encoding='utf-8')).get('permit_version') == 7
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    while not _startup_stop_requested(directory):
        try:
            await _run_live_once(config_path, directory, permit,
                                 ignore_uptime_proxy=ignore_uptime_proxy,
                                 supervisor_session_id=supervisor_session_id)
            return
        except ReconciliationRequired as exc:
            if not continuous_requested:
                raise
            if not await _wait_for_reconciliation(config_path, directory, exc):
                return
    atomic_write_json(directory / 'status.json', runtime_status(
        phase='stopped_before_activation', activated=False,
        reason='operator stop; account flatness is not asserted',
    ))


async def _run_live_once(
    config_path: Path,
    directory: Path,
    permit: Path,
    *,
    ignore_uptime_proxy: bool,
    supervisor_session_id: str | None,
) -> None:
    config = load_live_config(config_path)
    runtime = ProductionOrchestrator(config, directory)
    # This flag permits read-only startup retries only. Actual order authority
    # is still schema/build/policy/account/session validated by activate().
    continuous_requested = False
    try:
        continuous_requested = json.loads(permit.read_text(encoding="utf-8")).get("permit_version") == 7
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    try:
        try:
            await runtime.prepare()
            if _startup_stop_requested(directory):
                atomic_write_json(directory / 'status.json', runtime_status(
                    phase='stopped_before_activation', activated=False,
                    reason='operator stop; account flatness is not asserted',
                ))
                return
            await runtime.activate(
                permit,
                ignore_uptime_proxy=ignore_uptime_proxy,
                supervisor_session_id=supervisor_session_id,
            )
        except Exception as exc:
            if runtime._activation_consumed:
                if not runtime._continuous_service and not isinstance(
                    exc, (ConfigurationError, RuntimeError, ValueError, TimeoutError)
                ):
                    raise  # Preserve legacy unknown-exception no-restart behavior.
                phase = "unknown"
                try:
                    phase = str(json.loads(
                        (directory / "status.json").read_text(encoding="utf-8")
                    ).get("phase", "unknown"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pass
                raise LiveRunnerExit(
                    5 if phase == "stopped_flat" and (not runtime._continuous_service or runtime._recoverable_exit) else 3,
                    type(exc).__name__ + "; inspect redacted runtime logs",
                    retry_after=retry_after_seconds(exc),
                ) from exc
            if continuous_requested and (transient_failure(exc) or isinstance(exc, NoMarketsAvailable)):
                raise LiveRunnerExit(6, "read-only startup waiting for venue/catalog availability",
                                     retry_after=retry_after_seconds(exc)) from exc
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


async def _reconcile_executions(config_path: Path, directory: Path) -> None:
    runtime = ProductionOrchestrator(load_preflight_config(config_path),directory,read_only=True)
    try:
        try:
            await runtime.prepare()
        except RuntimeError as exc:
            if str(exc) != 'cash reconciliation requires matched account state':
                raise
        count = await runtime.reconcile_observed_external_reductions()
        print(f'Exact authenticated trade history reconciled, including actual fees; imported executions={count}. No orders submitted; activation remains paused.')
    finally:
        await runtime.close()


async def _record_cash_exception(config_path: Path) -> None:
    from .cash_exception import confirmed_usd, ledger_digest, record_approval
    from .storage import WindowsOwnerLock, shared_control_directory
    from .rate_limits import AdmissionGovernor
    from .rest_client import GeminiReadRestClient
    from .reconciliation import build_account_snapshot, assert_stable_account_bracket
    import hashlib

    config = load_preflight_config(config_path)
    control = shared_control_directory()
    path = control / 'production.sqlite'
    if not path.is_file():
        raise RuntimeError('cash exception requires the existing canonical journal')
    with WindowsOwnerLock(control / 'mutation-owner.lock'):
        journal = SQLiteJournal(path)
        try:
            digest = ledger_digest(journal)
            rest = GeminiReadRestClient(AdmissionGovernor(), api_key=config.api_key, api_secret=config.api_secret)
            before = build_account_snapshot(await rest.positions(), await rest.active_orders())
            cash_before = confirmed_usd(await rest.account_balances())
            after = build_account_snapshot(await rest.positions(), await rest.active_orders())
            cash_after = confirmed_usd(await rest.account_balances())
            assert_stable_account_bracket(before, after)
            if cash_before != cash_after or digest != ledger_digest(journal):
                raise RuntimeError('cash exception evidence changed while recording approval')
            record_approval(journal, identity=hashlib.sha256(config.api_key.encode()).hexdigest(),
                            observed_cash=cash_after, positions=after.signed_positions,
                            now=datetime.now(timezone.utc))
            print('One immutable incident approval recorded; cash is unchanged, exception not applied. '
                  f'Active orders={len(after.active_orders)}. No orders submitted or canceled.')
        finally:
            journal.close()


async def _apply_cash_exception(config_path: Path, directory: Path) -> None:
    from .cash_exception import apply_with_fresh_proof
    from .reconciliation_errors import UnclassifiedCash

    runtime = ProductionOrchestrator(load_preflight_config(config_path), directory, read_only=True)
    try:
        try:
            await runtime.prepare()
        except UnclassifiedCash:
            await apply_with_fresh_proof(runtime)
        else:
            raise RuntimeError('cash exception unnecessary; preparation already reconciled')
        print('One-time unexplained cash exception applied and consumed; fills, fees, positions, '
              'deadlines and loss history preserved. No orders or activation. Fresh preparation required.')
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
    prepare.add_argument('--result-token')
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
    executions = sub.add_parser('reconcile-executions',help='explicit exact execution-history repair; no trading')
    executions.add_argument('--env',type=Path,default=Path('.env'))
    executions.add_argument('--directory',type=Path,default=Path('data/production'))
    executions.add_argument('--acknowledgement',required=True,choices=('I_CONFIRM_EXECUTION_RECOVERY',))
    for name in ('record-cash-exception', 'apply-cash-exception'):
        exception = sub.add_parser(name, help='explicit September 16 one-time unexplained cash exception; no trading')
        exception.add_argument('--env', type=Path, default=Path('.env'))
        exception.add_argument('--acknowledgement', required=True,
                               choices=('I_ACCEPT_UNEXPLAINED_CASH_20260916_ONCE',))
        if name == 'apply-cash-exception':
            exception.add_argument('--directory', type=Path, default=Path('data/cash-exception-20260916'))
    live = sub.add_parser("run-live", help="reconcile then activate with an owner-approved permit")
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
            asyncio.run(_prepare(args.env, args.directory, result_token=args.result_token))
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "reconcile-manual-flat":
        try:
            asyncio.run(_reconcile_manual_flat(args.env, args.directory))
        except (ConfigurationError, RuntimeError, ValueError, TimeoutError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == 'reconcile-executions':
        try:
            asyncio.run(_reconcile_executions(args.env,args.directory))
        except (ConfigurationError,RuntimeError,ValueError,TimeoutError) as exc:
            parser.error(str(exc))
        return 0
    if args.command in {'record-cash-exception', 'apply-cash-exception'}:
        try:
            if args.command == 'record-cash-exception':
                asyncio.run(_record_cash_exception(args.env))
            else:
                asyncio.run(_apply_cash_exception(args.env, args.directory))
        except Exception as exc:
            # No raw authenticated payloads, balances, or parser values.
            print(f'Cash exception not completed: {type(exc).__name__}; no activation.', file=sys.stderr)
            return 2
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
                outcome={5: "recoverable_flat", 6: "startup_wait"}.get(exc.code, "failed"),
                retry_after=exc.retry_after,
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
