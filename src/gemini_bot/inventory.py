from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from .domain import Outcome, canonical_symbol


class ExitAction(str, Enum):
    KEEP_PASSIVE = "keep_passive"
    CANCEL_BUILDING_ORDERS = "cancel_building_orders"
    PLACE_TIMED_EXIT = "place_timed_exit"
    PLACE_EXPIRY_EXIT = "place_expiry_exit"
    HALT_UNKNOWN_STATE = "halt_unknown_state"


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    outcome: Outcome
    quantity: Decimal
    average_entry_price: Decimal
    opened_at: datetime
    expiry_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        if self.quantity <= 0:
            raise ValueError("position quantity must be positive")
        if not Decimal("0") < self.average_entry_price < Decimal("1"):
            raise ValueError("entry price must be between zero and one")
        if self.opened_at.tzinfo is None or self.expiry_at.tzinfo is None:
            raise ValueError("position times must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TimedExitPolicy:
    max_inventory_age_seconds: int = 900
    force_exit_before_expiry_seconds: int = 300

    def __post_init__(self) -> None:
        if self.max_inventory_age_seconds <= 0:
            raise ValueError("inventory age must be positive")
        if self.force_exit_before_expiry_seconds <= 0:
            raise ValueError("expiry exit lead must be positive")


@dataclass(frozen=True, slots=True)
class ExitDecision:
    action: ExitAction
    reason: str
    deadline: datetime


def position_exit_deadline(position: Position, policy: TimedExitPolicy) -> datetime:
    inventory_deadline = position.opened_at + timedelta(seconds=policy.max_inventory_age_seconds)
    expiry_deadline = position.expiry_at - timedelta(seconds=policy.force_exit_before_expiry_seconds)
    return min(inventory_deadline, expiry_deadline)


def plan_timed_exit(
    position: Position,
    policy: TimedExitPolicy,
    *,
    now: datetime,
    building_orders_open: int,
    all_order_state_known: bool,
    account_state_fresh: bool,
    book_fresh: bool,
) -> ExitDecision:
    """Plan exit ordering; this function never sends an order itself."""
    deadline = position_exit_deadline(position, policy)
    if now < deadline:
        return ExitDecision(ExitAction.KEEP_PASSIVE, "inventory deadline not reached", deadline)
    if not all_order_state_known or not account_state_fresh:
        return ExitDecision(ExitAction.HALT_UNKNOWN_STATE, "cannot exit with unknown account/order state", deadline)
    if building_orders_open:
        return ExitDecision(ExitAction.CANCEL_BUILDING_ORDERS, "cancel risk-adding orders before exit", deadline)
    if not book_fresh:
        return ExitDecision(ExitAction.HALT_UNKNOWN_STATE, "cannot price exact exit from a fresh book", deadline)

    expiry_due = now >= position.expiry_at - timedelta(seconds=policy.force_exit_before_expiry_seconds)
    if expiry_due:
        return ExitDecision(ExitAction.PLACE_EXPIRY_EXIT, "pre-expiry deadline reached", deadline)
    return ExitDecision(ExitAction.PLACE_TIMED_EXIT, "inventory age deadline reached", deadline)


def pending_capacity(current_pending_orders: int, requested_orders: int, cap: int = 30) -> bool:
    """Filled positions are intentionally not inputs to this order-count gate."""
    if min(current_pending_orders, requested_orders) < 0:
        raise ValueError("order counts cannot be negative")
    if cap > 30:
        raise ValueError("cap exceeds the operator-approved maximum")
    return current_pending_orders + requested_orders <= cap
