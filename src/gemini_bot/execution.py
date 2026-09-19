from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import hashlib
import json
import secrets
import time
from typing import Protocol

from .domain import Outcome, QuoteLeg, Side


class IntentState(str, Enum):
    PREPARED = "prepared"
    SENT_UNKNOWN = "sent_unknown"
    ACKNOWLEDGED = "acknowledged"
    TERMINAL = "terminal"


class OrderPurpose(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass(slots=True)
class OrderIntent:
    client_order_id: str
    leg: QuoteLeg
    purpose: OrderPurpose
    state: IntentState = IntentState.PREPARED
    server_order_id: str | None = None


class IntentStore(Protocol):
    def record_intent(self, intent: OrderIntent) -> None: ...
    def update_intent(self, intent: OrderIntent) -> None: ...


def new_client_order_id(symbol: str, purpose: OrderPurpose) -> str:
    marker = "e" if purpose is OrderPurpose.ENTRY else "x"
    symbol_hash = hashlib.blake2s(symbol.encode("utf-8"), digest_size=4).hexdigest()
    value = f"gmr-{marker}-{int(time.time())}-{symbol_hash}-{secrets.token_hex(4)}"
    if len(value) > 36:
        raise RuntimeError("generated Gemini client order ID exceeds venue limit")
    return value


def expected_signed_position_delta(leg: QuoteLeg) -> Decimal:
    """Return the account-position delta implied by one complete fill."""
    if leg.quantity <= 0:
        raise ValueError("quantity must be positive")
    outcome_sign = Decimal("1") if leg.outcome is Outcome.YES else Decimal("-1")
    side_sign = Decimal("1") if leg.side is Side.BUY else Decimal("-1")
    return leg.quantity * outcome_sign * side_sign


def validate_exact_exit(leg: QuoteLeg, signed_position: Decimal) -> None:
    """Prove that a proposed order exactly flattens reconciled inventory.

    Gemini collateral-return accounts reduce NO inventory by buying the
    complementary YES outcome.  Positive YES inventory still exits by selling
    YES.  Both forms must produce the exact opposite signed position delta.
    """
    if signed_position == 0:
        raise ValueError("cannot exit a flat position")
    if leg.quantity != abs(signed_position):
        raise ValueError("exit quantity must exactly match the authenticated position")
    if signed_position + expected_signed_position_delta(leg) != 0:
        raise ValueError("exit would not flatten the authenticated position")
    if signed_position > 0 and (leg.side is not Side.SELL or leg.outcome is not Outcome.YES):
        raise ValueError("positive inventory must exit by selling YES")
    if signed_position < 0 and (leg.side is not Side.BUY or leg.outcome is not Outcome.YES):
        raise ValueError("negative inventory must exit by buying complementary YES")


def place_request(request_id: int, intent: OrderIntent) -> dict[str, object]:
    leg = intent.leg
    if intent.purpose is not OrderPurpose.ENTRY:
        raise ValueError("maker entry request requires an ENTRY intent")
    if leg.side is not Side.BUY:
        raise ValueError("initial reward strategy only permits BUY outcome orders")
    if leg.outcome not in (Outcome.YES, Outcome.NO):
        raise ValueError("invalid outcome")
    if not (Decimal("0.01") <= leg.price <= Decimal("0.99")):
        raise ValueError("price must be in [0.01, 0.99]")
    if leg.quantity <= 0:
        raise ValueError("quantity must be positive")
    return {
        "id": request_id,
        "method": "order.place",
        "params": {
            "symbol": leg.symbol,
            "side": leg.side.value,
            "type": "LIMIT",
            "timeInForce": "MOC",
            "price": format(leg.price, "f"),
            "quantity": format(leg.quantity, "f"),
            "eventOutcome": leg.outcome.value,
            "clientOrderId": intent.client_order_id,
        },
    }


def immediate_entry_request(request_id: int, intent: OrderIntent) -> dict[str, object]:
    """Build an aggressive LIMIT+IOC entry for bounded commissioning only."""
    request = place_request(request_id, intent)
    params = request["params"]
    if not isinstance(params, dict):
        raise RuntimeError("order request params are malformed")
    params["timeInForce"] = "IOC"
    return request


def cancel_request(request_id: int, order_id: str) -> dict[str, object]:
    if not order_id:
        raise ValueError("order_id is required")
    return {"id": request_id, "method": "order.cancel", "params": {"orderId": order_id}}


def exit_request(
    request_id: int,
    intent: OrderIntent,
    *,
    signed_position: Decimal,
) -> dict[str, object]:
    leg = intent.leg
    if intent.purpose is not OrderPurpose.EXIT:
        raise ValueError("exit request requires an EXIT intent")
    validate_exact_exit(leg, signed_position)
    if not (Decimal("0.01") <= leg.price <= Decimal("0.99")):
        raise ValueError("exit price must be in [0.01, 0.99]")
    if leg.quantity <= 0:
        raise ValueError("exit quantity must be positive")
    return {
        "id": request_id,
        "method": "order.place",
        "params": {
            "symbol": leg.symbol,
            "side": leg.side.value,
            "type": "LIMIT",
            "timeInForce": "IOC",
            "price": format(leg.price, "f"),
            "quantity": format(leg.quantity, "f"),
            "eventOutcome": leg.outcome.value,
            "clientOrderId": intent.client_order_id,
        },
    }


class IntentLedger:
    """Mutation ownership and ambiguous-outcome protection."""

    def __init__(self, store: IntentStore | None = None) -> None:
        self._items: dict[str, OrderIntent] = {}
        self._store = store

    def prepare(self, leg: QuoteLeg, purpose: OrderPurpose) -> OrderIntent:
        intent = OrderIntent(new_client_order_id(leg.symbol, purpose), leg, purpose)
        self._items[intent.client_order_id] = intent
        if self._store is not None:
            self._store.record_intent(intent)
        return intent

    def mark_sent(self, client_order_id: str) -> None:
        intent = self._items[client_order_id]
        if intent.state is not IntentState.PREPARED:
            raise RuntimeError("only a prepared intent may be sent")
        intent.state = IntentState.SENT_UNKNOWN
        if self._store is not None:
            self._store.update_intent(intent)

    def acknowledge(self, client_order_id: str, server_order_id: str) -> None:
        intent = self._items[client_order_id]
        intent.server_order_id = server_order_id
        intent.state = IntentState.ACKNOWLEDGED
        if self._store is not None:
            self._store.update_intent(intent)

    def mark_terminal(self, client_order_id: str) -> None:
        intent = self._items[client_order_id]
        if intent.state not in (IntentState.SENT_UNKNOWN, IntentState.ACKNOWLEDGED):
            raise RuntimeError("only a sent intent may become terminal")
        intent.state = IntentState.TERMINAL
        if self._store is not None:
            self._store.update_intent(intent)

    def may_retry(self, client_order_id: str) -> bool:
        # Even a locally unsent-looking network failure can be ambiguous after send.
        return self._items[client_order_id].state is IntentState.PREPARED

    def owns(self, client_order_id: str) -> bool:
        return client_order_id in self._items

    def get(self, client_order_id: str) -> OrderIntent | None:
        return self._items.get(client_order_id)

    def unresolved(self) -> tuple[OrderIntent, ...]:
        return tuple(item for item in self._items.values() if item.state is IntentState.SENT_UNKNOWN)

    def prune_terminal(self, keep: int = 512) -> None:
        terminal = [key for key, value in self._items.items() if value.state is IntentState.TERMINAL]
        for key in terminal[:-keep]:
            self._items.pop(key)


def safe_log_envelope(message: dict[str, object]) -> str:
    """Order envelopes contain no credentials, but keep output compact."""
    return json.dumps(message, separators=(",", ":"), sort_keys=True)
