from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from .capacity import MAX_PENDING_ORDERS


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class RiskWindowPolicy:
    stop_loss_usd: Decimal
    max_pending_orders: int = 30

    def __post_init__(self) -> None:
        if self.stop_loss_usd <= 0:
            raise ValueError("risk-window stop loss must be positive")
        if not 1 <= self.max_pending_orders <= MAX_PENDING_ORDERS:
            raise ValueError("pending-order cap exceeds the approved ceiling")

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("risk-window time must be timezone-aware")
        local = now.astimezone(EASTERN)
        start_hour = (local.hour // 6) * 6
        start = datetime.combine(local.date(), time(start_hour), tzinfo=EASTERN)
        end = start + timedelta(hours=6)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def pending_order_cap(self, realized_loss_usd: Decimal) -> int:
        """Keep full reward breadth until closed-trade loss reaches HARD."""
        loss = max(Decimal("0"), realized_loss_usd)
        return 0 if loss >= self.stop_loss_usd else self.max_pending_orders


@dataclass(frozen=True, slots=True)
class RiskWindowMark:
    start: datetime
    end: datetime
    realized_pnl: Decimal
    realized_loss: Decimal
    pending_order_cap: int


def evaluate_window(
    policy: RiskWindowPolicy,
    *,
    now: datetime,
    realized_pnl: Decimal,
) -> RiskWindowMark:
    start, end = policy.bounds(now)
    realized_loss = max(Decimal("0"), -realized_pnl)
    return RiskWindowMark(
        start=start,
        end=end,
        realized_pnl=realized_pnl,
        realized_loss=realized_loss,
        pending_order_cap=policy.pending_order_cap(realized_loss),
    )
