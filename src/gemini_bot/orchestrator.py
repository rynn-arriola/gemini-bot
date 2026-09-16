from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from websockets.exceptions import ConnectionClosed

from .account_runtime import AccountRuntime
from .activation import (
    consume_activation_permit,
    finish_supervised_activation,
    load_activation_permit,
    policy_digest,
)
from .config import DEFAULT_POLICY_PATH, LiveConfig
from .discovery import RewardUniverseLoader
from .catalog import join_reward_contracts
from .domain import EngineState, Outcome, canonical_symbol
from .engine import LiveEngineGate
from .execution import IntentLedger, IntentState, OrderPurpose
from .exit_pricing import exact_crossing_exit
from .live_ws import (
    EntryStateChangedBeforeSend,
    LiveWebSocket,
    MutationNotSentError,
    QuoteChangedBeforeSend,
)
from .operational_logging import ErrorClass, OperationalLogger, configure_logging
from .persistent_risk import PersistentRiskWindow
from .profitability import (
    ProfitabilityTracker,
    active_maker_rebate_fractions,
    conservative_taker_fee,
)
from .public_ws import PublicMarketSocket
from .rank_report import public_rank_rows, rank_live_universe
from .rate_limits import AdmissionGovernor
from .reward_ranking import select_sticky_candidates
from .reconciliation import (
    AccountSnapshot,
    assert_position_consistency,
    assert_stable_account_bracket,
    build_account_snapshot,
    exposure_from_snapshot,
)
from .rest_client import ApiError, GeminiReadRestClient
from .risk_gate import admit_quote_pair
from .risk_gate import ExposureState
from .risk_windows import RiskWindowPolicy
from .runtime_files import atomic_write_json, runtime_status
from .storage import (
    AuthenticatedCashAdjustment,
    SQLiteJournal,
    WindowsOwnerLock,
    shared_control_directory,
)
from .uptime import sample_and_load_progress, eastern_day_window
from .reward_uptime_report import ExchangeRewardUptime, REFRESH_SECONDS, event_identity
from .market_state import LocalBook
from .stream_recovery import RecoveryPending, terminal_history_status


_UPTIME_PROXY_REJECTION = "50% uptime is no longer reachable in the estimated window"
_STREAM_RECOVERY_SECONDS = 60


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


