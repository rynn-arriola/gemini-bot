from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .risk_windows import RiskWindowPolicy


class RiskWindowStore(Protocol):
    def load_realized_risk_window(self, window_start: datetime) -> dict[str, object] | None: ...

    def upsert_realized_risk_window(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        realized_pnl: Decimal,
        loss_high_water: Decimal,
        pending_order_cap: int,
        classification_complete: bool,
        updated_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PersistentRiskMark:
    window_start: datetime
    window_end: datetime
    realized_pnl: Decimal
    loss_high_water: Decimal
    pending_order_cap: int
    classification_complete: bool

    @property
    def permits_new_risk(self) -> bool:
        return self.classification_complete and self.pending_order_cap > 0


class PersistentRiskWindow:
    """Six-hour closed-trade loss authority that stays fixed-width until HARD."""

    def __init__(self, policy: RiskWindowPolicy, store: RiskWindowStore) -> None:
        self.policy = policy
        self.store = store

    def update(
        self,
        *,
        now: datetime,
        realized_pnl: Decimal,
        classification_complete: bool,
        reconciled: bool = False,
    ) -> PersistentRiskMark:
        # Only a caller that has reconciled account state and replayed closing
        # evidence may clear an incomplete classification. HARD stays latched.
        start, end = self.policy.bounds(now)
        stored = self.store.load_realized_risk_window(start)
        if stored is None:
            prior_loss = Decimal("0")
            prior_cap = self.policy.max_pending_orders
            prior_classification = True
        else:
            prior_loss = Decimal(stored["loss_high_water"])
            prior_cap = int(stored["pending_order_cap"])
            prior_classification = bool(stored["classification_complete"])
        observed_loss = max(Decimal("0"), -realized_pnl)
        high_water = max(prior_loss, observed_loss)
        cap = min(prior_cap, self.policy.pending_order_cap(high_water))
        complete = (prior_classification or reconciled) and classification_complete
        self.store.upsert_realized_risk_window(
            window_start=start,
            window_end=end,
            realized_pnl=realized_pnl,
            loss_high_water=high_water,
            pending_order_cap=cap,
            classification_complete=complete,
            updated_at=now,
        )
        return PersistentRiskMark(start, end, realized_pnl, high_water, cap, complete)
