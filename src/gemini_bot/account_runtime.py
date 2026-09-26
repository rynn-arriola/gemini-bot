from __future__ import annotations

from .reconciliation_errors import UnclassifiedPositions

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any
import time
import json
from copy import deepcopy
from collections.abc import Callable

from .account_state import AccountReducer, OrderTransition, OwnedOrder
from .cash_accounting import fill_cash_delta, netted_quantity
from .domain import Outcome, Side, canonical_symbol
from .execution import IntentLedger, IntentState, OrderPurpose
from .storage import AuthenticatedCashAdjustment, AuthenticatedSettlement, SQLiteJournal


class MissingExecutionEvidence(RuntimeError):
    """A terminal cumulative update exposed a fill absent from the event journal."""


def _event_time(frame: dict[str, Any]) -> datetime:
    raw = int(frame.get("T", frame.get("E", 0)))
    if raw <= 0:
        return datetime.now(timezone.utc)
    seconds = raw / (1_000_000_000 if raw > 10**15 else 1_000)
    return datetime.fromtimestamp(seconds, timezone.utc)


@dataclass(frozen=True, slots=True)
class AccountReadiness:
    subscription_acknowledged: bool
    balances_seen: bool
    positions_seen: bool

    @property
    def complete(self) -> bool:
        return self.subscription_acknowledged and self.balances_seen and self.positions_seen