class ProductionOrchestrator:
    """Single Windows process that owns state, rate admission, and mutations.

    Construction is inert. ``prepare`` performs reads/subscriptions only. The
    mutation path cannot be entered without a valid one-use activation permit.
    """

    def __init__(self, config: LiveConfig, directory: Path, *, read_only: bool = False) -> None:
        self.config = config
        self.read_only = read_only
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = SQLiteJournal(self.directory / "production.sqlite")
        self.control_directory = shared_control_directory()
        self.lock = WindowsOwnerLock(self.control_directory / "mutation-owner.lock")
        self._lifecycle_lock = asyncio.Lock()
        self._exit_uncertain: set[str] = set()
        self.governor = AdmissionGovernor()
        self.ledger = IntentLedger(self.journal)
        self.rest = GeminiReadRestClient(
            self.governor, api_key=config.api_key, api_secret=config.api_secret
        )
        self.public = PublicMarketSocket(self.governor)
        self.private = LiveWebSocket(
            config.api_key, config.api_secret, self.governor, self.ledger,
            read_only=read_only, cancel_on_disconnect=not read_only,
        )
        self.account = AccountRuntime(self.ledger, self.journal)
        self.account.inventory_age_seconds = config.risk.max_inventory_age_seconds
        self.account.exit_before_expiry_seconds = config.risk.force_exit_before_expiry_seconds
        self.gate = LiveEngineGate()
        self.private.mutation_guard = self._validate_send
        self.universe = []
        self.ranked = []
        self.profitability = ProfitabilityTracker()
        self._maker_rebate_fractions: dict[str, Decimal] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._activated_pool_ids: set[str] = set()
        self._sticky_pool_ids: set[str] = set()
        self._sticky_markets: dict[str, tuple[str, str]] = {}
        self._expiry: dict[str, datetime] = {}
        self._trade_start: dict[str, datetime] = {}
        self._sports_start: dict[str, datetime] = {}
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
        self._reward_epoch: tuple[str, bool] | None = None
        self._catalog_scope = False
        self._commissioning_expected_yes_price: Decimal | None = None
        self._commissioning_expected_no_price: Decimal | None = None
        self._quote_cooldown_until: dict[str, float] = {}
        self._cancel_tasks: dict[str, asyncio.Task[None]] = {}
        self._adverse_tasks: dict[str, asyncio.Task[bool]] = {}
        self._entry_cancel_symbols: dict[str, int] = {}
        self._cash_reconciliation_task: asyncio.Task[int] | None = None
        self._read_recovery_attempts: dict[str, int] = {}
        self._risk_read_verified = False
        self._stream_holds: set[str] = set()
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
        self.account.event_sink = lambda event, fields: self.log.event(logging.INFO, event, **fields)
        self._last_logged_status: tuple[str, bool, str] | None = None
        self._last_status_log_at = 0.0
        self.log.event(logging.INFO, "runner_created", read_only=read_only)

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
             "stream_recovery_holds": sorted(self._stream_holds),
             "session_expires_at": self._permit_expires_at.isoformat() if self._permit_expires_at else None,
             "uptime_override_active": self._override_active(),
             "uptime_override_until": self._uptime_override_until.isoformat() if self._uptime_override_until else None,
             "continue_after_override": self._continue_after_override,
             "reward_epoch": self._reward_epoch},
        )

    def _override_active(self, now: datetime | None = None) -> bool:
        return self._commissioning_ignore_uptime_proxy and (
            self._uptime_override_until is None or (now or _utc_now()) < self._uptime_override_until
        )

    def _current_reward_epoch(self, now: datetime) -> tuple[str, bool]:
        return eastern_day_window(now)[0].isoformat(), self._override_active(now)

    def _advance_reward_epoch(self, now: datetime) -> bool:
        """Rebuild day-specific eligibility before admitting any new-day entry."""
        epoch = self._current_reward_epoch(now)
        if epoch == self._reward_epoch:
            return False
        self._uptime = sample_and_load_progress(
            self.journal, self.universe, self.account.reducer, now=now,
            books=self.public.books, market_data_verified_at=self._market_data_verified_at(),
            exit_before_expiry_seconds=self.config.risk.force_exit_before_expiry_seconds,
            sports_exit_before_start_seconds=self.config.risk.sports_exit_before_start_seconds,
        )
        self.ranked = rank_live_universe(
            self.universe, self.public.books, now=now,
            quantity=self.config.strategy.quote_quantity, offset_ticks=self.config.strategy.quote_offset_ticks,
            uptime=self._uptime, account=self.account.reducer,
            market_data_verified_at=self._market_data_verified_at(), profitability=self.profitability,
            target_uptime=self.config.strategy.target_uptime, maker_rebate_fractions=self._maker_rebate_fractions,
            preferred_symbols={pool: pair[1] for pool, pair in self._sticky_markets.items()},
            allow_uptime_override=self._override_active(now),
        )
        self._reward_epoch = epoch
        self.log.event(logging.INFO, "reward_epoch_changed", eastern_day_start=epoch[0],
                       uptime_override_active=epoch[1], same_process=True)
        return True

    def _load_sticky_markets(self) -> None:
        raw = self.journal.get_runtime_state("sticky_markets_v1")
        if raw is None:
            return
        value = json.loads(raw)
        if (not isinstance(value, dict) or len(value) > 15
                or any(not isinstance(pool, str) or not isinstance(pair, list) or len(pair) != 2
                       or any(not isinstance(s, str) or not s for s in pair)
                       for pool, pair in value.items())):
            raise RuntimeError("persisted market cohort is malformed")
        self._sticky_markets = {pool: tuple(pair) for pool, pair in value.items()}
        self._sticky_pool_ids = set(self._sticky_markets)

    def _save_sticky_markets(self, desired: list[Any], locked_pools: set[str]) -> None:
        markets = {c.pool_id: (c.event_ticker, c.symbol) for c in desired}
        for c in self.universe:
            if c.pool.pool_id in locked_pools:
                markets[c.pool.pool_id] = (c.event_ticker, c.rules.symbol)
        if markets != self._sticky_markets:
            self.journal.set_runtime_state("sticky_markets_v1", json.dumps(markets, sort_keys=True), updated_at=_utc_now())
            self._sticky_markets = markets

    def _recovery_stop_requested(self) -> bool:
        now = _utc_now()
        return (self._closed or (self.directory / "STOP").exists()
                or self.gate.state not in {EngineState.QUOTING, EngineState.READY}
                or (self._permit_expires_at is not None and now >= self._permit_expires_at)
                or (not self._continue_after_override and self._uptime_override_until is not None
                    and now >= self._uptime_override_until))

    def _check_stream_recovery(self) -> None:
        if self._stream_recovery_task is not None and self._stream_recovery_task.done():
            self._stream_recovery_task.result()

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

    async def _reconnect_stream(self, component: str) -> None:
        transport = self.public if component == "public" else self.private
        for attempt in range(1, 4):
            self._stream_attempts[component] += 1
            if self._stream_attempts[component] > 3:
                raise RuntimeError("stream reconnect attempt budget exhausted")
            if self._recovery_stop_requested():
                raise RuntimeError("stream recovery interrupted by stop or expiry")
            try:
                async with asyncio.timeout(15):
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
            except (ConnectionClosed, ConnectionError, TimeoutError, OSError) as exc:
                self.log.exception("stream_reconnect_retry", ErrorClass.TRANSPORT, exc,
                                   component=component, attempt=attempt)
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** (attempt - 1))

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
        # Private disconnect can hide a fill. Check every pre-disconnect working
        # order against exact terminal history; cash equality alone is not proof.
        histories: dict[str, list[dict[str, Any]]] = {}
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
            status = terminal_history_status(order, matches[0])
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

    async def _prove_stream_recovery(self) -> None:
        try:
            async with asyncio.timeout(_STREAM_RECOVERY_SECONDS):
                while self._stream_holds:
                    if self._recovery_stop_requested():
                        raise RuntimeError("recovery proof interrupted by stop or expiry")
                    for task in self._stream_restarts.values():
                        if task.done():
                            task.result()
                    # REST active-order evidence precedes cancellation: the old
                    # connection's cancel-on-disconnect may already have removed
                    # cached orders. Never blindly cancel that stale cache.
                    if "private" in self._stream_ready and self.account.snapshots_fresh and self.account.readiness.complete:
                        try:
                            generation = dict(self._stream_generation)
                            async with self._lifecycle_lock:
                                await self._recovery_account_proof()
                        except RecoveryPending:
                            await asyncio.sleep(1)
                            continue
                        except ApiError as exc:
                            if exc.status not in {429, 500, 502, 503, 504}:
                                raise
                            await asyncio.sleep(2)
                            continue
                        # No await between the final authority check and resume.
                        if (generation != self._stream_generation or self._stream_ready != {"public", "private"}
                                or not self.universe or not all(self._book_fresh(item.rules.symbol) for item in self.universe)):
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
        transient = exc.status in {429, 500, 502, 503, 504} or (
            exc.status is None and exc.reason in {"URLError", "TimeoutError", "ConnectionError", "ConnectionResetError"}
        )
        if (not transient or self.gate.state is not EngineState.QUOTING
                or not self.account.snapshots_fresh or not self.account.equity_classification_complete):
            raise exc
        attempt = self._read_recovery_attempts.get(component, 0) + 1
        self._read_recovery_attempts[component] = attempt
        delay = min(60, 5 * 2 ** min(attempt - 1, 4))
        self.log.event(logging.WARNING, "read_recovery_wait", component=component,
                       attempt=attempt, retry_seconds=delay, http_status=exc.status,
                       entries_paused=True, exits_enabled=True)
        return float(delay)

    def _complete_read_recovery(self, component: str) -> None:
        if self._read_recovery_attempts.pop(component, None) is not None:
            self.log.event(logging.INFO, "read_recovery_completed", component=component,
                           remaining_entry_holds=sorted(self._read_recovery_attempts))

    def _write_rank_report(self, now: datetime) -> None:
        rows = public_rank_rows(self.ranked)
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
        atomic_write_json(self.directory / "rank-report.json", {
            "generated_at": now.isoformat(), "candidates": rows,
            "exchange_reward_uptime": self.exchange_reward_uptime.metadata(now=now),
            "exchange_uptime_by_event": views,
        })

    async def _reward_reporting_loop(self) -> None:
        # Reporting errors only change availability; never cancel quotes or
        # rewrite local feasibility, risk, or non-trading-cash classification.
        while not self._closed:
            await self.exchange_reward_uptime.refresh(self.reward_reporting_rest, now=_utc_now())
            self.log.event(
                logging.WARNING if self.exchange_reward_uptime.error else logging.INFO,
                "reward_uptime_report_refresh",
                fetch_status="failed" if self.exchange_reward_uptime.error else "ok",
                reported_event_dates=len(self.exchange_reward_uptime.rows),
            )
            await asyncio.sleep(REFRESH_SECONDS)

    def _handle_contract_status(self, frame: dict[str, Any]) -> None:
        symbol = str(frame.get("s", "")).upper()
        if symbol not in self._expiry or str(frame.get("n", "")).lower() == "active":
            return
        self.account.blocked_entries.add(symbol)
        self.journal.audit("contract_lifecycle_block", {"symbol": symbol})
        if self.account.reducer.positions.get(symbol, Decimal("0")) != 0:
            raise RuntimeError("tracked contract changed lifecycle with open inventory")
        if self.account.reducer.risk_adding_order_ids(symbol):
            if self.gate.state is not EngineState.QUOTING:
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
                except (StopAsyncIteration, ConnectionClosed, ConnectionError, TimeoutError, OSError) as exc:
                    await self._recover_stream("public", exc, generation=generation)
                    frames, generation = self.public.frames(), self._stream_generation["public"]
                    continue
                if generation != self._stream_generation["public"]:
                    await self._recover_stream("public", ConnectionError("obsolete stream"), generation=generation)
                    frames, generation = self.public.frames(), self._stream_generation["public"]
                    continue
                observed_at = _utc_now()
                if frame.get("e") == "contractStatus":
                    self._handle_contract_status(frame)
                if (all(key in frame for key in ("s", "t", "p", "q", "m"))
                        and str(frame["s"]).upper() in self.public.books):
                    self.profitability.observe_trade(frame, observed_at=observed_at)
                updated_symbol = self.public.apply_frame(frame)
                if (
                    frame.get("e") == "depthUpdate"
                    and updated_symbol is not None
                    and updated_symbol not in self.public.resync_required
                ):
                    symbol = str(frame.get("s", "")).upper()
                    book = self.public.books.get(symbol)
                    if book is not None:
                        self.profitability.observe_book(book, observed_at=observed_at)
                self._mark_live_risk()
                if updated_symbol is not None and updated_symbol in self.public.resync_required:
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
                except (StopAsyncIteration, ConnectionClosed, ConnectionError, TimeoutError, OSError) as exc:
                    await self._recover_stream("private", exc, generation=generation)
                    frames, generation = self.private.messages(), self._stream_generation["private"]
                    continue
                if generation != self._stream_generation["private"]:
                    await self._recover_stream("private", ConnectionError("obsolete stream"), generation=generation)
                    frames, generation = self.private.messages(), self._stream_generation["private"]
                    continue
                self.account.apply(frame)
                self._mark_live_risk()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gate.halt()
            self.log.exception("private_stream_failed", ErrorClass.TRANSPORT, exc)
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

    async def prepare(self, *, timeout: float = 30, manual_flat_repair: bool = False) -> PreparedState:
        """Read-only startup. This method contains no order mutation call."""
        if manual_flat_repair and not self.read_only:
            raise RuntimeError("manual-flat preparation requires read-only mode")
        self.lock.acquire()
        # All output directories share one durable mutation/permit/risk history.
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
        self.ledger = IntentLedger(canonical)
        self.private._ledger = self.ledger
        self.account = AccountRuntime(self.ledger, canonical)
        self.account.event_sink = lambda event, fields: self.log.event(logging.INFO, event, **fields)
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
                raise RuntimeError("Gemini liquidity rewards are not currently enabled")
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
                    raise RuntimeError("an unowned active order blocks production startup")

            self._load_sticky_markets()
            self.universe = await RewardUniverseLoader(self.rest).load(
                maximum_pools=self.config.risk.max_tracked_markets,
                preferred=self._sticky_markets, now=_utc_now(),
            )
            if not self.universe:
                raise RuntimeError("no active reward pools passed discovery")
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
            if any(row.symbol not in self._expiry for row in before.positions):
                raise RuntimeError("existing inventory outside the subscribed universe requires reconciliation")
            # Seed identity before sparse terminal WebSocket updates can arrive.
            for row in before.active_orders:
                self.account.reducer.apply_order_update({
                    "e": "orderUpdate", "i": row.order_id, "c": row.client_order_id,
                    "s": row.symbol, "S": row.side.value, "O": row.outcome.value,
                    "q": str(row.original_quantity), "z": str(row.remaining_quantity),
                    "p": str(row.price), "X": "OPEN",
                })
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
            await self.private.subscribe_account()
            await self._wait_ready(timeout)

            positions_after, orders_after = await asyncio.gather(
                self.rest.positions(), self.rest.active_orders()
            )
            after = build_account_snapshot(positions_after, orders_after)
            assert_stable_account_bracket(before, after)
            assert_position_consistency(after, self.account.reducer.positions)
            repair_flat = manual_flat_repair and not after.positions and not after.active_orders
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
            )
            self.ranked = rank_live_universe(
                self.universe, self.public.books, now=now,
                quantity=self.config.strategy.quote_quantity,
                offset_ticks=self.config.strategy.quote_offset_ticks,
                uptime=self._uptime,
                account=self.account.reducer,
                market_data_verified_at=self._market_data_verified_at(),
                profitability=self.profitability,
                target_uptime=self.config.strategy.target_uptime,
                maker_rebate_fractions=self._maker_rebate_fractions,
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

    async def _cancel_order_and_confirm(self, order_id: str, *, timeout: float = 45) -> None:
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
        try:
            await asyncio.shield(task)
        finally:
            # Keep failures latched: an unknown cancel cannot be blindly resent.
            if task.done() and not task.cancelled() and task.exception() is None:
                if self._cancel_tasks.get(order_id) is task:
                    self._cancel_tasks.pop(order_id, None)

    async def _cancel_order_and_confirm_once(self, order_id: str, *, timeout: float = 45) -> None:
        terminal_event = self.account.terminal_events.setdefault(order_id, asyncio.Event())
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
                rows = await self.rest.active_orders()
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
                    history = await self.rest.recent_order_history(
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
                    if order is not None:
                        order.remaining_quantity = Decimal("0")
                        order.status = str(terminal["status"]).upper().replace("CANCELLED", "CANCELED")
                        self.journal.resolve_intent_terminal(order.client_order_id, order_id)
                        intent = self.ledger.get(order.client_order_id)
                        if intent is not None and intent.state in {IntentState.SENT_UNKNOWN, IntentState.ACKNOWLEDGED}:
                            self.ledger.mark_terminal(order.client_order_id)
                    return
            await asyncio.sleep(0.25)
        raise TimeoutError("order cancellation was not proven terminal") from last_transient

    async def _cancel_symbol_entries(self, symbol: str) -> None:
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
        if not self._book_fresh(symbol):
            raise QuoteChangedBeforeSend("market became stale while waiting to send")
        if params["side"] == "BUY":
            if self._continue_after_override:
                if self._reward_epoch != self._current_reward_epoch(_utc_now()):
                    raise EntryStateChangedBeforeSend("reward day/override changed; fresh ranking required")
                current = next((c for c in self.ranked if c.symbol == symbol), None)
                if current is None or not self._candidate_actionable(current):
                    raise EntryStateChangedBeforeSend("fresh reward eligibility is required")
            if self._read_recovery_attempts or self._stream_holds:
                raise EntryStateChangedBeforeSend("read recovery pauses new entries")
            if params.get("timeInForce") == "MOC":
                target = self._current_quote_target(symbol)
                leg = None if target is None else (
                    target.yes if params["eventOutcome"] == "YES" else target.no
                )
                if (leg is None or Decimal(str(params["price"])) != leg.price
                        or Decimal(str(params["quantity"])) != leg.quantity
                        or symbol in self._entry_cancel_symbols):
                    raise QuoteChangedBeforeSend("maker quote changed before send")
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
                if deadline is None or _utc_now() >= deadline:
                    raise EntryStateChangedBeforeSend(
                        "inventory safety action must precede any new entry"
                    )
            exposure = self._websocket_exposure()
            quantity = Decimal(str(params["quantity"]))
            if (exposure.pending_orders + 1 > self._dynamic_pending_cap
                    or exposure.gross_reservation_usd + quantity > self.config.risk.max_gross_reservation_usd
                    or exposure.market_exposure_usd.get(symbol, Decimal("0")) + quantity > self.config.risk.max_market_exposure_usd):
                raise EntryStateChangedBeforeSend(
                    "entry exposure changed while waiting to send"
                )
        else:
            if self._stream_holds and ("private" in self._stream_holds or "private" not in self._stream_ready or not self.account.snapshots_fresh
                                       or not self.account.equity_classification_complete):
                raise EntryStateChangedBeforeSend("recovery has not proved exact exit state")
            position = self.account.reducer.positions.get(symbol, Decimal("0"))
            if (abs(position) != Decimal(str(params["quantity"])) or position == 0
                    or params["eventOutcome"] != ("YES" if position > 0 else "NO")):
                raise RuntimeError("position changed while waiting to send exact exit")

    def _market_data_verified_at(self) -> datetime | None:
        observed = getattr(self.public, "last_message_at", None)
        if observed is not None:
            return observed
        # Test adapters and old recorded fixtures may not expose connection
        # metadata. Their newest applied book timestamp is the conservative
        # equivalent; production PublicMarketSocket always exposes it.
        values = [book.observed_at for book in self.public.books.values() if book.observed_at is not None]
        return max(values) if values else None

    def _public_stream_fresh(self) -> bool:
        observed = self._market_data_verified_at()
        if observed is None:
            return False
        age = (_utc_now() - observed).total_seconds()
        return 0 <= age <= float(self.config.strategy.stale_book_seconds)

    def _book_fresh(self, symbol: str) -> bool:
        book = self.public.books.get(symbol)
        return bool(book is not None and book.synchronized and self._public_stream_fresh())

    def _candidate_actionable(self, candidate: Any) -> bool:
        """Allow only the uptime proxy to be ignored by explicit approval.

        The candidate remains reported as reward-ineligible. This exception is
        solely for testing maker behavior and measuring live outcomes.
        """
        reward_or_proxy_ok = candidate.eligible_now or bool(
            self._override_active()
            and candidate.quote is not None
            and tuple(candidate.reasons) == (_UPTIME_PROXY_REJECTION,)
        )
        return bool(
            reward_or_proxy_ok
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

    async def _quote_pair_locked(self, candidate: Any) -> None:
        try:
            await self._quote_pair_current(candidate)
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
        if self._read_recovery_attempts or self._stream_holds:
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
            self._activated_pool_ids.add(candidate.pool_id)
            return
        second_id = await self.private.place_maker(candidate.quote.no)
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
        self._activated_pool_ids.add(candidate.pool_id)
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
                await self._cancel_symbol_entries(order.symbol)

    def _current_quote_target(self, symbol: str) -> Any | None:
        contract = next(
            (item for item in self.universe if item.rules.symbol == symbol), None,
        )
        if contract is None or not self._book_fresh(symbol):
            return None
        candidates = rank_live_universe(
            [contract], self.public.books, now=_utc_now(),
            quantity=self.config.strategy.quote_quantity,
            offset_ticks=self.config.strategy.quote_offset_ticks,
            account=self.account.reducer,
            market_data_verified_at=self._market_data_verified_at(),
            target_uptime=self.config.strategy.target_uptime,
        )
        return candidates[0].quote if candidates else None

    async def _cancel_too_aggressive_quote(self, symbol: str) -> bool:
        """Cancel when a book move leaves our entry closer than two ticks.

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
        target = self._current_quote_target(symbol)
        if target is None:
            await self._cancel_symbol_entries(symbol)
            return True
        targets = {Outcome.YES: target.yes.price, Outcome.NO: target.no.price}
        if any(order.price > targets[order.outcome] for order in working):
            await self._cancel_symbol_entries(symbol)
            self.journal.audit("adverse_move_quote_canceled", {
                "symbol": symbol,
                "reason": "inside_approved_two_tick_distance",
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
        if self._read_recovery_attempts:
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
        expected = {
            Outcome.YES: (candidate.quote.yes.price, candidate.quote.yes.quantity),
            Outcome.NO: (candidate.quote.no.price, candidate.quote.no.quantity),
        }
        actual = {order.outcome: (order.price, order.remaining_quantity) for order in working}
        if getattr(self, "_commissioning_single_leg", False):
            expected.pop(Outcome.NO)
        if working and (len(working) != len(expected) or actual != expected):
            await self._cancel_symbol_entries(candidate.symbol)
            working = []
        if not working:
            await self._quote_pair_locked(candidate)

    def _websocket_exposure(self) -> ExposureState:
        """Conservative live exposure from authenticated WebSocket truth."""
        gross = sum((abs(value) for value in self.account.reducer.positions.values()), Decimal("0"))
        market = {symbol: abs(value) for symbol, value in self.account.reducer.positions.items()}
        outcomes: dict[str, set[Outcome]] = {}
        pending = 0
        for order in self.account.reducer.orders.values():
            if not order.working:
                continue
            pending += 1
            if order.side.value == "BUY":
                reservation = order.remaining_quantity * Decimal("1")
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
        while self.gate.state in {EngineState.QUOTING, EngineState.DRAINING}:
            symbol = await self.account.safety_symbols.get()
            if "private" in self._stream_holds:
                # Coordinator first verifies which pre-disconnect orders still
                # exist. Cancel-on-disconnect may already have removed them.
                self.account.safety_symbols.task_done()
                continue
            while self._stream_holds and "private" not in self._stream_ready:
                self._check_stream_recovery()
                await asyncio.sleep(.1)
            async with self._lifecycle_lock:
                await self._cancel_symbol_entries(symbol)
            self.account.safety_symbols.task_done()

    async def _exit_due_positions(self, *, force: bool = False) -> None:
        async with self._lifecycle_lock:
            await self._exit_due_positions_locked(force=force)

    async def _exit_due_positions_locked(self, *, force: bool = False) -> None:
        for symbol, signed in tuple(self.account.reducer.positions.items()):
            if signed == 0:
                continue
            outcome = Outcome.YES if signed > 0 else Outcome.NO
            deadline = self.journal.get_deadline(symbol, outcome.value)
            if not force and deadline is None:
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
            if symbol in self._exit_uncertain or self.journal.pending_exit(symbol):
                raise TimeoutError("prior exit still requires terminal reconciliation; retry prohibited")
            self.account.blocked_entries.add(symbol)
            await self._cancel_symbol_entries(symbol)
            snapshot = build_account_snapshot(await self.rest.positions(), [])
            assert_position_consistency(snapshot, self.account.reducer.positions)
            signed = snapshot.signed_positions.get(symbol, Decimal("0"))
            if not signed:
                continue
            exit_started_at = asyncio.get_running_loop().time()
            attempt = 0
            while signed:
                if attempt >= 3 or asyncio.get_running_loop().time() - exit_started_at >= 30:
                    raise RuntimeError("exact IOC exit exhausted its bounded retry authority")
                if symbol in self._exit_uncertain or self.journal.pending_exit(symbol):
                    raise TimeoutError("prior exit still requires terminal reconciliation; retry prohibited")
                book = self.public.books.get(symbol)
                if not self._book_fresh(symbol):
                    raise RuntimeError("exit book is stale or unsynchronized")
                leg = exact_crossing_exit(book, signed) if book is not None else None
                if leg is None:
                    raise RuntimeError("fresh full depth cannot support an exact position exit")

                submitted_signed = signed
                attempt += 1
                self._exit_uncertain.add(symbol)
                try:
                    client_id = await self.private.exit_position(
                        leg, signed_position=submitted_signed,
                    )
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
                    raise RuntimeError("exact IOC exit exhausted its bounded retry authority")
                signed = retry_residual
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
            self.journal.clear_deadline(symbol, outcome.value)
            self._exit_uncertain.discard(symbol)
            self.account.blocked_entries.discard(symbol)

    async def _risk_mark(self) -> int:
        self._risk_read_verified = False
        fill_version = self.account.fill_version
        snapshot = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        now = _utc_now()
        policy = RiskWindowPolicy(self.config.risk.risk_window_loss_usd)
        start, end = policy.bounds(now)
        realized_pnl, history_complete = self.journal.realized_pnl_for_window(start, end)
        if not history_complete:
            raise RuntimeError("closed-fill realized P&L cannot be classified safely")
        if self.gate.state is EngineState.QUOTING and (
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
        while self.gate.state is EngineState.QUOTING:
            now = _utc_now()
            clock = asyncio.get_running_loop().time()
            self._check_stream_recovery()
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
                if not self.account.equity_classification_complete and clock - unmatched_since > 10:
                    self.gate.halt()
                    raise RuntimeError(
                        "cash/position changes remain unmatched; reconciliation required"
                    ) from last_cash_reconcile_error
            if self.account.equity_classification_complete:
                unmatched_since = None
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
        policy = RiskWindowPolicy(self.config.risk.risk_window_loss_usd)
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

    async def _reward_pools_still_active(self) -> bool:
        config = await self.rest.reward_config()
        if config.get("enabled") is not True:
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
        for item in self.universe:
            reward = active_rows.get(item.event_ticker)
            if reward is None:
                self.account.blocked_entries.add(item.rules.symbol)
                refreshed.append(replace(item, pool=replace(item.pool, active=False)))
                continue
            current = join_reward_contracts([reward], {item.event_ticker: await self.rest.event(item.event_ticker)})
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
                self.account.blocked_entries.add(item.rules.symbol)
                refreshed.append(replace(item, pool=replace(item.pool, active=False)))
                continue
            refreshed.append(chosen)
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
        )
        self.log.event(logging.INFO, "reward_metadata_refreshed", tracked_symbols=len(self.universe),
                       usable_permitted_symbol=usable_permitted_symbol)
        # A v6 catalog session can wait safely for the next discovery interval.
        return usable_permitted_symbol or (self._continue_after_override and self._catalog_scope)

    async def _rollover_flat_markets(self, rewards: list[dict[str, Any]]) -> None:
        """Replace only expired/inactive owners after terminal and flat proof."""
        if self._stream_holds or self._read_recovery_attempts or self._recovery_stop_requested():
            return
        retiring = [c for c in self.universe if not c.pool.active or not self._entry_time_valid(c.rules.symbol)]
        if not retiring and len(self.universe) >= self.config.risk.max_tracked_markets:
            return
        preferred = {c.pool.pool_id: (c.event_ticker, c.rules.symbol) for c in self.universe if c not in retiring}
        proposed = await RewardUniverseLoader(self.rest).load(
            maximum_pools=self.config.risk.max_tracked_markets, preferred=preferred,
            now=_utc_now(), rewards=rewards,
        )
        async with self._lifecycle_lock:
            if self._stream_holds or self._read_recovery_attempts or self._recovery_stop_requested():
                return
            removable = []
            for contract in retiring:
                symbol = contract.rules.symbol
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
                    self._sticky_pool_ids.discard(c.pool.pool_id)
                    self._sticky_markets.pop(c.pool.pool_id, None)
            if self._stream_holds or self._recovery_stop_requested():
                return
            pools = {c.pool.pool_id for c in self.universe}
            additions = []
            for c in proposed:
                if len(self.universe) + len(additions) >= self.config.risk.max_tracked_markets:
                    break
                if c.pool.pool_id in pools or c.rules.symbol in self.account.blocked_entries:
                    continue
                pools.add(c.pool.pool_id)
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
            self.account.expiry = self._expiry
            self._permitted_symbols.update(c.rules.symbol for c in additions)
            if additions:
                await self.public.subscribe(tuple(c.rules.symbol for c in additions))
            if removable or additions:
                self.journal.set_runtime_state("sticky_markets_v1", json.dumps(self._sticky_markets, sort_keys=True), updated_at=_utc_now())
                self._reward_epoch = None  # New identities require freshly computed eligibility.
                self.log.event(logging.INFO, "catalog_owners_refreshed", retired=len(removable), added=len(additions))

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
            {item.rules.symbol for item in self.universe}
            if permit.universe_scope == "reward_catalog"
            else set(permit.symbols)
        )
        self._permit_policy_digest = permit.policy_sha256
        if not self._permitted_symbols <= {item.rules.symbol for item in self.universe}:
            raise RuntimeError("approved symbols are not in the reconciled universe")
        if commissioning_seconds is not None and (not 2 <= commissioning_seconds <= 30 or permit.maximum_active_pools != 1):
            raise ValueError("commissioning requires 2-30 seconds and a one-pool permit")
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
        self._continue_after_override = permit.permit_version == 6
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
            if self._last_snapshot.positions:
                await self._exit_due_positions(force=True)
            await self._risk_mark()
            self.gate.activate(True)
            if commissioning_seconds is None:
                self._reward_reporting_task = asyncio.create_task(
                    self._reward_reporting_loop(), name="gemini-reward-uptime-reporting",
                )
            activation_reason = (
                f"bounded supervised permit {permit.permit_id} active"
                if permit.permit_version in {5, 6}
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
            next_reward_check_at = asyncio.get_running_loop().time() + 300
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
                        next_reward_check_at = clock + 300
                    reward_check = None
                if reward_check is None and clock >= next_reward_check_at:
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
                            books=self.public.books,
                            market_data_verified_at=self._market_data_verified_at(),
                            exit_before_expiry_seconds=self.config.risk.force_exit_before_expiry_seconds,
                            sports_exit_before_start_seconds=self.config.risk.sports_exit_before_start_seconds,
                        )
                        self.journal.set_runtime_state(
                            "profitability_telemetry_v1",
                            self.profitability.dumps(now=now),
                            updated_at=now,
                        )
                        next_uptime_at = clock + 60
                    self.ranked = rank_live_universe(
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
                        allow_uptime_override=self._override_active(),
                    )
                    if clock >= getattr(self, "_next_report_at", 0.0):
                        self._write_rank_report(_utc_now())
                        self._write_status("quoting", True, "runtime heartbeat; approval active")
                        self.account.prune_terminal()
                        self._next_report_at = clock + 15
                    while self.public.dirty.pop() is not None:
                        pass
                    pool_by_symbol = {item.rules.symbol: item.pool.pool_id for item in self.universe}
                    locked_pools = {
                        pool_by_symbol[symbol]
                        for symbol, quantity in self.account.reducer.positions.items()
                        if quantity != 0 and symbol in pool_by_symbol
                    }
                    # Cohorts are sticky: live telemetry affects initial choice
                    # and replacement of unsafe/ineligible pools, but one noisy
                    # minute cannot churn otherwise healthy quotes and destroy
                    # the daily uptime needed for any reward.
                    desired, desired_pools = select_sticky_candidates(
                        self.ranked,
                        sticky_pool_ids=self._sticky_pool_ids,
                        locked_pool_ids=locked_pools,
                        permitted_symbols=self._permitted_symbols,
                        maximum_active_pools=permit.maximum_active_pools,
                        actionable=self._candidate_actionable,
                        entry_time_valid=self._entry_time_valid,
                    )
                    self._sticky_pool_ids = set(desired_pools)
                    self._save_sticky_markets(desired, locked_pools)
                    candidates_by_symbol = {item.symbol: item for item in desired}
                    await self._cancel_ineligible_working_entries(candidates_by_symbol)
                    self._activated_pool_ids = set(locked_pools)
                    for candidate in desired:
                        await self._maintain_candidate(candidate)
                    next_rank_at = clock + 1
                    next_stale_check_at = clock + 3
                await self._exit_due_positions()
                await asyncio.sleep(1)
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
            finish_supervised_activation(
                permit, self.journal,
                supervisor_session_id=supervisor_session_id,
                outcome="recoverable_flat" if drained_flat else "attention",
            )
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

    async def drain(self) -> None:
        self.gate.state = EngineState.DRAINING
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
            RiskWindowPolicy(self.config.risk.risk_window_loss_usd), self.journal,
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
        if self._closed:
            return
        self._closed = True
        self.log.event(logging.INFO, "runner_closing", phase=self.gate.state.value,
                       account_flat_verified=False, reason="closing local resources; not venue flatness proof")
        recovery_tasks = list(self._stream_restarts.values())
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
        if self._cash_reconciliation_task is not None:
            self._cash_reconciliation_task.cancel()
            await asyncio.gather(self._cash_reconciliation_task, return_exceptions=True)
            self._cash_reconciliation_task = None
        try:
            await asyncio.gather(self.private.close(), self.public.close(), return_exceptions=True)
        finally:
            try:
                self._logging.close()
            finally:
                self.journal.close()
                self.lock.release()

    async def __aenter__(self) -> ProductionOrchestrator:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()
