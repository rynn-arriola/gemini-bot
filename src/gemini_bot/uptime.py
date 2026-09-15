from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from .account_state import AccountReducer
from .catalog import RewardedContract
from .domain import Outcome, UptimeProgress
from .market_state import LocalBook


class UptimeStore(Protocol):
    def sample_reward_uptime(self, **kwargs: object) -> None: ...
    def load_reward_uptime(
        self, pool_id: str, window_start: datetime, window_end: datetime
    ) -> tuple[int, int]: ...


def parse_reward_window(contract: RewardedContract) -> tuple[datetime, datetime] | None:
    if not contract.reward_starts_at or not contract.reward_ends_at:
        return None
    start = datetime.fromisoformat(contract.reward_starts_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(contract.reward_ends_at.replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("invalid reward interval")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def locally_qualifying(contract: RewardedContract, account: AccountReducer, book: LocalBook | None = None, *, now: datetime | None = None) -> bool:
    if (book is None or not book.synchronized or book.top() is None or now is None
            or not 0 <= (now - book.observed_at).total_seconds() <= 3
            or not contract.pool.active or not contract.rules.active or contract.rules.post_only_mode):
        return False
    midpoint = book.top().midpoint
    outcomes = {
        order.outcome
        for order in account.orders.values()
        if order.symbol == contract.rules.symbol
        and order.working
        and order.side.value == "BUY"
        and order.remaining_quantity >= 10
        and abs((order.price if order.outcome is Outcome.YES else 1 - order.price) - midpoint) <= Decimal(".10")
    }
    return bool(outcomes)  # Either side can score; two sides are a bonus, not a prerequisite.


def sample_and_load_progress(
    store: UptimeStore,
    contracts: list[RewardedContract],
    account: AccountReducer,
    *,
    now: datetime,
    books: dict[str, LocalBook] | None = None,
) -> dict[str, UptimeProgress]:
    if now.tzinfo is None:
        raise ValueError("uptime sample time must be timezone-aware")
    progress: dict[str, UptimeProgress] = {}
    seen: set[str] = set()
    for contract in contracts:
        if contract.pool.pool_id in seen:
            continue
        seen.add(contract.pool.pool_id)
        window = parse_reward_window(contract)
        if window is None:
            # The public row currently exposes an end but not a score-day start.
            # Keep a clearly labeled UTC-day operational proxy; do not treat it
            # as venue-certified eligibility.
            start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            if contract.reward_ends_at:
                advertised_end = datetime.fromisoformat(
                    contract.reward_ends_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                end = min(end, advertised_end)
            window = (start, end)
        start, end = window
        # Advertised pool periods may span weeks; rewards are daily. The UTC
        # day is only an operational proxy until the venue denominator is known.
        day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = max(start, day_start), min(end, day_start + timedelta(days=1))
        if end <= start:
            progress[contract.pool.pool_id] = UptimeProgress()
            continue
        if start <= now < end:
            store.sample_reward_uptime(
                pool_id=contract.pool.pool_id, window_start=start, window_end=end,
                sample_at=now, eligible=contract.pool.active and contract.rules.active,
                qualifying=locally_qualifying(contract, account, (books or {}).get(contract.rules.symbol), now=now),
            )
        eligible, qualifying = store.load_reward_uptime(contract.pool.pool_id, start, end)
        remaining = max(0, int((end - max(now, start)).total_seconds() // 60))
        progress[contract.pool.pool_id] = UptimeProgress(eligible, qualifying, remaining)
    return progress
