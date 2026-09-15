from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .domain import BookTop, canonical_symbol


class SequenceGap(RuntimeError):
    pass


@dataclass(slots=True)
class LocalBook:
    symbol: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_update_id: int | None = None
    synchronized: bool = False
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.symbol = canonical_symbol(self.symbol)

    def apply_snapshot(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        first_update_id: int,
        last_update_id: int,
        observed_at: datetime | None = None,
    ) -> None:
        self._validate_levels(bids + asks)
        if first_update_id < 0 or last_update_id < first_update_id:
            raise ValueError("invalid snapshot sequence interval")
        self.bids = {price: qty for price, qty in bids if qty > 0}
        self.asks = {price: qty for price, qty in asks if qty > 0}
        self.last_update_id = last_update_id
        self.synchronized = True
        self.observed_at = observed_at or datetime.now(timezone.utc)
        self._validate_uncrossed()

    def apply_delta(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        first_update_id: int,
        last_update_id: int,
        observed_at: datetime | None = None,
    ) -> None:
        if not self.synchronized or self.last_update_id is None:
            raise SequenceGap(f"{self.symbol}: delta received before snapshot")
        if first_update_id > self.last_update_id + 1:
            self.synchronized = False
            raise SequenceGap(f"{self.symbol}: expected <= {self.last_update_id + 1}, got {first_update_id}")
        if last_update_id <= self.last_update_id:
            return
        self._apply_levels(self.bids, bids)
        self._apply_levels(self.asks, asks)
        self.last_update_id = last_update_id
        self.observed_at = observed_at or datetime.now(timezone.utc)
        self._validate_uncrossed()

    @staticmethod
    def _validate_levels(changes: list[tuple[Decimal, Decimal]]) -> None:
        if any(not price.is_finite() or not quantity.is_finite() or not 0 < price < 1 or quantity < 0 for price, quantity in changes):
            raise ValueError("invalid price/quantity in depth")

    @staticmethod
    def _apply_levels(target: dict[Decimal, Decimal], changes: list[tuple[Decimal, Decimal]]) -> None:
        LocalBook._validate_levels(changes)
        for price, quantity in changes:
            if quantity == 0:
                target.pop(price, None)
            elif quantity > 0:
                target[price] = quantity
            else:
                raise ValueError("negative depth quantity")

    def _validate_uncrossed(self) -> None:
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.synchronized = False
            raise SequenceGap(f"{self.symbol}: crossed local book")

    def top(self) -> BookTop | None:
        if not self.synchronized or not self.bids or not self.asks or self.observed_at is None:
            return None
        bid = max(self.bids)
        ask = min(self.asks)
        return BookTop(
            symbol=self.symbol,
            bid=bid,
            bid_size=self.bids[bid],
            ask=ask,
            ask_size=self.asks[ask],
            update_id=self.last_update_id or 0,
            observed_at=self.observed_at,
        )