class AccountRuntime:
    """Single reducer for private WebSocket state and durable execution facts."""

    def __init__(self, ledger: IntentLedger, journal: SQLiteJournal) -> None:
        self.ledger = ledger
        self.journal = journal
        self.reducer = AccountReducer()
        self.subscription_acknowledged = False
        self.balances_seen = False
        self.positions_seen = False
        self.complete_position_snapshots = True
        self.available_usd: Decimal | None = None
        self.confirmed_usd: Decimal | None = None
        self.events: dict[str, asyncio.Event] = {}
        self.terminal_events: dict[str, asyncio.Event] = {}
        self.safety_symbols: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.unowned_order_seen = False
        self._known_account_activity_at = 0.0
        self._unmatched_balance_change_at: float | None = None
        self.last_balance_at: float | None = None
        self.last_position_at: float | None = None
        saved_cash = journal.get_runtime_state("expected_confirmed_usd")
        self.expected_confirmed_usd = None if saved_cash is None else Decimal(saved_cash)
        self.inventory_age_seconds = 300
        self.exit_before_expiry_seconds = 300
        self.expiry: dict[str, datetime] = {}
        self.hard_exit_cutoff: dict[str, datetime] = {}
        saved_precisions = journal.get_runtime_state("quote_asset_precision_v1")
        self.quote_asset_precision: dict[str, int] = {} if saved_precisions is None else {
            canonical_symbol(key): int(value)
            for key, value in json.loads(saved_precisions).items()
            if 0 <= int(value) <= 18
        }
        self.fill_blocked_entries: set[str] = journal.deadline_symbols()
        self.other_entry_blocks: set[str] = set()
        self.entry_hold_reasons: dict[str, set[str]] = {}
        self.blocked_entries: set[str] = set(self.fill_blocked_entries)
        self.changed = asyncio.Event()
        self.fill_version = 0
        self.missing_execution_orders: set[str] = set()
        self.event_sink: Callable[[str, dict[str, object]], None] | None = None
        saved_positions = journal.get_runtime_state("expected_positions")
        self.expected_positions = None if saved_positions is None else {
            key: Decimal(value) for key, value in json.loads(saved_positions).items()
        }

    @property
    def readiness(self) -> AccountReadiness:
        return AccountReadiness(
            self.subscription_acknowledged,
            self.balances_seen,
            self.positions_seen,
        )

    def block_entries(self, symbol: str, *, fill: bool = False, reason: str | None = None) -> None:
        if reason is not None:
            self.entry_hold_reasons.setdefault(symbol, set()).add(reason)
        else:
            (self.fill_blocked_entries if fill else self.other_entry_blocks).add(symbol)
        self.blocked_entries.add(symbol)

    def release_entry_reason(self, symbol: str, reason: str) -> None:
        reasons = self.entry_hold_reasons.get(symbol, set())
        reasons.discard(reason)
        if not reasons:
            self.entry_hold_reasons.pop(symbol, None)
        if (symbol not in self.fill_blocked_entries and symbol not in self.other_entry_blocks
                and symbol not in self.entry_hold_reasons):
            self.blocked_entries.discard(symbol)

    def release_fill_block(self, symbol: str) -> None:
        self.fill_blocked_entries.discard(symbol)
        if symbol not in self.other_entry_blocks and symbol not in self.entry_hold_reasons:
            self.blocked_entries.discard(symbol)

    @property
    def equity_classification_complete(self) -> bool:
        return (
            self.confirmed_usd is not None
            and self.expected_confirmed_usd == self.confirmed_usd
            and self.expected_positions == self.reducer.positions
            and not self.unowned_order_seen
            and not self.missing_execution_orders
        )

    @property
    def snapshots_fresh(self) -> bool:
        now = time.monotonic()
        return all(value is not None and 0 <= now - value <= 3 for value in (self.last_balance_at, self.last_position_at))

    def _known_account_activity(self) -> None:
        # Timing proximity is never evidence that a cash movement is a fill.
        self._known_account_activity_at = time.monotonic()

    def _persist_expected_cash(self, *, initialize_watermark: bool = False) -> None:
        if self.expected_confirmed_usd is not None:
            observed_at = datetime.now(timezone.utc)
            self.journal.set_runtime_state(
                "expected_confirmed_usd", str(self.expected_confirmed_usd),
                updated_at=observed_at,
            )
            if (
                initialize_watermark
                and self.journal.get_runtime_state("cash_reconciliation_watermark") is None
            ):
                # The first authenticated balance already contains all earlier
                # non-trading cash. Only later venue-paid credits may explain a
                # future positive delta.
                self.journal.set_runtime_state(
                    "cash_reconciliation_watermark", "baseline",
                    updated_at=observed_at,
                )

    def switch_to_deltas(self) -> None:
        self.complete_position_snapshots = False

    def _persist_expected_positions(self) -> None:
        self.journal.set_runtime_state(
            "expected_positions", json.dumps({k: str(v) for k, v in self.expected_positions.items()}),
            updated_at=datetime.now(timezone.utc),
        )

    def adopt_verified_manual_flat(self, *, observed_at: datetime) -> None:
        """Adopt a proven external/manual flatten without inventing fill facts."""
        if not self.readiness.complete or not self.snapshots_fresh:
            raise RuntimeError("manual-flat reconciliation requires fresh account snapshots")
        if self.confirmed_usd is None:
            raise RuntimeError("manual-flat reconciliation requires a confirmed USD balance")
        if self.reducer.positions:
            raise RuntimeError("manual-flat reconciliation requires zero positions")
        if self.reducer.pending_order_count():
            raise RuntimeError("manual-flat reconciliation requires zero active orders")
        if self.unowned_order_seen:
            raise RuntimeError("manual-flat reconciliation cannot accept unknown order ownership")
        self.journal.adopt_verified_flat_baseline(
            confirmed_usd=self.confirmed_usd, observed_at=observed_at,
        )
        self.expected_positions = {}
        self.expected_confirmed_usd = self.confirmed_usd
        self.blocked_entries.clear()
        self.fill_blocked_entries.clear()
        self.other_entry_blocks.clear()
        self.entry_hold_reasons.clear()

    def reconcile_authenticated_cash_adjustments(
        self,
        adjustments: tuple[AuthenticatedCashAdjustment, ...],
        *,
        observed_at: datetime,
    ) -> int:
        """Classify exact authenticated account activity as non-trading cash."""
        if self.confirmed_usd is None or self.expected_confirmed_usd is None:
            raise RuntimeError("cash reconciliation requires both cash values")
        if self.expected_positions != self.reducer.positions or self.unowned_order_seen:
            raise UnclassifiedPositions("cash reconciliation requires matched account state")
        count = self.journal.reconcile_authenticated_cash_adjustments(
            expected_cash=self.expected_confirmed_usd,
            confirmed_cash=self.confirmed_usd,
            adjustments=adjustments,
            observed_at=observed_at,
        )
        self.expected_confirmed_usd = self.confirmed_usd
        return count

    def reconcile_legacy_quote_precision_fills(self, *, observed_at: datetime) -> int:
        """Repair post-watermark fills written before cash precision was stored.

        The repair is accepted only when the complete candidate correction
        exactly equals the fresh authenticated private-stream balance. Older
        rows remain unchanged because their cash was already covered by the
        durable reconciliation watermark or an explicit prior exception.
        """
        if observed_at.tzinfo is None:
            raise ValueError("legacy fill reconciliation requires an aware timestamp")
        if self.confirmed_usd is None or self.expected_confirmed_usd is None:
            return 0
        if self.expected_positions != self.reducer.positions or self.unowned_order_seen:
            return 0
        watermark = self.journal.get_runtime_state_record("cash_reconciliation_watermark")
        if watermark is None:
            return 0
        rows = self.journal.connection.execute(
            """SELECT f.trade_id,f.symbol,f.side,f.outcome,f.quantity,f.price,f.fee,
                      i.purpose,f.event_at
                 FROM execution_fill f
                 JOIN order_intent i ON i.client_order_id=f.client_order_id
                WHERE f.cash_delta='' AND f.event_at>=?
                ORDER BY f.event_at,f.trade_id""",
            (watermark[1].isoformat(),),
        ).fetchall()
        plans: list[tuple[str, str, Decimal]] = []
        correction = Decimal("0")
        for trade_id, symbol, side, outcome, quantity, price, fee, purpose, _ in rows:
            precision = self.quote_asset_precision.get(canonical_symbol(str(symbol)))
            if precision is None:
                continue
            qty, execution_price, execution_fee = (
                Decimal(str(quantity)), Decimal(str(price)), Decimal(str(fee))
            )
            exact = fill_cash_delta(
                side=Side(str(side).upper()), outcome=Outcome(str(outcome).upper()),
                purpose=OrderPurpose(str(purpose)), quantity=qty, price=execution_price,
                fee=execution_fee, quote_asset_precision=precision,
            )
            legacy = ((-qty * execution_price) if str(side).upper() == "BUY"
                      else (qty * execution_price)) - execution_fee
            correction += exact - legacy
            plans.append((str(trade_id), canonical_symbol(str(symbol)), exact))
        if not plans or self.expected_confirmed_usd + correction != self.confirmed_usd:
            return 0
        with self.journal.transaction():
            changed = 0
            for trade_id, _, exact in plans:
                changed += self.journal.connection.execute(
                    "UPDATE execution_fill SET cash_delta=? WHERE trade_id=? AND cash_delta=''",
                    (str(exact), trade_id),
                ).rowcount
            if changed != len(plans):
                raise RuntimeError("legacy fill precision repair lost atomic identity")
            self.journal.set_runtime_state(
                "expected_confirmed_usd", str(self.confirmed_usd), updated_at=observed_at,
            )
            self.journal.audit("legacy_fill_quote_precision_reconciled", {
                "fill_count": changed,
                "symbols": sorted({symbol for _, symbol, _ in plans}),
            })
        self.expected_confirmed_usd = self.confirmed_usd
        self.fill_version += 1
        self._known_account_activity()
        return len(plans)

    def reconcile_legacy_collateral_exit_rounding(self, *, observed_at: datetime) -> int:
        """Repair old BUY-YES exit credits only from an exact fresh cash match.

        The first collateral-return exit implementation rounded the gross
        complementary credit down.  Gemini rounds that credit up at the
        contract quote precision.  This migration is deliberately narrow,
        post-watermark, position-consistent, and accepted only when all
        candidate corrections exactly explain the authenticated balance.
        """
        if observed_at.tzinfo is None:
            raise ValueError("collateral exit reconciliation requires an aware timestamp")
        if self.confirmed_usd is None or self.expected_confirmed_usd is None:
            return 0
        if self.expected_positions != self.reducer.positions or self.unowned_order_seen:
            return 0
        watermark = self.journal.get_runtime_state_record("cash_reconciliation_watermark")
        if watermark is None:
            return 0
        rows = self.journal.connection.execute(
            """SELECT f.trade_id,f.symbol,f.quantity,f.price,f.fee,f.cash_delta
                 FROM execution_fill f
                 JOIN order_intent i ON i.client_order_id=f.client_order_id
                WHERE f.side='BUY' AND f.outcome='YES' AND i.purpose='exit'
                  AND f.cash_delta<>'' AND f.event_at>=?
                ORDER BY f.event_at,f.trade_id""",
            (watermark[1].isoformat(),),
        ).fetchall()
        plans: list[tuple[str, str, Decimal, Decimal]] = []
        correction = Decimal("0")
        for trade_id, symbol, quantity, price, fee, stored_cash_delta in rows:
            canonical = canonical_symbol(str(symbol))
            precision = self.quote_asset_precision.get(canonical)
            if precision is None:
                continue
            exact = fill_cash_delta(
                side=Side.BUY, outcome=Outcome.YES, purpose=OrderPurpose.EXIT,
                quantity=Decimal(str(quantity)), price=Decimal(str(price)),
                fee=Decimal(str(fee)), quote_asset_precision=precision,
            )
            stored = Decimal(str(stored_cash_delta))
            if exact != stored:
                correction += exact - stored
                plans.append((str(trade_id), canonical, stored, exact))
        if not plans or self.expected_confirmed_usd + correction != self.confirmed_usd:
            return 0
        with self.journal.transaction():
            changed = 0
            for trade_id, _, stored, exact in plans:
                changed += self.journal.connection.execute(
                    "UPDATE execution_fill SET cash_delta=? WHERE trade_id=? AND cash_delta=?",
                    (str(exact), trade_id, str(stored)),
                ).rowcount
            if changed != len(plans):
                raise RuntimeError("collateral exit rounding repair lost atomic identity")
            self.journal.set_runtime_state(
                "expected_confirmed_usd", str(self.confirmed_usd), updated_at=observed_at,
            )
            self.journal.audit("legacy_collateral_exit_rounding_reconciled", {
                "fill_count": changed,
                "symbols": sorted({symbol for _, symbol, _, _ in plans}),
            })
        self.expected_confirmed_usd = self.confirmed_usd
        self.fill_version += 1
        self._known_account_activity()
        return len(plans)

    def reconcile_authenticated_settlement(
        self,
        settlement: AuthenticatedSettlement,
        *,
        observed_at: datetime,
    ) -> bool:
        """Apply one exact venue settlement without treating it as a trade fill."""
        if self.expected_positions is None or self.expected_confirmed_usd is None:
            raise RuntimeError("settlement reconciliation requires a durable account baseline")
        if self.reducer.positions.get(settlement.symbol, Decimal("0")) != 0:
            raise RuntimeError("settlement reconciliation requires an authenticated flat position")
        # Settlement is stronger position/cash evidence than a locally stale
        # working-order view: the contract can no longer create inventory.
        # Keep the symbol quarantined while its delayed terminal order event is
        # reconciled, but do not stop unrelated markets.
        self.block_entries(settlement.symbol, reason="settlement")
        inserted = self.journal.record_authenticated_settlement(
            settlement,
            expected_positions=dict(self.expected_positions),
            expected_cash=self.expected_confirmed_usd,
            observed_at=observed_at,
        )
        if inserted:
            self.expected_positions.pop(settlement.symbol)
            self.expected_confirmed_usd += settlement.payout
            self.release_fill_block(settlement.symbol)
            self.fill_version += 1
            self._known_account_activity()
        return inserted

    @staticmethod
    def _frame_settlements(frame: dict[str, Any]) -> tuple[tuple[str, Decimal], ...]:
        """Return terminal symbol/payout pairs; reject ambiguous settlement rows."""
        result: list[tuple[str, Decimal]] = []
        seen: set[str] = set()
        for row in frame.get("P", []):
            if not isinstance(row, dict) or row.get("t") != "ec":
                continue
            amounts: dict[str, Decimal] = {}
            for amount in row.get("a", []):
                if not isinstance(amount, dict):
                    raise RuntimeError("settlement position amount is invalid")
                kind = str(amount.get("t", ""))
                if kind in amounts:
                    raise RuntimeError("settlement position amount is duplicated")
                try:
                    value = Decimal(str(amount.get("v", "0")))
                except Exception as exc:
                    raise RuntimeError("settlement position amount is invalid") from exc
                if not value.is_finite():
                    raise RuntimeError("settlement position amount is invalid")
                amounts[kind] = value
            if "settlement_payout" not in amounts:
                continue
            if amounts.get("position") != 0:
                raise RuntimeError("settlement payout did not close the position")
            symbol = canonical_symbol(str(row.get("s", "")))
            if symbol in seen:
                raise RuntimeError("settlement position row is duplicated")
            seen.add(symbol)
            result.append((symbol, amounts["settlement_payout"]))
        return tuple(result)

    def _signal(self, client_order_id: str) -> None:
        self.events.setdefault(client_order_id, asyncio.Event()).set()

    def prune_terminal(self, keep: int = 512) -> None:
        terminal = [key for key, value in self.reducer.orders.items() if not value.working]
        for key in terminal[:-keep]:
            order = self.reducer.orders.pop(key)
            self.events.pop(order.client_order_id, None)
            self.terminal_events.pop(order.order_id, None)
        self.ledger.prune_terminal(keep)

    async def wait_for_order_event(self, client_order_id: str, timeout: float = 10) -> None:
        event = self.events.setdefault(client_order_id, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=timeout)
        event.clear()

    def apply(self, frame: dict[str, Any]) -> None:
        if "status" in frame:
            if frame.get("status") != 200:
                raise RuntimeError("private WebSocket request was rejected")
            self.subscription_acknowledged = True
            return
        event_type = frame.get("e")
        if event_type in {"balanceUpdate", "positionReport", "orderUpdate"}:
            self.changed.set()
        if event_type == "balanceUpdate":
            self.last_balance_at = time.monotonic()
            self.balances_seen = True
            for row in frame.get("B", []):
                if isinstance(row, dict) and str(row.get("a", "")).upper() == "USD":
                    available = Decimal(str(row.get("f", "0")))
                    confirmed = Decimal(str(row.get("c", "0")))
                    if self.expected_confirmed_usd is None:
                        self.expected_confirmed_usd = confirmed
                        self._persist_expected_cash(initialize_watermark=True)
                    self.available_usd = available
                    self.confirmed_usd = confirmed
            return
        if event_type == "positionReport":
            self.last_position_at = time.monotonic()
            settlements = self._frame_settlements(frame)
            prior_positions = dict(self.reducer.positions)
            self.reducer.apply_position_report(
                frame,
                complete_snapshot=self.complete_position_snapshots,
            )
            if self.expected_positions is None:
                self.expected_positions = dict(self.reducer.positions)
                self._persist_expected_positions()
            for symbol, payout in settlements:
                signed = prior_positions.get(symbol)
                if signed is None:
                    signed = self.expected_positions.get(symbol)
                if signed is None or signed == 0:
                    # A complete snapshot may repeat a terminal row after its
                    # exact settlement was already committed. It cannot create
                    # a second cash credit.
                    existing = self.journal.connection.execute(
                        "SELECT payout FROM prediction_settlement WHERE symbol=?",
                        (symbol,),
                    ).fetchone()
                    if existing is None or Decimal(str(existing[0])) != payout:
                        raise RuntimeError("settlement has no matching owned inventory")
                    continue
                self.reconcile_authenticated_settlement(
                    AuthenticatedSettlement(
                        symbol=symbol,
                        signed_quantity=signed,
                        payout=payout,
                        settled_at=_event_time(frame),
                    ),
                    observed_at=datetime.now(timezone.utc),
                )
            self.positions_seen = True
            return
        if event_type != "orderUpdate":
            return

        recovered = self.journal.get_runtime_state('external_reduction_terminal:' + str(frame.get('i', '')))
        if recovered is not None:
            # Operator-reconciled external reducer: accept only duplicate
            # execution IDs or an exact terminal cumulative acknowledgement.
            proof = json.loads(recovered)
            for field, value in (('c', proof['client']), ('s', proof['symbol']),
                                 ('O', proof['outcome']), ('S', 'SELL')):
                if field in frame and ((str(frame[field]) != str(value)) if field == 'c'
                                       else (str(frame[field]).upper() != str(value).upper())):
                    raise RuntimeError('late recovered-order identity changed')
            if frame.get('X') not in {'NEW','OPEN','PARTIALLY_FILLED','MODIFIED','FILLED','CANCELED'}:
                raise RuntimeError('late recovered-order status is invalid')
            executed = Decimal(str(frame.get('Z', '0')))
            if not executed.is_finite() or not 0 <= executed <= Decimal(proof['quantity']):
                raise RuntimeError('late recovered-order execution exceeds terminal proof')
            if str(frame.get('t','')) not in proof['trades'] and not (
                frame.get('X') in {'CANCELED','FILLED'} and executed == Decimal(proof['quantity'])
            ):
                raise RuntimeError('late external reduction lacks exact execution identity')
            return

        previous_order = deepcopy(self.reducer.orders.get(str(frame.get("i", ""))))
        transition = self.reducer.apply_order_update(frame)
        order = self.reducer.orders[transition.order_id]
        intent = self.ledger.get(order.client_order_id)
        purpose_value = (
            intent.purpose.value if intent is not None
            else self.journal.intent_purpose(order.client_order_id)
        )
        if intent is None:
            if not self.journal.knows_client_order_id(order.client_order_id):
                self.unowned_order_seen = True
                raise RuntimeError("unowned account order event")
        elif intent.state is IntentState.SENT_UNKNOWN:
            self.ledger.acknowledge(order.client_order_id, order.order_id)

        if transition.newly_filled > 0:
            try:
                recorded = self._commit_fill(frame, order, transition)
            except BaseException:
                # A rolled-back write must not consume the reducer's trade ID;
                # replay can then persist the same fill exactly once.
                if previous_order is None:
                    self.reducer.orders.pop(order.order_id, None)
                else:
                    self.reducer.orders[order.order_id] = previous_order
                raise
            if (recorded and transition.cancel_other_entry_orders
                    and purpose_value == OrderPurpose.ENTRY.value):
                self.block_entries(order.symbol, fill=True)
                try:
                    self.safety_symbols.put_nowait(order.symbol)
                except asyncio.QueueFull as exc:
                    raise RuntimeError("safety-action queue overflow") from exc

        if transition.became_terminal:
            self.terminal_events.setdefault(order.order_id, asyncio.Event()).set()
            if intent is None:
                self.journal.resolve_intent_terminal(order.client_order_id, order.order_id)
            else:
                current = self.ledger.get(order.client_order_id)
                if current is not None and current.state in {
                    IntentState.SENT_UNKNOWN,
                    IntentState.ACKNOWLEDGED,
                }:
                    self.ledger.mark_terminal(order.client_order_id)
        self.journal.audit("order_transition", {
            "client_order_id": order.client_order_id, "order_id": order.order_id,
            "symbol": order.symbol, "status": order.status,
        })
        if self.event_sink is not None:
            self.event_sink("order_transition", {
                "symbol": order.symbol, "order_id": order.order_id,
                "client_order_id": order.client_order_id,
                "side": order.side.value, "outcome": order.outcome.value,
                "status": order.status, "has_new_fill": transition.newly_filled > 0,
            })
        self._signal(order.client_order_id)

    def _commit_fill(self, frame: dict[str, Any], order: OwnedOrder, transition: OrderTransition) -> bool:
        """One durable unit: fill identity, cash, position and safety deadline."""
        trade_id = str(frame.get("t") or "")
        if not trade_id:
            self.missing_execution_orders.add(order.order_id)
            raise MissingExecutionEvidence("fill without durable trade identity requires reconciliation")
        price = Decimal(str(frame.get("L", frame.get("p", "0"))))
        fee = Decimal(str(frame.get("n", "0")))
        if not price.is_finite() or not Decimal("0") < price < Decimal("1") or not fee.is_finite():
            raise RuntimeError("fill cash cannot be classified without execution price")
        quantity = transition.newly_filled
        intent = self.ledger.get(order.client_order_id)
        purpose_value = (
            intent.purpose.value if intent is not None
            else self.journal.intent_purpose(order.client_order_id)
        )
        try:
            purpose = OrderPurpose(str(purpose_value))
        except ValueError as exc:
            raise RuntimeError("fill purpose is not classified") from exc
        precision = self.quote_asset_precision.get(order.symbol)
        if precision is None:
            raise RuntimeError("fill quote-asset precision is not classified")
        prior_position = (
            None if self.expected_positions is None
            else self.expected_positions.get(order.symbol, Decimal("0"))
        )
        cash_delta = fill_cash_delta(
            side=Side(order.side.value), outcome=Outcome(order.outcome.value), purpose=purpose,
            quantity=quantity, price=price, fee=fee, quote_asset_precision=precision,
            prior_signed_position=prior_position,
        )
        # Only contracts that open new exposure start an inventory deadline; a
        # fill that nets against held opposite inventory reduces risk.
        opened_quantity = quantity - (
            Decimal("0") if prior_position is None else netted_quantity(
                side=order.side, outcome=order.outcome, quantity=quantity,
                prior_signed_position=prior_position,
            )
        )
        expected_cash = self.expected_confirmed_usd
        if expected_cash is not None:
            expected_cash += cash_delta
        expected_positions = None if self.expected_positions is None else dict(self.expected_positions)
        if expected_positions is not None:
            change = quantity * (1 if order.outcome.value == "YES" else -1) * (1 if order.side.value == "BUY" else -1)
            expected_positions[order.symbol] = expected_positions.get(order.symbol, Decimal("0")) + change
            if expected_positions[order.symbol] == 0:
                expected_positions.pop(order.symbol)
        now = datetime.now(timezone.utc)
        with self.journal.transaction():
            recorded = self.journal.record_fill(
                trade_id=trade_id, order_id=order.order_id, client_order_id=order.client_order_id,
                symbol=order.symbol, side=order.side.value, outcome=order.outcome.value,
                quantity=quantity, price=price, fee=fee, cash_delta=cash_delta,
                event_at=_event_time(frame),
            )
            if not recorded:
                return False
            if expected_cash is not None:
                self.journal.set_runtime_state("expected_confirmed_usd", str(expected_cash), updated_at=now)
            if expected_positions is not None:
                self.journal.set_runtime_state(
                    "expected_positions", json.dumps({k: str(v) for k, v in expected_positions.items()}), updated_at=now,
                )
            if (transition.cancel_other_entry_orders and purpose is OrderPurpose.ENTRY
                    and opened_quantity > 0):
                opened = min(_event_time(frame), now)
                deadline = opened + timedelta(seconds=self.inventory_age_seconds)
                if order.symbol in self.expiry:
                    deadline = min(deadline, self.expiry[order.symbol] - timedelta(seconds=self.exit_before_expiry_seconds))
                if order.symbol in self.hard_exit_cutoff:
                    deadline = min(deadline, self.hard_exit_cutoff[order.symbol])
                self.journal.persist_deadline(order.symbol, order.outcome.value, opened, deadline, order.cumulative_filled)
        self.expected_confirmed_usd = expected_cash
        self.expected_positions = expected_positions
        self.fill_version += 1
        self._known_account_activity()
        return True
