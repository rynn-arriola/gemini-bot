from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class RiskWindowPolicy:
    stop_loss_usd: Decimal
    max_pending_orders: int = 30
    remaining_budget_tiers: tuple[tuple[Decimal, int], ...] = (
        (Decimal("0.80"), 25),
        (Decimal("0.60"), 20),
        (Decimal("0.40"), 15),
        (Decimal("0.20"), 10),
    )

    def __post_init__(self) -> None:
        if self.stop_loss_usd <= 0:
            raise ValueError("risk-window stop loss must be positive")
        if not 1 <= self.max_pending_orders <= 30:
            raise ValueError("pending-order cap must be between 1 and 30")
        previous_fraction = Decimal("1")
        previous_cap = self.max_pending_orders
        for remaining, cap in self.remaining_budget_tiers:
            if not Decimal("0") < remaining < previous_fraction:
                raise ValueError("remaining fractions must strictly decrease")
            if not 0 <= cap <= previous_cap:
                raise ValueError("tier order caps must not increase")
            previous_fraction = remaining
            previous_cap = cap

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("risk-window time must be timezone-aware")
        local = now.astimezone(EASTERN)
        start_hour = (local.hour // 6) * 6
        start = datetime.combine(local.date(), time(start_hour), tzinfo=EASTERN)
        end = start + timedelta(hours=6)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def pending_order_cap(self, realized_loss_usd: Decimal, marked_loss_usd: Decimal) -> int:
        """Open marks can only tighten entry capacity, never expand it."""
        loss = max(Decimal("0"), realized_loss_usd, marked_loss_usd)
        if loss >= self.stop_loss_usd:
            return 0
        cap = self.max_pending_orders
        for remaining, tier_cap in self.remaining_budget_tiers:
            if loss >= self.stop_loss_usd * (Decimal("1") - remaining):
                cap = min(cap, tier_cap)
        return cap


@dataclass(frozen=True, slots=True)
class RiskWindowMark:
    start: datetime
    end: datetime
    opening_equity: Decimal
    current_equity: Decimal
    marked_equity: Decimal
    pending_order_cap: int


def evaluate_window(
    policy: RiskWindowPolicy,
    *,
    now: datetime,
    opening_equity: Decimal,
    current_equity: Decimal,
    marked_equity: Decimal,
) -> RiskWindowMark:
    start, end = policy.bounds(now)
    realized = max(Decimal("0"), opening_equity - current_equity)
    marked = max(Decimal("0"), opening_equity - marked_equity)
    return RiskWindowMark(
        start=start,
        end=end,
        opening_equity=opening_equity,
        current_equity=current_equity,
        marked_equity=marked_equity,
        pending_order_cap=policy.pending_order_cap(realized, marked),
    )
