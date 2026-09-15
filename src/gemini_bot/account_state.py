from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .domain import Outcome, QuoteLeg, Side, canonical_symbol


TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED"})
WORKING_ORDER_STATUSES = frozenset({"NEW", "OPEN", "PARTIALLY_FILLED", "MODIFIED"})


@dataclass(slots=True)
class OwnedOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    outcome: Outcome
    price: Decimal
    original_quantity: Decimal
    remaining_quantity: Decimal
    cumulative_filled: Decimal
    status: str
    update_ns: int
    seen_trade_ids: set[str] = field(default_factory=set)

    @property
    def working(self) -> bool:
        return self.status in WORKING_ORDER_STATUSES and self.remaining_quantity > 0


@dataclass(frozen=True, slots=True)
class OrderTransition:
    order_id: str
    newly_filled: Decimal
    became_terminal: bool
    cancel_other_entry_orders: bool


class AccountReducer:
    """Single owner for account order/position events."""

    def __init__(self) -> None:
        self.orders: dict[str, OwnedOrder] = {}
        self.positions: dict[str, Decimal] = {}
        self.position_update_ns = 0

    def apply_order_update(self, frame: dict[str, Any]) -> OrderTransition:
        if frame.get("e") != "orderUpdate":
            raise ValueError("not an order update")
        order_id = str(frame["i"])
        status = str(frame["X"]).upper()
        if status not in TERMINAL_ORDER_STATUSES | WORKING_ORDER_STATUSES:
            raise ValueError(f"unknown order status: {status}")
        update_ns = int(frame.get("T", frame.get("E", 0)))
        current = self.orders.get(order_id)
        if current is None:
            required = ("c", "s", "S", "O", "q")
            if any(key not in frame for key in required):
                raise ValueError("first order event lacks identity fields; reconcile required")
            original = Decimal(str(frame["q"]))
            remaining = Decimal(str(frame.get("z", original)))
            current = OwnedOrder(
                order_id=order_id,
                client_order_id=str(frame["c"]),
                symbol=canonical_symbol(str(frame["s"])),
                side=Side(str(frame["S"]).upper()),
                outcome=Outcome(str(frame["O"]).upper()),
                price=Decimal(str(frame.get("p", "0"))),
                original_quantity=original,
                remaining_quantity=remaining,
                cumulative_filled=Decimal("0"),
                status=status,
                update_ns=update_ns,
            )
            self.orders[order_id] = current
        elif update_ns < current.update_ns:
            return OrderTransition(order_id, Decimal("0"), not current.working, False)

        newly_filled = Decimal("0")
        execution = Decimal(str(frame.get("Z", "0")))
        trade_id = str(frame.get("t", ""))
        if status in {"FILLED", "PARTIALLY_FILLED"} and execution > 0:
            if not trade_id:
                raise ValueError("fill event lacks trade id; reconcile required")
            if trade_id not in current.seen_trade_ids:
                current.seen_trade_ids.add(trade_id)
                newly_filled = execution
                current.cumulative_filled += execution
        elif status in TERMINAL_ORDER_STATUSES and execution > current.cumulative_filled:
            # Terminal Z is cumulative, not a new execution. Capture only the
            # previously unseen difference and require position reconciliation.
            newly_filled = execution - current.cumulative_filled
            current.cumulative_filled = execution

        if current.cumulative_filled > current.original_quantity:
            raise ValueError("filled quantity exceeds original; reconcile required")
        if "z" in frame:
            current.remaining_quantity = Decimal(str(frame["z"]))
        elif status in TERMINAL_ORDER_STATUSES:
            current.remaining_quantity = Decimal("0")
        current.status = status
        if "p" in frame:
            current.price = Decimal(str(frame["p"]))
        current.update_ns = max(current.update_ns, update_ns)
        return OrderTransition(
            order_id=order_id,
            newly_filled=newly_filled,
            became_terminal=status in TERMINAL_ORDER_STATUSES,
            cancel_other_entry_orders=newly_filled > 0 and current.side is Side.BUY,
        )

    def apply_position_report(
        self,
        frame: dict[str, Any],
        *,
        complete_snapshot: bool = False,
    ) -> set[str]:
        if frame.get("e") != "positionReport":
            raise ValueError("not a position report")
        update_ns = int(frame.get("u", frame.get("E", 0)))
        if update_ns < self.position_update_ns:
            return set()
        changed: set[str] = set()
        reported_symbols: set[str] = set()
        for row in frame.get("P", []):
            if row.get("t") != "ec":
                continue
            symbol = canonical_symbol(str(row["s"]))
            reported_symbols.add(symbol)
            values = {str(item.get("t")): Decimal(str(item.get("v", "0"))) for item in row.get("a", [])}
            if "position" not in values:
                continue
            quantity = values["position"]
            if quantity == 0:
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = quantity
            changed.add(symbol)
        if complete_snapshot:
            absent = set(self.positions) - reported_symbols
            for symbol in absent:
                self.positions.pop(symbol, None)
            changed.update(absent)
        self.position_update_ns = update_ns
        return changed

    def pending_order_count(self) -> int:
        return sum(1 for order in self.orders.values() if order.working)

    def risk_adding_order_ids(self, symbol: str) -> tuple[str, ...]:
        target = canonical_symbol(symbol)
        return tuple(sorted(order.order_id for order in self.orders.values()
                            if order.symbol == target and order.working and order.side is Side.BUY))

    def exact_exit_leg(self, symbol: str, price: Decimal) -> QuoteLeg | None:
        target = canonical_symbol(symbol)
        signed = self.positions.get(target, Decimal("0"))
        if signed == 0:
            return None
        outcome = Outcome.YES if signed > 0 else Outcome.NO
        return QuoteLeg(target, outcome, Side.SELL, price, abs(signed))
