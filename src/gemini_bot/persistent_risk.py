from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .risk_windows import RiskWindowPolicy


class RiskWindowStore(Protocol):
    def load_risk_window(self, window_start: datetime) -> dict[str, object] | None: ...

    def upsert_risk_window(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        opening_equity: Decimal,
        loss_high_water: Decimal,
        pending_order_cap: int,
        classification_complete: bool,
        updated_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PersistentRiskMark:
    window_start: datetime
    window_end: datetime
    opening_equity: Decimal
    adjusted_equity: Decimal
    loss_high_water: Decimal
    pending_order_cap: int
    classification_complete: bool

    @property
    def permits_new_risk(self) -> bool:
        return self.classification_complete and self.pending_order_cap > 0


class PersistentRiskWindow:
    """Six-hour loss authority whose loss/cap can only tighten within a window."""

    def __init__(self, policy: RiskWindowPolicy, store: RiskWindowStore) -> None:
        self.policy = policy
        self.store = store

    def update(
        self,
        *,
        now: datetime,
        adjusted_equity: Decimal,
        classification_complete: bool,
    ) -> PersistentRiskMark:
        start, end = self.policy.bounds(now)
        stored = self.store.load_risk_window(start)
        if stored is None:
            opening = adjusted_equity
            prior_loss = Decimal("0")
            prior_cap = self.policy.max_pending_orders
            prior_classification = True
        else:
            opening = Decimal(stored["opening_equity"])
            prior_loss = Decimal(stored["loss_high_water"])
            prior_cap = int(stored["pending_order_cap"])
            prior_classification = bool(stored["classification_complete"])
        observed_loss = max(Decimal("0"), opening - adjusted_equity)
        high_water = max(prior_loss, observed_loss)
        cap = min(prior_cap, self.policy.pending_order_cap(high_water, high_water))
        complete = prior_classification and classification_complete
        self.store.upsert_risk_window(
            window_start=start,
            window_end=end,
            opening_equity=opening,
            loss_high_water=high_water,
            pending_order_cap=cap,
            classification_complete=complete,
            updated_at=now,
        )
        return PersistentRiskMark(start, end, opening, adjusted_equity, high_water, cap, complete)
