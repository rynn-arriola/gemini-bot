from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from websockets.exceptions import ConnectionClosed

from .account_runtime import AccountRuntime, MissingExecutionEvidence
from .activation import (
    consume_activation_permit,
    finish_supervised_activation,
    load_activation_permit,
    policy_digest,
)
from .config import DEFAULT_POLICY_PATH, LiveConfig
from .continuity import RestingObservation
from .queue_shadow import QueueShadow, ShadowArchive
from .queue_pilot import QueuePilot
from .adaptive_queue import AdaptivePolicy, AdaptiveQueuePilot
from .journal_backup import JournalBackups, verify_journal
from .sticky_recovery import StickyRecovery, RECOVERY_GRACE_SECONDS
from .capacity import MAX_ACTIVE_MARKETS
from .recovery_policy import transient_failure, retry_after_seconds, NoMarketsAvailable
from .reconciliation_errors import ExternalOrderPresent, InventoryMarketUnavailable
from .discovery import RewardUniverseLoader, DISCOVERY_RANKING_VERSION, read_reward_event
from .catalog import join_reward_contracts
from .domain import EngineState, Outcome, QuoteLeg, QuotePair, Side, canonical_symbol, uptime_progress_key
from .engine import LiveEngineGate
from .execution import IntentLedger, IntentState, OrderPurpose, validate_exact_exit
from .exit_pricing import exact_crossing_exit
from .entry_quality import evaluate_entry_quality
from .exit_recovery import ExitDeferred, ExitWait
from .live_ws import (
    EntryStateChangedBeforeSend,
    LiveWebSocket,
    MutationNotSentError,
    QuoteChangedBeforeSend,
    TransportChangedBeforeSend,
    AdmissionTimeoutBeforeSend,
)
from .operational_logging import ErrorClass, OperationalLogger, configure_logging
from .persistent_risk import PersistentRiskWindow
from .profitability import (
    ProfitabilityTracker,
    active_maker_rebate_fractions,
    conservative_taker_fee,
)
from .public_ws import PublicMarketSocket
from .rank_report import public_rank_rows, rank_live_universe, portfolio_reward_report
from .rate_limits import AdmissionGovernor
from .reward_ranking import select_sticky_markets, resting_quote_safe, marginal_reward_proxy, pool_reward_proxy
from .domain import market_owner_key
from .reconciliation import (
    AccountSnapshot,
    assert_position_consistency,
    assert_stable_account_bracket,
    build_account_snapshot,
    exposure_from_snapshot,
)
from .rest_client import ApiError, GeminiReadRestClient, RewardCatalogUnstable
from .risk_gate import admit_quote_pair
from .risk_gate import ExposureState
from .risk_windows import RiskWindowPolicy
from .runtime_files import atomic_write_json, runtime_status
from .storage import (
    AuthenticatedCashAdjustment,
    AuthenticatedSettlement,
    SQLiteJournal,
    WindowsOwnerLock,
    shared_control_directory,
)
from .uptime import sample_and_load_progress, eastern_day_window, locally_qualifying
from .reward_uptime_report import ExchangeRewardUptime, REFRESH_SECONDS, event_identity
from .qualification_estimator import estimate_50_percent, order_for_resting_focus
from .reward_focus import RewardFocus
from .market_state import LocalBook
from .stream_recovery import RecoveryPending, terminal_history_status
from .ws_requests import WebSocketRequestRejected
from .trade_recovery import import_terminal_trades, timestamp as history_timestamp
from .public_shards import PublicMarketShards, ShardEvent, PUBLIC_SHARD_COUNT


