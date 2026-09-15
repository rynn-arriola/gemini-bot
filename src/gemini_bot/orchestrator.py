from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account_runtime import AccountRuntime
from .activation import consume_activation_permit, load_activation_permit, policy_digest
from .config import DEFAULT_POLICY_PATH, LiveConfig
from .discovery import RewardUniverseLoader
from .catalog import reward_pool_key, join_reward_contracts
from .domain import EngineState, Outcome
from .engine import LiveEngineGate
from .execution import IntentLedger, IntentState, OrderPurpose
from .exit_pricing import exact_crossing_exit
from .live_ws import LiveWebSocket
from .operational_logging import ErrorClass, OperationalLogger, configure_logging
from .persistent_risk import PersistentRiskWindow
from .public_ws import PublicMarketSocket
from .rank_report import public_rank_rows, rank_live_universe
from .rate_limits import AdmissionGovernor
from .reconciliation import (
    AccountSnapshot,
    assert_position_consistency,
    assert_stable_account_bracket,
    build_account_snapshot,
    exposure_from_snapshot,
)
from .rest_client import GeminiReadRestClient
from .risk_gate import admit_quote_pair
from .risk_gate import ExposureState
from .risk_windows import RiskWindowPolicy
from .runtime_files import atomic_write_json, runtime_status
from .storage import SQLiteJournal, WindowsOwnerLock, shared_control_directory
from .uptime import sample_and_load_progress


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

    def __init__(self, config: LiveConfig, directory: Path) -> None:
        self.config = config
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
            config.api_key, config.api_secret, self.governor, self.ledger
        )
        self.account = AccountRuntime(self.ledger, self.journal)
        self.account.inventory_age_seconds = config.risk.max_inventory_age_seconds
        self.account.exit_before_expiry_seconds = config.risk.force_exit_before_expiry_seconds
        self.gate = LiveEngineGate()
        self.private.mutation_guard = self._validate_send
        self.universe = []
        self.ranked = []
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._activated_pool_ids: set[str] = set()
        self._expiry: dict[str, datetime] = {}
        self._opening_equity_marked = False
        self._dynamic_pending_cap = config.risk.max_pending_orders
        self._last_snapshot = AccountSnapshot((), ())
        self._uptime = {}
        self._permit_expires_at: datetime | None = None
        self._permitted_symbols: set[str] = set()
        self._last_live_mark: tuple[object, ...] | None = None
        self._permit_policy_digest: str | None = None
        self._maker_ack_count = 0
        self._logging = configure_logging(
            self.directory / "logs", secrets=(config.api_key, config.api_secret)
        )
        self.log = OperationalLogger(self._logging, self.journal)

    def _write_status(self, phase: str, activated: bool, reason: str) -> None:
        atomic_write_json(
            self.directory / "status.json",
            runtime_status(
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
            ),
        )

    async def _public_pump(self) -> None:
        try:
            async for frame in self.public.frames():
                if frame.get("e") == "contractStatus":
                    symbol = str(frame.get("s", "")).upper()
                    if symbol in self._expiry and str(frame.get("n", "")).lower() != "active":
                        self.account.blocked_entries.add(symbol)
                        self.gate.halt()
                        raise RuntimeError("tracked contract lifecycle changed; reconciliation required")
                gap = self.public.apply_frame(frame)
                self._mark_live_risk()
                if gap is not None and gap in self.public.resync_required:
                    # Do not accept buffered old deltas as a replacement snapshot.
                    # Reconnect/reconcile under fresh approval, never resume quoting.
                    raise RuntimeError("depth sequence gap requires fresh connection and reconciliation")
            self.gate.halt()
            self._write_status("halted", False, "public stream ended; reconciliation required")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gate.halt()
            self.log.exception("public_stream_failed", ErrorClass.TRANSPORT, exc)
            self._write_status("halted", False, "public stream lost; approval is required after reconciliation")
            raise

    async def _private_pump(self) -> None:
        try:
            async for frame in self.private.messages():
                self.account.apply(frame)
                self._mark_live_risk()
            self.gate.halt()
            self._write_status("halted", False, "account stream ended; reconciliation required")
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

    async def prepare(self, *, timeout: float = 30) -> PreparedState:
        """Read-only startup. This method contains no order mutation call."""
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
        self.account.inventory_age_seconds = self.config.risk.max_inventory_age_seconds
        self.account.exit_before_expiry_seconds = self.config.risk.force_exit_before_expiry_seconds
        self.log = OperationalLogger(self._logging, canonical)
        self.gate.begin_reconciliation()
        self._write_status("reconciling", False, "read-only startup")
        try:
            terms, positions_before, orders_before, history = await asyncio.gather(
                self.rest.terms_status(), self.rest.positions(), self.rest.active_orders(),
                self.rest.order_history(),
            )
            if not terms.get("hasAcceptedLatest"):
                raise RuntimeError("latest Gemini Predictions terms are not accepted")
            reward_config = await self.rest.reward_config()
            if reward_config.get("enabled") is not True:
                raise RuntimeError("Gemini liquidity rewards are not currently enabled")
            self._resolve_prior_intents(history, orders_before)
            before = build_account_snapshot(positions_before, orders_before)
            for order in before.active_orders:
                if not order.client_order_id or not self.journal.knows_client_order_id(order.client_order_id):
                    raise RuntimeError("an unowned active order blocks production startup")

            self.universe = await RewardUniverseLoader(self.rest).load(
                maximum_pools=self.config.risk.max_tracked_markets
            )
            if not self.universe:
                raise RuntimeError("no active reward pools passed discovery")
            self._expiry = {item.rules.symbol: _parse_time(item.expiry) for item in self.universe}
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
            await self.public.subscribe(tuple(item.rules.symbol for item in self.universe))
            await self.private.subscribe_account()
            await self._wait_ready(timeout)

            positions_after, orders_after = await asyncio.gather(
                self.rest.positions(), self.rest.active_orders()
            )
            after = build_account_snapshot(positions_after, orders_after)
            assert_stable_account_bracket(before, after)
            assert_position_consistency(after, self.account.reducer.positions)
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
            )
            self.ranked = rank_live_universe(
                self.universe, self.public.books, now=now,
                quantity=self.config.strategy.quote_quantity,
                offset_ticks=self.config.strategy.quote_offset_ticks,
                uptime=self._uptime,
                account=self.account.reducer,
                market_data_verified_at=self._market_data_verified_at(),
            )
            atomic_write_json(
                self.directory / "rank-report.json",
                {"generated_at": now.isoformat(), "candidates": public_rank_rows(self.ranked)},
            )
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

    async def _cancel_order_and_confirm(self, order_id: str, *, timeout: float = 10) -> None:
        await self.private.cancel(order_id)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            rows = await self.rest.active_orders()
            if all(str(row.get("orderId")) != order_id for row in rows):
                history = await self.rest.order_history()
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
        raise TimeoutError("order cancellation was not proven terminal")

    async def _cancel_symbol_entries(self, symbol: str) -> None:
        for order_id in self.account.reducer.risk_adding_order_ids(symbol):
            await self._cancel_order_and_confirm(order_id)

    async def _quote_pair(self, candidate: Any) -> None:
        async with self._lifecycle_lock:
            await self._quote_pair_locked(candidate)

    def _entry_time_valid(self, symbol: str) -> bool:
        expiry = self._expiry.get(symbol)
        return expiry is not None and _utc_now() < expiry - timedelta(seconds=self.config.risk.force_exit_before_expiry_seconds)

    def _validate_send(self, payload: dict[str, Any]) -> None:
        """Last check after rate pacing; no await between this and transport send."""
        params = payload["params"]
        symbol = str(params["symbol"])
        if not self._book_fresh(symbol):
            raise RuntimeError("market became stale while waiting to send")
        if params["side"] == "BUY":
            if self._permit_policy_digest is not None and policy_digest(DEFAULT_POLICY_PATH) != self._permit_policy_digest:
                raise RuntimeError("policy changed after approval")
            if (not self.gate.may_submit or not self._entry_time_valid(symbol)
                    or symbol not in self._permitted_symbols
                    or symbol in self.account.blocked_entries
                    or not self.account.equity_classification_complete
                    or not self.account.snapshots_fresh
                    or (self._permit_expires_at is not None and _utc_now() >= self._permit_expires_at)):
                raise RuntimeError("entry permission changed while waiting to send")
            for held_symbol, signed in self.account.reducer.positions.items():
                deadline = self.journal.get_deadline(held_symbol, "YES" if signed > 0 else "NO")
                if deadline is None or _utc_now() >= deadline:
                    raise RuntimeError("inventory safety action must precede any new entry")
            exposure = self._websocket_exposure()
            quantity = Decimal(str(params["quantity"]))
            if (exposure.pending_orders + 1 > self._dynamic_pending_cap
                    or exposure.gross_reservation_usd + quantity > self.config.risk.max_gross_reservation_usd
                    or exposure.market_exposure_usd.get(symbol, Decimal("0")) + quantity > self.config.risk.max_market_exposure_usd):
                raise RuntimeError("entry exposure changed while waiting to send")
        else:
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

    async def _quote_pair_locked(self, candidate: Any) -> None:
        if not self.gate.may_submit or not candidate.eligible_now or candidate.quote is None:
            return
        if not self._entry_time_valid(candidate.symbol) or not self._book_fresh(candidate.symbol):
            return
        if candidate.symbol in self.account.blocked_entries or self.journal.pending_exit(candidate.symbol):
            return
        if self.account.reducer.positions.get(candidate.symbol, Decimal("0")) != 0:
            return
        if self.account.reducer.risk_adding_order_ids(candidate.symbol):
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
            raise RuntimeError("first maker leg lacked positive working acknowledgment")
        first_order = self.account.reducer.orders.get(first.server_order_id or "")
        if (not self.gate.may_submit or candidate.symbol in self.account.blocked_entries
                or first_order is None or not first_order.working or first_order.cumulative_filled > 0
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
        if second is None or second.state is not IntentState.ACKNOWLEDGED:
            await self._cancel_symbol_entries(candidate.symbol)
            raise RuntimeError("second maker leg lacked positive working acknowledgment")
        self._activated_pool_ids.add(candidate.pool_id)
        self._maker_ack_count += 1

    async def _maintain_candidate(self, candidate: Any) -> None:
        async with self._lifecycle_lock:
            await self._maintain_candidate_locked(candidate)

    async def _maintain_candidate_locked(self, candidate: Any) -> None:
        working = [
            order for order in self.account.reducer.orders.values()
            if order.symbol == candidate.symbol and order.working and order.side.value == "BUY"
        ]
        if (not candidate.eligible_now or candidate.quote is None or not self._entry_time_valid(candidate.symbol)
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
            if not force and _utc_now() < deadline:
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
            book = self.public.books.get(symbol)
            if not self._book_fresh(symbol):
                raise RuntimeError("exit book is stale or unsynchronized")
            leg = exact_crossing_exit(book, signed) if book is not None else None
            if leg is None:
                raise RuntimeError("fresh full depth cannot support an exact position exit")
            self._exit_uncertain.add(symbol)
            client_id = await self.private.exit_position(leg, signed_position=signed)
            await self.account.wait_for_order_event(client_id)
            deadline_at = asyncio.get_running_loop().time() + 10
            while True:
                rest_positions = build_account_snapshot(await self.rest.positions(), []).signed_positions
                intent = self.ledger.get(client_id)
                if (rest_positions.get(symbol, Decimal("0")) == 0
                        and self.account.reducer.positions.get(symbol, Decimal("0")) == 0
                        and intent is not None and intent.state is IntentState.TERMINAL):
                    break
                if asyncio.get_running_loop().time() >= deadline_at:
                    raise RuntimeError("exact IOC exit did not prove the position flat")
                await asyncio.sleep(0.25)
            self.journal.clear_deadline(symbol, outcome.value)
            self._exit_uncertain.discard(symbol)
            self.account.blocked_entries.discard(symbol)

    async def _risk_mark(self) -> int:
        snapshot = build_account_snapshot(await self.rest.positions(), await self.rest.active_orders())
        assert_position_consistency(snapshot, self.account.reducer.positions)
        if self.account.confirmed_usd is None:
            raise RuntimeError("confirmed USD balance is unknown")
        adjusted = self.account.confirmed_usd + snapshot.position_market_value
        mark = PersistentRiskWindow(
            RiskWindowPolicy(self.config.risk.risk_window_loss_usd), self.journal
        ).update(
            now=_utc_now(), adjusted_equity=adjusted,
            classification_complete=self.account.equity_classification_complete,
        )
        if not mark.classification_complete:
            raise RuntimeError("equity changes cannot be classified safely")
        self._last_snapshot = snapshot
        self._dynamic_pending_cap = mark.pending_order_cap
        return mark.pending_order_cap

    async def _live_safety_clock(self) -> None:
        """Independent of ranking/REST waits: observe risk, expiry and exit timers."""
        unmatched_since: float | None = None
        while self.gate.state is EngineState.QUOTING:
            now = _utc_now()
            clock = asyncio.get_running_loop().time()
            if not self.account.snapshots_fresh:
                self.gate.halt()
                raise RuntimeError("periodic account snapshots are stale")
            if self._permit_expires_at is not None and now >= self._permit_expires_at:
                self.gate.state = EngineState.DRAINING
                return
            for task in self._tasks:
                if task.done():
                    self.gate.halt()
                    task.result()
                    raise RuntimeError("required stream worker ended")
            if not self.account.equity_classification_complete:
                unmatched_since = clock if unmatched_since is None else unmatched_since
                if clock - unmatched_since > 10:
                    self.gate.halt()
                    raise RuntimeError("cash/position changes remain unmatched; reconciliation required")
            else:
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
        equity = self.account.confirmed_usd
        for symbol, signed in self.account.reducer.positions.items():
            book = self.public.books.get(symbol)
            if not self._book_fresh(symbol) or book.top() is None:
                return
            midpoint = book.top().midpoint
            equity += abs(signed) * (midpoint if signed > 0 else 1 - midpoint)
        now = _utc_now()
        policy = RiskWindowPolicy(self.config.risk.risk_window_loss_usd)
        key = (equity, policy.bounds(now))
        if key == self._last_live_mark:
            return
        mark = PersistentRiskWindow(policy, self.journal).update(now=now, adjusted_equity=equity, classification_complete=True)
        self._last_live_mark = key
        self._dynamic_pending_cap = mark.pending_order_cap

    async def _enforce_pending_cap(self, cap: int) -> None:
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
        active: set[str] = set()
        for row in rows:
            if Decimal(str(row.get("daily_pool_usd", "0"))) <= 0:
                continue
            ends_at = str(row.get("ends_at", ""))
            if ends_at and _parse_time(ends_at) <= now:
                continue
            active.add(reward_pool_key(row))
        if not self._activated_pool_ids <= active:
            return False
        by_event = {str(row.get("event_ticker", "")).upper(): row for row in rows}
        for item in self.universe:
            reward = by_event.get(item.event_ticker)
            if reward is None:
                return False
            current = join_reward_contracts([reward], {item.event_ticker: await self.rest.event(item.event_ticker)})
            chosen = next((row for row in current if row.rules.symbol == item.rules.symbol), None)
            if (chosen is None or chosen.rules != item.rules or chosen.expiry != item.expiry
                    or not chosen.pool.active or not chosen.rules.active or chosen.rules.post_only_mode):
                return False
        return True

    async def activate(self, permit_path: Path, *, commissioning_seconds: int | None = None, single_leg: bool = False) -> None:
        """Enter the mutation path only after preparation and one-use approval."""
        self.config.validate()
        if self.gate.state is not EngineState.READY:
            raise RuntimeError("read-only reconciliation must complete before activation")
        permit = load_activation_permit(
            permit_path, policy_path=DEFAULT_POLICY_PATH, store=self.journal,
            account_fingerprint=hashlib.sha256(self.config.api_key.encode()).hexdigest(),
        )
        self._permitted_symbols = set(permit.symbols)
        self._permit_policy_digest = permit.policy_sha256
        if not self._permitted_symbols <= {item.rules.symbol for item in self.universe}:
            raise RuntimeError("approved symbols are not in the reconciled universe")
        if commissioning_seconds is not None and (not 2 <= commissioning_seconds <= 30 or permit.maximum_active_pools != 1):
            raise ValueError("commissioning requires 2-30 seconds and a one-pool permit")
        if single_leg and commissioning_seconds is None:
            raise ValueError("one-leg mode is restricted to bounded commissioning")
        self._commissioning_single_leg = single_leg
        # A permit is consumed before the first possible mutation. A failed attempt
        # therefore cannot be replayed without fresh owner approval.
        consume_activation_permit(permit, self.journal)
        self._permit_expires_at = permit.expires_at
        safety: asyncio.Task[None] | None = None
        safety_clock: asyncio.Task[None] | None = None
        reward_check: asyncio.Task[bool] | None = None
        try:
            for order in self._last_snapshot.active_orders:
                await self._cancel_order_and_confirm(order.order_id)
            if self._last_snapshot.positions:
                await self._exit_due_positions(force=True)
            await self._risk_mark()
            self.gate.activate(True)
            self._write_status("quoting", True, f"one-use permit {permit.permit_id} consumed")
            safety = asyncio.create_task(self._safety_loop(), name="gemini-fill-safety")
            safety_clock = asyncio.create_task(self._live_safety_clock(), name="gemini-live-safety-clock")
            next_rank_at = 0.0
            next_stale_check_at = 0.0
            next_risk_at = asyncio.get_running_loop().time() + 60
            next_uptime_at = 0.0
            next_reward_check_at = asyncio.get_running_loop().time() + 300
            stop_at = None if commissioning_seconds is None else asyncio.get_running_loop().time() + commissioning_seconds
            while self.gate.state is EngineState.QUOTING:
                if safety is not None and safety.done():
                    safety.result()
                if safety_clock is not None and safety_clock.done():
                    safety_clock.result()
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
                if reward_check is not None and reward_check.done():
                    if not reward_check.result():
                        self.gate.state = EngineState.DRAINING
                        break
                    reward_check = None
                    next_reward_check_at = clock + 300
                if reward_check is None and clock >= next_reward_check_at:
                    reward_check = asyncio.create_task(
                        self._reward_pools_still_active(), name="gemini-reward-refresh"
                    )
                if clock >= next_risk_at:
                    cap = await self._risk_mark()
                    await self._enforce_pending_cap(cap)
                    next_risk_at = clock + 60
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
                        )
                        next_uptime_at = clock + 60
                    self.ranked = rank_live_universe(
                        self.universe, self.public.books, now=_utc_now(),
                        quantity=self.config.strategy.quote_quantity,
                        offset_ticks=self.config.strategy.quote_offset_ticks,
                        uptime=self._uptime,
                        account=self.account.reducer,
                        market_data_verified_at=self._market_data_verified_at(),
                    )
                    if clock >= getattr(self, "_next_report_at", 0.0):
                        atomic_write_json(
                            self.directory / "rank-report.json",
                            {"generated_at": _utc_now().isoformat(), "candidates": public_rank_rows(self.ranked)},
                        )
                        self._write_status("quoting", True, "runtime heartbeat; approval active")
                        self.account.prune_terminal()
                        self._next_report_at = clock + 15
                    while self.public.dirty.pop() is not None:
                        pass
                    candidates_by_symbol = {item.symbol: item for item in self.ranked}
                    for order in tuple(self.account.reducer.orders.values()):
                        candidate = candidates_by_symbol.get(order.symbol)
                        if (
                            order.working and order.side.value == "BUY"
                            and (candidate is None or not candidate.eligible_now)
                        ):
                            await self._cancel_symbol_entries(order.symbol)
                    for candidate in self.ranked:
                        if candidate.symbol not in self._permitted_symbols:
                            continue
                        if (
                            candidate.pool_id not in self._activated_pool_ids
                            and len(self._activated_pool_ids) >= permit.maximum_active_pools
                        ):
                            continue
                        await self._maintain_candidate(candidate)
                    next_rank_at = clock + 1
                    next_stale_check_at = clock + 3
                await self._exit_due_positions()
                await asyncio.sleep(1)
            await self.drain()
            if commissioning_seconds is not None and self._maker_ack_count == 0:
                raise RuntimeError("commissioning did not observe a working maker order; no success claimed")
        except Exception as exc:
            self.log.exception("active_runtime_failed", ErrorClass.EXECUTION, exc)
            try:
                await self.drain()
            except Exception as drain_exc:
                self.gate.halt()
                self.log.exception("emergency_drain_failed", ErrorClass.EXECUTION, drain_exc)
                self._write_status("halted", False, "emergency drain failed; manual reconciliation required")
            raise
        finally:
            if safety_clock is not None:
                safety_clock.cancel()
                await asyncio.gather(safety_clock, return_exceptions=True)
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
        self._resolve_prior_intents(await self.rest.order_history())
        if not self.account.equity_classification_complete:
            raise RuntimeError("flat position snapshot obtained but cash reconciliation is incomplete")
        self._exit_uncertain.clear()
        self.gate.halt()
        self._write_status("stopped_flat", False, "drain completed and account is flat")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
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
