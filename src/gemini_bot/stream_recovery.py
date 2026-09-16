"""Narrow connection-recovery proof rules; never a mutation retry policy."""
from decimal import Decimal
from typing import Any

from .account_state import OwnedOrder


class RecoveryPending(RuntimeError):
    """Read-only proof is not published yet; bounded retry may wait for it."""


def terminal_history_status(order: OwnedOrder, row: dict[str, Any]) -> str:
    """A REST terminal cannot silently account for a missed execution fill."""
    identity = (str(row.get("orderId", "")), str(row.get("clientOrderId", "")),
                str(row.get("symbol", "")).upper(), str(row.get("side", "")).upper(),
                str(row.get("outcome", "")).upper())
    expected = (order.order_id, order.client_order_id, order.symbol, order.side.value, order.outcome.value)
    if identity != expected:
        raise RuntimeError("recovery history identity disagrees with owned order")
    status = str(row.get("status", "")).upper().replace("CANCELLED", "CANCELED")
    if status not in {"CANCELED", "FILLED", "REJECTED"}:
        raise RecoveryPending("order terminal history is not published")
    try:
        filled = Decimal(str(row["filledQuantity"]))
        quantity = Decimal(str(row["quantity"]))
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise RuntimeError("recovery history lacks exact execution quantities") from exc
    if (not filled.is_finite() or not quantity.is_finite()
            or filled != order.cumulative_filled or quantity != order.original_quantity):
        raise RuntimeError("recovery detected missing or inconsistent execution evidence")
    return status