_UPTIME_PROXY_REJECTION = "50% uptime is no longer reachable in the estimated window"
_STREAM_RECOVERY_SECONDS = 60
_EXIT_WORKER_SECONDS = 45
_DRAIN_SECONDS = 90
_DRAIN_JOIN_SECONDS = 50
_CANCEL_CONFIRM_SECONDS = 45
_CORRELATED_CANCEL_MAX_SECONDS = 180
_CANCEL_READ_CACHE_SECONDS = .5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("contract expiry must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PreparedState:
    account: AccountSnapshot
    ranked_count: int
    eligible_count: int
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MakerPairGuard:
    first_client_id: str
    first_server_id: str
    first_leg: QuoteLeg
    second_leg: QuoteLeg
    public_generation: int | tuple[int, int]
    private_generation: int


class ProductionOrchestrator:
    """Single Windows process that owns state, rate admission, and mutations.

    Construction is inert. ``prepare`` performs reads/subscriptions only. The
    mutation path cannot be entered without a valid one-use activation permit.
    """

    def __init__(self, config: LiveConfig, directory: Path, *, read_only: bool = False,
                 public_shard_count: int = PUBLIC_SHARD_COUNT) -> None:
        self.config = config
        self.read_only = read_only
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = SQLiteJournal(self.directory / "production.sqlite")
        self.control_directory = shared_control_directory()
        self._journal_backups: JournalBackups | None = None
        self.lock = WindowsOwnerLock(self.control_directory / "mutation-owner.lock")
        self._lifecycle_lock = asyncio.Lock()
        self._exit_uncertain: set[str] = set()
        self._exit_rejected: set[str] = set()
        self._exit_residual_limits: dict[str, Decimal] = {}
        self._exit_round_deadlines: dict[str, float] = {}
        self._position_proof_wait_started: dict[str, float] = {}
        self._exit_workers: dict[str, asyncio.Task[None]] = {}
        self._exit_waits: dict[str, ExitWait] = {}
        self.governor = AdmissionGovernor()
        self.ledger = IntentLedger(self.journal)
        self.rest = GeminiReadRestClient(
            self.governor, api_key=config.api_key, api_secret=config.api_secret
        )
        self.public = (PublicMarketSocket(self.governor) if public_shard_count == 1
                       else PublicMarketShards(self.governor, count=public_shard_count))
        self.private = LiveWebSocket(
            config.api_key, config.api_secret, self.governor, self.ledger,
            read_only=read_only, cancel_on_disconnect=not read_only,
        )
        self.account = AccountRuntime(self.ledger, self.journal)
        self.account.inventory_age_seconds = config.risk.max_inventory_age_seconds
        self.account.exit_before_expiry_seconds = config.risk.force_exit_before_expiry_seconds
        self.gate = LiveEngineGate()
        self.private.mutation_guard = self._validate_send
        self.private.cancel_is_terminal = lambda order_id: (
            order_id in self.account.reducer.orders and not self.account.reducer.orders[order_id].working
        )
        self.universe = []
        self.ranked = []
        self.profitability = ProfitabilityTracker()
        self._maker_rebate_fractions: dict[str, Decimal] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._activated_pool_ids: set[str] = set()
        self._sticky_pool_ids: set[str] = set()
        self._sticky_markets: dict[str, tuple[str, str]] = {}
        self._sticky_recovery = StickyRecovery()
        self._sticky_recovery_saved_at = 0.0
        self._next_owner_release_check = 0.0
        # Exact, previously owned inventory can outlive a transient omission
        # from reward discovery. These symbols remain entry-blocked and count
        # toward exposure/pool ownership, but do not make healthy markets wait.
        self._inventory_catalog_holds: set[str] = set()
        self._expiry: dict[str, datetime] = {}
        self._trade_start: dict[str, datetime] = {}
        self._sports_start: dict[str, datetime] = {}
        self._market_status_versions: dict[str, int] = {}
        self._market_status: dict[str, str] = {}
        self._dynamic_pending_cap = config.risk.max_pending_orders
        self._last_snapshot = AccountSnapshot((), ())
        self._uptime = {}
        self._permit_expires_at: datetime | None = None
        self._permitted_symbols: set[str] = set()
        self._last_live_mark: tuple[object, ...] | None = None
        self._permit_policy_digest: str | None = None
        self._activation_consumed = False
        self._maker_ack_count = 0
        self._commissioning_ignore_uptime_proxy = False
        self._uptime_override_until: datetime | None = None
        self._continue_after_override = False
        self._continuous_service = False
        self._reward_program_paused = False
        self._reward_refresh_requested = False
        self._resting_observations: dict[str, RestingObservation] = {}
        self._two_sided_resting_observations: dict[str, RestingObservation] = {}
        self._retained_pairs = 0
        self._replaced_pairs = 0
        self._quote_cancel_reasons: Counter[str] = Counter()
        self._retention_reason = "not_evaluated"
        self._recoverable_exit = False
        self._private_request_failure: WebSocketRequestRejected | None = None
        self._reward_epoch: tuple[str, bool] | None = None
        self._catalog_scope = False
        self._commissioning_expected_yes_price: Decimal | None = None
        self._commissioning_expected_no_price: Decimal | None = None
        self._quote_cooldown_until: dict[str, float] = {}
        self._cancel_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_active_read: tuple[float, asyncio.Task[list[dict[str, Any]]]] | None = None
        self._cancel_history_reads: dict[
            str, tuple[float, asyncio.Task[list[dict[str, Any]]]]
        ] = {}
        self._adverse_tasks: dict[str, asyncio.Task[bool]] = {}
        self._entry_cancel_symbols: dict[str, int] = {}
        self._maker_pair_guards: dict[str, MakerPairGuard] = {}
        self._cash_reconciliation_task: asyncio.Task[int] | None = None
        self._settlement_reconciliation_task: asyncio.Task[int] | None = None
        self._read_recovery_attempts: dict[str, int] = {}
        self._risk_read_verified = False
        self._stream_holds: set[str] = set()
        self._shard_restarts: dict[int, asyncio.Task[None]] = {}
        self._shard_attempts: dict[int, int] = {}
        self._stream_restarts: dict[str, asyncio.Task[None]] = {}
        self._stream_recovery_task: asyncio.Task[None] | None = None
        self._stream_recovery_started = 0.0
        self._private_recovery_orders: set[str] = set()
        self._stream_ready: set[str] = {"public", "private"}
        self._stream_generation = {"public": 0, "private": 0}
        self._stream_attempts = {"public": 0, "private": 0}
        self.exchange_reward_uptime = ExchangeRewardUptime()
        # Separate read I/O lock/short timeout, but the SAME rate authority.
        # Slow reporting must not hold the safety reconciliation client's lock.
        self.reward_reporting_rest = GeminiReadRestClient(
            self.governor, api_key=config.api_key, api_secret=config.api_secret,
            timeout_seconds=3,
        )
        self._reward_reporting_task: asyncio.Task[None] | None = None
        self._logging = configure_logging(
            self.directory / "logs", secrets=(config.api_key, config.api_secret)
        )
        self.log = OperationalLogger(self._logging, self.journal)
        self.account.event_sink = self._account_event
        self._last_logged_status: tuple[str, bool, str] | None = None
        self._last_status_log_at = 0.0
        self.queue_shadow = QueueShadow()
        self._queue_archive = ShadowArchive(self.directory)
        self._queue_watch_symbols: set[str] = set()
        self._queue_live_orders: dict[str, Any] = {}
        self.queue_pilot = (AdaptiveQueuePilot(
                config.strategy.queue_pilot, config.strategy.quote_quantity)
            if isinstance(config.strategy.queue_pilot, AdaptivePolicy) else
            QueuePilot(config.strategy.queue_pilot, config.strategy.quote_quantity)
            if config.strategy.queue_pilot else None)
        self._pilot_proof_tasks: dict[str, asyncio.Task] = {}
        self._pilot_next_proof: dict[str, float] = {}
        self._pilot_proved: dict[str, tuple] = {}
        self._adaptive_breach_saved = False
        self._adaptive_storage_failure = None
        self.log.event(logging.INFO, "runner_created", read_only=read_only)

    def _rank_live_universe(self, *args, **kwargs):
        if self.queue_pilot is not None:
            kwargs["symbol_offsets"] = {s: 0 for s in self.queue_pilot.symbols if self._pilot_at_touch(s)}
        return rank_live_universe(*args, **kwargs)

    def _sync_adaptive_scope(self):
        """Bind all-selected policy to durable owners, never to all watched books.

        Existing retirement paths supply authenticated terminal/flat proof. Do
        not discard local safety state while orders, inventory or proof work
        remain. Legacy adaptive owners may use normal fallback while waiting;
        stay-best owners cannot enter until their proof-tracked slot is present.
        """
        pilot = self.queue_pilot
        if not isinstance(pilot, AdaptiveQueuePilot) or pilot.policy.scope != 'all_selected':
            return
        selected = {s for _, s in self._sticky_markets.values()} & self._permitted_symbols
        maximum = self.config.risk.max_two_sided_quotes
        if len(selected) > maximum:
            raise RuntimeError('selected adaptive owner count exceeds capacity')
        retained = set()
        for symbol in pilot.symbols - selected:
            task = self._pilot_proof_tasks.get(symbol)
            if task is not None and task.done():
                task.result()  # Never discard an unknown account failure.
            if (self.account.reducer.positions.get(symbol)
                    or self.account.reducer.risk_adding_order_ids(symbol)
                    or self.journal.has_nonterminal_intents(symbol)
                    or self.journal.pending_exit(symbol) or symbol in self._exit_workers
                    or symbol in self._exit_uncertain or symbol in self._entry_cancel_symbols
                    or any(self.journal.get_deadline(symbol, o) is not None for o in ('YES', 'NO'))
                    or (task is not None and not task.done())):
                retained.add(symbol)
        names = (set(pilot.symbols) & selected) | retained
        for symbol in sorted(selected - names):
            if len(names) >= maximum:
                break
            names.add(symbol)
        for symbol in pilot.symbols - names:
            self._pilot_proof_tasks.pop(symbol, None)
            self._pilot_proved.pop(symbol, None)
            self._pilot_next_proof.pop(symbol, None)
        pilot.sync_selected(names)

    def _pilot_at_touch(self, symbol):
        pilot = self.queue_pilot
        return bool(pilot is not None and symbol in pilot.symbols
                    and (not isinstance(pilot, AdaptiveQueuePilot) or pilot.modes[symbol] == 'best'))

    def _adaptive_desired(self, symbol):
        pilot = self.queue_pilot
        if not isinstance(pilot, AdaptiveQueuePilot) or symbol not in pilot.symbols:
            return None
        try:
            return pilot.desired_mode(symbol, now=_utc_now(), generation=self._pilot_generation(symbol),
                                      safe=self._pilot_evidence_safe(symbol))
        except Exception as exc:
            pilot.fail(symbol, exc)
            return 'best' if pilot.policy.placement_mode == 'stay_best' else 'fallback'

    def _adaptive_transition(self, symbol):
        desired = self._adaptive_desired(symbol)
        return desired is not None and desired != self.queue_pilot.modes[symbol]

    def _pilot_generation(self, symbol):
        return self._public_generation(symbol), self._stream_generation["private"]

    def _pilot_evidence_safe(self, symbol):
        # An owned cancellation blocks entry, but does not make a healthy
        # book/account observation disappear. Actual fills, stale evidence,
        # recovery and accounting uncertainty still reset adaptive warmup.
        return bool(self._book_fresh(symbol) and not self._public_shard_held(symbol)
                    and not self._stream_holds and not self._read_recovery_attempts
                    and not self._reward_program_paused and self.account.snapshots_fresh
                    and self.account.equity_classification_complete
                    and symbol not in self.account.blocked_entries
                    and not self.account.reducer.positions.get(symbol)
                    and not self.journal.pending_exit(symbol) and symbol not in self._exit_workers
                    and symbol not in self._exit_uncertain)

    def _pilot_safe(self, symbol):
        return self._pilot_evidence_safe(symbol) and symbol not in self._entry_cancel_symbols

    def _refresh_adaptive_evidence(self):
        # Bounded local checks continue while pairs are canceled/cooling down
        # and while ranking awaits I/O. Quiet books need no new depth tick to
        # prove transport/account continuity. This grants no order permission.
        if isinstance(self.queue_pilot, AdaptiveQueuePilot):
            for symbol in self.queue_pilot.symbols:
                self._adaptive_desired(symbol)

    def _pilot_advice(self, symbol, *, allow_entry=False):
        pilot = self.queue_pilot
        if pilot is None or symbol not in pilot.symbols:
            return "outside_pilot"
        try:
            if isinstance(pilot, AdaptiveQueuePilot) and not self._pilot_safe(symbol):
                self._adaptive_desired(symbol)
                return "withdraw"
            return pilot.advice(symbol, now=_utc_now(), generation=self._pilot_generation(symbol),
                                orders=tuple(self._queue_live_orders.values()), safe=self._pilot_safe(symbol),
                                allow_entry=allow_entry)
        except Exception as exc:
            pilot.fail(symbol, exc)
            return "withdraw"

    def _pilot_proof_current(self, symbol):
        proof = self._pilot_proved.get(symbol)
        return bool(proof and 0 <= time.monotonic() - proof[0] <= 3
                    and proof[1] == self._pilot_generation(symbol) and proof[2] == self.account.fill_version)

    def _observe_queue_pilot(self, symbol, *, frame=None):
        pilot = self.queue_pilot
        if pilot is None or symbol not in pilot.symbols:
            return
        try:
            if (not self._book_fresh(symbol) or self._public_shard_held(symbol)
                    or self._stream_holds or not self.account.snapshots_fresh):
                pilot.invalidate(symbol)
            elif frame is not None:
                pilot.observe_trade(frame, generation=self._pilot_generation(symbol), at=_utc_now())
            else:
                pilot.observe_book(self.public.books[symbol], generation=self._pilot_generation(symbol),
                                   at=_utc_now(), orders=tuple(self._queue_live_orders.values()))
        except Exception as exc:
            pilot.fail(symbol, exc)  # Local evidence failure never clears risk/account gates.
        # Trade depletion must wake the guard too, without waiting for a depth tick.
        self.public.adverse_dirty.mark(symbol)
        self.public.depth_changed.set()

    async def _pilot_flat_proof(self, symbol):
        pilot = self.queue_pilot
        try:
            generation, version = self._pilot_generation(symbol), self.account.fill_version
            async with asyncio.timeout(15):
                snapshot = await self._exit_account_snapshot(symbol)
            if (symbol not in pilot.symbols or snapshot.signed_positions.get(symbol) or generation != self._pilot_generation(symbol)
                    or version != self.account.fill_version or not self._pilot_safe(symbol)
                    or self.journal.has_nonterminal_intents(symbol)):
                return
            # A missed execution cannot be converted into a zero-fill requeue.
            # Filled lifecycles are released only by normal exact-flat cleanup.
            pilot.proof_required.discard(symbol)
            self._pilot_proved[symbol] = (time.monotonic(), generation, version)
            self.journal.audit("queue_pilot_flat_proof", {"symbol": symbol})
            self.public.dirty.mark(symbol)
        except (ExitDeferred, TimeoutError):
            return
        except ApiError as exc:
            if not transient_failure(exc):
                raise
        finally:
            self._pilot_next_proof[symbol] = time.monotonic() + 60

    def _pilot_entry_ready(self, symbol):
        pilot = self.queue_pilot
        if (isinstance(pilot, AdaptiveQueuePilot) and pilot.policy.placement_mode == 'stay_best'
                and symbol not in pilot.symbols):
            return False  # A watched reserve cannot skip the selected-owner proof path.
        if pilot is None or symbol not in pilot.symbols:
            return True
        if symbol not in pilot.markets:
            self._observe_queue_pilot(symbol)
        adaptive = isinstance(pilot, AdaptiveQueuePilot)
        desired = self._adaptive_desired(symbol) if adaptive else None
        if symbol in self._entry_cancel_symbols:
            return False  # Continuous observation is not cancellation proof.
        task = self._pilot_proof_tasks.get(symbol)
        if task is not None and task.done():
            task.result()  # Unknown account/ownership failures remain fatal.
            self._pilot_proof_tasks.pop(symbol)
        if (adaptive and desired == pilot.modes[symbol] == 'fallback'
                and symbol not in pilot.proof_required and symbol not in pilot.settling
                and symbol not in pilot.cooldown_at):
            return True  # Initial normal quoting; no best-bid permission.
        if adaptive and symbol in pilot.cooldown_at:
            book = self.public.books.get(symbol)
            if (book is None or book.last_update_id <= pilot.cancel_sequence.get(symbol, -1)
                    or Decimal(str((_utc_now()-pilot.cooldown_at[symbol]).total_seconds()))
                        < pilot.policy.limits.refresh_cooldown_seconds):
                return False  # Do not spend a short-lived proof during cooldown.
        if not self._pilot_proof_current(symbol):
            pilot.proof_required.add(symbol)
        if symbol in pilot.proof_required:
            # At full breadth, serialize additional adaptive REST proofs. The
            # shared transport governor remains authoritative for every read.
            proof_busy = (adaptive and pilot.policy.scope == 'all_selected'
                          and any(not t.done() for t in self._pilot_proof_tasks.values()))
            if (symbol not in self._pilot_proof_tasks and self._pilot_safe(symbol)
                    and not proof_busy
                    and time.monotonic() >= self._pilot_next_proof.get(symbol, 0)):
                self._pilot_proof_tasks[symbol] = asyncio.create_task(self._pilot_flat_proof(symbol))
            return False
        if adaptive:
            # Mode changes happen only between pairs, not between maker legs.
            # Even fallback after withdrawal needs terminal/flat proof, cooldown
            # and a later public sequence. No observation clears this barrier.
            if desired != pilot.modes[symbol] or desired == 'fallback':
                book = self.public.books.get(symbol)
                last = pilot.cooldown_at.get(symbol)
                if (self.account.reducer.risk_adding_order_ids(symbol)
                        or self.journal.has_nonterminal_intents(symbol)
                        or not self._pilot_safe(symbol) or book is None
                        or book.last_update_id <= pilot.cancel_sequence.get(symbol, -1)
                        or (last is not None and Decimal(str((_utc_now()-last).total_seconds()))
                            < pilot.policy.limits.refresh_cooldown_seconds)):
                    return False
                if desired != pilot.modes[symbol]:
                    pilot.modes[symbol] = desired
                    pilot.settling.discard(symbol)
                    self.journal.audit('adaptive_queue_mode', {'symbol': symbol, 'mode': desired})
                    self.public.dirty.mark(symbol)
                    return False  # Rerank at the new offset; never reuse candidate.
                pilot.settling.discard(symbol)
                return True
        return self._pilot_advice(symbol, allow_entry=True) == "allow"

    def _observe_queue_shadow(self, symbol: str, *, frame: dict | None = None) -> None:
        """Diagnostic failure disables only the observer, never trading."""
        self._observe_queue_pilot(symbol, frame=frame)
        observer = getattr(self, "queue_shadow", None)
        if observer is None or observer.error:
            return
        try:
            if (symbol not in self._queue_watch_symbols
                    or not self._public_stream_fresh(symbol)
                    or self._stream_holds or self._public_shard_held(symbol)
                    or not self.account.snapshots_fresh):
                observer.invalidate(symbol)
                return
            generation = (self._public_generation(symbol), self._stream_generation["private"])
            if frame is not None:
                observer.observe_trade(frame, generation=generation, at=_utc_now())
            else:
                book = self.public.books.get(symbol)
                if book is not None:
                    observer.observe_book(book, generation=generation, at=_utc_now(),
                                          orders=tuple(self._queue_live_orders.values()))
        except Exception as exc:
            observer.disable(exc)

    def _queue_shadow_report(self, now: datetime, rows: list[dict]) -> dict:
        observer = getattr(self, "queue_shadow", None)
        if observer is None:
            return {"mode": "unavailable", "used_for_trading": False}
        try:
            self._queue_watch_symbols = {c.rules.symbol for c in self.universe}
            observer.retain(self._queue_watch_symbols)
            self._queue_live_orders = {o.order_id: o for o in self.account.reducer.orders.values() if o.working}
            if len(self._queue_live_orders) > 40:
                raise ValueError("shadow working-order bound exceeded")
            # Quiet synchronized books are still current when their own stream
            # is verified. Seed missing observers here, never backfill warmup.
            for symbol in self._queue_watch_symbols:
                if symbol not in observer.markets:
                    self._observe_queue_shadow(symbol)
            fresh = {s for s in observer.markets if self._public_stream_fresh(s)
                     and not self._stream_holds and not self._public_shard_held(s)
                     and self.account.snapshots_fresh
                     and observer.markets[s].generation == (
                         self._public_generation(s), self._stream_generation["private"])}
            report = observer.report(at=now, fresh_symbols=fresh)
            for symbol in set(observer.markets) - fresh:
                observer.invalidate(symbol)
            self._queue_archive.write(at=now, report=report, forecasts=rows)
            report["archive_error_type"] = self._queue_archive.error
            return report
        except Exception as exc:
            observer.disable(exc)
            return {"mode": "unavailable", "used_for_trading": False, "error_type": observer.error}

    def _queue_cancel_done(self, started: float, task: asyncio.Task) -> None:
        elapsed = time.monotonic() - started
        succeeded = not task.cancelled() and task.exception() is None
        if isinstance(self.queue_pilot, AdaptiveQueuePilot):
            try:
                self.queue_pilot.record_cancel(elapsed, succeeded=succeeded)
            except Exception:
                self.queue_pilot.cancel_envelope_breached = True
            if self.queue_pilot.cancel_envelope_breached and not self._adaptive_breach_saved:
                try:
                    self.journal.set_runtime_state(self.queue_pilot.policy.breach_key,
                        json.dumps({'breached': True, 'maximum_seconds': str(self.queue_pilot.cancel_maximum)}),
                        updated_at=_utc_now())
                    self._adaptive_breach_saved = True
                except Exception as exc:
                    # Persistence/integrity failure is not a market-local signal.
                    # Surface it after the loop as well as blocking new sends.
                    self._adaptive_storage_failure = exc
                    self.gate.halt()
        observer = getattr(self, "queue_shadow", None)
        if observer is not None:
            try:
                observer.record_cancel(elapsed, succeeded=succeeded)
            except Exception as exc:
                observer.disable(exc)

    def _restore_adaptive_breach(self):
        pilot = self.queue_pilot
        if not isinstance(pilot, AdaptiveQueuePilot):
            return
        raw = self.journal.get_runtime_state(pilot.policy.breach_key)
        if raw is None:
            return
        value = json.loads(raw)
        if set(value) != {'breached', 'maximum_seconds'} or value['breached'] is not True:
            raise RuntimeError('invalid durable adaptive cancellation breach')
        maximum = Decimal(value['maximum_seconds'])
        if not maximum.is_finite() or maximum < 0:
            raise RuntimeError('invalid durable adaptive cancellation maximum')
        pilot.cancel_maximum = max(pilot.cancel_maximum, maximum)
        pilot.cancel_envelope_breached = self._adaptive_breach_saved = True

    def _write_status(self, phase: str, activated: bool, reason: str) -> None:
        clock = time.monotonic()
        status_key = (phase, activated, reason)
        if status_key != self._last_logged_status or clock - self._last_status_log_at >= 60:
            self.log.event(
                logging.ERROR if phase == "halted" else logging.INFO,
                "runtime_status" if status_key != self._last_logged_status else "runtime_heartbeat",
                phase=phase, activated=activated, reason=reason,
                pending_orders=self.account.reducer.pending_order_count(),
                open_positions=len(self.account.reducer.positions),
                tracked_symbols=len(self.universe), dropped_log_records=self._logging.dropped_records,
                entry_read_holds=sorted(self._read_recovery_attempts),
            )
            self._last_logged_status, self._last_status_log_at = status_key, clock
        atomic_write_json(
            self.directory / "status.json",
            {**runtime_status(
                phase=phase,
                activated=activated,
                reason=reason,
                counters={
                    "tracked_symbols": len(self.universe),
                    "ranked_candidates": len(self.ranked),
                    "pending_orders": self.account.reducer.pending_order_count(),
                    "open_positions": len(self.account.reducer.positions),
                    "dropped_log_records": self._logging.dropped_records,
                },
            ), "exchange_reward_uptime": self.exchange_reward_uptime.metadata(now=_utc_now()),
             "entry_read_holds": sorted(self._read_recovery_attempts),
             "journal_backup": self._journal_backups.report() if self._journal_backups else None,
             "stream_recovery_holds": sorted(self._stream_holds),
             "public_shards": self.public.status() if isinstance(self.public, PublicMarketShards) else [],
             "public_shard_reconnect_attempts": dict(self._shard_attempts),
             "session_expires_at": self._permit_expires_at.isoformat() if self._permit_expires_at else None,
             "uptime_override_active": self._override_active(),
             "uptime_override_until": self._uptime_override_until.isoformat() if self._uptime_override_until else None,
             "continue_after_override": self._continue_after_override,
              "continuous_service": self._continuous_service,
              "reward_program_paused": self._reward_program_paused,
              "resting_continuity": self._continuity_view(),
              "exit_recovery": {s: {"reason": w.reason, "rounds": w.rounds,
                                     "retry_in_seconds": max(0, w.retry_at - time.monotonic())}
                               for s, w in self._exit_waits.items()},
              "inventory_catalog_holds": sorted(self._inventory_catalog_holds),
              "sticky_recovery": {owner: {"reason": failure.reason,
                  "observed_failure_seconds": failure.observed_seconds,
                  "release_grace_seconds": RECOVERY_GRACE_SECONDS,
                  "economic_reservation_until": (
                      self._sticky_recovery.economic_hold_until(owner).isoformat()
                      if self._sticky_recovery.economic_hold_until(owner) is not None else None),
                  "reservation_is_not_quoting_or_uptime_credit": True}
                  for owner, failure in self._sticky_recovery.failures.items()},
              "reward_epoch": self._reward_epoch},
        )

    def _account_event(self, event: str, fields: dict[str, object]) -> None:
        self.log.event(logging.INFO, event, **fields)
        if event == "order_transition":
            symbol = str(fields["symbol"])
            order = self.account.reducer.orders.get(str(fields.get("order_id", "")))
            if order is not None:
                if order.working:
                    self._queue_live_orders[order.order_id] = order
                else:
                    self._queue_live_orders.pop(order.order_id, None)
                if len(self._queue_live_orders) > 40:
                    raise RuntimeError("working-order cache exceeds approved capacity")
                if (self.queue_pilot is not None and symbol in self.queue_pilot.symbols
                        and (not order.working or order.cumulative_filled)):
                    # Spontaneous MOC cancellation or fill is just as binding
                    # as an operator/guard cancellation; never reuse old proof.
                    self.queue_pilot.canceled(symbol, _utc_now())
                    self._pilot_proved.pop(symbol, None)
                    self.public.adverse_dirty.mark(symbol)
                    self.public.depth_changed.set()
            observer = getattr(self, "queue_shadow", None)
            if observer is not None and not observer.error:
                try:
                    order = self.account.reducer.orders.get(str(fields.get("order_id", "")))
                    if order is not None:
                        if order.working:
                            self._queue_live_orders[order.order_id] = order
                        else:
                            self._queue_live_orders.pop(order.order_id, None)
                        if len(self._queue_live_orders) > 40:
                            raise ValueError("shadow working-order bound exceeded")
                        # Private/public publication ordering is uncertain;
                        # never carry a queue scenario across our own mutation.
                        observer.invalidate(symbol)
                except Exception as exc:
                    observer.disable(exc)
            # Observe every lifecycle event as well as the clock. Otherwise a
            # cancel and replacement inside one 250ms tick can hide a gap.
            self._observe_resting(symbol_filter=symbol)
            observation = self._resting_observations.get(symbol)
            if observation is not None and fields.get("side") == "BUY":
                if fields.get("status") == "NEW":
                    observation.order_new_events += 1
                elif fields.get("status") == "CANCELED":
                    observation.order_cancel_events += 1

    def _observe_resting(self, *, symbol_filter: str | None = None) -> None:
        now, clock = _utc_now(), time.monotonic()
        # Bounded to current owners, with per-run measurements. No historical
        # event/day counters are replaced and no offline time is credited.
        symbols = {c.rules.symbol for c in self.universe}
        self._resting_observations = {s: v for s, v in self._resting_observations.items() if s in symbols}
        self._two_sided_resting_observations = {
            s: v for s, v in self._two_sided_resting_observations.items() if s in symbols
        }
        healthy = (not self._stream_holds and not self._reward_program_paused
                   and self.account.snapshots_fresh and self.account.equity_classification_complete)
        for c in self.universe:
            if symbol_filter is not None and c.rules.symbol != symbol_filter:
                continue
            if c.owner_key not in self._sticky_pool_ids and c.rules.symbol not in self._resting_observations:
                continue
            qualifies = healthy and self._entry_time_valid(c.rules.symbol) and locally_qualifying(
                c, self.account.reducer, self.public.books.get(c.rules.symbol), now=now,
                market_data_verified_at=self._market_data_verified_at(),
            )
            self._resting_observations.setdefault(c.rules.symbol, RestingObservation()).observe(clock, qualifies)
            both = qualifies and locally_qualifying(
                c, self.account.reducer, self.public.books.get(c.rules.symbol), now=now,
                market_data_verified_at=self._market_data_verified_at(), minimum_sides=2,
            )
            self._two_sided_resting_observations.setdefault(c.rules.symbol, RestingObservation()).observe(clock, both)

    def _continuity_view(self) -> dict[str, Any]:
        return {"source": "local_per_run_observation_not_exchange_snapshots",
                "retained_pair_checks": self._retained_pairs, "replaced_pairs": self._replaced_pairs,
                "cancel_decisions_by_reason": dict(self._quote_cancel_reasons),
                "two_sided_symbols": {s: v.view() for s, v in self._two_sided_resting_observations.items()},
                "symbols": {s: v.view() for s, v in self._resting_observations.items()}}

    def _record_quote_cancel(self, symbol: str, reason: str) -> None:
        self._quote_cancel_reasons[reason] += 1
        self.log.event(logging.INFO, "quote_cancel_decision", symbol=symbol, reason=reason)

    def _override_active(self, now: datetime | None = None) -> bool:
        return self._commissioning_ignore_uptime_proxy and (
            self._uptime_override_until is None or (now or _utc_now()) < self._uptime_override_until
        )

    def _current_reward_epoch(self, now: datetime) -> tuple[str, bool]:
        return eastern_day_window(now)[0].isoformat(), self._override_active(now)

    def _crypto_entry_cutoff_seconds(self) -> int:
        policy = self.config.strategy.entry_quality
        return policy.crypto_entry_before_expiry_seconds if policy is not None else 300

    def _advance_reward_epoch(self, now: datetime) -> bool:
        """Rebuild day-specific eligibility before admitting any new-day entry."""
        epoch = self._current_reward_epoch(now)
        if epoch == self._reward_epoch:
            return False
        self._uptime = sample_and_load_progress(
            self.journal, self.universe, self.account.reducer, now=now,
            books={} if self._reward_program_paused else self.public.books,
            market_data_verified_at=self._market_data_verified_at(),
            exit_before_expiry_seconds=self.config.risk.force_exit_before_expiry_seconds,
            sports_exit_before_start_seconds=self.config.risk.sports_exit_before_start_seconds,
            crypto_entry_before_expiry_seconds=self._crypto_entry_cutoff_seconds(),
        )
        self.ranked = self._rank_live_universe(
            self.universe, self.public.books, now=now,
            quantity=self.config.strategy.quote_quantity, offset_ticks=self.config.strategy.quote_offset_ticks,
            uptime=self._uptime, account=self.account.reducer,
            market_data_verified_at=self._market_data_verified_at(), profitability=self.profitability,
            target_uptime=self.config.strategy.target_uptime, maker_rebate_fractions=self._maker_rebate_fractions,
            preferred_symbols={pool: pair[1] for pool, pair in self._sticky_markets.items()},
            # Gemini's exact snapshot phase/denominator is unavailable. Local
            # 50% reachability remains telemetry, not an entry admission rule.
            allow_uptime_override=True,
            evaluate_working_quotes=True,
        )
        self._reward_epoch = epoch
        self.log.event(logging.INFO, "reward_epoch_changed", eastern_day_start=epoch[0],
                       uptime_override_active=epoch[1], same_process=True)
        return True

    def _load_sticky_markets(self) -> None:
        raw = self.journal.get_runtime_state("sticky_markets_v2")
        legacy = raw is None
        if legacy:
            raw = self.journal.get_runtime_state("sticky_markets_v1")
        if raw is None:
            return
        value = json.loads(raw)
        if (not isinstance(value, dict) or len(value) > MAX_ACTIVE_MARKETS
                or any(not isinstance(pool, str) or not isinstance(pair, list) or len(pair) != 2
                       or any(not isinstance(s, str) or not s for s in pair)
                       for pool, pair in value.items())):
            raise RuntimeError("persisted market cohort is malformed")
        if legacy:
            value = {market_owner_key(pool, pair[1]): pair for pool, pair in value.items()}
        if any(key != market_owner_key(key.split("\x1e")[0], pair[1]) for key, pair in value.items()):
            raise RuntimeError("persisted market owner identity is malformed")
        if len({pair[1] for pair in value.values()}) != len(value):
            raise RuntimeError("duplicate persisted market symbol")
        self._sticky_markets = {owner: tuple(pair) for owner, pair in value.items()}
        self._sticky_pool_ids = set(self._sticky_markets)
        recovery = self.journal.get_runtime_state("sticky_recovery_v2")
        if recovery is not None:
            self._sticky_recovery.restore_state(recovery, self._sticky_pool_ids)
        else:
            self._sticky_recovery.restore(self.journal.get_runtime_state("sticky_recovery_v1"), self._sticky_pool_ids)

    def _sync_quote_asset_precisions(self, contracts: list[Any]) -> None:
        """Merge fresh metadata without forgetting held or historical symbols."""
        merged = dict(self.account.quote_asset_precision)
        for contract in contracts:
            symbol = canonical_symbol(contract.rules.symbol)
            precision = int(contract.rules.quote_asset_precision)
            if not 0 <= precision <= 18:
                raise RuntimeError("contract quote-asset precision is invalid")
            prior = merged.get(symbol)
            if prior is not None and prior != precision:
                raise RuntimeError("contract quote-asset precision changed unexpectedly")
            merged[symbol] = precision
        self.account.quote_asset_precision = merged
        self.journal.set_runtime_state(
            "quote_asset_precision_v1",
            json.dumps(merged, separators=(",", ":"), sort_keys=True),
            updated_at=_utc_now(),
        )

    def _owner_event(self, event: str, owner: str, symbol: str, reason: str) -> None:
        fields = {"owner_key": owner, "symbol": symbol, "reason": reason}
        self.journal.audit(event, fields)
        self.log.event(logging.INFO, event, **fields)

    def _rank_for_owner_check(self) -> None:
        self.ranked = self._rank_live_universe(
            self.universe, self.public.books, now=_utc_now(),
            quantity=self.config.strategy.quote_quantity, offset_ticks=self.config.strategy.quote_offset_ticks,
            uptime=self._uptime, account=self.account.reducer,
            market_data_verified_at=self._market_data_verified_at(), profitability=self.profitability,
            target_uptime=self.config.strategy.target_uptime, maker_rebate_fractions=self._maker_rebate_fractions,
            allow_uptime_override=True, evaluate_working_quotes=True,
        )

    def _select_with_reserved_owners(self, reserved: set[str], maximum: int):
        if not hasattr(self, '_reward_focus'):
            self._reward_focus = RewardFocus()
        self._reward_focus.update(self.exchange_reward_uptime.reward_results, now=_utc_now())
        return select_sticky_markets(
            self.ranked, sticky_pool_ids=self._sticky_pool_ids, locked_pool_ids=reserved,
            permitted_symbols=self._permitted_symbols, maximum_active_pools=maximum,
            actionable=self._candidate_actionable, entry_time_valid=self._entry_time_valid,
            reward_focus=self._reward_focus if self._continuous_service else None,
        )

    def _owner_failure_reason(self, symbol: str) -> str:
        if not self._entry_time_valid(symbol):
            return "entry_time_unavailable"
        candidate = next((c for c in self.ranked if c.symbol == symbol), None)
        if candidate is None:
            target, target_reason = self._quote_target_state(symbol)
            if target is not None:
                quality_reason = self._entry_quality_reason(symbol, target)
                if quality_reason is not None:
                    return quality_reason
            return "rank_candidate_missing:" + target_reason
        reasons = tuple(r for r in candidate.reasons if r != _UPTIME_PROXY_REJECTION)
        if reasons:
            return "; ".join(reasons)
        if candidate.quote is not None:
            quality_reason = self._entry_quality_reason(symbol, candidate.quote)
            if quality_reason is not None:
                return quality_reason
        return "non_positive_marginal_pool_economics"

    async def _select_recovery_cohort(self, locked: set[str], maximum: int):
        """Cancel unsafe quotes separately; release a slot only after recovery/proof."""
        if not self._continuous_service:
            desired, owners = self._select_with_reserved_owners(locked, maximum)
            return desired, owners, locked
        reserved = set(locked) | set(self._sticky_pool_ids)
        desired, _ = self._select_with_reserved_owners(reserved, maximum)
        healthy = {c.owner_key for c in desired}
        changed = False
        holds_before = dict(self._sticky_recovery.economic_holds)
        for owner, (_, symbol) in tuple(self._sticky_markets.items()):
            previous = self._sticky_recovery.failures.get(owner)
            if owner in locked or owner in healthy:
                if previous is not None:
                    self._owner_event("sticky_owner_recovered" if owner in healthy else "sticky_owner_protected",
                                      owner, symbol, "quoting_checks_passed" if owner in healthy else "inventory_or_stream_hold")
                    changed = True
                self._sticky_recovery.observe(owner, None, _utc_now(),
                                              preserve_economic_hold=owner in locked)
                continue
            reason = self._owner_failure_reason(symbol)
            available = (not self._stream_holds and not self._read_recovery_attempts
                         and not self._reward_program_paused and self._book_fresh(symbol)
                         and self.account.snapshots_fresh and self.account.equity_classification_complete)
            protected = self._sticky_recovery.protect_economics(owner, reason, _utc_now())
            if owner not in holds_before and owner in self._sticky_recovery.economic_holds:
                self._owner_event("sticky_owner_daily_reserved", owner, symbol, reason)
            mature = self._sticky_recovery.observe(owner, reason, _utc_now(),
                                                   evidence_available=available and not protected)
            if previous is None or previous.reason != reason:
                self._owner_event("sticky_owner_recovery_hold", owner, symbol, reason)
                changed = True
            if not mature or time.monotonic() < self._next_owner_release_check:
                continue
            # One proof attempt per minute account-wide; never poll each owner.
            self._next_owner_release_check = time.monotonic() + 60
            if (self._recovery_stop_requested() or symbol in self._exit_workers
                    or symbol in self._exit_uncertain or self.journal.pending_exit(symbol)
                    or any(self.journal.get_deadline(symbol, o) is not None for o in ("YES", "NO"))):
                continue
            await self._cancel_symbol_entries(symbol)
            try:
                async with asyncio.timeout(15):
                    snapshot = await self._exit_account_snapshot(symbol)
            except Exception as exc:
                if isinstance(exc, ExitDeferred) or (isinstance(exc, (ApiError, TimeoutError)) and transient_failure(exc)):
                    continue
                raise
            if snapshot.signed_positions.get(symbol) or self._recovery_stop_requested():
                continue
            # The book may recover while awaiting cancellation/account proof.
            # Fresh ranking, not the earlier failed score, decides retirement.
            self._rank_for_owner_check()
            refreshed, _ = self._select_with_reserved_owners(reserved, maximum)
            if owner in {c.owner_key for c in refreshed}:
                self._sticky_recovery.observe(owner, None, _utc_now())
                self._owner_event("sticky_owner_recovered", owner, symbol, "fresh_post_proof_checks_passed")
                changed = True
                continue
            fresh_reason = self._owner_failure_reason(symbol)
            if (self._sticky_recovery.protect_economics(owner, fresh_reason, _utc_now())
                    or fresh_reason != reason):
                # An awaited proof can change the failure classification. A new
                # economic hold or different fault needs its own recovery period.
                self._sticky_recovery.observe(owner, fresh_reason, _utc_now(), evidence_available=False)
                changed = True
                continue
            if (not self._book_fresh(symbol) or self._stream_holds or self._read_recovery_attempts
                    or self._reward_program_paused or symbol in self._exit_workers
                    or symbol in self._exit_uncertain or self.journal.pending_exit(symbol)
                    or not self.account.snapshots_fresh or not self.account.equity_classification_complete
                    or self.account.reducer.positions.get(symbol)
                    or self.journal.has_nonterminal_intents(symbol)
                    or any(self.journal.get_deadline(symbol, o) is not None for o in ("YES", "NO"))):
                continue
            self._owner_event("sticky_owner_released", owner, symbol,
                              "sustained_600s_failure_terminal_flat:" + self._owner_failure_reason(symbol))
            self._sticky_pool_ids.discard(owner)
            self._sticky_markets.pop(owner)
            reserved.discard(owner)
            self._sticky_recovery.observe(owner, None, _utc_now())
            changed = True
            break  # No stale pre-proof ranking can release a second owner.
        if (changed or holds_before != self._sticky_recovery.economic_holds
                or time.monotonic() - self._sticky_recovery_saved_at >= 30):
            self.journal.set_runtime_state("sticky_recovery_v2", self._sticky_recovery.dumps_state(), updated_at=_utc_now())
            self._sticky_recovery_saved_at = time.monotonic()
        desired, owners = self._select_with_reserved_owners(reserved, maximum)
        return desired, owners, reserved

    def _save_sticky_markets(self, desired: list[Any], locked_pools: set[str]) -> None:
        markets = {c.owner_key: (c.event_ticker, c.symbol) for c in desired}
        for c in self.universe:
            if c.owner_key in locked_pools:
                markets[c.owner_key] = (c.event_ticker, c.rules.symbol)
        # A catalog-omitted held market is deliberately absent from universe;
        # preserve its exact durable owner identity until it is flat.
        for pool_id in locked_pools:
            if pool_id in self._sticky_markets:
                markets.setdefault(pool_id, self._sticky_markets[pool_id])
        if markets != self._sticky_markets or self.journal.get_runtime_state("sticky_markets_v2") is None:
            for owner, (_, symbol) in markets.items():
                if owner not in self._sticky_markets:
                    self._owner_event("sticky_owner_admitted", owner, symbol, "vacant_slot_pool_aware_selection")
            self.journal.set_runtime_state("sticky_markets_v2", json.dumps(markets, sort_keys=True), updated_at=_utc_now())
            self._sticky_markets = markets
        self._sync_adaptive_scope()

    def _inventory_locked_pool_ids(self) -> set[str]:
        held = {symbol for symbol, quantity in self.account.reducer.positions.items() if quantity}
        return {
            pool_id for pool_id, (_, symbol) in self._sticky_markets.items()
            if symbol in held
        }

    def _recovery_stop_requested(self) -> bool:
        now = _utc_now()
        return (self._closed or (self.directory / "STOP").exists()
                or self.gate.state not in {EngineState.QUOTING, EngineState.READY}
                or (self._permit_expires_at is not None and now >= self._permit_expires_at)
                or (not self._continue_after_override and self._uptime_override_until is not None
                    and now >= self._uptime_override_until))

    def _check_stream_recovery(self) -> None:
        for task in self._pilot_proof_tasks.values():
            if task.done() and not task.cancelled():
                task.result()
        for shard, task in tuple(self._shard_restarts.items()):
            if task.done():
                task.result()
                self._shard_restarts.pop(shard)
        if self._stream_recovery_task is not None and self._stream_recovery_task.done():
            self._stream_recovery_task.result()

    def _check_background_failures(self, *workers) -> None:
        if self._adaptive_storage_failure is not None:
            raise self._adaptive_storage_failure
        self._check_stream_recovery()
        tasks = (*workers, *self._tasks, *self._adverse_tasks.values(),
                 *self._exit_workers.values(), *self._cancel_tasks.values())
        for task in tasks:
            if task is not None and task.done() and not task.cancelled():
                task.result()

    def _public_generation(self, symbol: str):
        if isinstance(self.public, PublicMarketShards):
            shard = self.public.assignments.get(canonical_symbol(symbol))
            return (
                self._stream_generation['public'],
                -1 if shard is None else self.public.generations[shard],
            )
        return self._stream_generation['public']

    def _public_shard_held(self, symbol: str) -> bool:
        return (isinstance(self.public, PublicMarketShards)
                and self.public.assignments.get(symbol) in self.public.held)

    def _save_shard_assignments(self) -> None:
        if isinstance(self.public, PublicMarketShards):
            self.journal.set_runtime_state('public_shards_v1', json.dumps(self.public.assignments, sort_keys=True), updated_at=_utc_now())

    def _begin_shard_recovery(self, event: ShardEvent) -> None:
        if not isinstance(self.public, PublicMarketShards) or not self.public.accepts(event):
            return
        if self.gate.state is not EngineState.QUOTING or self._recovery_stop_requested():
            raise RuntimeError('public shard loss outside active session') from event.error
        if self.journal.unresolved_ids():
            raise RuntimeError('shard loss during unknown mutation requires reconciliation') from event.error
        self.public.invalidate(event.shard)
        task = self._shard_restarts.get(event.shard)
        if task is not None and not task.done():
            return  # The owner observes the generation change and reconnects.
        if task is not None:
            task.result()
        self._shard_attempts[event.shard] = 0
        self.log.event(logging.WARNING, 'public_shard_paused', shard=event.shard,
                       affected_symbols=list(self.public.symbols(event.shard)), other_shards_preserved=True)
        self._shard_restarts[event.shard] = asyncio.create_task(
            self._recover_public_shard(event.shard), name=f'gemini-shard-recovery-{event.shard}',
        )

    async def _recover_public_shard(self, shard: int) -> None:
        try:
            while True:
                if self._recovery_stop_requested():
                    return  # Hold remains; drain never grants entry permission.
                if self._stream_holds or not self.account.snapshots_fresh:
                    await asyncio.sleep(.25)
                    continue
                symbols = self.public.symbols(shard)
                for symbol in symbols:
                    await self._cancel_symbol_entries(symbol)
                self._shard_attempts[shard] += 1
                if not self._continuous_service and self._shard_attempts[shard] > 3:
                    raise RuntimeError('public shard retry budget exhausted')
                generation = self.public.generations[shard]
                try:
                    async with asyncio.timeout(15):
                        await self.public.reconnect(shard)
                except Exception as exc:
                    if not (transient_failure(exc) or isinstance(exc, OSError)):
                        raise
                    delay = retry_after_seconds(exc)
                    if delay:
                        self.governor.apply_server_cooldown('public_ws', delay)
                    self.log.exception('public_shard_retry', ErrorClass.TRANSPORT, exc,
                                       shard=shard, attempt=self._shard_attempts[shard])
                    for _ in range(min(60, 2 ** min(self._shard_attempts[shard] - 1, 6))):
                        if self._recovery_stop_requested():
                            return
                        await asyncio.sleep(1)
                    continue
                proof_started = time.monotonic()
                while generation == self.public.generations[shard]:
                    if self._recovery_stop_requested():
                        return
                    if self._stream_holds:
                        proof_started = time.monotonic()
                        await asyncio.sleep(.25)
                        continue
                    if time.monotonic() - proof_started >= 60:
                        raise RuntimeError('public shard did not prove readiness within budget')
                    observed = self.public.sockets[shard].last_message_at
                    fresh = observed is not None and 0 <= (_utc_now() - observed).total_seconds() <= float(self.config.strategy.stale_book_seconds)
                    if (fresh and all(self.public.books[s].synchronized for s in symbols)
                            and not self._stream_holds and self.account.snapshots_fresh
                            and self.account.equity_classification_complete
                            and not self.journal.unresolved_ids()
                            and not any(o.working and o.side is Side.BUY and o.symbol in symbols
                                        for o in self.account.reducer.orders.values())):
                        # Account socket never changed; terminal cancels and
                        # exact classified live account state are mandatory.
                        self.public.held.discard(shard)
                        self.log.event(logging.INFO, 'public_shard_recovered', shard=shard,
                                       other_shards_preserved=True)
                        return
                    await asyncio.sleep(.05)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gate.halt()
            self.log.exception('public_shard_recovery_failed', ErrorClass.TRANSPORT, exc, shard=shard)
            raise

    async def _recover_stream(self, component: str, cause: BaseException, *, generation: int | None = None) -> None:
        if generation is not None and generation != self._stream_generation[component]:
            task = self._stream_restarts.get(component)
            if task is not None:
                await asyncio.shield(task)
            return
        allowed = self.gate.state is EngineState.QUOTING or (self.read_only and self.gate.state is EngineState.READY)
        if not allowed or self._recovery_stop_requested():
            raise RuntimeError("stream recovery is outside active session authority") from cause
        self._check_stream_recovery()
        # A send without acknowledgement remains ambiguous; never replay it.
        if self.journal.unresolved_ids():
            raise RuntimeError("stream loss during an unknown mutation requires reconciliation") from cause
        previous = self._stream_restarts.get(component)
        if component not in self._stream_holds or previous is None or previous.done():
            if not self._stream_holds:
                self._stream_recovery_started = time.monotonic()
                self._stream_attempts = {"public": 0, "private": 0}
            self._stream_generation[component] += 1
            self._stream_holds.add(component)
            self._stream_ready.discard(component)
            if component == "public":
                for book in self.public.books.values():
                    book.synchronized = False
                self.public.last_message_at = None
            else:
                self._private_recovery_orders.update(
                    order.order_id for order in self.account.reducer.orders.values() if order.working
                )
                self.account.last_balance_at = self.account.last_position_at = None
                self.account.balances_seen = self.account.positions_seen = False
                self.account.subscription_acknowledged = False
                self.account.reducer.position_update_ns = 0
            self.log.exception("stream_recovery_started", ErrorClass.TRANSPORT, cause,
                               component=component, entries_paused=True)
            self._stream_restarts[component] = asyncio.create_task(
                self._reconnect_stream(component), name="gemini-reconnect-" + component,
            )
            if self._stream_recovery_task is None or self._stream_recovery_task.done():
                self._stream_recovery_task = asyncio.create_task(
                    self._prove_stream_recovery(), name="gemini-stream-reconciliation",
                )
        await asyncio.shield(self._stream_restarts[component])

    async def _reconnect_stream(self, component: str, *, for_drain: bool = False) -> None:
        transport = self.public if component == "public" else self.private
        attempt = 0
        while True:
            attempt += 1
            self._stream_attempts[component] += 1
            if (for_drain and attempt > 3) or (not for_drain and not self._continuous_service and self._stream_attempts[component] > 3):
                raise RuntimeError("stream reconnect attempt budget exhausted")
            if self._closed or (not for_drain and self._recovery_stop_requested()):
                raise RuntimeError("stream recovery interrupted by stop or expiry")
            try:
                budget = (15 * len(self.public.sockets)
                          if component == 'public' and isinstance(self.public, PublicMarketShards) else 15)
                async with asyncio.timeout(budget):
                    await transport.close()
                    if component == "public":
                        self.public.books = {item.rules.symbol: LocalBook(item.rules.symbol) for item in self.universe}
                        self.public.resync_required.clear()
                    await transport.connect()  # Relearn conninfo through the shared governor.
                    if component == "public":
                        await self.public.subscribe(tuple(item.rules.symbol for item in self.universe))
                    else:
                        await self.private.subscribe_account()
                self._stream_ready.add(component)
                self.log.event(logging.INFO, "stream_reconnected", component=component, attempt=attempt,
                               trading_resumed=False)
                return
            except Exception as exc:
                if not (transient_failure(exc) or isinstance(exc, OSError)):
                    raise
                delay = retry_after_seconds(exc)
                if delay:
                    self.governor.apply_server_cooldown(component + "_ws", delay)
                self.log.exception("stream_reconnect_retry", ErrorClass.TRANSPORT, exc,
                                   component=component, attempt=attempt)
                if (for_drain or not self._continuous_service) and attempt == 3:
                    raise
                # Bounded attempts, paced indefinitely only for transport loss.
                # Wake for STOP at most one second later; server cooldowns are
                # still enforced by the shared transport governor on connect.
                for _ in range(min(60, 2 ** min(attempt - 1, 6))):
                    if self._closed or (not for_drain and self._recovery_stop_requested()):
                        raise RuntimeError("stream recovery interrupted by stop or expiry")
                    await asyncio.sleep(1)

    async def _recovery_account_proof(self) -> None:
        before_version = self.account.fill_version
        generation = dict(self._stream_generation)
        before = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        for order in before.active_orders:
            if not order.client_order_id or not self.journal.knows_client_order_id(order.client_order_id):
                raise RuntimeError("stream recovery found unowned active order")
            owned = self.account.reducer.orders.get(order.order_id)
            if owned is None or (owned.client_order_id, owned.symbol, owned.side, owned.outcome, owned.original_quantity) != (
                order.client_order_id, order.symbol, order.side, order.outcome, order.original_quantity
            ):
                raise RuntimeError("stream recovery active order identity is not proven")
            if self.read_only:
                raise RuntimeError("read-only recovery cannot quarantine active orders")
            if order.side.value == "SELL":
                raise RecoveryPending("owned reducer has not yet become terminal")
            await self._cancel_order_and_confirm(order.order_id, timeout=10)
        # A placement response/NEW may be lost even though Gemini created then
        # canceled the order. Resolve only an exact zero-fill terminal ENTRY;
        # missing history, any execution or an EXIT remains unknown/fail-closed.
        if self.gate.state is EngineState.DRAINING:
            await self._resolve_zero_fill_unacknowledged_entries()
        # Private disconnect can hide a fill. Check every pre-disconnect working
        # order against exact terminal history; cash equality alone is not proof.
        histories: dict[str, list[dict[str, Any]]] = {}
        proofs = []
        for order_id in tuple(self._private_recovery_orders):
            order = self.account.reducer.orders.get(order_id)
            if order is None:
                raise RuntimeError("recovery lost owned order evidence")
            if order.symbol not in histories:
                histories[order.symbol] = await self.rest.recent_order_history(symbol=order.symbol)
            matches = [row for row in histories[order.symbol] if str(row.get("orderId", "")) == order_id]
            if not matches:
                raise RecoveryPending("terminal cancellation history unavailable")
            if len(matches) != 1:
                raise RuntimeError("recovery history contains duplicate order identity")
            proofs.append((order,matches[0]))
        await self._recover_terminal_executions([row for _,row in proofs])
        for order,row in proofs:
            order_id = order.order_id
            status = terminal_history_status(order,row)
            order.status, order.remaining_quantity = status, Decimal("0")
            intent = self.ledger.get(order.client_order_id)
            if intent is not None and intent.state in {IntentState.SENT_UNKNOWN, IntentState.ACKNOWLEDGED}:
                self.ledger.mark_terminal(order.client_order_id)
            else:
                self.journal.resolve_intent_terminal(order.client_order_id, order_id)
            self.account.terminal_events.setdefault(order_id, asyncio.Event()).set()
        after = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        if before_version != self.account.fill_version or generation != self._stream_generation:
            raise RecoveryPending("fills changed during recovery proof")
        if after.active_orders or self.journal.nonterminal_sent_ids():
            raise RecoveryPending("recovery still awaits terminal owned orders")
        assert_position_consistency(before, self.account.reducer.positions)
        assert_position_consistency(after, self.account.reducer.positions)
        if not self.account.snapshots_fresh or not self.account.equity_classification_complete:
            raise RecoveryPending("recovery waits for exact cash and position classification")
        for symbol, signed in self.account.reducer.positions.items():
            outcome = "YES" if signed > 0 else "NO"
            if self.journal.get_deadline(symbol, outcome) is None:
                raise RuntimeError("recovered position lacks its original deadline")
        self._last_snapshot = after

    async def _resolve_zero_fill_unacknowledged_entries(self) -> None:
        histories: dict[str, list[dict[str, Any]]] = {}
        for client_id in self.journal.unresolved_ids():
            intent = self.ledger.get(client_id)
            if intent is None or intent.purpose is not OrderPurpose.ENTRY or intent.leg.side is not Side.BUY:
                raise RuntimeError('unacknowledged mutation is not a known maker entry')
            leg = intent.leg
            if leg.symbol not in histories:
                histories[leg.symbol] = await self.rest.recent_order_history(symbol=leg.symbol)
            matches = [row for row in histories[leg.symbol] if str(row.get('clientOrderId', '')) == client_id]
            if not matches:
                raise RecoveryPending('unacknowledged maker terminal history not published')
            if len(matches) != 1:
                raise RuntimeError('unacknowledged maker history is ambiguous')
            row = matches[0]
            identity = (str(row.get('symbol', '')).upper(), str(row.get('side', '')).upper(),
                        str(row.get('outcome', '')).upper(), Decimal(str(row.get('price'))),
                        Decimal(str(row.get('quantity'))))
            if identity != (leg.symbol, leg.side.value, leg.outcome.value, leg.price, leg.quantity):
                raise RuntimeError('unacknowledged maker history identity disagrees')
            status = str(row.get('status', '')).upper()
            if status not in {'CANCELLED', 'CANCELED', 'REJECTED'}:
                raise RecoveryPending('unacknowledged maker is not proven canceled')
            if Decimal(str(row.get('filledQuantity'))) != 0:
                raise RuntimeError('unacknowledged maker has missing execution evidence')
            order_id = '' if row.get('orderId') is None else str(row['orderId'])
            if not order_id or (intent.server_order_id is not None and intent.server_order_id != order_id):
                raise RuntimeError('unacknowledged maker lacks exact server identity')
            self.ledger.acknowledge(client_id, order_id)
            self.ledger.mark_terminal(client_id)
            self.account.events.setdefault(client_id, asyncio.Event()).set()
            self.log.event(logging.INFO, 'unacknowledged_maker_terminal_proven',
                           symbol=leg.symbol, executions=0, request_replayed=False)

    async def _prove_stream_recovery(self) -> None:
        try:
            proof_started: float | None = None
            quarantined_generation: dict[str, int] | None = None
            read_failures = 0
            async with asyncio.timeout(None if self._continuous_service else _STREAM_RECOVERY_SECONDS):
                while self._stream_holds:
                    if self._recovery_stop_requested():
                        raise RuntimeError("recovery proof interrupted by stop or expiry")
                    for task in self._stream_restarts.values():
                        if task.done():
                            task.result()
                    if self._continuous_service:
                        if self._stream_ready != {"public", "private"}:
                            proof_started = None
                        elif proof_started is None:
                            proof_started = time.monotonic()
                        elif time.monotonic() - proof_started >= _STREAM_RECOVERY_SECONDS:
                            raise RuntimeError("connected streams did not reconcile within proof budget")
                        if (quarantined_generation == self._stream_generation
                                and self._stream_ready != {"public", "private"}):
                            # Already proved/canceled owned entries while public
                            # transport is down. Do not poll REST every second.
                            await asyncio.sleep(1)
                            continue
                    # REST active-order evidence precedes cancellation: the old
                    # connection's cancel-on-disconnect may already have removed
                    # cached orders. Never blindly cancel that stale cache.
                    if "private" in self._stream_ready and self.account.snapshots_fresh and self.account.readiness.complete:
                        try:
                            generation = dict(self._stream_generation)
                            async with self._lifecycle_lock:
                                await self._recovery_account_proof()
                            quarantined_generation = generation
                            read_failures = 0
                        except RecoveryPending:
                            await asyncio.sleep(1)
                            continue
                        except ApiError as exc:
                            if not transient_failure(exc):
                                raise
                            if self._continuous_service:
                                proof_started = None  # Explicit venue/read outage, not mismatched truth.
                            read_failures += 1
                            for _ in range(min(60, 2 ** min(read_failures, 6))):
                                if self._recovery_stop_requested():
                                    raise RuntimeError("recovery proof interrupted by stop or expiry")
                                await asyncio.sleep(1)
                            continue
                        # No await between the final authority check and resume.
                        if (generation != self._stream_generation or self._stream_ready != {"public", "private"}
                                or (not self.universe and not self._continuous_service)
                                or not all(self._book_fresh(item.rules.symbol) for item in self.universe
                                           if not self._public_shard_held(item.rules.symbol))):
                            await asyncio.sleep(1)
                            continue
                        if self._recovery_stop_requested() or not self.account.snapshots_fresh or not self.account.equity_classification_complete:
                            raise RuntimeError("recovery authority changed before resume")
                        self._stream_holds.clear()
                        self._private_recovery_orders.clear()
                        self.log.event(logging.INFO, "stream_recovery_completed",
                                       duration_seconds=round(time.monotonic() - self._stream_recovery_started, 3),
                                       deadlines_preserved=True)
                        self._write_status("ready_paused" if self.read_only else "quoting", not self.read_only,
                                           "bounded stream recovery reconciled; original session unchanged")
                        return
                    await asyncio.sleep(.1)
        except Exception as exc:
            self.gate.halt()
            self.log.exception("stream_recovery_failed", ErrorClass.RECONCILIATION, exc)
            self._write_status("halted", False, "stream recovery could not prove safe resumption")
            raise

    def _defer_read_failure(self, component: str, exc: ApiError) -> float:
        """Recover only typed idempotent-read failures with healthy WS truth.

        No mutation retry, identity repair, credential bypass or reconnect is
        authorized here. Existing safety/exit workers and the permit clock run.
        """
        transient = transient_failure(exc) and (
            not isinstance(exc, RewardCatalogUnstable) or component == "reward_metadata"
        )
        if (not transient or self.gate.state is not EngineState.QUOTING
                or not self.account.snapshots_fresh or not self.account.equity_classification_complete):
            raise exc
        attempt = self._read_recovery_attempts.get(component, 0) + 1
        self._read_recovery_attempts[component] = attempt
        delay = max(retry_after_seconds(exc), min(60, 5 * 2 ** min(attempt - 1, 4)))
        self.log.event(logging.WARNING, "read_recovery_wait", component=component,
                       attempt=attempt, retry_seconds=delay, http_status=exc.status,
                       entries_paused=True, exits_enabled=True)
        return float(delay)

    def _reward_refresh_due(self, clock: float, next_check_at: float) -> bool:
        # Lifecycle/depth signals may request an early refresh, but cannot
        # defeat an outstanding metadata retry backoff. The symbol's existing
        # lifecycle block/cancellation still applies while waiting.
        return clock >= next_check_at or (
            self._reward_refresh_requested
            and "reward_metadata" not in self._read_recovery_attempts
        )

    def _complete_read_recovery(self, component: str) -> None:
        if self._read_recovery_attempts.pop(component, None) is not None:
            self.log.event(logging.INFO, "read_recovery_completed", component=component,
                           remaining_entry_holds=sorted(self._read_recovery_attempts))

    def _write_rank_report(self, now: datetime) -> None:
        rows = public_rank_rows(self.ranked)
        quality_policy = self.config.strategy.entry_quality
        quality_passed = quality_considered = 0
        otherwise_actionable = otherwise_actionable_passed = 0
        for candidate, row in zip(self.ranked, rows):
            if quality_policy is None:
                row["entry_quality"] = None
                continue
            quality_considered += 1
            book = self.public.books.get(candidate.symbol)
            quality = (evaluate_entry_quality(book, candidate.quote, quality_policy,
                       account=self.account.reducer)
                       if book is not None and candidate.quote is not None
                       and self._book_fresh(candidate.symbol) else None)
            passed = quality is not None and quality.allowed
            quality_passed += int(passed)
            row["entry_quality"] = {
                "passed": passed,
                "reason": quality.reason if quality is not None else "exit_book_unavailable",
                "yes_exit_depth_fraction": str(quality.yes.depth_fraction) if quality is not None else None,
                "no_exit_depth_fraction": str(quality.no.depth_fraction) if quality is not None else None,
                "yes_retained_value_fraction": str(quality.yes.retained_value_fraction) if quality is not None else None,
                "no_retained_value_fraction": str(quality.no.retained_value_fraction) if quality is not None else None,
            }
            concrete_ok = candidate.eligible_now or tuple(candidate.reasons) == (_UPTIME_PROXY_REJECTION,)
            if concrete_ok and self._entry_time_valid(candidate.symbol):
                otherwise_actionable += 1
                otherwise_actionable_passed += int(passed)
        identities = {item.event_ticker: item.event_id for item in self.universe}
        tickers_by_id: dict[str, set[str]] = {}
        for ticker, raw_id in identities.items():
            identity = event_identity(raw_id)
            if identity is not None:
                tickers_by_id.setdefault(identity, set()).add(ticker)
        views = {
            ticker: self.exchange_reward_uptime.event_view(
                identity if len(tickers_by_id.get(event_identity(identity) or "", set())) == 1 else None,
                now=now,
            )
            for ticker, identity in identities.items()
        }
        for row in rows:
            row["exchange_reported_uptime"] = views.get(str(row["event_ticker"]))
            identity = event_identity(row.get("event_id"))
            if identity is not None and len(tickers_by_id.get(identity, set())) != 1:
                identity = None
            row["qualification_estimate"] = estimate_50_percent(
                identity,
                self._uptime.get(uptime_progress_key(
                    str(row["event_ticker"]), str(row["pool_id"]),
                )),
                self.exchange_reward_uptime.reward_results, now=now,
            )
            row["qualification_estimate"]["local_minutes_used_for_selected_funding_priority"] = self._continuous_service
        atomic_write_json(self.directory / "rank-report.json", {
            "generated_at": now.isoformat(), "candidates": rows,
            "entry_quality_summary": ({
                "ranked_passed": quality_passed,
                "ranked_considered": quality_considered,
                "otherwise_actionable_passed": otherwise_actionable_passed,
                "otherwise_actionable_considered": otherwise_actionable,
                "as_of": now.isoformat(),
                "point_in_time_not_reward_qualification": True,
            } if quality_policy is not None else None),
            "queue_shadow": self._queue_shadow_report(now, rows),
            "queue_pilot": self.queue_pilot.report() if self.queue_pilot else {"configured": False},
            "discovery_ranking": {
                "version": DISCOVERY_RANKING_VERSION,
                "method": "daily_budget_divided_by_sqrt_estimated_pool_breadth",
                "scope": "new_candidates_not_forced_incumbent_rotation",
                "is_payout_estimate": False,
            },
            "exchange_reward_uptime": self.exchange_reward_uptime.metadata(now=now),
            "exchange_uptime_by_event": views,
            "selected_portfolio_model": getattr(self, "_selected_portfolio_report", None),
            "exchange_reward_results": self.exchange_reward_uptime.reward_results,
            "reward_focus_calibration": (self._reward_focus.report()
                if hasattr(self, '_reward_focus') else None),
            "exchange_reward_results_stale": self.exchange_reward_uptime.metadata(now=now)["stale"],
            "reward_history_scope": "last_14_reported_dates_not_proof_of_continuous_coverage",
            "reward_history_incomplete": self.exchange_reward_uptime.history_incomplete,
        })

    async def _reward_reporting_loop(self) -> None:
        # Reporting errors only change availability; never cancel quotes or
        # rewrite local feasibility, risk, or non-trading-cash classification.
        while not self._closed:
            refreshed = await self.exchange_reward_uptime.refresh(self.reward_reporting_rest, now=_utc_now())
            if refreshed:
                self.journal.set_runtime_state(
                    "reward_results_v1", self.exchange_reward_uptime.reward_history_payload(),
                    updated_at=_utc_now(),
                )
            self.log.event(
                logging.WARNING if self.exchange_reward_uptime.error else logging.INFO,
                "reward_uptime_report_refresh",
                fetch_status="failed" if self.exchange_reward_uptime.error else "ok",
                reported_event_dates=len(self.exchange_reward_uptime.rows),
            )
            await asyncio.sleep(REFRESH_SECONDS)

    def _handle_contract_status(self, frame: dict[str, Any]) -> None:
        symbol = str(frame.get("s", "")).upper()
        if symbol not in self._expiry:
            return
        self._market_status_versions[symbol] = self._market_status_versions.get(symbol, 0) + 1
        self._market_status[symbol] = str(frame.get("n", "")).lower()
        if self._market_status[symbol] == "active":
            return  # Metadata and terminal/flat proof, not this event, release it.
        self.account.block_entries(symbol, reason="lifecycle")
        self._reward_refresh_requested = True
        signed = self.account.reducer.positions.get(symbol, Decimal("0"))
        self.journal.audit("contract_lifecycle_block", {
            "symbol": symbol,
            "status": self._market_status[symbol],
            "has_inventory": signed != 0,
        })
        if signed:
            outcome = "YES" if signed > 0 else "NO"
            isolatable = (
                self._continuous_service
                and self.gate.state is EngineState.QUOTING
                and self.account.snapshots_fresh
                and self.account.equity_classification_complete
                and self.journal.get_deadline(symbol, outcome) is not None
                and symbol not in self._exit_uncertain
                and not self.journal.pending_exit(symbol)
                and not self.journal.has_nonterminal_intents(symbol)
            )
            if not isolatable:
                raise RuntimeError(
                    "tracked contract changed lifecycle without complete scoped inventory proof"
                )
            # A terminal/paused contract commonly loses executable depth before
            # Gemini publishes settlement.  Preserve the exact position and its
            # original deadline, keep its exit/settlement worker alive, and let
            # unrelated shards continue.  Account-wide ambiguity still takes
            # the fail-closed branch above.
            self.account.block_entries(symbol, fill=True)
            self.log.event(
                logging.WARNING,
                "market_lifecycle_inventory_quarantined",
                symbol=symbol,
                lifecycle_status=self._market_status[symbol],
                unrelated_markets_allowed=True,
                pending_order_slots=0,
                original_deadline_preserved=True,
            )
        if self.account.reducer.risk_adding_order_ids(symbol):
            if self.gate.state not in {EngineState.QUOTING, EngineState.DRAINING}:
                raise RuntimeError("tracked contract changed lifecycle with working entries")
            try:
                self.account.safety_symbols.put_nowait(symbol)
            except asyncio.QueueFull as exc:
                raise RuntimeError("contract lifecycle safety queue is full") from exc

    async def _public_pump(self) -> None:
        try:
            frames = self.public.frames()
            generation = self._stream_generation["public"]
            while True:
                try:
                    frame = await anext(frames)
                except ApiError as exc:
                    if not transient_failure(exc):
                        raise
                    await self._recover_stream("public", exc, generation=generation)
                    frames, generation = self.public.frames(), self._stream_generation["public"]
                    continue
                except (StopAsyncIteration, ConnectionClosed, ConnectionError, TimeoutError, OSError) as exc:
                    await self._recover_stream("public", exc, generation=generation)
                    frames, generation = self.public.frames(), self._stream_generation["public"]
                    continue
                if generation != self._stream_generation["public"]:
                    await self._recover_stream("public", ConnectionError("obsolete stream"), generation=generation)
                    frames, generation = self.public.frames(), self._stream_generation["public"]
                    continue
                shard_event = frame if isinstance(frame, ShardEvent) else None
                if shard_event is not None:
                    if not self.public.accepts(shard_event):
                        continue
                    if shard_event.error is not None:
                        if not (transient_failure(shard_event.error) or isinstance(shard_event.error, OSError)):
                            raise shard_event.error
                        self._begin_shard_recovery(shard_event)
                        continue
                    if not self.public.owns_frame(shard_event):
                        continue
                    frame = shard_event.frame or {}
                observed_at = _utc_now()
                if frame.get("e") == "contractStatus":
                    self._handle_contract_status(frame)
                if (all(key in frame for key in ("s", "t", "p", "q", "m"))
                        and str(frame["s"]).upper() in self.public.books):
                    self.profitability.observe_trade(frame, observed_at=observed_at)
                    self._observe_queue_shadow(str(frame["s"]).upper(), frame=frame)
                updated_symbol = self.public.apply_frame(shard_event if shard_event is not None else frame)
                if (
                    frame.get("e") == "depthUpdate"
                    and updated_symbol is not None
                    and updated_symbol not in self.public.resync_required
                ):
                    symbol = str(frame.get("s", "")).upper()
                    book = self.public.books.get(symbol)
                    if book is not None:
                        self.profitability.observe_book(book, observed_at=observed_at)
                        self._observe_queue_shadow(symbol)
                self._mark_live_risk()
                if updated_symbol is not None and updated_symbol in self.public.resync_required:
                    if shard_event is not None:
                        self._begin_shard_recovery(ShardEvent(shard_event.shard, shard_event.generation,
                                                            error=ConnectionError('depth sequence gap')))
                        continue
                    await self._recover_stream("public", ConnectionError("depth sequence gap"), generation=generation)
                    frames, generation = self.public.frames(), self._stream_generation["public"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gate.halt()
            self.log.exception("public_stream_failed", ErrorClass.TRANSPORT, exc)
            self._write_status("halted", False, "public stream lost; approval is required after reconciliation")
            raise

    async def _public_liveness_pump(self) -> None:
        """Prove quiet public transport with WebSocket ping/pong once per second."""
        try:
            while True:
                await asyncio.sleep(1)
                if "public" not in self._stream_ready:
                    continue
                generation = self._stream_generation["public"]
                try:
                    await self.public.prove_liveness(timeout=2)
                except (ConnectionClosed, ConnectionError, TimeoutError, OSError) as exc:
                    await self._recover_stream("public", exc, generation=generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gate.halt()
            self.log.exception("public_liveness_failed", ErrorClass.TRANSPORT, exc)
            self._write_status("halted", False, "public liveness failed; reconciliation required")
            raise

    async def _private_pump(self) -> None:
        try:
            frames = self.private.messages()
            generation = self._stream_generation["private"]
            while True:
                try:
                    frame = await anext(frames)
                except ApiError as exc:
                    if not transient_failure(exc):
                        raise
                    await self._recover_stream("private", exc, generation=generation)
                    frames, generation = self.private.messages(), self._stream_generation["private"]
                    continue
                except (StopAsyncIteration, ConnectionClosed, ConnectionError, TimeoutError, OSError) as exc:
                    await self._recover_stream("private", exc, generation=generation)
                    frames, generation = self.private.messages(), self._stream_generation["private"]
                    continue
                if generation != self._stream_generation["private"]:
                    await self._recover_stream("private", ConnectionError("obsolete stream"), generation=generation)
                    frames, generation = self.private.messages(), self._stream_generation["private"]
                    continue
                try:
                    self.account.apply(frame)
                except MissingExecutionEvidence as exc:
                    if self.gate.state is not EngineState.QUOTING and not (
                        self.gate.state is EngineState.DRAINING and self._stream_holds
                    ):
                        raise
                    self._private_recovery_orders.update(self.account.missing_execution_orders)
                    if not self._stream_holds:
                        await self._recover_stream('private', exc, generation=generation)
                        frames, generation = self.private.messages(), self._stream_generation['private']
                    continue
                self._mark_live_risk()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, WebSocketRequestRejected):
                self._private_request_failure = exc
            self.gate.halt()
            details = ({'method': exc.method, 'status': exc.status, 'code': exc.code}
                       if isinstance(exc, WebSocketRequestRejected) else {})
            self.log.exception("private_stream_failed", ErrorClass.TRANSPORT, exc, **details)
            self._write_status("halted", False, "account stream lost; orders cancel on disconnect")
            raise

    async def _wait_ready(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for task in self._tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("stream ended during reconciliation")
            books_initialized = bool(self.universe) and all(
                (book := self.public.books.get(item.rules.symbol)) is not None
                and book.synchronized
                for item in self.universe
            )
            usable_book_exists = any(
                (book := self.public.books.get(item.rules.symbol)) is not None
                and book.synchronized and book.top() is not None
                and self._entry_time_valid(item.rules.symbol)
                for item in self.universe
            )
            if (
                self.account.readiness.complete
                and books_initialized
                and usable_book_exists
                and self._public_stream_fresh()
            ):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("account/depth startup snapshots did not become ready")

    async def _authenticated_nontrading_cash(
        self, *, observed_at: datetime,
    ) -> tuple[AuthenticatedCashAdjustment, ...]:
        """Read authenticated account activity that can explain a cash delta."""
        eastern_day = observed_at.astimezone(ZoneInfo("America/New_York")).date()
        watermark = self.journal.get_runtime_state_record("cash_reconciliation_watermark")
        if watermark is None:
            watermark = self.journal.get_runtime_state_record("expected_confirmed_usd")
        if watermark is None:
            raise RuntimeError("non-trading cash has no durable reconciliation boundary")
        maker_rows, liquidity_rows, transfer_rows = await asyncio.gather(
            self.rest.maker_rebate_payouts(limit=100, offset=0),
            self.rest.liquidity_reward_daily(
                date_from=(eastern_day - timedelta(days=7)).isoformat(),
                date_to=eastern_day.isoformat(),
            ),
            self.rest.past_transfers(since=watermark[1]),
        )
        credits: list[AuthenticatedCashAdjustment] = []
        for row in maker_rows:
            if str(row.get("status", "")).upper() != "PAID":
                continue
            try:
                if row.get("id") in {None, ""}:
                    raise ValueError("missing payout id")
                amount = Decimal(str(row["total_rebate_usd"]))
                if not amount.is_finite() or amount < 0:
                    raise ValueError("invalid maker-rebate amount")
                if amount == 0:
                    continue
                credit = AuthenticatedCashAdjustment(
                    source="maker_rebate",
                    adjustment_id=str(row["id"]),
                    amount=amount,
                    occurred_at=_parse_time(str(row["paid_at"])),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("authenticated maker-rebate payout is invalid") from exc
            credits.append(credit)
        for row in liquidity_rows:
            if str(row.get("payout_status", "")).upper() != "PAID":
                continue
            try:
                if row.get("payout_date") in {None, ""}:
                    raise ValueError("missing payout date")
                amount = Decimal(str(row["total_reward_usd"]))
                if not amount.is_finite() or amount < 0:
                    raise ValueError("invalid liquidity-reward amount")
                if amount == 0:
                    continue
                credit = AuthenticatedCashAdjustment(
                    source="liquidity_reward",
                    adjustment_id=str(row["payout_date"]),
                    amount=amount,
                    occurred_at=_parse_time(str(row["paid_at"])),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("authenticated liquidity-reward payout is invalid") from exc
            credits.append(credit)
        for row in transfer_rows:
            transfer_type = str(row.get("type", "")).upper()
            if transfer_type in {"REWARD", "ADMINDEBIT", "ADMINCREDIT"}:
                # Prediction reward/rebate endpoints provide the authoritative
                # detail. Do not double-count their aggregate transfer row.
                continue
            if transfer_type not in {"DEPOSIT", "WITHDRAWAL"}:
                raise RuntimeError("authenticated transfer has an unknown type")
            status = str(row.get("status", "")).upper()
            if status == "PENDING":
                continue
            valid_statuses = (
                {"COMPLETE", "ADVANCED"}
                if transfer_type == "DEPOSIT" else {"COMPLETE"}
            )
            if status not in valid_statuses:
                raise RuntimeError("authenticated transfer has an unknown status")
            if str(row.get("currency", "")).upper() != "USD":
                continue
            try:
                if row.get("eid") in {None, ""}:
                    raise ValueError("missing transfer id")
                amount = Decimal(str(row["amount"]))
                fee = Decimal(str(row.get("feeAmount", "0")))
                timestamp_ms = int(row["timestampms"])
                if (
                    not amount.is_finite() or amount <= 0
                    or not fee.is_finite() or fee < 0 or timestamp_ms <= 0
                ):
                    raise ValueError("invalid transfer economics")
                if fee and str(row.get("feeCurrency", "")).upper() != "USD":
                    raise ValueError("non-USD transfer fee cannot explain USD cash")
                signed = amount if transfer_type == "DEPOSIT" else -(amount + fee)
                credit = AuthenticatedCashAdjustment(
                    source=transfer_type.lower(),
                    adjustment_id=str(row["eid"]),
                    amount=signed,
                    occurred_at=datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("authenticated transfer is invalid") from exc
            credits.append(credit)
        return tuple(credits)

    async def _reconcile_authenticated_nontrading_cash(self) -> int:
        if self.account.equity_classification_complete:
            return 0
        observed_at = _utc_now()
        credits = await self._authenticated_nontrading_cash(observed_at=observed_at)
        count = self.account.reconcile_authenticated_cash_adjustments(
            credits, observed_at=observed_at,
        )
        if not self.account.equity_classification_complete:
            raise RuntimeError("authenticated non-trading cash did not complete classification")
        return count

    def _event_ticker_for_symbol(self, symbol: str) -> str:
        target = canonical_symbol(symbol)
        matches = {
            item.event_ticker for item in self.universe
            if item.rules.symbol == target
        }
        matches.update(
            event_ticker for event_ticker, sticky_symbol in self._sticky_markets.values()
            if canonical_symbol(sticky_symbol) == target
        )
        if len(matches) != 1:
            raise RuntimeError("settled position lacks one exact event identity")
        return next(iter(matches))

    @staticmethod
    def _parse_authenticated_settlement(
        row: dict[str, Any], *, symbol: str, signed_quantity: Decimal,
    ) -> AuthenticatedSettlement | None:
        row_symbol = canonical_symbol(str(row.get("instrumentSymbol", row.get("symbol", ""))))
        if row_symbol != symbol:
            return None
        try:
            outcome = str(row["outcome"]).upper()
            quantity = Decimal(str(row["positionQuantity"]))
            position = Decimal(str(row["position"]))
            payout = Decimal(str(row["payout"]))
            settled_at = history_timestamp(row["settledAt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("authenticated settled-position row is invalid") from exc
        derived = quantity if outcome == "YES" else -quantity if outcome == "NO" else None
        if (
            derived is None
            or not all(value.is_finite() for value in (quantity, position, payout))
            or quantity <= 0
            or position != derived
            or position != signed_quantity
            or payout < 0
            or payout > quantity
        ):
            raise RuntimeError("authenticated settlement disagrees with owned inventory")
        return AuthenticatedSettlement(symbol, position, payout, settled_at)

    async def _reconcile_missed_settlements(
        self, snapshot: AccountSnapshot | None = None,
    ) -> int:
        """Close positions absent from live state only with exact settled REST proof."""
        if self.account.expected_positions is None:
            raise RuntimeError("settlement reconciliation lacks expected positions")
        expected = dict(self.account.expected_positions)
        actual = dict(self.account.reducer.positions)
        missing = {
            symbol: quantity for symbol, quantity in expected.items()
            if actual.get(symbol, Decimal("0")) == 0
        }
        if not missing:
            return 0
        if any(actual.get(symbol) != quantity for symbol, quantity in expected.items() if symbol in actual):
            raise RuntimeError("position change is not an exact settlement candidate")
        if set(actual) - set(expected):
            raise RuntimeError("unexpected position cannot be settlement-reconciled")

        before = snapshot or build_account_snapshot(
            await self.rest.positions(), await self.rest.active_orders(),
        )
        if before.signed_positions != actual:
            raise RuntimeError("settlement reconciliation lacks matching REST/stream state")
        plans: list[AuthenticatedSettlement] = []
        for symbol, signed in sorted(missing.items()):
            if any(order.symbol == symbol for order in before.active_orders):
                raise RuntimeError("settlement candidate still has an active order")
            ticker = self._event_ticker_for_symbol(symbol)
            rows = await self.rest.settled_positions(event_ticker=ticker)
            matches = [
                parsed for row in rows
                if (parsed := self._parse_authenticated_settlement(
                    row, symbol=symbol, signed_quantity=signed,
                )) is not None
            ]
            if len(matches) != 1:
                raise RuntimeError("settled-position history lacks one exact owned match")
            plans.append(matches[0])

        after = build_account_snapshot(
            await self.rest.positions(), await self.rest.active_orders(),
        )
        assert_stable_account_bracket(before, after)
        assert_position_consistency(after, self.account.reducer.positions)
        observed_at = _utc_now()
        count = 0
        for settlement in plans:
            count += int(self.account.reconcile_authenticated_settlement(
                settlement, observed_at=observed_at,
            ))
            self._inventory_catalog_holds.discard(settlement.symbol)
            self._exit_uncertain.discard(settlement.symbol)
            self._exit_rejected.discard(settlement.symbol)
            self._exit_residual_limits.pop(settlement.symbol, None)
            self._exit_round_deadlines.pop(settlement.symbol, None)
            self._position_proof_wait_started.pop(settlement.symbol, None)
        return count

    def _resolve_prior_intents(self, history: list[dict[str, Any]], active: list[dict[str, Any]] | None = None) -> None:
        by_client = {str(row.get("clientOrderId", "")): row for row in history}
        working = {str(row.get("clientOrderId", "")): row for row in active or []}
        for client_id in self.journal.nonterminal_sent_ids():
            if client_id in working:
                row = working[client_id]
                self.journal.reconcile_working_intent(client_id, str(row.get("orderId", "")))
                continue
            row = by_client.get(client_id)
            if row is None:
                raise RuntimeError("an earlier mutation outcome remains unknown")
            status = str(row.get("status", "")).upper().replace("CANCELLED", "CANCELED")
            if status not in {"FILLED", "CANCELED", "REJECTED"}:
                raise RuntimeError("an earlier mutation is not terminal")
            self.journal.resolve_intent_terminal(client_id, str(row.get("orderId", "")) or None)

    async def _recover_terminal_executions(self, rows: list[dict[str, Any]]) -> int:
        missing = []
        for row in rows:
            if not self.journal.knows_client_order_id(str(row.get('clientOrderId',''))):
                continue
            oid = str(row.get('orderId',''))
            recorded = sum((Decimal(r[0]) for r in self.journal.connection.execute(
                'SELECT quantity FROM execution_fill WHERE order_id=?',(oid,))), Decimal(0))
            executed = Decimal(str(row.get('filledQuantity','0')))
            if not executed.is_finite() or executed < recorded:
                raise RuntimeError('historical execution evidence regressed')
            if executed > recorded or self.journal.get_runtime_state('maker_history_terminal:' + oid):
                if not all(row.get(k) is not None for k in ('createdAt','updatedAt','price','quantity','side','outcome')):
                    raise RuntimeError('terminal execution evidence is incomplete')
                missing.append(row)
        if not missing:
            return 0
        by_symbol: dict[str, int] = {}
        for row in missing:
            symbol = str(row['symbol']).upper()
            since = int(history_timestamp(row['createdAt']).timestamp()*1000)
            by_symbol[symbol] = min(since,by_symbol.get(symbol,since))
        trades = []
        for symbol,since in sorted(by_symbol.items()):
            trades.extend(await self.rest.recent_account_trades(symbol=symbol,since_ms=since))
        return import_terminal_trades(self.account,missing,trades,now=_utc_now())

    async def _targeted_prior_intent_history(
        self, active_orders: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Read history only for unresolved symbols absent from active orders."""
        active_client_ids = {
            str(row.get("clientOrderId", "")) for row in active_orders
        }
        unresolved_by_symbol: dict[str, set[str]] = {}
        for client_id, symbol in self.journal.nonterminal_sent_intents():
            if client_id not in active_client_ids:
                unresolved_by_symbol.setdefault(symbol, set()).add(client_id)
        history: list[dict[str, Any]] = []
        for symbol in sorted(unresolved_by_symbol):
            rows = await self.rest.recent_order_history(symbol=symbol)
            if len(rows) >= 1000:
                # An absent ID on a full newest page is not proof that an older
                # mutation never existed or became terminal.
                found = {str(row.get("clientOrderId", "")) for row in rows}
                if not unresolved_by_symbol[symbol] <= found:
                    raise RuntimeError(
                        "symbol order history is full and cannot resolve prior intent"
                    )
            history.extend(rows)
        return history

    async def reconcile_observed_external_reductions(self) -> int:
        """Explicit operator repair of exact trade evidence, without a baseline reset."""
        if not self.read_only or not self.account.readiness.complete or not self.account.snapshots_fresh:
            raise RuntimeError('operator execution repair requires fresh read-only account streams')
        before = build_account_snapshot(await self.rest.positions(),await self.rest.active_orders())
        if before.active_orders or self.journal.unresolved_ids():
            raise RuntimeError('operator execution repair requires no active or unknown orders')
        assert_position_consistency(before,self.account.reducer.positions)
        boundaries = {str(s): history_timestamp(t) for s,t in self.journal.connection.execute(
            'SELECT symbol,MIN(opened_at) FROM position_deadline GROUP BY symbol')}
        if not (set(before.signed_positions) | set(self.account.expected_positions or {})) <= boundaries.keys():
            raise RuntimeError('inventory without owned fill history cannot be repaired')
        if len(boundaries) > self.config.risk.max_tracked_markets:
            raise RuntimeError('execution recovery symbol bound exceeded')
        trades, history = [], []
        for symbol,begin in sorted(boundaries.items()):
            rows = await self.rest.recent_account_trades(symbol=symbol,since_ms=int(begin.timestamp()*1000)-1000)
            orders = await self.rest.recent_order_history(symbol=symbol)
            ids = {str(t.get('order_id','')) for t in rows}
            matched = [row for row in orders if str(row.get('orderId','')) in ids]
            if len(matched) != len(ids):
                raise RuntimeError('exact terminal order evidence for operator repair is incomplete')
            trades.extend(rows)
            history.extend(matched)
        after = build_account_snapshot(await self.rest.positions(),await self.rest.active_orders())
        assert_stable_account_bracket(before,after)
        assert_position_consistency(after,self.account.reducer.positions)
        count = import_terminal_trades(self.account,history,trades,now=_utc_now(),allow_external_reductions=True)
        if not self.account.equity_classification_complete:
            await self._reconcile_authenticated_nontrading_cash()
        if not self.account.equity_classification_complete or not self.account.snapshots_fresh:
            raise RuntimeError('execution history did not prove current cash and positions')
        self.journal.replace_account_snapshot([r.as_storage() for r in after.positions],
            [r.as_storage() for r in after.active_orders],observed_at=_utc_now())
        for symbol in tuple(self.account.fill_blocked_entries):
            if not self.account.reducer.positions.get(symbol):
                await self._complete_flat_fill(symbol)
        await self._risk_mark()
        self._write_status('reconciled_paused',False,'authenticated execution history reconciled; no activation')
        return count

    async def prepare(self, *, timeout: float = 30, manual_flat_repair: bool = False) -> PreparedState:
        """Read-only startup. This method contains no order mutation call."""
        if manual_flat_repair and not self.read_only:
            raise RuntimeError("manual-flat preparation requires read-only mode")
        self.lock.acquire()
        # All output directories share one durable mutation/permit/risk history.
        canonical_path = self.control_directory / "production.sqlite"
        if canonical_path.exists():
            await asyncio.to_thread(verify_journal, canonical_path, required_tables=frozenset({
                "order_intent", "runtime_state", "execution_fill", "position_deadline",
                "realized_risk_window_state",
            }))
        canonical = SQLiteJournal(self.control_directory / "production.sqlite")
        identity = hashlib.sha256(self.config.api_key.encode()).hexdigest()
        previous_identity = canonical.get_runtime_state("credential_identity")
        if previous_identity not in {None, identity}:
            canonical.close()
            self.lock.release()
            raise RuntimeError("credential change requires explicit journal reconciliation")
        if self.journal.connection.execute("SELECT 1 FROM order_intent LIMIT 1").fetchone():
            canonical.close()
            self.lock.release()
            raise RuntimeError("legacy runtime journal must be migrated before startup")
        canonical.set_runtime_state("credential_identity", identity, updated_at=_utc_now())
        self.journal.close()
        self.journal = canonical
        self._restore_adaptive_breach()
        if self._journal_backups is None:
            self._journal_backups = JournalBackups(canonical_path)
            self._journal_backups.start()
        self.exchange_reward_uptime.restore_reward_history(
            canonical.get_runtime_state("reward_results_v1"), now=_utc_now(),
        )
        self.ledger = IntentLedger(canonical)
        self.private._ledger = self.ledger
        self.account = AccountRuntime(self.ledger, canonical)
        # Preserve event-level continuity when startup replaces the account.
        self.account.event_sink = self._account_event
        self.account.inventory_age_seconds = self.config.risk.max_inventory_age_seconds
        self.account.exit_before_expiry_seconds = self.config.risk.force_exit_before_expiry_seconds
        self.log = OperationalLogger(self._logging, canonical)
        saved_profitability = canonical.get_runtime_state("profitability_telemetry_v1")
        if saved_profitability is not None:
            self.profitability.loads(saved_profitability, now=_utc_now())
        self.gate.begin_reconciliation()
        self._write_status("reconciling", False, "read-only startup")
        try:
            terms, positions_before, orders_before = await asyncio.gather(
                self.rest.terms_status(), self.rest.positions(), self.rest.active_orders(),
            )
            if not terms.get("hasAcceptedLatest"):
                raise RuntimeError("latest Gemini Predictions terms are not accepted")
            reward_config = await self.rest.reward_config()
            if reward_config.get("enabled") is not True:
                raise NoMarketsAvailable("Gemini liquidity rewards are not currently enabled")
            if hasattr(self.rest, "maker_rebate_rates"):
                try:
                    rebate_rows = await self.rest.maker_rebate_rates()
                except ApiError:
                    self.journal.audit("maker_rebate_rates_unavailable", {})
                else:
                    self._maker_rebate_fractions = active_maker_rebate_fractions(
                        rebate_rows, now=_utc_now(),
                    )
            history = await self._targeted_prior_intent_history(orders_before)
            self._resolve_prior_intents(history, orders_before)
            before = build_account_snapshot(positions_before, orders_before)
            for order in before.active_orders:
                if not order.client_order_id or not self.journal.knows_client_order_id(order.client_order_id):
                    raise ExternalOrderPresent("an unowned active order blocks production startup")

            self._load_sticky_markets()
            # Inventory is itself a tracked owner. Reserve catalog capacity so
            # a temporarily omitted held symbol can be restored later without
            # exceeding the reviewed 32-owner/public-subscription bound.
            discovery_capacity = max(
                1,
                self.config.risk.max_tracked_markets - len(before.positions),
            )
            self.universe = await RewardUniverseLoader(
                self.rest, self._record_missing_reward_event, self._crypto_entry_cutoff_seconds(),
            ).load(
                maximum_pools=discovery_capacity,
                preferred=self._sticky_markets, now=_utc_now(),
                quantity=self.config.strategy.quote_quantity,
                offset_ticks=self.config.strategy.quote_offset_ticks,
            )
            if not self.universe:
                raise NoMarketsAvailable("no active reward pools passed discovery")
            self._sync_quote_asset_precisions(self.universe)
            self._expiry = {item.rules.symbol: _parse_time(item.expiry) for item in self.universe}
            self._trade_start = {
                item.rules.symbol: _parse_time(item.trade_starts_at)
                for item in self.universe if item.trade_starts_at
            }
            self._sports_start = {
                item.rules.symbol: _parse_time(item.event_starts_at)
                for item in self.universe
                if item.event_category.casefold() == "sports" and item.event_starts_at
            }
            self.account.hard_exit_cutoff = {
                symbol: starts_at - timedelta(
                    seconds=self.config.risk.sports_exit_before_start_seconds
                )
                for symbol, starts_at in self._sports_start.items()
            }
            self.account.expiry = self._expiry
            missing_inventory = {
                row.symbol for row in before.positions if row.symbol not in self._expiry
            }
            if missing_inventory:
                sticky_symbols = {
                    symbol for _, symbol in self._sticky_markets.values()
                }
                active_order_symbols = {row.symbol for row in before.active_orders}
                safe_holds = {
                    row.symbol for row in before.positions
                    if (
                        row.symbol in missing_inventory
                        and row.symbol in sticky_symbols
                        and row.symbol not in active_order_symbols
                        and not self.journal.pending_exit(row.symbol)
                        and not self.journal.has_nonterminal_intents(row.symbol)
                        and self.journal.get_deadline(row.symbol, row.outcome.value) is not None
                    )
                }
                if safe_holds != missing_inventory:
                    # Unknown ownership, deadline or mutation evidence remains
                    # a global pre-activation blocker. Only the exact durable
                    # incumbent case is safe to quarantine by market.
                    raise InventoryMarketUnavailable(
                        "held inventory market is absent without complete scoped ownership proof"
                    )
                self._inventory_catalog_holds = safe_holds
                for symbol in safe_holds:
                    self.account.block_entries(symbol, fill=True)
                self.log.event(
                    logging.WARNING,
                    "inventory_catalog_quarantined",
                    symbols=sorted(safe_holds),
                    unrelated_markets_allowed=True,
                    pending_order_slots=0,
                )
            # Seed identity before sparse terminal WebSocket updates can arrive.
            for row in before.active_orders:
                self.account.reducer.apply_order_update({
                    "e": "orderUpdate", "i": row.order_id, "c": row.client_order_id,
                    "s": row.symbol, "S": row.side.value, "O": row.outcome.value,
                    "q": str(row.original_quantity), "z": str(row.remaining_quantity),
                    "p": str(row.price), "X": "OPEN",
                })
            if isinstance(self.public, PublicMarketShards):
                self.public.restore_assignments(self.journal.get_runtime_state('public_shards_v1'))
                current_symbols = {c.rules.symbol for c in self.universe}
                self.public.assignments = {s: i for s, i in self.public.assignments.items() if s in current_symbols}
            await self.public.connect()
            await self.private.connect()
            self._tasks = [
                asyncio.create_task(self._public_pump(), name="gemini-public-pump"),
                asyncio.create_task(self._private_pump(), name="gemini-account-pump"),
            ]
            if hasattr(self.public, "prove_liveness"):
                self._tasks.append(asyncio.create_task(
                    self._public_liveness_pump(), name="gemini-public-liveness",
                ))
            await self.public.subscribe(tuple(item.rules.symbol for item in self.universe))
            self._save_shard_assignments()
            await self.private.subscribe_account()
            await self._wait_ready(timeout)

            positions_after, orders_after = await asyncio.gather(
                self.rest.positions(), self.rest.active_orders()
            )
            after = build_account_snapshot(positions_after, orders_after)
            assert_stable_account_bracket(before, after)
            assert_position_consistency(after, self.account.reducer.positions)
            if not self.account.equity_classification_complete:
                self.account.reconcile_legacy_quote_precision_fills(observed_at=_utc_now())
            if not self.account.equity_classification_complete:
                self.account.reconcile_legacy_collateral_exit_rounding(observed_at=_utc_now())
            repair_flat = manual_flat_repair and not after.positions and not after.active_orders
            if (
                self.account.expected_positions != self.account.reducer.positions
                and not repair_flat
            ):
                await self._reconcile_missed_settlements(after)
            if not self.account.equity_classification_complete and not repair_flat:
                targets: dict[str, set[str]] = {}
                for oid, symbol in self.journal.connection.execute('SELECT order_id,symbol FROM account_order'):
                    targets.setdefault(str(symbol), set()).add(str(oid))
                if len(targets) > self.config.risk.max_tracked_markets:
                    raise RuntimeError('execution recovery symbol bound exceeded')
                recovered_history = {str(row.get('orderId', '')): row for row in history}
                for symbol in sorted(targets):
                    rows = await self.rest.recent_order_history(symbol=symbol)
                    found = {str(row.get('orderId', '')) for row in rows}
                    active_ids = {order.order_id for order in after.active_orders}
                    if len(rows) >= 1000 and not targets[symbol] <= found | active_ids:
                        raise RuntimeError('execution recovery history is truncated')
                    recovered_history.update({str(row.get('orderId', '')): row for row in rows
                                              if str(row.get('orderId', '')) in targets[symbol]})
                await self._recover_terminal_executions(list(recovered_history.values()))
                refreshed = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
                assert_stable_account_bracket(after, refreshed)
                assert_position_consistency(refreshed, self.account.reducer.positions)
                after = refreshed
            if manual_flat_repair and not repair_flat:
                raise RuntimeError("manual-flat preparation requires zero positions and orders")
            if not self.account.equity_classification_complete and not repair_flat:
                await self._reconcile_authenticated_nontrading_cash()
            if not self.account.equity_classification_complete and not repair_flat:
                raise RuntimeError("startup cash/position classification is incomplete")
            self.journal.replace_account_snapshot(
                [row.as_storage() for row in after.positions],
                [row.as_storage() for row in after.active_orders],
                observed_at=_utc_now(),
            )
            self._last_snapshot = after
            now = _utc_now()
            self._uptime = sample_and_load_progress(
                self.journal, self.universe, self.account.reducer, now=now,
                books=self.public.books,
                market_data_verified_at=self._market_data_verified_at(),
                exit_before_expiry_seconds=self.config.risk.force_exit_before_expiry_seconds,
                sports_exit_before_start_seconds=self.config.risk.sports_exit_before_start_seconds,
                crypto_entry_before_expiry_seconds=self._crypto_entry_cutoff_seconds(),
            )
            self.ranked = self._rank_live_universe(
                self.universe, self.public.books, now=now,
                quantity=self.config.strategy.quote_quantity,
                offset_ticks=self.config.strategy.quote_offset_ticks,
                uptime=self._uptime,
                account=self.account.reducer,
                market_data_verified_at=self._market_data_verified_at(),
                profitability=self.profitability,
                target_uptime=self.config.strategy.target_uptime,
                maker_rebate_fractions=self._maker_rebate_fractions,
                allow_uptime_override=True,
            )
            self.journal.set_runtime_state(
                "profitability_telemetry_v1",
                self.profitability.dumps(now=now),
                updated_at=now,
            )
            self._write_rank_report(now)
            self.gate.reconciliation.terms_accepted = True
            self.gate.reconciliation.balances_known = self.account.available_usd is not None
            self.gate.reconciliation.positions_known = True
            self.gate.reconciliation.open_orders_known = True
            self.gate.reconciliation.account_stream_live = True
            self.gate.reconciliation.public_stream_live = True
            self.gate.mark_ready()
            # Stay on complete 1s snapshots. Mixed full/delta subscriptions cannot
            # safely distinguish a delayed full snapshot from a delta by payload.
            self._write_status("ready_paused", False, "reconciled; no activation permit consumed")
            return PreparedState(
                after, len(self.ranked), sum(item.eligible_now for item in self.ranked),
                tuple(item.rules.symbol for item in self.universe),
            )
        except Exception as exc:
            self.gate.halt()
            self.log.exception("startup_failed", ErrorClass.RECONCILIATION, exc)
            self._write_status("halted", False, "read-only startup failed")
            raise

    def _cancel_confirmation_budget(self, requested: float) -> float:
        """Scale only a correlated-outage proof queue, with a hard ceiling."""
        correlated = bool(self._stream_holds) or (
            isinstance(self.public, PublicMarketShards) and len(self.public.held) > 1
        )
        if (not self._continuous_service or self.gate.state is not EngineState.QUOTING
                or requested < _CANCEL_CONFIRM_SECONDS or not correlated):
            return requested
        working_entries = sum(
            1 for order in self.account.reducer.orders.values()
            if order.working and order.side is Side.BUY
        )
        # Private REST is intentionally serialized. Give a full-width outage
        # three seconds of bounded proof headroom per entry, without changing
        # the ordinary per-market timeout or permitting any mutation replay.
        return max(requested, min(
            _CORRELATED_CANCEL_MAX_SECONDS,
            _CANCEL_CONFIRM_SECONDS + 3 * working_entries,
        ))

    async def _coalesced_cancel_active_orders(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._cancel_active_read
        task = cached[1] if cached is not None else None
        reusable = task is not None and (
            not task.done() or (
                not task.cancelled() and task.exception() is None
                and now - cached[0] <= _CANCEL_READ_CACHE_SECONDS
            )
        )
        if not reusable:
            task = asyncio.create_task(
                self.rest.active_orders(), name="gemini-cancel-active-orders-proof",
            )
            self._cancel_active_read = (now, task)
        try:
            return await asyncio.shield(task)
        except BaseException:
            if (task.done() and self._cancel_active_read is not None
                    and self._cancel_active_read[1] is task):
                self._cancel_active_read = None
            raise

    async def _coalesced_cancel_history(self, symbol: str | None) -> list[dict[str, Any]]:
        key = "" if symbol is None else symbol
        now = time.monotonic()
        cached = self._cancel_history_reads.get(key)
        task = cached[1] if cached is not None else None
        reusable = task is not None and (
            not task.done() or (
                not task.cancelled() and task.exception() is None
                and now - cached[0] <= _CANCEL_READ_CACHE_SECONDS
            )
        )
        if not reusable:
            task = asyncio.create_task(
                self.rest.recent_order_history(symbol=symbol),
                name="gemini-cancel-history-proof",
            )
            self._cancel_history_reads[key] = (now, task)
        try:
            return await asyncio.shield(task)
        except BaseException:
            current = self._cancel_history_reads.get(key)
            if task.done() and current is not None and current[1] is task:
                self._cancel_history_reads.pop(key, None)
            raise

    async def _cancel_order_and_confirm(
        self, order_id: str, *, timeout: float = _CANCEL_CONFIRM_SECONDS,
    ) -> None:
        # A fast public-book guard and the inventory/quote owner can request the
        # same cancellation. Share one mutation and its terminal proof.
        task = self._cancel_tasks.get(order_id)
        if task is None:
            current = self.account.reducer.orders.get(order_id)
            if current is not None and not current.working:
                return
            task = asyncio.create_task(
                self._cancel_order_and_confirm_once(order_id, timeout=timeout),
                name="gemini-cancel-confirm",
            )
            self._cancel_tasks[order_id] = task
            task.add_done_callback(lambda completed, started=time.monotonic():
                                   self._queue_cancel_done(started, completed))
        try:
            await asyncio.shield(task)
        finally:
            # Keep failures latched: an unknown cancel cannot be blindly resent.
            if task.done() and not task.cancelled() and task.exception() is None:
                if self._cancel_tasks.get(order_id) is task:
                    self._cancel_tasks.pop(order_id, None)

    async def _cancel_order_and_confirm_once(
        self, order_id: str, *, timeout: float = _CANCEL_CONFIRM_SECONDS,
    ) -> None:
        # Includes admission and socket send, not just the confirmation phase.
        # A timeout remains latched in _cancel_tasks; it never authorizes replay.
        effective_timeout = self._cancel_confirmation_budget(timeout)
        async with asyncio.timeout(effective_timeout):
            send_cancel = True
            while True:
                try:
                    await self._cancel_order_and_confirm_bounded(
                        order_id, timeout=effective_timeout, send_cancel=send_cancel,
                    )
                    return
                except (TransportChangedBeforeSend, AdmissionTimeoutBeforeSend) as exc:
                    # These exceptions prove that no byte of the cancel request
                    # reached Gemini. They are the only cancel failures that may
                    # be retried, and only after fresh authenticated REST state
                    # proves whether the exact owned order is still active.
                    if not self._continuous_service or self.gate.state is not EngineState.QUOTING:
                        raise
                    if isinstance(exc, TransportChangedBeforeSend):
                        await self._await_private_cancel_replacement(exc)
                    send_cancel = await self._reconcile_unsent_cancel(order_id)

    async def _await_private_cancel_replacement(self, cause: BaseException) -> None:
        """Wait for the current private reconnect without awaiting its proof task.

        The proof task can itself join this order's shared cancellation owner, so
        awaiting the whole proof here would deadlock. If the private pump has not
        observed the loss yet, this cancellation owner starts the reconnect.
        """
        if "private" in self._stream_holds:
            restart = self._stream_restarts.get("private")
            if restart is None:
                raise RuntimeError("private recovery hold has no reconnect owner") from cause
            await asyncio.shield(restart)
            return
        await self._recover_stream(
            "private", cause, generation=self._stream_generation["private"],
        )

    async def _reconcile_unsent_cancel(self, order_id: str) -> bool:
        """Return True only when fresh REST proves the exact order still active."""
        while True:
            current = self.account.reducer.orders.get(order_id)
            if current is not None and not current.working:
                return False
            if current is None or not self.journal.knows_client_order_id(current.client_order_id):
                raise RuntimeError("unsent cancel no longer has exact owned order identity")
            try:
                active = build_account_snapshot([], await self.rest.active_orders()).active_orders
            except ApiError as exc:
                if not transient_failure(exc):
                    raise
                await asyncio.sleep(.5)
                continue
            matches = [order for order in active if order.order_id == order_id]
            if not matches:
                return False
            order = matches[0]
            expected = (
                current.client_order_id, current.symbol, current.side, current.outcome,
                current.price, current.original_quantity,
            )
            observed = (
                order.client_order_id, order.symbol, order.side, order.outcome,
                order.price, order.original_quantity,
            )
            if observed != expected:
                raise RuntimeError("unsent cancel active order identity is not proven")
            return True

    async def _cancel_order_and_confirm_bounded(
        self, order_id: str, *, timeout: float, send_cancel: bool = True,
    ) -> None:
        current = self.account.reducer.orders.get(order_id)
        if current is not None and not current.working:
            return
        terminal_event = self.account.terminal_events.setdefault(order_id, asyncio.Event())
        if send_cancel:
            await self.private.cancel(order_id)
        deadline = asyncio.get_running_loop().time() + timeout
        current = self.account.reducer.orders.get(order_id)
        if current is not None and not current.working:
            return
        # Give the authenticated order stream a short head start. A terminal
        # event wakes this immediately; only a missing event falls back to REST.
        try:
            await asyncio.wait_for(terminal_event.wait(), timeout=min(.35, timeout))
        except TimeoutError:
            pass
        last_transient: ApiError | None = None
        while asyncio.get_running_loop().time() < deadline:
            # The authenticated order stream is the lowest-latency terminal
            # proof and avoids unnecessary REST reads in the normal path.
            current = self.account.reducer.orders.get(order_id)
            if current is not None and not current.working:
                return
            try:
                rows = await self._coalesced_cancel_active_orders()
            except ApiError as exc:
                if exc.status is not None and exc.status not in {408, 418, 429} and not 500 <= exc.status < 600:
                    raise
                last_transient = exc
                await asyncio.sleep(0.5)
                continue
            if all(str(row.get("orderId")) != order_id for row in rows):
                try:
                    # A just-cancelled order should be on the newest page. Use
                    # the contract filter and maximum documented page size; a
                    # full page is valid and must not be mistaken for overflow.
                    history = await self._coalesced_cancel_history(
                        symbol=current.symbol if current is not None else None,
                    )
                except ApiError as exc:
                    if exc.status is not None and exc.status not in {408, 418, 429} and not 500 <= exc.status < 600:
                        raise
                    last_transient = exc
                    await asyncio.sleep(0.5)
                    continue
                terminal = next((row for row in history if str(row.get("orderId")) == order_id), None)
                if terminal is not None and str(terminal.get("status", "")).upper() in {"CANCELLED", "CANCELED", "FILLED", "REJECTED"}:
                    order = self.account.reducer.orders.get(order_id)
                    status = str(terminal["status"]).upper().replace("CANCELLED", "CANCELED")
                    if order is not None and (not send_cancel or
                            (self.queue_pilot is not None and order.symbol in self.queue_pilot.symbols)):
                        # No new cancel was sent because disconnect/admission may
                        # already have terminalized it. Require exact history so
                        # a missed partial fill can never be erased as a cancel.
                        status = terminal_history_status(order, terminal)
                    if order is not None:
                        order.remaining_quantity = Decimal("0")
                        order.status = status
                        self.journal.resolve_intent_terminal(order.client_order_id, order_id)
                        intent = self.ledger.get(order.client_order_id)
                        if intent is not None and intent.state in {IntentState.SENT_UNKNOWN, IntentState.ACKNOWLEDGED}:
                            self.ledger.mark_terminal(order.client_order_id)
                    return
            await asyncio.sleep(0.25)
        raise TimeoutError("order cancellation was not proven terminal") from last_transient

    async def _cancel_symbol_entries(self, symbol: str) -> None:
        if self.queue_pilot is not None and symbol in self.queue_pilot.symbols:
            if self.account.reducer.risk_adding_order_ids(symbol):
                self.queue_pilot.canceled(symbol, _utc_now())
        self._entry_cancel_symbols[symbol] = self._entry_cancel_symbols.get(symbol, 0) + 1
        try:
            order_ids = self.account.reducer.risk_adding_order_ids(symbol)
            # Issue both cancels promptly; one slow confirmation must not leave
            # its sibling resting. The transport still paces every mutation.
            results = await asyncio.gather(
                *(self._cancel_order_and_confirm(order_id) for order_id in order_ids),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            if order_ids and self.queue_pilot is not None and symbol in self.queue_pilot.symbols:
                self.queue_pilot.canceled(symbol, _utc_now())
                self._pilot_proved.pop(symbol, None)
        finally:
            self._entry_cancel_symbols[symbol] -= 1
            if self._entry_cancel_symbols[symbol] == 0:
                self._entry_cancel_symbols.pop(symbol)

    async def _quote_pair(self, candidate: Any) -> None:
        async with self._lifecycle_lock:
            await self._quote_pair_locked(candidate)

    def _entry_time_valid(self, symbol: str) -> bool:
        metadata = next((item for item in self.universe if item.rules.symbol == symbol), None)
        if metadata is not None and metadata.event_category.casefold() == "sports":
            if metadata.event_is_live or not metadata.event_starts_at:
                return False
        expiry = self._expiry.get(symbol)
        if expiry is None:
            return False
        trade_start = self._trade_start.get(symbol)
        if trade_start is not None and _utc_now() < trade_start:
            return False
        cutoff = expiry - timedelta(seconds=self.config.risk.force_exit_before_expiry_seconds)
        quality = self.config.strategy.entry_quality
        if (quality is not None and metadata is not None
                and metadata.event_category.casefold() == "crypto"):
            cutoff = min(cutoff, expiry - timedelta(seconds=quality.crypto_entry_before_expiry_seconds))
        sports_start = self._sports_start.get(symbol)
        if sports_start is not None:
            cutoff = min(
                cutoff,
                sports_start - timedelta(
                    seconds=self.config.risk.sports_exit_before_start_seconds
                ),
            )
        return _utc_now() < cutoff

    def _validate_send(self, payload: dict[str, Any]) -> None:
        """Last check after rate pacing; no await between this and transport send."""
        params = payload["params"]
        symbol = str(params["symbol"])
        client_order_id = str(params.get("clientOrderId", ""))
        intent = self.ledger.get(client_order_id)
        purpose = (
            intent.purpose.value if intent is not None
            else self.journal.intent_purpose(client_order_id)
        )
        if purpose not in {OrderPurpose.ENTRY.value, OrderPurpose.EXIT.value}:
            raise RuntimeError("order mutation lacks a classified durable intent")
        is_exit = purpose == OrderPurpose.EXIT.value
        if not self._book_fresh(symbol):
            raise QuoteChangedBeforeSend("market became stale while waiting to send")
        if not is_exit:
            if self._continue_after_override:
                if self._reward_epoch != self._current_reward_epoch(_utc_now()):
                    raise EntryStateChangedBeforeSend("reward day/override changed; fresh ranking required")
                current = next((c for c in self.ranked if c.symbol == symbol), None)
                if current is None or not self._candidate_actionable(current):
                    raise EntryStateChangedBeforeSend("fresh reward eligibility is required")
            if self._read_recovery_attempts or self._stream_holds or self._reward_program_paused:
                raise EntryStateChangedBeforeSend("read recovery pauses new entries")
            if params.get("timeInForce") == "MOC":
                pair = self._maker_pair_guards.get(symbol)
                if pair is not None:
                    first = self.account.reducer.orders.get(pair.first_server_id)
                    intent = self.ledger.get(pair.first_client_id)
                    if (
                        first is None or not first.working or first.cumulative_filled != 0
                        or first.client_order_id != pair.first_client_id
                        or first.symbol != symbol or first.side is not Side.BUY
                        or first.outcome is not pair.first_leg.outcome
                        or first.price != pair.first_leg.price
                        or first.original_quantity != pair.first_leg.quantity
                        or first.remaining_quantity != pair.first_leg.quantity
                        or intent is None or intent.state is not IntentState.ACKNOWLEDGED
                        or intent.server_order_id != pair.first_server_id
                        or self._public_generation(symbol) != pair.public_generation
                        or self._stream_generation["private"] != pair.private_generation
                        or params["eventOutcome"] != pair.second_leg.outcome.value
                        or Decimal(str(params["price"])) != pair.second_leg.price
                        or Decimal(str(params["quantity"])) != pair.second_leg.quantity
                    ):
                        raise EntryStateChangedBeforeSend("maker pair first leg changed before second send")
                target = self._current_quote_target(symbol)
                leg = None if target is None else (
                    target.yes if params["eventOutcome"] == "YES" else target.no
                )
                if (leg is None or Decimal(str(params["price"])) != leg.price
                        or Decimal(str(params["quantity"])) != leg.quantity
                        or symbol in self._entry_cancel_symbols):
                    raise QuoteChangedBeforeSend("maker quote changed before send")
                quality_reason = self._entry_quality_reason(symbol, target)
                if quality_reason is not None:
                    raise QuoteChangedBeforeSend(quality_reason)
            if self._permit_policy_digest is not None and policy_digest(DEFAULT_POLICY_PATH) != self._permit_policy_digest:
                raise RuntimeError("policy changed after approval")
            if symbol not in self._permitted_symbols:
                raise RuntimeError("entry symbol is outside approved scope")
            if not self.account.snapshots_fresh:
                raise RuntimeError("account snapshots became stale while waiting to send")
            if not self.gate.may_submit:
                raise RuntimeError("global entry permission changed while waiting to send")
            if not self._entry_time_valid(symbol):
                raise EntryStateChangedBeforeSend("entry cutoff crossed while waiting to send")
            if symbol in self.account.blocked_entries:
                raise EntryStateChangedBeforeSend("symbol became entry-blocked while waiting to send")
            if not self.account.equity_classification_complete:
                raise EntryStateChangedBeforeSend("account transition is reconciling while waiting to send")
            if self._permit_expires_at is not None and _utc_now() >= self._permit_expires_at:
                raise EntryStateChangedBeforeSend("activation permit expired while waiting to send")
            for held_symbol, signed in self.account.reducer.positions.items():
                deadline = self.journal.get_deadline(held_symbol, "YES" if signed > 0 else "NO")
                if deadline is None or (_utc_now() >= deadline and not self._isolated_exit_proof_current(held_symbol)):
                    raise EntryStateChangedBeforeSend(
                        "inventory safety action must precede any new entry"
                    )
            exposure = self._websocket_exposure()
            quantity = Decimal(str(params["quantity"]))
            reservation = Decimal(str(params["price"])) * quantity
            if (exposure.pending_orders + 1 > self._dynamic_pending_cap
                    or exposure.gross_reservation_usd + reservation > self.config.risk.max_gross_reservation_usd
                    or exposure.market_exposure_usd.get(symbol, Decimal("0")) + reservation > self.config.risk.max_market_exposure_usd):
                raise EntryStateChangedBeforeSend(
                    "entry exposure changed while waiting to send"
                )
            if self._adaptive_transition(symbol):
                raise QuoteChangedBeforeSend("adaptive mode changed before send")
            if (isinstance(self.queue_pilot, AdaptiveQueuePilot)
                    and symbol in self.queue_pilot.symbols
                    and (symbol in self.queue_pilot.proof_required
                         or (symbol in self.queue_pilot.cooldown_at and not self._pilot_proof_current(symbol)))):
                raise QuoteChangedBeforeSend("adaptive transition proof required")
            if self._pilot_at_touch(symbol):
                if (params.get("timeInForce") != "MOC" or not self._pilot_proof_current(symbol)
                        or self._pilot_advice(symbol, allow_entry=True) != "allow"):
                    raise QuoteChangedBeforeSend("queue pilot evidence changed before send")
                self.queue_pilot.anchor_send(symbol, Outcome(params["eventOutcome"]),
                    params["clientOrderId"], Decimal(str(params["price"])), _utc_now())
        else:
            round_deadline = self._exit_round_deadlines.get(symbol)
            if round_deadline is not None and time.monotonic() >= round_deadline:
                raise EntryStateChangedBeforeSend("exit round expired before send")
            if self._continuous_service and (not self.account.snapshots_fresh
                    or not self.account.equity_classification_complete):
                raise EntryStateChangedBeforeSend("exact classified account proof is stale")
            if self._stream_holds and ("private" in self._stream_holds or "private" not in self._stream_ready or not self.account.snapshots_fresh
                                       or not self.account.equity_classification_complete):
                raise EntryStateChangedBeforeSend("recovery has not proved exact exit state")
            position = self.account.reducer.positions.get(symbol, Decimal("0"))
            try:
                validate_exact_exit(
                    QuoteLeg(
                        symbol=symbol,
                        outcome=Outcome(str(params["eventOutcome"])),
                        side=Side(str(params["side"])),
                        price=Decimal(str(params["price"])),
                        quantity=Decimal(str(params["quantity"])),
                    ),
                    position,
                )
            except (ValueError, KeyError) as exc:
                raise RuntimeError("position changed while waiting to send exact exit")
            if intent is None and purpose != OrderPurpose.EXIT.value:
                raise RuntimeError("exact exit lacks a durable owned intent")

    def _market_data_verified_at(self, symbol: str | None = None):
        if isinstance(self.public, PublicMarketShards):
            if symbol is not None:
                return self.public.verified_at(symbol)
            return {s: self.public.verified_at(s) for s in self.public.assignments}
        observed = getattr(self.public, "last_message_at", None)
        if observed is not None:
            return observed
        # Test adapters and old recorded fixtures may not expose connection
        # metadata. Their newest applied book timestamp is the conservative
        # equivalent; production PublicMarketSocket always exposes it.
        values = [book.observed_at for book in self.public.books.values() if book.observed_at is not None]
        return max(values) if values else None

    def _public_stream_fresh(self, symbol: str | None = None) -> bool:
        if isinstance(self.public, PublicMarketShards) and symbol is None:
            return bool(self.public.assignments) and all(self._public_stream_fresh(s) for s in self.public.assignments)
        observed = self._market_data_verified_at(symbol)
        if observed is None:
            return False
        age = (_utc_now() - observed).total_seconds()
        return 0 <= age <= float(self.config.strategy.stale_book_seconds)

    def _book_fresh(self, symbol: str) -> bool:
        book = self.public.books.get(symbol)
        return bool(book is not None and book.synchronized and self._public_stream_fresh(symbol))

    def _candidate_actionable(self, candidate: Any) -> bool:
        """Treat the uncertain local 50% calculation as reporting only.

        Every concrete venue, book, price, size, economics, lifecycle and risk
        rejection still blocks. Only the single local reachability proxy can be
        ignored because Gemini's exact snapshot phase/denominator is unknown.
        """
        reward_or_proxy_ok = candidate.eligible_now or bool(
            candidate.quote is not None
            and tuple(candidate.reasons) == (_UPTIME_PROXY_REJECTION,)
        )
        return bool(
            reward_or_proxy_ok
            and candidate.quote is not None
            and self._entry_quality_reason(candidate.symbol, candidate.quote) is None
            and (
                self._commissioning_expected_yes_price is None
                or (
                    candidate.quote is not None
                    and candidate.quote.yes.price == self._commissioning_expected_yes_price
                )
            )
            and (
                self._commissioning_expected_no_price is None
                or (
                    candidate.quote is not None
                    and candidate.quote.no.price == self._commissioning_expected_no_price
                )
            )
        )

    def _entry_quality_reason(self, symbol: str, quote: QuotePair) -> str | None:
        policy = self.config.strategy.entry_quality
        if policy is None:
            return None
        if not self._book_fresh(symbol):
            return "entry_quality:exit_book_unavailable"
        book = self.public.books.get(symbol)
        if book is None:
            return "entry_quality:exit_book_unavailable"
        result = evaluate_entry_quality(book, quote, policy, account=self.account.reducer)
        return None if result.allowed else "entry_quality:" + result.reason

    async def _quote_pair_locked(self, candidate: Any) -> None:
        try:
            await self._quote_pair_current(candidate)
        except (TransportChangedBeforeSend, AdmissionTimeoutBeforeSend) as exc:
            if not self._continuous_service or self.gate.state is not EngineState.QUOTING:
                raise
            # The transport terminalized only this definitely-unsent intent.
            # Recovery owns any earlier acknowledged sibling; do not issue a
            # second cancel on the disconnected generation or replay the quote.
            if "private" not in self._stream_holds:
                await self._recover_stream("private", exc, generation=self._stream_generation["private"])
        except (QuoteChangedBeforeSend, EntryStateChangedBeforeSend) as exc:
            # Definitely unsent. A fill, cutoff, lifecycle block, transient
            # accounting transition, exposure change, or fresh-book change can
            # legitimately invalidate the second leg after the first was sent.
            # Clear any still-working sibling and let the safety/rank workers
            # handle the new state without escalating an expected race into an
            # account-wide drain.
            await self._cancel_symbol_entries(candidate.symbol)
            self.journal.audit("maker_pair_presend_state_change", {
                "symbol": candidate.symbol,
                "reason": str(exc),
                "position": str(self.account.reducer.positions.get(candidate.symbol, Decimal("0"))),
            })

    async def _quote_pair_current(self, candidate: Any) -> None:
        if self._read_recovery_attempts or self._stream_holds or self._reward_program_paused:
            return
        if (not self.gate.may_submit or not self._candidate_actionable(candidate)
                or candidate.quote is None
                or not self.account.equity_classification_complete
                or not self.account.snapshots_fresh):
            return
        clock = asyncio.get_running_loop().time()
        cooldown_until = self._quote_cooldown_until.get(candidate.symbol, 0.0)
        if clock < cooldown_until:
            return
        self._quote_cooldown_until.pop(candidate.symbol, None)
        if not self._entry_time_valid(candidate.symbol) or not self._book_fresh(candidate.symbol):
            return
        if candidate.symbol in self.account.blocked_entries or self.journal.pending_exit(candidate.symbol):
            return
        if self.account.reducer.positions.get(candidate.symbol, Decimal("0")) != 0:
            return
        if self.account.reducer.risk_adding_order_ids(candidate.symbol):
            return
        if not self._pilot_entry_ready(candidate.symbol):
            return
        target = self._current_quote_target(candidate.symbol)
        if target is None:
            return
        if target != candidate.quote:
            # The rank cycle may have waited on earlier markets. Re-rank on the
            # next cycle rather than using stale profitability/admission inputs.
            return
        state = self._websocket_exposure()
        if state.pending_orders + 2 > self._dynamic_pending_cap:
            return
        admission = admit_quote_pair(
            self.config.risk, state, candidate.quote,
            stressed_exit_loss_usd=max(
                candidate.quote.yes.price * candidate.quote.yes.quantity,
                candidate.quote.no.price * candidate.quote.no.quantity,
            ),
            opens_new_owner=candidate.symbol not in state.market_exposure_usd,
        )
        available = self.account.available_usd
        if not admission.allowed or available is None or candidate.quote.total_reservation_proxy_usd > available:
            return
        pair_generations = dict(self._stream_generation)
        pair_public_generation = self._public_generation(candidate.symbol)
        first_id = await self.private.place_maker(candidate.quote.yes)
        await self.account.wait_for_order_event(first_id)
        first = self.ledger.get(first_id)
        if first is None or first.state is not IntentState.ACKNOWLEDGED:
            first_order = (
                None if first is None or first.server_order_id is None
                else self.account.reducer.orders.get(first.server_order_id)
            )
            if first_order is not None and first_order.cumulative_filled > 0:
                # A maker can fill between its NEW event and this waiter being
                # scheduled. The durable fill and deadline already exist; remove
                # any residual/sibling entry and let fill safety own the exit.
                await self._cancel_symbol_entries(candidate.symbol)
                return
            if first is not None and await self._clean_zero_fill_moc_terminal(first, candidate.symbol):
                self._quote_cooldown_until[candidate.symbol] = asyncio.get_running_loop().time() + 5
                return
            raise RuntimeError("first maker leg lacked positive working acknowledgment")
        first_order = self.account.reducer.orders.get(first.server_order_id or "")
        if first_order is not None and first_order.cumulative_filled > 0:
            await self._cancel_symbol_entries(candidate.symbol)
            return
        if (not self.gate.may_submit or candidate.symbol in self.account.blocked_entries
                or first_order is None or not first_order.working
                or self.account.reducer.positions.get(candidate.symbol, Decimal("0")) != 0
                or not self._entry_time_valid(candidate.symbol) or not self._book_fresh(candidate.symbol)):
            return
        if getattr(self, "_commissioning_single_leg", False):
            self._maker_ack_count += 1
            self._activated_pool_ids.add(candidate.owner_key)
            return
        # The first leg can terminate while the second waits for admission.
        # Keep its exact identity/geometry bound through the final send guard.
        pair_guard = MakerPairGuard(
            first_id, first.server_order_id, candidate.quote.yes, candidate.quote.no,
            pair_public_generation, pair_generations["private"],
        )
        self._maker_pair_guards[candidate.symbol] = pair_guard
        try:
            second_id = await self.private.place_maker(candidate.quote.no)
        finally:
            if self._maker_pair_guards.get(candidate.symbol) is pair_guard:
                self._maker_pair_guards.pop(candidate.symbol)
        await self.account.wait_for_order_event(second_id)
        second = self.ledger.get(second_id)
        second_order = (
            None if second is None or second.server_order_id is None
            else self.account.reducer.orders.get(second.server_order_id)
        )
        if second_order is not None and second_order.cumulative_filled > 0:
            # The second leg has the same legitimate NEW -> fill race as the
            # first. Durable fill safety owns the exit; cancel both residuals.
            await self._cancel_symbol_entries(candidate.symbol)
            return
        if second is None or second.state is not IntentState.ACKNOWLEDGED:
            if second is not None and await self._clean_zero_fill_moc_terminal(second, candidate.symbol):
                self._quote_cooldown_until[candidate.symbol] = asyncio.get_running_loop().time() + 5
                return
            await self._cancel_symbol_entries(candidate.symbol)
            raise RuntimeError("second maker leg lacked positive working acknowledgment")
        self._activated_pool_ids.add(candidate.owner_key)
        self._maker_ack_count += 1

    async def _clean_zero_fill_moc_terminal(self, intent: Any, symbol: str) -> bool:
        """Prove an immediately canceled maker leg left no exposure.

        Gemini can emit NEW and CANCELED back-to-back for MOC. The waiter may
        therefore observe the durable intent after it has already advanced to
        TERMINAL. Only a positively identified CANCELED order with zero fills,
        followed by cancellation of its sibling and REST/WS agreement that the
        symbol is flat, is recoverable. Every other terminal/ambiguous state
        remains fatal to activation.
        """
        order = (
            self.account.reducer.orders.get(intent.server_order_id)
            if intent.server_order_id else None
        )
        if (
            intent.state is not IntentState.TERMINAL
            or order is None
            or order.status != "CANCELED"
            or order.working
            or order.cumulative_filled != 0
        ):
            return False
        await self._cancel_symbol_entries(symbol)
        snapshot = build_account_snapshot(await self.rest.positions(), [])
        assert_position_consistency(snapshot, self.account.reducer.positions)
        return (
            snapshot.signed_positions.get(symbol, Decimal("0")) == 0
            and self.account.reducer.positions.get(symbol, Decimal("0")) == 0
        )

    async def _maintain_candidate(self, candidate: Any) -> None:
        async with self._lifecycle_lock:
            await self._maintain_candidate_locked(candidate)

    async def _cancel_ineligible_working_entries(self, candidates_by_symbol: dict[str, Any]) -> None:
        recorded: set[str] = set()
        for order in tuple(self.account.reducer.orders.values()):
            candidate = candidates_by_symbol.get(order.symbol)
            if (
                order.working
                and order.side.value == "BUY"
                and (
                    candidate is None
                    or not self._candidate_actionable(candidate)
                    or not self._entry_time_valid(order.symbol)
                )
            ):
                if order.symbol not in recorded:
                    self._record_quote_cancel(order.symbol, "selection_unquotable:" + self._owner_failure_reason(order.symbol))
                    recorded.add(order.symbol)
                await self._cancel_symbol_entries(order.symbol)

    def _current_quote_target(self, symbol: str) -> Any | None:
        return self._quote_target_state(symbol)[0]

    def _quote_target_state(self, symbol: str) -> tuple[Any | None, str]:
        contract = next(
            (item for item in self.universe if item.rules.symbol == symbol), None,
        )
        if contract is None:
            return None, "missing_contract"
        book = self.public.books.get(symbol)
        if book is None:
            return None, "book_missing"
        if not book.synchronized:
            return None, "book_unsynchronized"
        if not self._public_stream_fresh(symbol):
            return None, "market_stream_stale"
        if book.top() is None:
            return None, "public_book_one_sided"
        candidates = self._rank_live_universe(
            [contract], self.public.books, now=_utc_now(),
            quantity=self.config.strategy.quote_quantity,
            offset_ticks=self.config.strategy.quote_offset_ticks,
            account=self.account.reducer,
            market_data_verified_at=self._market_data_verified_at(),
            target_uptime=self.config.strategy.target_uptime,
        )
        if not candidates:
            return None, "external_book_one_sided"
        if candidates[0].quote is None:
            return None, "invalid_quote_target"
        return candidates[0].quote, "available"

    async def _cancel_too_aggressive_quote(self, symbol: str) -> bool:
        """Cancel when a book move leaves our entry inside the configured offset.

        Moving away from the touch is not canceled here; the normal sticky
        maintenance path can improve it without turning every depth frame into
        quote churn that harms qualifying uptime.
        """
        working = [
            order for order in self.account.reducer.orders.values()
            if order.symbol == symbol and order.working and order.side.value == "BUY"
        ]
        if not working:
            return False
        if self._adaptive_transition(symbol):
            self._record_quote_cancel(symbol, "adaptive_mode_transition")
            await self._cancel_symbol_entries(symbol)
            return True
        if self._pilot_at_touch(symbol):
            if self._pilot_advice(symbol) != "keep":
                self._record_quote_cancel(symbol, "queue_pilot:" + self.queue_pilot.reasons.get(symbol, "evidence"))
                await self._cancel_symbol_entries(symbol)
                return True
            return False
        target, target_reason = self._quote_target_state(symbol)
        if target is None:
            self._record_quote_cancel(symbol, target_reason)
            await self._cancel_symbol_entries(symbol)
            return True
        targets = {Outcome.YES: target.yes.price, Outcome.NO: target.no.price}
        if any(order.price > targets[order.outcome] for order in working):
            self._record_quote_cancel(symbol, "inside_configured_tick_distance")
            await self._cancel_symbol_entries(symbol)
            self.journal.audit("adverse_move_quote_canceled", {
                "symbol": symbol,
                "reason": "inside_configured_tick_distance",
                "offset_ticks": self.config.strategy.quote_offset_ticks,
            })
            return True
        return False

    async def _adverse_quote_guard(self) -> None:
        """Cancel independently of placement/exit waits; one worker per symbol."""
        try:
            while self.gate.state is EngineState.QUOTING:
                await self.public.depth_changed.wait()
                self.public.depth_changed.clear()
                for symbol, task in tuple(self._adverse_tasks.items()):
                    if task.done():
                        task.result()
                        self._adverse_tasks.pop(symbol)
                while (symbol := self.public.adverse_dirty.pop()) is not None:
                    if symbol not in self._adverse_tasks:
                        task = asyncio.create_task(
                            self._run_adverse_cancel(symbol),
                            name="gemini-adverse-cancel",
                        )
                        self._adverse_tasks[symbol] = task
                        task.add_done_callback(lambda _: self.public.depth_changed.set())
        finally:
            for task in self._adverse_tasks.values():
                task.cancel()
            await asyncio.gather(*self._adverse_tasks.values(), return_exceptions=True)
            self._adverse_tasks.clear()

    async def _run_adverse_cancel(self, symbol: str) -> bool:
        try:
            if self._stream_holds:
                return False  # Recovery coordinator cancels after reconnect.
            return await self._cancel_too_aggressive_quote(symbol)
        except Exception:
            self.gate.halt()
            raise

    async def _maintain_candidate_locked(self, candidate: Any) -> None:
        if self._stream_holds:
            return
        working = [
            order for order in self.account.reducer.orders.values()
            if order.symbol == candidate.symbol and order.working and order.side.value == "BUY"
        ]
        if self._read_recovery_attempts or self._reward_program_paused:
            # Unknown reward/account audit blocks new risk, but does not kill
            # the process, abandon positions, or reset the 50% uptime history.
            if working:
                await self._cancel_symbol_entries(candidate.symbol)
            return
        if (not self._candidate_actionable(candidate) or candidate.quote is None or not self._entry_time_valid(candidate.symbol)
                or candidate.symbol in self.account.blocked_entries):
            if working:
                await self._cancel_symbol_entries(candidate.symbol)
            return
        if self.account.reducer.positions.get(candidate.symbol, Decimal("0")) != 0:
            if working:
                await self._cancel_symbol_entries(candidate.symbol)
            return
        if (working and (self._adaptive_transition(candidate.symbol)
                or (self._pilot_at_touch(candidate.symbol) and self._pilot_advice(candidate.symbol) != "keep"))):
            await self._cancel_symbol_entries(candidate.symbol)
            return
        expected = {
            Outcome.YES: (candidate.quote.yes.price, candidate.quote.yes.quantity),
            Outcome.NO: (candidate.quote.no.price, candidate.quote.no.quantity),
        }
        actual = {order.outcome: (order.price, order.remaining_quantity) for order in working}
        if getattr(self, "_commissioning_single_leg", False):
            expected.pop(Outcome.NO)
        retention_required = self._continuous_service and not getattr(self, "_commissioning_single_leg", False)
        if retention_required and working and self._retain_resting_pair(candidate, working):
            self._retained_pairs += 1
            return
        if working and (retention_required or len(working) != len(expected) or actual != expected):
            self._replaced_pairs += 1
            reason = self._retention_reason if retention_required else "legacy_exact_target_change"
            self._record_quote_cancel(candidate.symbol, reason)
            self.log.event(logging.INFO, "quote_pair_replacement", symbol=candidate.symbol,
                           reason=reason)
            await self._cancel_symbol_entries(candidate.symbol)
            working = []
        if not working:
            await self._quote_pair_locked(candidate)

    def _retain_resting_pair(self, candidate: Any, working: list[Any]) -> bool:
        """Preserve queue/resting time only for intact, safe qualifying pairs.

        Every NEW order still uses the exact configured target. Existing orders
        may be farther away, never closer, and BOTH must remain within the
        documented reward distance. Fills/partial quantities never qualify.
        """
        self._retention_reason = "pair_incomplete"
        if len(working) != 2 or {o.outcome for o in working} != {Outcome.YES, Outcome.NO}:
            return False
        self._retention_reason = "account_unverified"
        if not self.account.snapshots_fresh or not self.account.equity_classification_complete:
            return False
        target = self._current_quote_target(candidate.symbol)
        book = self.public.books.get(candidate.symbol)
        if target is None or book is None or book.top() is None:
            self._retention_reason = "book_unavailable"
            return False
        targets = {Outcome.YES: target.yes, Outcome.NO: target.no}
        self._retention_reason = "filled_or_wrong_quantity"
        if not all(
            o.cumulative_filled == 0 and o.remaining_quantity == targets[o.outcome].quantity
            for o in working
        ):
            return False
        orders = {o.outcome: o for o in working}
        pair = QuotePair(*(
            QuoteLeg(candidate.symbol, outcome, Side.BUY, orders[outcome].price, orders[outcome].remaining_quantity)
            for outcome in (Outcome.YES, Outcome.NO)
        ))
        quality_reason = self._entry_quality_reason(candidate.symbol, pair)
        if quality_reason is not None:
            self._retention_reason = quality_reason
            return False
        contract = next(c for c in self.universe if c.rules.symbol == candidate.symbol)
        self._retention_reason = "inside_configured_tick_distance_or_invalid_price"
        if not resting_quote_safe(pair, target, contract.rules, self.config.strategy.quote_quantity):
            return False
        ranked = self._rank_live_universe(
            [contract], self.public.books, now=_utc_now(),
            quantity=self.config.strategy.quote_quantity, offset_ticks=self.config.strategy.quote_offset_ticks,
            uptime=self._uptime, account=self.account.reducer, market_data_verified_at=self._market_data_verified_at(),
            profitability=self.profitability, target_uptime=self.config.strategy.target_uptime,
            maker_rebate_fractions=self._maker_rebate_fractions, resting_quotes={candidate.symbol: pair},
            pool_competition_scores={candidate.pool_id: candidate.pool_competition_score_proxy},
        )
        retained = bool(ranked and self._candidate_actionable(ranked[0])
                        and marginal_reward_proxy(ranked[0], candidate.other_pool_daily_score_proxy)
                        > ranked[0].expected_execution_cost_usd)
        self._retention_reason = "retained" if retained else "reward_distance_or_economics"
        return retained

    def _websocket_exposure(self) -> ExposureState:
        """Reserve BUY cost, retaining the existing held-inventory allowance.

        Whole-pair admission and the final send guard use the same price-times-
        quantity reservation. Never net inventory against an unfilled SELL or
        assume an exit/settlement will free capital before authenticated proof.
        """
        gross = sum((abs(value) for value in self.account.reducer.positions.values()), Decimal("0"))
        market = {symbol: abs(value) for symbol, value in self.account.reducer.positions.items()}
        outcomes: dict[str, set[Outcome]] = {}
        pending = 0
        for order in self.account.reducer.orders.values():
            if not order.working:
                continue
            pending += 1
            if order.side.value == "BUY":
                reservation = order.remaining_quantity * order.price
                gross += reservation
                market[order.symbol] = market.get(order.symbol, Decimal("0")) + reservation
                outcomes.setdefault(order.symbol, set()).add(order.outcome)
        return ExposureState(
            gross_reservation_usd=gross,
            market_exposure_usd=market,
            pending_orders=pending,
            two_sided_quotes=sum(value == {Outcome.YES, Outcome.NO} for value in outcomes.values()),
            tracked_markets=len(set(market) | {order.symbol for order in self.account.reducer.orders.values() if order.working}),
        )

    async def _safety_loop(self) -> None:
        workers: dict[str, asyncio.Task[None]] = {}
        try:
            while self.gate.state in {EngineState.QUOTING, EngineState.DRAINING}:
                for symbol, task in tuple(workers.items()):
                    if task.done():
                        task.result()  # Unknown cancellation must never be swallowed.
                        workers.pop(symbol)
                try:
                    symbol = await asyncio.wait_for(self.account.safety_symbols.get(), .1)
                except TimeoutError:
                    continue
                self.account.safety_symbols.task_done()
                if "private" in self._stream_holds or "private" not in self._stream_ready:
                    # Recovery owns pre-disconnect terminal proof/cancellation.
                    continue
                if symbol not in workers:
                    if len(workers) >= 64:
                        raise RuntimeError("fill cancellation worker bound exceeded")
                    # No lifecycle lock: a blocked fill cannot add risk, and
                    # per-order cancellation already has one deduplicated owner.
                    workers[symbol] = asyncio.create_task(
                        self._cancel_fill_entries_if_blocked(symbol), name="gemini-fill-cancel",
                    )
        finally:
            for task in workers.values():
                task.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)

    async def _cancel_fill_entries_if_blocked(self, symbol: str) -> None:
        # A queued notification may outlive exact flat cleanup. Do not cancel
        # a newly eligible pair because of an obsolete fill notification.
        if (symbol in self.account.blocked_entries and "private" not in self._stream_holds
                and "private" in self._stream_ready):
            await self._cancel_symbol_entries(symbol)

    def _isolated_exit_proof_current(self, symbol: str) -> bool:
        wait = self._exit_waits.get(symbol)
        return bool(
            self._continuous_service and wait is not None and wait.residual
            and 0 <= time.monotonic() - wait.proved_at <= 60
            and wait.fill_version == self.account.fill_version
            and wait.generation == (self._public_generation(symbol), self._stream_generation["private"])
            and not self._stream_holds and self.account.snapshots_fresh
            and self.account.equity_classification_complete
            and self.account.reducer.positions.get(symbol) == wait.residual
            and symbol not in self._exit_workers and symbol not in self._exit_uncertain
            and not self.journal.has_nonterminal_intents(symbol)
            and not any(o.working and o.symbol == symbol for o in self.account.reducer.orders.values())
        )

    async def _exit_account_snapshot(self, symbol: str) -> AccountSnapshot:
        """Read-only bracket. A fill/generation change invalidates this proof."""
        if (self._stream_holds or not self.account.snapshots_fresh
                or not self.account.equity_classification_complete):
            raise ExitDeferred("account_proof_pending")
        version, generation = self.account.fill_version, dict(self._stream_generation)
        snapshot = build_account_snapshot(
            await self.rest.positions(),
            await self.rest.active_orders(),
        )
        for order in snapshot.active_orders:
            if not order.client_order_id or not self.journal.knows_client_order_id(order.client_order_id):
                raise RuntimeError("exit proof found an unowned active order")
        if (version != self.account.fill_version or generation != self._stream_generation
                or self._stream_holds or not self.account.snapshots_fresh
                or not self.account.equity_classification_complete):
            raise ExitDeferred("account_proof_changed")
        rest_positions = snapshot.signed_positions
        ws_positions = self.account.reducer.positions
        differences = {s for s in set(rest_positions) | set(ws_positions)
                       if rest_positions.get(s, Decimal(0)) != ws_positions.get(s, Decimal(0))}
        if differences == {symbol}:
            # Normal fill/exit publication can briefly lag on REST. No mutation
            # or entry proof is allowed during this bounded convergence wait.
            started = self._position_proof_wait_started.setdefault(symbol, time.monotonic())
            if time.monotonic() - started <= 10:
                raise ExitDeferred("position_proof_pending")
        assert_position_consistency(snapshot, ws_positions)
        self._position_proof_wait_started.pop(symbol, None)
        if any(order.symbol == symbol for order in snapshot.active_orders):
            raise ExitDeferred("working_order_proof_pending")
        if (self.journal.has_nonterminal_intents(symbol)
                or any(o.working and o.symbol == symbol for o in self.account.reducer.orders.values())):
            raise ExitDeferred("terminal_intent_proof_pending")
        return snapshot

    async def _complete_flat_fill(self, symbol: str) -> bool:
        if symbol in self._exit_uncertain or self.journal.pending_exit(symbol):
            raise RuntimeError("flat lifecycle still has an uncertain exit")
        snapshot = await self._exit_account_snapshot(symbol)
        if snapshot.signed_positions.get(symbol, Decimal(0)):
            return False
        # No await between fresh proof and this atomic cleanup. Never adopt
        # manual cash, fabricate fills or clear an independent lifecycle hold.
        with self.journal.transaction():
            for outcome in ("YES", "NO"):
                self.journal.clear_deadline(symbol, outcome)
            self.journal.audit("fill_lifecycle_flat", {"symbol": symbol})
        self.account.release_fill_block(symbol)
        self._exit_waits.pop(symbol, None)
        self._exit_residual_limits.pop(symbol, None)
        return True

    async def _run_exit_worker(self, symbol: str) -> None:
        previous = self._exit_waits.get(symbol)
        try:
            async with asyncio.timeout(_EXIT_WORKER_SECONDS):
                if not self.account.reducer.positions.get(symbol):
                    await self._cancel_symbol_entries(symbol)
                    if not await self._complete_flat_fill(symbol):
                        raise ExitDeferred("position_changed_during_flat_proof")
                else:
                    await self._exit_due_positions_locked(symbol_filter=symbol, scoped=True)
        except Exception as exc:
            # Only explicitly no-send/terminal scheduling waits and read errors
            # qualify. Unknown mutation outcomes and integrity failures escape.
            if not isinstance(exc, ExitDeferred) and not (
                isinstance(exc, (ApiError, TimeoutError)) and transient_failure(exc)
            ):
                raise
            if (symbol in self._exit_uncertain or self.journal.pending_exit(symbol)
                    or self.journal.has_nonterminal_intents(symbol)
                    or any(o.working and o.symbol == symbol for o in self.account.reducer.orders.values())):
                raise
            rounds = 1 if previous is None else previous.rounds + 1
            delay = max(min(5 * 2 ** min(rounds - 1, 4), 60), retry_after_seconds(exc))
            reason = str(exc) if isinstance(exc, ExitDeferred) else "transient_account_read"
            if reason == "position_proof_pending":
                delay = 2
            # Proof is renewed below only on known depth/round boundaries;
            # stale/mismatched account proof never licenses unrelated entries.
            proven = isinstance(exc, ExitDeferred) and reason in {
                "exit_book_unavailable", "exit_depth_unavailable", "exit_round_exhausted",
            } and self.account.snapshots_fresh and self.account.equity_classification_complete
            residual = self.account.reducer.positions.get(symbol) if proven else self._exit_residual_limits.get(symbol)
            self._exit_waits[symbol] = ExitWait(
                reason, time.monotonic() + delay, rounds, residual,
                time.monotonic() if proven else 0, self.account.fill_version if proven else -1,
                (self._public_generation(symbol), self._stream_generation["private"]),
            )
            self.log.event(logging.WARNING, "market_exit_wait", symbol=symbol,
                           reason=reason, retry_seconds=delay, rounds=rounds,
                           original_deadline_preserved=True)
        finally:
            self._exit_round_deadlines.pop(symbol, None)

    def _schedule_exit_workers(self) -> None:
        for symbol, task in tuple(self._exit_workers.items()):
            if task.done():
                task.result()
                self._exit_workers.pop(symbol)
        if self.gate.state is not EngineState.QUOTING or self._stream_holds:
            return
        symbols = set(self.account.reducer.positions) | self.account.fill_blocked_entries
        for symbol in sorted(symbols):
            if symbol in self._exit_workers:
                continue
            wait = self._exit_waits.get(symbol)
            if wait is not None and time.monotonic() < wait.retry_at:
                continue
            signed = self.account.reducer.positions.get(symbol, Decimal(0))
            if signed:
                deadline = self.journal.get_deadline(symbol, "YES" if signed > 0 else "NO")
                if deadline is None:
                    raise RuntimeError("inventory has no proven fill deadline")
                deadline = min(deadline, self.account.hard_exit_cutoff.get(symbol, deadline))
                if _utc_now() < deadline:
                    continue
            if len(self._exit_workers) >= self.config.risk.max_tracked_markets:
                raise RuntimeError("exit worker owner bound exceeded")
            self.account.block_entries(symbol, fill=True)
            self._exit_workers[symbol] = asyncio.create_task(
                self._run_exit_worker(symbol), name="gemini-market-exit",
            )

    async def _exit_due_positions(self, *, force: bool = False) -> None:
        if self._continuous_service and not force:
            self._schedule_exit_workers()
            return
        async with self._lifecycle_lock:
            await self._exit_due_positions_locked(force=force)

    async def _exit_due_positions_locked(
        self, *, force: bool = False, symbol_filter: str | None = None, scoped: bool = False,
    ) -> None:
        for symbol, signed in tuple(self.account.reducer.positions.items()):
            if signed == 0 or (symbol_filter is not None and symbol != symbol_filter):
                continue
            outcome = Outcome.YES if signed > 0 else Outcome.NO
            deadline = self.journal.get_deadline(symbol, outcome.value)
            if not force and deadline is None:
                if scoped:
                    raise RuntimeError("inventory has no proven fill deadline")
                self.gate.halt()
                self._write_status("halted", False, "inventory has no proven fill deadline")
                return
            hard_cutoff = self.account.hard_exit_cutoff.get(symbol)
            if deadline is not None and hard_cutoff is not None:
                deadline = min(deadline, hard_cutoff)
            if not force and _utc_now() < deadline:
                continue
            if not force and self._stream_holds and (
                "private" in self._stream_holds or "private" not in self._stream_ready or not self.account.snapshots_fresh
                or not self.account.equity_classification_complete or not self._book_fresh(symbol)
            ):
                # Never extend the original deadline or use stale executable
                # depth. Service this overdue exit as soon as exact state returns.
                continue
            if symbol in self._exit_rejected:
                raise RuntimeError("prior exit rejected; automatic retry prohibited")
            if symbol in self._exit_uncertain or self.journal.pending_exit(symbol):
                raise TimeoutError("prior exit still requires terminal reconciliation; retry prohibited")
            self.account.block_entries(symbol, fill=True)
            await self._cancel_symbol_entries(symbol)
            snapshot = await self._exit_account_snapshot(symbol) if scoped else build_account_snapshot(await self.rest.positions(), [])
            assert_position_consistency(snapshot, self.account.reducer.positions)
            signed = snapshot.signed_positions.get(symbol, Decimal("0"))
            if not signed:
                if scoped:
                    await self._complete_flat_fill(symbol)
                continue
            limit = self._exit_residual_limits.get(symbol) if scoped else None
            if limit is not None and (
                (signed > 0) != (limit > 0) or abs(signed) > abs(limit)
            ):
                raise RuntimeError("exit residual increased or reversed between rounds")
            if scoped:
                self._exit_residual_limits[symbol] = signed
            exit_started_at = asyncio.get_running_loop().time()
            if scoped:
                self._exit_round_deadlines[symbol] = time.monotonic() + 30
            attempt = 0
            while signed:
                elapsed = asyncio.get_running_loop().time() - exit_started_at
                if attempt >= 3:
                    if scoped:
                        raise ExitDeferred("exit_round_exhausted")
                    raise RuntimeError("exact IOC exit exhausted its bounded retry authority")
                if elapsed >= 30:
                    if scoped:
                        raise ExitDeferred(
                            "account_proof_pending" if attempt == 0 else "exit_round_exhausted"
                        )
                    if attempt == 0:
                        raise RuntimeError("exact IOC exit remained blocked before transport send")
                    raise RuntimeError("exact IOC exit exhausted its bounded retry authority")
                if symbol in self._exit_uncertain or self.journal.pending_exit(symbol):
                    raise TimeoutError("prior exit still requires terminal reconciliation; retry prohibited")
                book = self.public.books.get(symbol)
                if scoped and (self._stream_holds or not self.account.snapshots_fresh
                        or not self.account.equity_classification_complete):
                    raise ExitDeferred("account_proof_pending")
                if not self._book_fresh(symbol):
                    if scoped:
                        raise ExitDeferred("exit_book_unavailable")
                    raise RuntimeError("exit book is stale or unsynchronized")
                leg = exact_crossing_exit(book, signed) if book is not None else None
                if leg is None:
                    if scoped:
                        raise ExitDeferred("exit_depth_unavailable")
                    raise RuntimeError("fresh full depth cannot support an exact position exit")

                submitted_signed = signed
                self._exit_uncertain.add(symbol)
                try:
                    client_id = await self.private.exit_position(
                        leg, signed_position=submitted_signed,
                    )
                    attempt += 1
                except MutationNotSentError:
                    # The guard runs after pacing but before websocket.send.
                    # Reconcile the changed position instead of converting a
                    # successful prior fill into an ambiguous duplicate exit.
                    self._exit_uncertain.discard(symbol)
                    reconciliation_deadline = min(
                        exit_started_at + 30,
                        asyncio.get_running_loop().time() + 5,
                    )
                    while True:
                        rest_signed = build_account_snapshot(
                            await self.rest.positions(), [],
                        ).signed_positions.get(symbol, Decimal("0"))
                        websocket_signed = self.account.reducer.positions.get(
                            symbol, Decimal("0"),
                        )
                        if rest_signed == websocket_signed == 0:
                            signed = Decimal("0")
                            break
                        if (
                            rest_signed == websocket_signed
                            and rest_signed != 0
                            and (rest_signed > 0) == (submitted_signed > 0)
                            and abs(rest_signed) <= abs(submitted_signed)
                        ):
                            signed = rest_signed
                            self.journal.audit("exit_presend_position_reconciled", {
                                "symbol": symbol,
                                "remaining_quantity": str(abs(signed)),
                            })
                            break
                        if asyncio.get_running_loop().time() >= reconciliation_deadline:
                            raise RuntimeError(
                                "exit position changed before send and did not reconcile safely"
                            )
                        await asyncio.sleep(0.25)
                    if signed == 0:
                        break
                    await asyncio.sleep(0.25)
                    continue
                # A timeout here leaves the mutation ambiguous and deliberately
                # retains _exit_uncertain, preventing emergency drain from
                # blindly submitting another reducer.
                remaining_window = 30 - (
                    asyncio.get_running_loop().time() - exit_started_at
                )
                if remaining_window <= 0:
                    raise TimeoutError("exact IOC exit exceeded its total retry window")
                await self.account.wait_for_order_event(
                    client_id, timeout=min(10, remaining_window),
                )
                confirmation_deadline = asyncio.get_running_loop().time() + 10
                retry_residual: Decimal | None = None
                while True:
                    intent = self.ledger.get(client_id)
                    order = (
                        self.account.reducer.orders.get(intent.server_order_id)
                        if intent is not None and intent.server_order_id else None
                    )
                    terminal = bool(
                        intent is not None
                        and intent.state is IntentState.TERMINAL
                        and order is not None
                        and not order.working
                    )
                    if terminal:
                        # The prior network mutation is positively terminal.
                        # A later REST failure may leave position state unknown,
                        # but it must not falsely make this known-terminal IOC
                        # permanently ambiguous to the emergency drain.
                        self._exit_uncertain.discard(symbol)

                    remaining_window = 30 - (
                        asyncio.get_running_loop().time() - exit_started_at
                    )
                    if remaining_window <= 0:
                        raise TimeoutError("exact IOC exit exceeded its total retry window")
                    rest_positions = build_account_snapshot(
                        await asyncio.wait_for(
                            self.rest.positions(), timeout=remaining_window,
                        ), [],
                    ).signed_positions
                    rest_signed = rest_positions.get(symbol, Decimal("0"))
                    websocket_signed = self.account.reducer.positions.get(
                        symbol, Decimal("0"),
                    )
                    if terminal and rest_signed == 0 and websocket_signed == 0:
                        signed = Decimal("0")
                        break
                    if terminal and order is not None and order.status == "REJECTED":
                        self._exit_rejected.add(symbol)
                        raise RuntimeError("exact IOC exit was rejected; automatic retry prohibited")
                    if (
                        terminal
                        and order is not None
                        and order.status == "CANCELED"
                        and rest_signed == websocket_signed
                        and rest_signed != 0
                        and (rest_signed > 0) == (submitted_signed > 0)
                        and abs(rest_signed) <= abs(submitted_signed)
                    ):
                        # Gemini documents that IOC orders end CANCELED even
                        # after partial/full execution. Once that terminal state
                        # and the exact residual agree across REST and the
                        # authenticated WebSocket, a new exact reducer is safe.
                        retry_residual = rest_signed
                        if scoped:
                            self._exit_residual_limits[symbol] = rest_signed
                        self.journal.audit("exit_ioc_terminal_residual", {
                            "symbol": symbol,
                            "attempt": attempt,
                            "remaining_quantity": str(abs(rest_signed)),
                        })
                        break
                    if (
                        terminal
                        and order is not None
                        and order.status == "CANCELED"
                        and rest_signed == websocket_signed
                        and rest_signed != 0
                        and (
                            (rest_signed > 0) != (submitted_signed > 0)
                            or abs(rest_signed) > abs(submitted_signed)
                        )
                    ):
                        raise RuntimeError("exact IOC exit moved inventory away from flat")
                    if asyncio.get_running_loop().time() >= confirmation_deadline:
                        raise RuntimeError("exact IOC exit did not prove a safe terminal result")
                    await asyncio.sleep(0.25)

                if signed == 0:
                    break
                if retry_residual is None:
                    raise RuntimeError("exact IOC exit residual state is unavailable")
                if attempt >= 3 or asyncio.get_running_loop().time() - exit_started_at >= 30:
                    if scoped:
                        raise ExitDeferred("exit_round_exhausted")
                    raise RuntimeError("exact IOC exit exhausted its bounded retry authority")
                signed = retry_residual
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
            self._exit_uncertain.discard(symbol)
            if scoped:
                await self._complete_flat_fill(symbol)
            else:
                self.journal.clear_deadline(symbol, outcome.value)
                self.account.release_fill_block(symbol)

    async def _risk_mark(self) -> int:
        self._risk_read_verified = False
        started_quoting = self.gate.state is EngineState.QUOTING
        fill_version = self.account.fill_version
        snapshot = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        now = _utc_now()
        policy = RiskWindowPolicy(self.config.risk.risk_window_loss_usd, self.config.risk.max_pending_orders)
        start, end = policy.bounds(now)
        realized_pnl, history_complete = self.journal.realized_pnl_for_window(start, end)
        if not history_complete:
            raise RuntimeError("closed-fill realized P&L cannot be classified safely")
        if started_quoting and (
            not self.account.equity_classification_complete or fill_version != self.account.fill_version
        ):
            # Entries are already blocked. The independent safety clock owns
            # the bounded 10-second account-event/cash reconciliation grace.
            # Do not persist a false risk halt when the 60-second REST timer
            # happens to land between a fill and its balance report.
            return self._dynamic_pending_cap
        assert_position_consistency(snapshot, self.account.reducer.positions)
        mark = PersistentRiskWindow(
            policy, self.journal
        ).update(
            now=now, realized_pnl=realized_pnl,
            reconciled=True,
            classification_complete=(
                self.account.equity_classification_complete and history_complete
            ),
        )
        if not mark.classification_complete:
            raise RuntimeError("closed-fill realized P&L cannot be classified safely")
        self._last_snapshot = snapshot
        self._last_live_mark = (self.account.fill_version, (start, end))
        self._dynamic_pending_cap = mark.pending_order_cap
        self._risk_read_verified = True
        return mark.pending_order_cap

    async def _live_safety_clock(self) -> None:
        """Independent of ranking/REST waits: observe risk, expiry and exit timers."""
        unmatched_since: float | None = None
        last_cash_reconcile_at = 0.0
        last_cash_reconcile_error: BaseException | None = None
        last_settlement_reconcile_error: BaseException | None = None
        settlement_reconcile_attempted = False
        while self.gate.state is EngineState.QUOTING:
            now = _utc_now()
            clock = asyncio.get_running_loop().time()
            self._observe_resting()
            self._check_stream_recovery()
            self._refresh_adaptive_evidence()
            if self.queue_pilot is not None:
                for symbol in self.queue_pilot.symbols:
                    self.public.adverse_dirty.mark(symbol)
                self.public.depth_changed.set()
            if self._permit_expires_at is not None and now >= self._permit_expires_at:
                self.gate.state = EngineState.DRAINING
                return
            if (not self._continue_after_override and self._uptime_override_until is not None
                    and now >= self._uptime_override_until):
                self.gate.state = EngineState.DRAINING
                return
            if not self.account.snapshots_fresh and not self._stream_holds:
                await self._recover_stream("private", TimeoutError("periodic account snapshots are stale"),
                                           generation=self._stream_generation["private"])
            for task in self._tasks:
                if task.done():
                    self.gate.halt()
                    task.result()
                    raise RuntimeError("required stream worker ended")
            if self._stream_holds:
                await self._exit_due_positions()
                await asyncio.sleep(.25)
                continue
            if not self.account.equity_classification_complete:
                unmatched_since = clock if unmatched_since is None else unmatched_since
                expected_positions = self.account.expected_positions or {}
                actual_positions = self.account.reducer.positions
                settlement_candidate = (
                    expected_positions != actual_positions
                    and set(actual_positions) <= set(expected_positions)
                    and all(
                        actual_positions.get(symbol, Decimal("0")) in {quantity, Decimal("0")}
                        for symbol, quantity in expected_positions.items()
                    )
                )
                if settlement_candidate:
                    for symbol, quantity in expected_positions.items():
                        if quantity and not actual_positions.get(symbol):
                            self.account.block_entries(symbol, fill=True)
                    if (
                        self._settlement_reconciliation_task is None
                        and not settlement_reconcile_attempted
                    ):
                        self._settlement_reconciliation_task = asyncio.create_task(
                            self._reconcile_missed_settlements(),
                            name="gemini-settlement-reconciliation",
                        )
                        settlement_reconcile_attempted = True
                if (
                    self._settlement_reconciliation_task is not None
                    and self._settlement_reconciliation_task.done()
                ):
                    try:
                        self._settlement_reconciliation_task.result()
                    except BaseException as exc:
                        last_settlement_reconcile_error = exc
                    finally:
                        self._settlement_reconciliation_task = None
                positions_match = (
                    self.account.expected_positions == self.account.reducer.positions
                    and not self.account.unowned_order_seen
                )
                if (
                    positions_match
                    and clock - unmatched_since >= 1
                    and clock - last_cash_reconcile_at >= 2
                    and self._cash_reconciliation_task is None
                ):
                    self._cash_reconciliation_task = asyncio.create_task(
                        self._reconcile_authenticated_nontrading_cash(),
                        name="gemini-nontrading-cash-reconciliation",
                    )
                    last_cash_reconcile_at = clock
                if self._cash_reconciliation_task is not None and self._cash_reconciliation_task.done():
                    try:
                        self._cash_reconciliation_task.result()
                    except BaseException as exc:
                        last_cash_reconcile_error = exc
                    finally:
                        self._cash_reconciliation_task = None
                grace = 65 if settlement_candidate else 10
                if not self.account.equity_classification_complete and clock - unmatched_since > grace:
                    self.gate.halt()
                    raise RuntimeError(
                        "cash/position changes remain unmatched; reconciliation required"
                    ) from (last_settlement_reconcile_error or last_cash_reconcile_error)
            if self.account.equity_classification_complete:
                unmatched_since = None
                settlement_reconcile_attempted = False
                last_settlement_reconcile_error = None
                self._mark_live_risk()
                if self.account.reducer.pending_order_count() > self._dynamic_pending_cap:
                    await self._enforce_pending_cap(self._dynamic_pending_cap)
                if self._dynamic_pending_cap == 0:
                    self.gate.state = EngineState.DRAINING
                    return
            await self._exit_due_positions()
            await asyncio.sleep(0.25)

    def _mark_live_risk(self) -> None:
        if self.gate.state is not EngineState.QUOTING or not self.account.equity_classification_complete:
            return
        now = _utc_now()
        policy = RiskWindowPolicy(self.config.risk.risk_window_loss_usd, self.config.risk.max_pending_orders)
        start, end = policy.bounds(now)
        key = (self.account.fill_version, (start, end))
        if key == self._last_live_mark:
            return
        realized_pnl, history_complete = self.journal.realized_pnl_for_window(start, end)
        mark = PersistentRiskWindow(policy, self.journal).update(
            now=now,
            realized_pnl=realized_pnl,
            classification_complete=history_complete,
        )
        if not mark.classification_complete:
            self._dynamic_pending_cap = 0
            self.gate.halt()
            raise RuntimeError("closed-fill realized P&L cannot be classified safely")
        self._last_live_mark = key
        self._dynamic_pending_cap = mark.pending_order_cap

    async def _enforce_pending_cap(self, cap: int) -> None:
        if (self.account.snapshots_fresh and self.account.equity_classification_complete
                and self.account.reducer.pending_order_count() <= cap):
            return
        rows = build_account_snapshot([], await self.rest.active_orders()).active_orders
        if len(rows) <= cap:
            return
        priorities = {item.symbol: item.priority_score for item in self.ranked}
        ordered = sorted(
            rows,
            key=lambda row: (priorities.get(row.symbol, Decimal("0")), row.symbol, row.order_id),
        )
        remaining = len(rows)
        for order in ordered:
            if remaining <= cap:
                break
            if not order.client_order_id or not self.journal.knows_client_order_id(order.client_order_id):
                raise RuntimeError("pending-cap enforcement found an unowned order")
            await self._cancel_order_and_confirm(order.order_id)
            remaining -= 1

    def _record_missing_reward_event(self, ticker: str) -> None:
        self.log.event(logging.WARNING, "reward_event_unavailable", event_ticker=ticker,
                       http_status=404, reason="event_metadata_missing_not_settlement")

    async def _reward_pools_still_active(self) -> bool:
        config = await self.rest.reward_config()
        if config.get("enabled") is not True:
            if self._continuous_service:
                self._reward_program_paused = True
                self.log.event(logging.WARNING, "reward_program_paused", entries_paused=True)
                return True
            return False
        rows = await self.rest.reward_events()
        now = _utc_now()
        active_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            if Decimal(str(row.get("daily_pool_usd", "0"))) <= 0:
                continue
            ends_at = str(row.get("ends_at") or "")
            if ends_at and _parse_time(ends_at) <= now:
                continue
            active_rows[str(row.get("event_ticker", "")).upper()] = row
        usable_permitted_symbol = False
        refreshed = []
        event_cache: dict[str, Any] = {}
        for item in self.universe:
            reward = active_rows.get(item.event_ticker)
            if reward is None:
                self.account.block_entries(item.rules.symbol, reason="reward_metadata")
                refreshed.append(replace(item, pool=replace(item.pool, active=False)))
                continue
            status_version = self._market_status_versions.get(item.rules.symbol, 0)
            if item.event_ticker not in event_cache:
                event_cache[item.event_ticker] = await read_reward_event(
                    self.rest, item.event_ticker, self._record_missing_reward_event,
                )
                if event_cache[item.event_ticker] is None:
                    # Block every sibling before cancellation yields to senders.
                    for sibling in self.universe:
                        if sibling.event_ticker == item.event_ticker:
                            self.account.block_entries(sibling.rules.symbol, reason="reward_metadata")
            if event_cache[item.event_ticker] is None:
                self.account.block_entries(item.rules.symbol, reason="reward_metadata")
                await self._cancel_symbol_entries(item.rules.symbol)
                refreshed.append(replace(item, pool=replace(item.pool, active=False)))
                continue
            current = join_reward_contracts([reward], {item.event_ticker: event_cache[item.event_ticker]})
            chosen = next((row for row in current if row.rules.symbol == item.rules.symbol), None)
            invalid = (chosen is None or chosen.rules != item.rules or chosen.expiry != item.expiry
                    or chosen.pool.pool_id != item.pool.pool_id
                    or not chosen.pool.active or not chosen.rules.active or chosen.rules.post_only_mode
                    or (
                        chosen.event_category.casefold() == "sports"
                        and (
                            chosen.event_is_live
                            or not chosen.event_starts_at
                            or (
                                chosen.event_starts_at
                                and now >= _parse_time(chosen.event_starts_at)
                            )
                        )
                    ))
            if invalid:
                self.account.block_entries(item.rules.symbol, reason="reward_metadata")
                await self._cancel_symbol_entries(item.rules.symbol)
                refreshed.append(replace(item, pool=replace(item.pool, active=False)))
                continue
            refreshed.append(chosen)
            if self._continuous_service:
                await self._release_recovered_market(item.rules.symbol, status_version)
            # Keep identities/owners fixed, but use current budget, competition
            # and timing metadata for ranking. Never silently rotate live pools.
            if chosen.trade_starts_at:
                self._trade_start[item.rules.symbol] = _parse_time(chosen.trade_starts_at)
            if chosen.event_category.casefold() == "sports":
                self._sports_start[item.rules.symbol] = _parse_time(chosen.event_starts_at)
                cutoff = self._sports_start[item.rules.symbol] - timedelta(seconds=self.config.risk.sports_exit_before_start_seconds)
                old = self.account.hard_exit_cutoff.get(item.rules.symbol)
                self.account.hard_exit_cutoff[item.rules.symbol] = cutoff if old is None else min(old, cutoff)
            if (
                item.rules.symbol in self._permitted_symbols
                and item.rules.symbol not in self.account.blocked_entries
                and self._entry_time_valid(item.rules.symbol)
            ):
                usable_permitted_symbol = True
        self.universe = refreshed
        if self._continue_after_override and self._catalog_scope:
            await self._rollover_flat_markets(list(active_rows.values()))
            usable_permitted_symbol = any(
                c.pool.active and self._entry_time_valid(c.rules.symbol)
                and c.rules.symbol not in self.account.blocked_entries for c in self.universe
            )
        self._uptime = sample_and_load_progress(
            self.journal, self.universe, self.account.reducer, now=_utc_now(), books=self.public.books,
            market_data_verified_at=self._market_data_verified_at(),
            exit_before_expiry_seconds=self.config.risk.force_exit_before_expiry_seconds,
            sports_exit_before_start_seconds=self.config.risk.sports_exit_before_start_seconds,
            crypto_entry_before_expiry_seconds=self._crypto_entry_cutoff_seconds(),
        )
        self.log.event(logging.INFO, "reward_metadata_refreshed", tracked_symbols=len(self.universe),
                       usable_permitted_symbol=usable_permitted_symbol)
        self._reward_program_paused = False  # Clear only after complete successful metadata refresh.
        # A v6 catalog session can wait safely for the next discovery interval.
        return usable_permitted_symbol or (self._continue_after_override and self._catalog_scope)

    async def _release_recovered_market(self, symbol: str, status_version: int) -> None:
        reasons = self.account.entry_hold_reasons.get(symbol, set())
        if not reasons.intersection({"lifecycle", "reward_metadata"}):
            return
        if (self._stream_holds or self._read_recovery_attempts or self._recovery_stop_requested()
                or self._market_status_versions.get(symbol, 0) != status_version
                or self._market_status.get(symbol, "active") != "active"
                or self.account.reducer.positions.get(symbol)
                or not self._book_fresh(symbol) or not self._entry_time_valid(symbol)):
            return
        # Terminal confirmation shares the existing cancellation owner. Do not
        # clear an independent fill/manual hold or adopt account state.
        await self._cancel_symbol_entries(symbol)
        try:
            async with asyncio.timeout(15):
                snapshot = await self._exit_account_snapshot(symbol)
        except (ExitDeferred, TimeoutError):
            return
        if (snapshot.signed_positions.get(symbol) or self._stream_holds
                or self._recovery_stop_requested()
                or self._market_status_versions.get(symbol, 0) != status_version):
            return
        for reason in ("lifecycle", "reward_metadata"):
            self.account.release_entry_reason(symbol, reason)
        self.log.event(logging.INFO, "market_entry_hold_recovered", symbol=symbol,
                       terminal_flat_verified=True)

    async def _rollover_flat_markets(self, rewards: list[dict[str, Any]]) -> None:
        """Replace only expired/inactive owners after terminal and flat proof."""
        if self._stream_holds or self._read_recovery_attempts or self._recovery_stop_requested():
            return
        if isinstance(self.public, PublicMarketShards) and self.public.held:
            return  # Do not move owners/subscriptions while a shard is recovering.
        owned_symbols = {pair[1] for pair in self._sticky_markets.values()}
        failed_reserves = {
            c.symbol for c in self.ranked if c.symbol not in owned_symbols and any(
                reason in {"offset produces an invalid quote price", "quote exceeds maximum reward distance",
                           "non-positive conservative net-profit estimate"}
                for reason in c.reasons
            )
        }
        retiring = [c for c in self.universe if not c.pool.active
                    or not self._entry_time_valid(c.rules.symbol) or c.rules.symbol in failed_reserves]
        if (not retiring and not self._inventory_catalog_holds
                and len(self.universe) >= self.config.risk.max_tracked_markets):
            return
        preferred = {
            c.owner_key: (c.event_ticker, c.rules.symbol)
            for c in self.universe if c not in retiring
        }
        preferred.update({
            pool_id: pair for pool_id, pair in self._sticky_markets.items()
            if pair[1] in self._inventory_catalog_holds
            or pair[1] not in {c.rules.symbol for c in self.universe}
        })
        proposed = await RewardUniverseLoader(
            self.rest, self._record_missing_reward_event, self._crypto_entry_cutoff_seconds(),
        ).load(
            maximum_pools=self.config.risk.max_tracked_markets, preferred=preferred,
            now=_utc_now(), rewards=rewards,
            quantity=self.config.strategy.quote_quantity,
            offset_ticks=self.config.strategy.quote_offset_ticks,
            excluded_symbols=failed_reserves,
        )
        async with self._lifecycle_lock:
            if self._stream_holds or self._read_recovery_attempts or self._recovery_stop_requested():
                return
            if isinstance(self.public, PublicMarketShards) and self.public.held:
                return
            removable = []
            for contract in retiring:
                symbol = contract.rules.symbol
                if (self._continuous_service and self._entry_time_valid(symbol)
                        and self.account.entry_hold_reasons.get(symbol, set()).intersection(
                            {"lifecycle", "reward_metadata"})):
                    # Keep a temporarily unavailable owner discoverable for
                    # fresh metadata/proof recovery. Expired owners still retire.
                    continue
                # Inventory retains its owner and original exit deadline.
                if self.account.reducer.positions.get(symbol, Decimal("0")):
                    continue
                await self._cancel_symbol_entries(symbol)
                if (self.account.reducer.positions.get(symbol, Decimal("0"))
                        or symbol in self._exit_uncertain
                        or any(s == symbol for _, s in self.journal.nonterminal_sent_intents())
                        or any(self.journal.get_deadline(symbol, outcome) is not None for outcome in ("YES", "NO"))):
                    continue
                removable.append(contract)
            if removable:
                snapshot = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
                if not self.account.snapshots_fresh or not self.account.equity_classification_complete:
                    return
                assert_position_consistency(snapshot, self.account.reducer.positions)
                if any(not row.client_order_id or not self.journal.knows_client_order_id(row.client_order_id)
                       for row in snapshot.active_orders):
                    raise RuntimeError("catalog rollover found unowned orders")
                removable = [c for c in removable
                             if not any(p.symbol == c.rules.symbol for p in snapshot.positions)
                             and not any(o.symbol == c.rules.symbol for o in snapshot.active_orders)
                             and not self.account.reducer.positions.get(c.rules.symbol, Decimal("0"))]
            symbols = tuple(c.rules.symbol for c in removable)
            if symbols:
                await self.public.unsubscribe(symbols)
                self.universe = [c for c in self.universe if c.rules.symbol not in symbols]
                self._permitted_symbols.difference_update(symbols)
                for c in removable:
                    if c.owner_key in self._sticky_markets:
                        self._owner_event("sticky_owner_released", c.owner_key, c.rules.symbol,
                                          "catalog_reward_inactive" if not c.pool.active else "contract_entry_cutoff")
                        self._sticky_recovery.observe(c.owner_key, None, _utc_now())
                    self._sticky_pool_ids.discard(c.owner_key)
                    self._sticky_markets.pop(c.owner_key, None)
                    self._market_status_versions.pop(c.rules.symbol, None)
                    self._market_status.pop(c.rules.symbol, None)
                    for reason in ("lifecycle", "reward_metadata"):
                        self.account.release_entry_reason(c.rules.symbol, reason)
            if self._stream_holds or self._recovery_stop_requested():
                return
            owners = {c.owner_key for c in self.universe}
            additions = []
            for c in proposed:
                if len(self.universe) + len(additions) >= self.config.risk.max_tracked_markets:
                    break
                recovering_inventory = (
                    c.rules.symbol in self._inventory_catalog_holds
                    and bool(self.account.reducer.positions.get(c.rules.symbol, Decimal("0")))
                )
                if c.owner_key in owners or (
                    c.rules.symbol in self.account.blocked_entries and not recovering_inventory
                ):
                    continue
                owners.add(c.owner_key)
                additions.append(c)
            # Establish identities before subscriptions can deliver snapshots.
            for c in additions:
                symbol = c.rules.symbol
                self._expiry[symbol] = _parse_time(c.expiry)
                if c.trade_starts_at:
                    self._trade_start[symbol] = _parse_time(c.trade_starts_at)
                if c.event_category.casefold() == "sports" and c.event_starts_at:
                    self._sports_start[symbol] = _parse_time(c.event_starts_at)
                    self.account.hard_exit_cutoff[symbol] = self._sports_start[symbol] - timedelta(
                        seconds=self.config.risk.sports_exit_before_start_seconds)
                self.public.books[symbol] = LocalBook(symbol)
            self.universe.extend(additions)
            self._sync_quote_asset_precisions(additions)
            self.account.expiry = self._expiry
            self._permitted_symbols.update(c.rules.symbol for c in additions)
            if additions:
                await self.public.subscribe(tuple(c.rules.symbol for c in additions))
                recovered_inventory = {
                    c.rules.symbol for c in additions
                    if c.rules.symbol in self._inventory_catalog_holds
                }
                if recovered_inventory:
                    self._inventory_catalog_holds.difference_update(recovered_inventory)
                    self.log.event(
                        logging.INFO,
                        "inventory_catalog_recovered",
                        symbols=sorted(recovered_inventory),
                        exit_workers_resumed=True,
                    )
            if removable or additions:
                self._save_shard_assignments()
                self.journal.set_runtime_state("sticky_markets_v2", json.dumps(self._sticky_markets, sort_keys=True), updated_at=_utc_now())
                self._reward_epoch = None  # New identities require freshly computed eligibility.
                self.log.event(logging.INFO, "catalog_owners_refreshed", retired=len(removable), added=len(additions))
                self._sync_adaptive_scope()

    async def activate(
        self,
        permit_path: Path,
        *,
        commissioning_seconds: int | None = None,
        single_leg: bool = False,
        ignore_uptime_proxy: bool = False,
        expected_yes_price: Decimal | None = None,
        expected_no_price: Decimal | None = None,
        supervisor_session_id: str | None = None,
    ) -> None:
        """Enter the mutation path only after preparation and bounded approval."""
        if self.read_only:
            raise RuntimeError("read-only orchestrator cannot activate")
        self.config.validate()
        if self.gate.state is not EngineState.READY:
            raise RuntimeError("read-only reconciliation must complete before activation")
        permit = load_activation_permit(
            permit_path, policy_path=DEFAULT_POLICY_PATH, store=self.journal,
            account_fingerprint=hashlib.sha256(self.config.api_key.encode()).hexdigest(),
        )
        self._permitted_symbols = (
            {item.rules.symbol for item in self.universe} | self._inventory_catalog_holds
            if permit.universe_scope == "reward_catalog"
            else set(permit.symbols)
        )
        self._permit_policy_digest = permit.policy_sha256
        if self.queue_pilot is not None:
            incumbents = {symbol for _, symbol in self._sticky_markets.values()}
            policy = self.queue_pilot.policy
            all_selected = isinstance(policy, AdaptivePolicy) and policy.scope == 'all_selected'
            if (permit.permit_version != 7
                    or (all_selected and permit.universe_scope != 'reward_catalog')
                    or (not all_selected and (not policy.symbols <= incumbents
                                             or not policy.symbols <= self._permitted_symbols))):
                raise RuntimeError("queue strategy scope requires reconciled permitted ownership")
            if self.queue_pilot.policy.expired(_utc_now()):
                raise RuntimeError("queue pilot calibration expired before activation")
            self._sync_adaptive_scope()
        if not self._permitted_symbols <= (
            {item.rules.symbol for item in self.universe} | self._inventory_catalog_holds
        ):
            raise RuntimeError("approved symbols are not in the reconciled universe")
        if commissioning_seconds is not None and (not 2 <= commissioning_seconds <= 30 or permit.maximum_active_pools != 1):
            raise ValueError("commissioning requires 2-30 seconds and a one-pool permit")
        if permit.permit_version == 7 and commissioning_seconds is not None:
            raise ValueError("continuous-service permit cannot authorize a commissioning canary")
        if single_leg and commissioning_seconds is None:
            raise ValueError("one-leg mode is restricted to bounded commissioning")
        if (
            ignore_uptime_proxy
            and commissioning_seconds is None
            and permit.uptime_override_until is None
        ):
            raise ValueError("continuous uptime override requires an expiry-bound approval permit")
        if expected_yes_price is not None and (
            commissioning_seconds is None
            or expected_yes_price <= 0
            or expected_yes_price >= 1
        ):
            raise ValueError("expected YES price is restricted to bounded commissioning")
        if expected_no_price is not None and (
            commissioning_seconds is None
            or single_leg
            or expected_no_price <= 0
            or expected_no_price >= 1
        ):
            raise ValueError("expected NO price is restricted to bounded two-sided commissioning")
        self._commissioning_single_leg = single_leg
        self._commissioning_ignore_uptime_proxy = ignore_uptime_proxy
        self._uptime_override_until = permit.uptime_override_until if ignore_uptime_proxy else None
        self._continue_after_override = permit.permit_version in {6, 7}
        self._continuous_service = permit.permit_version == 7
        self._catalog_scope = permit.universe_scope == "reward_catalog"
        self._commissioning_expected_yes_price = expected_yes_price
        self._commissioning_expected_no_price = expected_no_price
        start, end = RiskWindowPolicy(self.config.risk.risk_window_loss_usd).bounds(_utc_now())
        _, complete = self.journal.realized_pnl_for_window(start, end)
        if not complete:
            raise RuntimeError("activation blocked: authenticated closing-fill evidence is incomplete")
        # Authority is persisted before the first possible mutation. Versions
        # 2-4 cannot replay; version 5 can resume only through the same bounded
        # supervisor session after this runner later records proven-flat exit.
        consume_activation_permit(
            permit, self.journal, supervisor_session_id=supervisor_session_id,
        )
        self._activation_consumed = True
        self._permit_expires_at = permit.expires_at
        safety: asyncio.Task[None] | None = None
        safety_clock: asyncio.Task[None] | None = None
        adverse_guard: asyncio.Task[None] | None = None
        reward_check: asyncio.Task[bool] | None = None
        try:
            for order in self._last_snapshot.active_orders:
                await self._cancel_order_and_confirm(order.order_id)
            await self._prepare_activation_inventory()
            await self._risk_mark()
            self.gate.activate(True)
            if self._continuous_service:
                self._schedule_exit_workers()
            if commissioning_seconds is None:
                self._reward_reporting_task = asyncio.create_task(
                    self._reward_reporting_loop(), name="gemini-reward-uptime-reporting",
                )
            activation_reason = (
                f"supervised permit {permit.permit_id} active"
                if permit.permit_version in {5, 6, 7}
                else f"one-use permit {permit.permit_id} consumed"
            )
            if self._override_active():
                activation_reason += "; uptime proxy exception active; no reward eligibility claim"
            self._write_status("quoting", True, activation_reason)
            safety = asyncio.create_task(self._safety_loop(), name="gemini-fill-safety")
            safety_clock = asyncio.create_task(self._live_safety_clock(), name="gemini-live-safety-clock")
            adverse_guard = asyncio.create_task(
                self._adverse_quote_guard(), name="gemini-adverse-quote-guard",
            )
            next_rank_at = 0.0
            next_stale_check_at = 0.0
            next_risk_at = asyncio.get_running_loop().time() + 60
            next_uptime_at = 0.0
            next_reward_check_at = asyncio.get_running_loop().time() + (
                60 if self._inventory_catalog_holds else 300
            )
            stop_at = None if commissioning_seconds is None else asyncio.get_running_loop().time() + commissioning_seconds
            while self.gate.state is EngineState.QUOTING:
                if self._continue_after_override and self._advance_reward_epoch(_utc_now()):
                    next_rank_at = next_stale_check_at = next_uptime_at = 0.0
                self._check_stream_recovery()
                if safety is not None and safety.done():
                    safety.result()
                if safety_clock is not None and safety_clock.done():
                    safety_clock.result()
                if adverse_guard is not None and adverse_guard.done():
                    adverse_guard.result()
                for task in self._tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("required stream ended")
                if (self.directory / "STOP").exists():
                    self.gate.state = EngineState.DRAINING
                    break
                clock = asyncio.get_running_loop().time()
                if stop_at is not None and clock >= stop_at:
                    self.gate.state = EngineState.DRAINING
                    break
                if self._stream_holds:
                    self._write_status("recovering", False, "stream reconnect/reconciliation; entries paused, original deadlines retained")
                    await asyncio.sleep(1)
                    continue
                if reward_check is not None and reward_check.done():
                    try:
                        reward_active = reward_check.result()
                    except ApiError as exc:
                        next_reward_check_at = clock + self._defer_read_failure("reward_metadata", exc)
                    else:
                        if not reward_active:
                            self.gate.state = EngineState.DRAINING
                            break
                        self._complete_read_recovery("reward_metadata")
                        next_reward_check_at = clock + (
                            30 if self._reward_program_paused
                            else 60 if self._inventory_catalog_holds
                            else 300
                        )
                    reward_check = None
                if reward_check is None and any(
                    (
                        not item.pool.active
                        or not self._entry_time_valid(item.rules.symbol)
                    )
                    and item.rules.symbol not in self.account.blocked_entries
                    for item in self.universe
                ):
                    self._reward_refresh_requested = True
                if reward_check is None and self._reward_refresh_due(clock, next_reward_check_at):
                    self._reward_refresh_requested = False
                    reward_check = asyncio.create_task(
                        self._reward_pools_still_active(), name="gemini-reward-refresh"
                    )
                if clock >= next_risk_at:
                    try:
                        cap = await self._risk_mark()
                    except ApiError as exc:
                        next_risk_at = clock + self._defer_read_failure("account_audit", exc)
                    else:
                        if self._risk_read_verified:
                            self._complete_read_recovery("account_audit")
                        await self._enforce_pending_cap(cap)
                        next_risk_at = clock + (60 if self._risk_read_verified else 2)
                        if cap == 0:
                            self.gate.state = EngineState.DRAINING
                            break
                if clock >= next_rank_at and (len(self.public.dirty) > 0 or clock >= next_stale_check_at):
                    if clock >= next_uptime_at:
                        now = _utc_now()
                        self._uptime = sample_and_load_progress(
                            self.journal, self.universe, self.account.reducer, now=now,
                            books={} if self._reward_program_paused else self.public.books,
                            market_data_verified_at=self._market_data_verified_at(),
                            exit_before_expiry_seconds=self.config.risk.force_exit_before_expiry_seconds,
                            sports_exit_before_start_seconds=self.config.risk.sports_exit_before_start_seconds,
                            crypto_entry_before_expiry_seconds=self._crypto_entry_cutoff_seconds(),
                        )
                        self.journal.set_runtime_state(
                            "profitability_telemetry_v1",
                            self.profitability.dumps(now=now),
                            updated_at=now,
                        )
                        next_uptime_at = clock + 60
                    self.ranked = self._rank_live_universe(
                        self.universe, self.public.books, now=_utc_now(),
                        quantity=self.config.strategy.quote_quantity,
                        offset_ticks=self.config.strategy.quote_offset_ticks,
                        uptime=self._uptime,
                        account=self.account.reducer,
                        market_data_verified_at=self._market_data_verified_at(),
                        profitability=self.profitability,
                        target_uptime=self.config.strategy.target_uptime,
                        maker_rebate_fractions=self._maker_rebate_fractions,
                        preferred_symbols={pool: pair[1] for pool, pair in self._sticky_markets.items()},
                        allow_uptime_override=True,
                        evaluate_working_quotes=True,
                    )
                    if clock >= getattr(self, "_next_report_at", 0.0):
                        self._write_rank_report(_utc_now())
                        standby = self._reward_program_paused or bool(self._read_recovery_attempts) or not any(
                            self._candidate_actionable(c) and self._entry_time_valid(c.symbol) for c in self.ranked
                        )
                        self._write_status("standby" if standby else "quoting", True,
                                           "entries paused; service and exit safety active" if standby
                                           else "runtime heartbeat; approval active")
                        self.account.prune_terminal()
                        self._next_report_at = clock + 15
                    while self.public.dirty.pop() is not None:
                        pass
                    pool_by_symbol = {item.rules.symbol: item.owner_key for item in self.universe}
                    locked_pools = {
                        pool_by_symbol[symbol]
                        for symbol, quantity in self.account.reducer.positions.items()
                        if quantity != 0 and symbol in pool_by_symbol
                    }
                    locked_pools.update(self._inventory_locked_pool_ids())
                    if isinstance(self.public, PublicMarketShards):
                        locked_pools.update(pool for pool, (_, symbol) in self._sticky_markets.items()
                                            if self.public.assignments.get(symbol) in self.public.held)
                    # Cohorts are sticky: live telemetry affects initial choice
                    # and replacement of unsafe/ineligible pools, but one noisy
                    # minute cannot churn otherwise healthy quotes and destroy
                    # the daily uptime needed for any reward.
                    desired, desired_pools, reserved_pools = await self._select_recovery_cohort(
                        locked_pools, permit.maximum_active_pools)
                    if self._continuous_service:
                        working_buy_counts = Counter(
                            order.symbol for order in self.account.reducer.orders.values()
                            if order.working and order.side is Side.BUY
                        )
                        desired = order_for_resting_focus(
                            desired, progress_by_key=self._uptime,
                            working_buy_counts=working_buy_counts,
                            incumbent_symbols={symbol for _, symbol in self._sticky_markets.values()},
                        )
                    self._sticky_pool_ids = set(desired_pools)
                    self._selected_portfolio_report = portfolio_reward_report(desired)
                    self._selected_portfolio_report['selection_preferences'] = [
                        {'symbol': c.symbol, 'event_id': c.event_id,
                         'vacant_slot_focus_score_proxy': str(c.selection_focus_score_proxy)
                             if c.selection_focus_score_proxy is not None else None,
                         'incumbent_retained': c.owner_key in self._sticky_markets}
                        for c in desired]
                    self._save_sticky_markets(desired, reserved_pools)
                    candidates_by_symbol = {item.symbol: item for item in desired}
                    await self._cancel_ineligible_working_entries(candidates_by_symbol)
                    self._activated_pool_ids = set(locked_pools)
                    for candidate in desired:
                        await self._maintain_candidate(candidate)
                    next_rank_at = clock + 1
                    next_stale_check_at = clock + 3
                await self._exit_due_positions()
                await asyncio.sleep(1)
            # Workers may halt the gate between iterations; don't silently close.
            self._check_background_failures(safety, safety_clock, adverse_guard, reward_check)
            await self.drain()
            if commissioning_seconds is not None and self._maker_ack_count == 0:
                raise RuntimeError("commissioning did not observe a working maker order; no success claimed")
            finish_supervised_activation(
                permit, self.journal,
                supervisor_session_id=supervisor_session_id, outcome="closed",
            )
        except Exception as exc:
            self.log.exception("active_runtime_failed", ErrorClass.EXECUTION, exc)
            drained_flat = False
            try:
                await self.drain()
                drained_flat = True
            except Exception as drain_exc:
                self.gate.halt()
                self.log.exception("emergency_drain_failed", ErrorClass.EXECUTION, drain_exc)
                self._write_status("halted", False, "emergency drain failed; manual reconciliation required")
            request_failure = getattr(self, '_private_request_failure', None)
            # A service response is not evidence that an attempted order failed.
            # Only the completed exact terminal/flat drain below permits a new
            # supervised session attempt; no old request is ever replayed.
            recovered_request = self._request_failure_proven_recoverable(exc, drained_flat=drained_flat)
            # A secondary acknowledgement timeout must not disguise a permanent
            # request rejection as a generic transient TimeoutError.
            can_recover = (recovered_request if self._continuous_service and request_failure is not None
                           else drained_flat and (not self._continuous_service or transient_failure(exc)))
            can_recover = can_recover and self._dynamic_pending_cap > 0
            if recovered_request:
                self.log.event(logging.WARNING, 'request_service_failure_reconciled_flat',
                               method=request_failure.method, status=request_failure.status,
                               code=request_failure.code, old_request_replayed=False)
            finish_supervised_activation(
                permit, self.journal,
                supervisor_session_id=supervisor_session_id,
                outcome="recoverable_flat" if can_recover else "attention",
            )
            self._recoverable_exit = can_recover
            raise
        finally:
            if self._reward_reporting_task is not None:
                self._reward_reporting_task.cancel()
                await asyncio.gather(self._reward_reporting_task, return_exceptions=True)
                self._reward_reporting_task = None
            if safety_clock is not None:
                safety_clock.cancel()
                await asyncio.gather(safety_clock, return_exceptions=True)
            if adverse_guard is not None:
                adverse_guard.cancel()
                await asyncio.gather(adverse_guard, return_exceptions=True)
            if reward_check is not None:
                reward_check.cancel()
                await asyncio.gather(reward_check, return_exceptions=True)
            if safety is not None:
                safety.cancel()
                await asyncio.gather(safety, return_exceptions=True)

    async def _prepare_activation_inventory(self) -> None:
        if not self._last_snapshot.positions and not self.account.reducer.positions:
            return
        if not self._continuous_service:
            await self._exit_due_positions(force=True)
            return
        # A fresh authorized v7 run may inherit *known* inventory. Its normal
        # scoped worker can wait for liquidity instead of failing startup.
        if not self.account.snapshots_fresh or not self.account.equity_classification_complete:
            raise RuntimeError('startup inventory is not exactly reconciled')
        # Startup cancellations may themselves fill. Do not reuse the earlier
        # prepare snapshot as if it described post-cancellation inventory.
        version = self.account.fill_version
        snapshot = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        if (snapshot.active_orders or version != self.account.fill_version
                or not self.account.snapshots_fresh or not self.account.equity_classification_complete):
            raise RuntimeError('startup inventory changed during authenticated proof')
        assert_position_consistency(snapshot, self.account.reducer.positions)
        catalog_symbols = {item.rules.symbol for item in self.universe}
        for symbol, signed in self.account.reducer.positions.items():
            if (symbol not in self._permitted_symbols or self.journal.pending_exit(symbol)
                    or self.journal.has_nonterminal_intents(symbol)
                    or self.journal.get_deadline(symbol, 'YES' if signed > 0 else 'NO') is None):
                raise RuntimeError('startup inventory lacks safe original ownership/deadline')
            if symbol not in catalog_symbols:
                if symbol not in self._inventory_catalog_holds:
                    raise RuntimeError('startup inventory is outside the reconciled catalog scope')
            self.account.block_entries(symbol, fill=True)
        self._last_snapshot = snapshot

    def _request_failure_proven_recoverable(self, exc: BaseException, *, drained_flat: bool) -> bool:
        failure = self._private_request_failure
        return bool(drained_flat and self._dynamic_pending_cap > 0
                    and self.account.snapshots_fresh and self.account.equity_classification_complete
                    and not self.journal.nonterminal_sent_ids()
                    and not self.account.reducer.positions and not self.account.reducer.pending_order_count()
                    and isinstance(failure, WebSocketRequestRejected) and failure.service_failure
                    and (exc is failure or isinstance(exc, TimeoutError)))

    async def drain(self) -> None:
        self.gate.state = EngineState.DRAINING
        async with asyncio.timeout(_DRAIN_SECONDS):
            for task in self._shard_restarts.values():
                task.cancel()
            await asyncio.gather(*self._shard_restarts.values(), return_exceptions=True)
            self._shard_restarts.clear()
            if isinstance(self.public, PublicMarketShards) and self.public.held:
                self._stream_holds.add('public')
            # A fatal pump failure (for example a rejected request) is not a
            # retryable outage and therefore may never install a recovery hold.
            # Still restore readers for shutdown proof/cancellation, not trading.
            # Otherwise every cancel waits for a terminal event on a dead pump
            # and full-width REST fallback exhausts the drain deadline.
            if self._stream_holds or any(task.done() for task in self._tasks):
                await self._restore_drain_streams()
            await self._drain_bounded()

    async def _restore_drain_streams(self) -> None:
        """Restore transports only for quarantine/proof/reduction, never entry."""
        tasks = list(self._tasks) + list(self._stream_restarts.values())
        if self._stream_recovery_task is not None:
            tasks.append(self._stream_recovery_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._stream_restarts.clear()
        self._stream_recovery_task = None
        self._stream_ready.clear()
        for component in ('public', 'private'):
            self._stream_generation[component] += 1
        self._private_recovery_orders.update(o.order_id for o in self.account.reducer.orders.values() if o.working)
        self.account.last_balance_at = self.account.last_position_at = None
        self.account.balances_seen = self.account.positions_seen = False
        self.account.subscription_acknowledged = False
        self.account.reducer.position_update_ns = 0
        # Failed attempted sends remain latched. Only a proven pre-send failure
        # can release its cancel owner after connection replacement.
        for oid, task in tuple(self._cancel_tasks.items()):
            if task.done() and not task.cancelled() and isinstance(task.exception(), TransportChangedBeforeSend):
                self._cancel_tasks.pop(oid)
        await asyncio.gather(self._reconnect_stream('public', for_drain=True),
                             self._reconnect_stream('private', for_drain=True))
        self._tasks = [asyncio.create_task(self._public_pump()), asyncio.create_task(self._private_pump())]
        if hasattr(self.public, 'prove_liveness'):
            self._tasks.append(asyncio.create_task(self._public_liveness_pump()))
        await self._wait_ready(15)
        while True:
            try:
                await self._recovery_account_proof()
                break
            except RecoveryPending:
                await asyncio.sleep(1)
        self._stream_holds.clear()
        self._private_recovery_orders.clear()
        self.log.event(logging.INFO, 'drain_transports_reconciled', trading_resumed=False)

    async def _drain_bounded(self) -> None:
        # Start known-entry cancellation immediately, even if another market's
        # exit is waiting. All callers still share the same per-order owner.
        entries = [o for o in tuple(self.account.reducer.orders.values()) if o.working and o.side is Side.BUY]
        owned = [o for o in entries if self.journal.knows_client_order_id(o.client_order_id)]
        unknown_entry = len(entries) != len(owned)
        cancels = [asyncio.create_task(self._cancel_order_and_confirm(o.order_id)) for o in owned]
        owners = set(self._exit_workers.values()) | set(cancels)
        try:
            if owners:
                deadline = time.monotonic() + _DRAIN_JOIN_SECONDS
                pending = owners
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("drain owners did not finish; mutation state retained for attention")
                    _, pending = await asyncio.wait(pending, timeout=min(5, remaining))
                    if pending:
                        self._write_status("draining", False, "waiting for existing mutation owners; no replay")
                # Do not hide a rejected/ambiguous worker behind a forced retry.
                for task in owners:
                    task.result()
        finally:
            for task in cancels:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*cancels, return_exceptions=True)
        self._exit_workers.clear()
        if unknown_entry:
            raise RuntimeError("drain found an unowned local entry; no mutation of it attempted")
        for order in build_account_snapshot([], await self.rest.active_orders()).active_orders:
            if not order.client_order_id or not self.journal.knows_client_order_id(order.client_order_id):
                raise RuntimeError("drain found an unowned active order")
            await self._cancel_order_and_confirm(order.order_id)
        reconciled = build_account_snapshot(await self.rest.positions(), [])
        assert_position_consistency(reconciled, self.account.reducer.positions)
        await self._exit_due_positions(force=True)
        final = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        if final.positions or final.active_orders or self.account.reducer.positions:
            raise RuntimeError("drain did not prove globally flat with zero active orders")
        self._resolve_prior_intents(await self._targeted_prior_intent_history([]))
        if not self.account.equity_classification_complete:
            raise RuntimeError("flat position snapshot obtained but cash reconciliation is incomplete")
        self._exit_uncertain.clear()
        self.gate.halt()
        self._write_status("stopped_flat", False, "drain completed and account is flat")

    async def _import_authenticated_manual_closes(
        self, *, window_start: datetime, window_end: datetime,
    ) -> int:
        """Resolve manual-flat gaps from exact, bounded Gemini order history."""
        imported = 0
        for gap in self.journal.manual_close_gaps(window_start, window_end):
            expected_outcome = "YES" if gap.signed_quantity > 0 else "NO"
            expected_quantity = abs(gap.signed_quantity)
            rows = await self.rest.filled_order_history(
                symbol=gap.symbol,
                closed_from=gap.opened_at,
                closed_to=gap.observed_at,
            )
            candidates: list[tuple[dict[str, Any], datetime, Decimal]] = []
            for row in rows:
                try:
                    status = str(row["status"]).upper().replace("CANCELLED", "CANCELED")
                    symbol = canonical_symbol(str(row["symbol"]))
                    side = str(row["side"]).upper()
                    outcome = str(row["outcome"]).upper()
                    quantity = Decimal(str(row["filledQuantity"]))
                    remaining = Decimal(str(row["remainingQuantity"]))
                    price = Decimal(str(row["avgExecutionPrice"]))
                    event_at = _parse_time(str(row["updatedAt"]))
                    order_id = str(row["orderId"])
                    client_order_id = str(row.get("clientOrderId", ""))
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "authenticated manual-close history has an invalid filled order"
                    ) from exc
                purpose = self.journal.intent_purpose(client_order_id)
                if purpose not in {None, "exit"}:
                    continue
                if (
                    status == "FILLED"
                    and symbol == gap.symbol
                    and side == "SELL"
                    and outcome == expected_outcome
                    and quantity == expected_quantity
                    and remaining == 0
                    and Decimal("0") < price < Decimal("1")
                    and gap.opened_at <= event_at < gap.observed_at
                    and order_id
                    and client_order_id
                ):
                    candidates.append((row, event_at, price))
            if len(candidates) != 1:
                raise RuntimeError(
                    "manual close cannot be matched uniquely to authenticated filled history"
                )
            row, event_at, price = candidates[0]
            if self.journal.record_reconciled_manual_close(
                order_id=str(row["orderId"]),
                client_order_id=str(row["clientOrderId"]),
                symbol=gap.symbol,
                side="SELL",
                outcome=expected_outcome,
                quantity=expected_quantity,
                price=price,
                conservative_fee=conservative_taker_fee(expected_quantity, price),
                event_at=event_at,
                audit_sequence=gap.audit_sequence,
            ):
                imported += 1
        if self.journal.manual_close_gaps(window_start, window_end):
            raise RuntimeError("manual-close history remains incomplete after reconciliation")
        return imported

    async def reconcile_verified_manual_flat(self) -> int:
        """Repair expected account state after a separately verified manual close."""
        if not self.read_only:
            raise RuntimeError("manual-flat reconciliation must use read-only venue access")
        if self.gate.state is not EngineState.READY:
            raise RuntimeError("manual-flat reconciliation requires completed startup reconciliation")
        if self._last_snapshot.positions or self._last_snapshot.active_orders:
            raise RuntimeError("manual-flat reconciliation requires a flat, order-free REST snapshot")
        now = _utc_now()
        start, end = RiskWindowPolicy(self.config.risk.risk_window_loss_usd).bounds(now)
        # A first reconciliation adopts the authenticated flat baseline and
        # creates the audit boundary.  A repeated repair of an existing audit
        # must not create another boundary before its historical close arrives.
        if not self.account.equity_classification_complete:
            self.account.adopt_verified_manual_flat(observed_at=now)
        if not self.account.equity_classification_complete:
            raise RuntimeError("manual-flat baseline did not reconcile account classification")
        imported = await self._import_authenticated_manual_closes(
            window_start=start, window_end=end,
        )
        realized_pnl, complete = self.journal.realized_pnl_for_window(start, end)
        if not complete:
            self._write_status("ready_paused", False, "flat reconciled; manual-close execution evidence missing; activation blocked")
            raise RuntimeError("manual close is flat but realized P&L requires authenticated closing fills")
        PersistentRiskWindow(
            RiskWindowPolicy(self.config.risk.risk_window_loss_usd, self.config.risk.max_pending_orders), self.journal,
        ).update(
            now=now,
            realized_pnl=realized_pnl,
            classification_complete=True,
            reconciled=True,
        )
        self._write_status(
            "ready_paused", False,
            "verified manual flatten reconciled; fresh activation still required",
        )
        return imported

    async def close(self) -> None:
        for task in self._pilot_proof_tasks.values():
            task.cancel()
        await asyncio.gather(*self._pilot_proof_tasks.values(), return_exceptions=True)
        self._pilot_proof_tasks.clear()
        if self._closed:
            return
        self._closed = True
        for task in self._exit_workers.values():
            task.cancel()
        await asyncio.gather(*self._exit_workers.values(), return_exceptions=True)
        self._exit_workers.clear()
        self.log.event(logging.INFO, "runner_closing", phase=self.gate.state.value,
                       account_flat_verified=False, reason="closing local resources; not venue flatness proof")
        recovery_tasks = list(self._stream_restarts.values()) + list(self._shard_restarts.values())
        if self._stream_recovery_task is not None:
            recovery_tasks.append(self._stream_recovery_task)
        for task in recovery_tasks:
            task.cancel()
        await asyncio.gather(*recovery_tasks, return_exceptions=True)
        if self._reward_reporting_task is not None:
            self._reward_reporting_task.cancel()
            await asyncio.gather(self._reward_reporting_task, return_exceptions=True)
            self._reward_reporting_task = None
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for task in self._cancel_tasks.values():
            task.cancel()
        await asyncio.gather(*self._cancel_tasks.values(), return_exceptions=True)
        self._cancel_tasks.clear()
        cancel_read_tasks = []
        if self._cancel_active_read is not None:
            cancel_read_tasks.append(self._cancel_active_read[1])
        cancel_read_tasks.extend(task for _, task in self._cancel_history_reads.values())
        for task in set(cancel_read_tasks):
            task.cancel()
        await asyncio.gather(*set(cancel_read_tasks), return_exceptions=True)
        self._cancel_active_read = None
        self._cancel_history_reads.clear()
        if self._cash_reconciliation_task is not None:
            self._cash_reconciliation_task.cancel()
            await asyncio.gather(self._cash_reconciliation_task, return_exceptions=True)
            self._cash_reconciliation_task = None
        if self._settlement_reconciliation_task is not None:
            self._settlement_reconciliation_task.cancel()
            await asyncio.gather(self._settlement_reconciliation_task, return_exceptions=True)
            self._settlement_reconciliation_task = None
        try:
            await asyncio.gather(self.private.close(), self.public.close(), return_exceptions=True)
        finally:
            try:
                archive = getattr(self, "_queue_archive", None)
                if archive is not None:
                    try:
                        archive.close()
                    except Exception:
                        pass  # Optional diagnostic close must not strand ownership.
                self._logging.close()
            finally:
                try:
                    if self._journal_backups is not None:
                        await self._journal_backups.close()
                finally:
                    try:
                        self.journal.close()
                    finally:
                        self.lock.release()

    async def __aenter__(self) -> ProductionOrchestrator:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()
