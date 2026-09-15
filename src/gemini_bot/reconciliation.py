from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .domain import Outcome, Side, canonical_symbol
from .risk_gate import ExposureState


@dataclass(frozen=True, slots=True)
class ReconciledPosition:
    symbol: str
    outcome: Outcome
    signed_quantity: Decimal
    average_price: Decimal
    market_value: Decimal

    def as_storage(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "outcome": self.outcome.value,
            "signed_quantity": str(self.signed_quantity),
            "average_price": str(self.average_price),
            "market_value": str(self.market_value),
        }


@dataclass(frozen=True, slots=True)
class ReconciledOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    outcome: Outcome
    price: Decimal
    original_quantity: Decimal
    remaining_quantity: Decimal
    status: str

    def as_storage(self) -> dict[str, str]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "outcome": self.outcome.value,
            "price": str(self.price),
            "original_quantity": str(self.original_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    positions: tuple[ReconciledPosition, ...]
    active_orders: tuple[ReconciledOrder, ...]

    @property
    def signed_positions(self) -> dict[str, Decimal]:
        return {position.symbol: position.signed_quantity for position in self.positions}

    @property
    def position_market_value(self) -> Decimal:
        return sum((row.market_value for row in self.positions), Decimal("0"))


def parse_positions(rows: list[dict[str, Any]]) -> tuple[ReconciledPosition, ...]:
    result: list[ReconciledPosition] = []
    seen: set[str] = set()
    for row in rows:
        symbol = canonical_symbol(str(row["symbol"]))
        if symbol in seen:
            raise ValueError("multiple position rows for one symbol require manual reconciliation")
        seen.add(symbol)
        outcome = Outcome(str(row["outcome"]).upper())
        quantity = Decimal(str(row["totalQuantity"]))
        average = Decimal(str(row["avgPrice"]))
        market_value = Decimal(str(row["marketValue"]))
        if quantity <= 0 or not Decimal("0") < average < Decimal("1") or market_value < 0:
            raise ValueError("invalid REST position values")
        signed = quantity if outcome is Outcome.YES else -quantity
        result.append(ReconciledPosition(symbol, outcome, signed, average, market_value))
    return tuple(sorted(result, key=lambda item: item.symbol))


def parse_active_orders(rows: list[dict[str, Any]]) -> tuple[ReconciledOrder, ...]:
    result: list[ReconciledOrder] = []
    seen: set[str] = set()
    for row in rows:
        order_id = str(row["orderId"])
        if not order_id or order_id in seen:
            raise ValueError("duplicate or empty active order ID")
        seen.add(order_id)
        original = Decimal(str(row["quantity"]))
        remaining = Decimal(str(row["remainingQuantity"]))
        price = Decimal(str(row["price"]))
        if original <= 0 or not Decimal("0") <= remaining <= original:
            raise ValueError("invalid active order quantities")
        if not Decimal("0.01") <= price <= Decimal("0.99"):
            raise ValueError("invalid active order price")
        status = str(row["status"]).upper()
        if status not in {"OPEN", "NEW", "PARTIALLY_FILLED", "MODIFIED"}:
            raise ValueError("REST active-order endpoint returned a terminal status")
        result.append(ReconciledOrder(
            order_id=order_id,
            client_order_id=str(row.get("clientOrderId", "")),
            symbol=canonical_symbol(str(row["symbol"])),
            side=Side(str(row["side"]).upper()),
            outcome=Outcome(str(row["outcome"]).upper()),
            price=price,
            original_quantity=original,
            remaining_quantity=remaining,
            status=status,
        ))
    return tuple(sorted(result, key=lambda item: item.order_id))


def build_account_snapshot(
    position_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
) -> AccountSnapshot:
    return AccountSnapshot(parse_positions(position_rows), parse_active_orders(order_rows))


def assert_position_consistency(
    snapshot: AccountSnapshot,
    websocket_positions: dict[str, Decimal],
) -> None:
    normalized = {canonical_symbol(key): value for key, value in websocket_positions.items() if value != 0}
    if snapshot.signed_positions != normalized:
        raise ValueError("REST and WebSocket positions disagree")


def assert_stable_account_bracket(before: AccountSnapshot, after: AccountSnapshot) -> None:
    """Ignore mark-to-market drift but reject position/order identity changes."""
    before_positions = {
        row.symbol: (row.outcome, row.signed_quantity, row.average_price)
        for row in before.positions
    }
    after_positions = {
        row.symbol: (row.outcome, row.signed_quantity, row.average_price)
        for row in after.positions
    }
    before_orders = {
        row.order_id: (
            row.client_order_id, row.symbol, row.side, row.outcome, row.price,
            row.original_quantity, row.remaining_quantity, row.status,
        ) for row in before.active_orders
    }
    after_orders = {
        row.order_id: (
            row.client_order_id, row.symbol, row.side, row.outcome, row.price,
            row.original_quantity, row.remaining_quantity, row.status,
        ) for row in after.active_orders
    }
    if before_positions != after_positions or before_orders != after_orders:
        raise ValueError("REST account state changed across startup bracket")


def exposure_from_snapshot(snapshot: AccountSnapshot) -> ExposureState:
    market: dict[str, Decimal] = {}
    gross = Decimal("0")
    for position in snapshot.positions:
        basis = abs(position.signed_quantity) * position.average_price
        gross += basis
        market[position.symbol] = market.get(position.symbol, Decimal("0")) + basis
    buy_outcomes: dict[str, set[Outcome]] = {}
    for order in snapshot.active_orders:
        if order.side is Side.BUY:
            reservation = order.remaining_quantity * order.price
            gross += reservation
            market[order.symbol] = market.get(order.symbol, Decimal("0")) + reservation
            buy_outcomes.setdefault(order.symbol, set()).add(order.outcome)
    tracked = set(market) | {order.symbol for order in snapshot.active_orders}
    return ExposureState(
        gross_reservation_usd=gross,
        market_exposure_usd=market,
        pending_orders=len(snapshot.active_orders),
        two_sided_quotes=sum(outcomes == {Outcome.YES, Outcome.NO} for outcomes in buy_outcomes.values()),
        tracked_markets=len(tracked),
    )
