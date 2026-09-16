from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import Protocol
from zoneinfo import ZoneInfo

from .account_state import AccountReducer
from .catalog import RewardedContract
from .domain import Outcome, UptimeProgress, uptime_progress_key
from .market_state import LocalBook


EASTERN = ZoneInfo("America/New_York")


def eastern_day_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the Eastern calendar day containing ``now``, expressed in UTC.

    Gemini's authenticated daily reward summaries are keyed by Eastern dates.
    Zone-aware local midnights preserve 23/25-hour DST transition days.
    """
    if now.tzinfo is None:
        raise ValueError("reward day requires a timezone-aware timestamp")
    local = now.astimezone(EASTERN)
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    next_date = local_start.date() + timedelta(days=1)
    local_end = datetime.combine(next_date, datetime.min.time(), tzinfo=EASTERN)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


class UptimeStore(Protocol):
    def sample_reward_uptime(self, **kwargs: object) -> None: ...
    def load_reward_uptime(
        self, event_ticker: str, pool_id: str, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, datetime | None]: ...


def parse_reward_window(contract: RewardedContract) -> tuple[datetime, datetime] | None:
    if not contract.reward_starts_at or not contract.reward_ends_at:
        return None
    start = datetime.fromisoformat(contract.reward_starts_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(contract.reward_ends_at.replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("invalid reward interval")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _parse_optional_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reward/contract timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def estimated_reward_window(
    contract: RewardedContract,
    now: datetime,
    *,
    exit_before_expiry_seconds: int = 300,
) -> tuple[datetime, datetime, str]:
    """Estimated venue denominator, never shortened by a local exit policy.

    The reward API currently publishes an end but no start. A documented
    reward start wins when available; otherwise the Eastern day and known
    contract trade start bound the conservative denominator. The observed
    first-seen bound is stored separately and never weakens admission.
    """
    if exit_before_expiry_seconds < 0:
        raise ValueError("pre-expiry offset cannot be negative")
    day_start, day_end = eastern_day_window(now)
    reward_start = _parse_optional_timestamp(contract.reward_starts_at)
    reward_end = _parse_optional_timestamp(contract.reward_ends_at)
    if reward_start is not None and reward_end is not None and reward_end <= reward_start:
        raise ValueError("invalid reward interval")
    trade_start = _parse_optional_timestamp(contract.trade_starts_at)
    expiry = _parse_optional_timestamp(contract.expiry)

    starts = [(day_start, "eastern_day_conservative_reward_start")]
    if trade_start is not None:
        starts.append((trade_start, "contract_effective_date_conservative_reward_start"))
    if reward_start is not None:
        starts.append((reward_start, "published_reward_start"))
    ends = [day_end]
    if reward_end is not None:
        ends.append(reward_end)
    if expiry is not None:
        ends.append(expiry)
    start, source = max(starts, key=lambda item: item[0])
    return start, min(ends), source


def quote_cutoff(
    contract: RewardedContract, venue_end: datetime, *,
    exit_before_expiry_seconds: int = 300, sports_exit_before_start_seconds: int = 1200,
) -> datetime:
    """Last local quote opportunity; not evidence that venue scoring ends."""
    ends = [venue_end]
    expiry = _parse_optional_timestamp(contract.expiry)
    if expiry is not None:
        ends.append(expiry - timedelta(seconds=exit_before_expiry_seconds))
    if contract.event_category.casefold() == "sports":
        start = _parse_optional_timestamp(contract.event_starts_at)
        if start is None or contract.event_is_live:
            # No safe sports admission when the start deadline is unknown.
            return datetime.min.replace(tzinfo=timezone.utc)
        ends.append(start - timedelta(seconds=sports_exit_before_start_seconds))
    return min(ends)


def locally_qualifying(
    contract: RewardedContract,
    account: AccountReducer,
    book: LocalBook | None = None,
    *,
    now: datetime | None = None,
    market_data_verified_at: datetime | None = None,
    minimum_sides: int = 1,
) -> bool:
    freshness_at = market_data_verified_at or (book.observed_at if book is not None else None)
    if (book is None or not book.synchronized or book.top() is None or now is None
            or freshness_at is None or not 0 <= (now - freshness_at).total_seconds() <= 3
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
    return len(outcomes) >= minimum_sides  # Either side can score; two sides are a bonus.


def sample_and_load_progress(
    store: UptimeStore,
    contracts: list[RewardedContract],
    account: AccountReducer,
    *,
    now: datetime,
    books: dict[str, LocalBook] | None = None,
    market_data_verified_at: datetime | None = None,
    exit_before_expiry_seconds: int = 300,
    sports_exit_before_start_seconds: int = 1200,
) -> dict[str, UptimeProgress]:
    if now.tzinfo is None:
        raise ValueError("uptime sample time must be timezone-aware")
    progress: dict[str, UptimeProgress] = {}
    seen: set[str] = set()
    for contract in contracts:
        key = uptime_progress_key(contract.event_ticker, contract.pool.pool_id)
        if key in seen:
            continue
        seen.add(key)
        start, end, start_source = estimated_reward_window(
            contract, now, exit_before_expiry_seconds=exit_before_expiry_seconds,
        )
        if end <= start:
            value = UptimeProgress(start_source=start_source, window_valid=False)
            progress[key] = value
            progress.setdefault(contract.pool.pool_id, value)
            continue
        cutoff = quote_cutoff(
            contract, end, exit_before_expiry_seconds=exit_before_expiry_seconds,
            sports_exit_before_start_seconds=sports_exit_before_start_seconds,
        )
        if start <= now < end:
            store.sample_reward_uptime(
                event_ticker=contract.event_ticker,
                pool_id=contract.pool.pool_id, window_start=start, window_end=end,
                sample_at=now, eligible=contract.pool.active and contract.rules.active,
                qualifying=now < cutoff and locally_qualifying(
                    contract, account, (books or {}).get(contract.rules.symbol),
                    now=now, market_data_verified_at=market_data_verified_at,
                ),
            )
        eligible, qualifying, first_observed_at = store.load_reward_uptime(
            contract.event_ticker, contract.pool.pool_id, start, end,
        )
        total = max(1, ceil((end - start).total_seconds() / 60))
        elapsed = min(total, max(0, int((min(now, end) - start).total_seconds() // 60) + 1))
        # Inactive/missing observations must not forgive elapsed minutes.
        eligible = max(eligible, elapsed)
        venue_remaining = max(0, total - eligible)
        remaining = min(venue_remaining, max(0, int((cutoff - max(now, start)).total_seconds() // 60)))
        observed_elapsed = None
        if first_observed_at is not None:
            first = max(first_observed_at, start)
            observed_elapsed = max(
                0, int((min(now, end) - first).total_seconds() // 60) + 1,
            )
        value = UptimeProgress(
            eligible, qualifying, remaining,
            observed_window_eligible_snapshots=observed_elapsed,
            window_start=start,
            window_end=end,
            start_source=start_source,
            remaining_venue_snapshots_estimate=venue_remaining,
            quote_cutoff=cutoff,
        )
        progress[key] = value
        # Compatibility alias for callers that only know a unique pool. Shared
        # pools are ranked with the event-specific composite key above.
        progress.setdefault(contract.pool.pool_id, value)
    return progress
