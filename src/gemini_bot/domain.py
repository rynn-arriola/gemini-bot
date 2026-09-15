from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


ZERO = Decimal("0")
ONE = Decimal("1")


def canonical_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    return symbol


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class EngineState(str, Enum):
    PAUSED = "paused"
    RECONCILING = "reconciling"
    READY = "ready"
    QUOTING = "quoting"
    DRAINING = "draining"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class BookTop:
    """Top of book expressed in YES-price space."""

    symbol: str
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal
    update_id: int
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        if not (ZERO < self.bid < self.ask < ONE):
            raise ValueError("book must satisfy 0 < bid < ask < 1")
        if self.bid_size < ZERO or self.ask_size < ZERO:
            raise ValueError("book quantities cannot be negative")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


@dataclass(frozen=True, slots=True)
class ContractRules:
    symbol: str
    tick_size: Decimal
    quantity_minimum: Decimal
    quantity_increment: Decimal
    active: bool = True
    post_only_mode: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        if self.tick_size <= ZERO:
            raise ValueError("tick_size must be positive")
        if self.quantity_minimum <= ZERO or self.quantity_increment <= ZERO:
            raise ValueError("quantity constraints must be positive")


@dataclass(frozen=True, slots=True)
class RewardPool:
    """One unique budget. Multiple contracts may share this identifier."""

    pool_id: str
    daily_budget_usd: Decimal
    active: bool = True
    qualifying_maker_count: int | None = None

    def __post_init__(self) -> None:
        if not self.pool_id:
            raise ValueError("pool_id is required")
        if self.daily_budget_usd < ZERO:
            raise ValueError("daily budget cannot be negative")
        if self.qualifying_maker_count is not None and self.qualifying_maker_count < 0:
            raise ValueError("maker count cannot be negative")


@dataclass(frozen=True, slots=True)
class QuoteLeg:
    symbol: str
    outcome: Outcome
    side: Side
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))


@dataclass(frozen=True, slots=True)
class QuotePair:
    yes: QuoteLeg
    no: QuoteLeg

    def __post_init__(self) -> None:
        if self.yes.symbol != self.no.symbol:
            raise ValueError("quote legs must use the same symbol")
        if self.yes.outcome is not Outcome.YES or self.no.outcome is not Outcome.NO:
            raise ValueError("quote pair must contain YES then NO")
        if self.yes.side is not Side.BUY or self.no.side is not Side.BUY:
            raise ValueError("reward quote pair uses BUY YES and BUY NO")

    @property
    def total_reservation_proxy_usd(self) -> Decimal:
        return self.yes.price * self.yes.quantity + self.no.price * self.no.quantity


@dataclass(frozen=True, slots=True)
class UptimeProgress:
    eligible_snapshots: int = 0
    qualifying_snapshots: int = 0
    remaining_snapshots_estimate: int = 0

    def __post_init__(self) -> None:
        if min(self.eligible_snapshots, self.qualifying_snapshots, self.remaining_snapshots_estimate) < 0:
            raise ValueError("snapshot counts cannot be negative")
        if self.qualifying_snapshots > self.eligible_snapshots:
            raise ValueError("qualifying snapshots cannot exceed eligible snapshots")

    @property
    def observed_ratio(self) -> Decimal | None:
        if self.eligible_snapshots == 0:
            return None
        return Decimal(self.qualifying_snapshots) / Decimal(self.eligible_snapshots)

    @property
    def best_possible_ratio(self) -> Decimal | None:
        denominator = self.eligible_snapshots + self.remaining_snapshots_estimate
        if denominator == 0:
            return None
        return Decimal(self.qualifying_snapshots + self.remaining_snapshots_estimate) / Decimal(denominator)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
