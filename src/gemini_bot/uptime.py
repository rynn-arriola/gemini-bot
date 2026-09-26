from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import Protocol
from zoneinfo import ZoneInfo

from .account_state import AccountReducer
from .catalog import RewardedContract
from .domain import Outcome, UptimeProgress, uptime_progress_key
from .market_state import LocalBook


EASTERN = ZoneInfo("America/New_York")


def eastern_day_window(now: datetime, boundary_hour: int = 0) -> tuple[datetime, datetime]:
    """Return the Eastern reward day containing ``now``, expressed in UTC.

    Gemini's authenticated daily reward summaries are keyed by Eastern dates.
    ``boundary_hour`` 0 is the calendar day; policy v9 uses 17, because every
    complete 1440-minute venue report was published the same evening, which
    places the scoring-day close at 5 pm ET. Zone-aware local boundaries
    preserve 23/25-hour DST transition days.
    """
    if now.tzinfo is None:
        raise ValueError("reward day requires a timezone-aware timestamp")
    if not 0 <= boundary_hour <= 23:
        raise ValueError("reward-day boundary hour must be 0-23")
    local = now.astimezone(EASTERN)
    start_date = local.date() if local.hour >= boundary_hour else local.date() - timedelta(days=1)
    local_start = datetime.combine(start_date, time(boundary_hour), tzinfo=EASTERN)
    local_end = datetime.combine(start_date + timedelta(days=1), time(boundary_hour), tzinfo=EASTERN)
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
    day_boundary_hour: int = 0,
    ignore_trade_start: bool = False,
) -> tuple[datetime, datetime, str]:
    """Estimated venue denominator, never shortened by a local exit policy.

    The reward API currently publishes an end but no start. A documented
    reward start wins when available; otherwise the Eastern day and known
    contract trade start bound the conservative denominator. The observed
    first-seen bound is stored separately and never weakens admission.
    """
    if exit_before_expiry_seconds < 0:
        raise ValueError("pre-expiry offset cannot be negative")
    day_start, day_end = eastern_day_window(now, day_boundary_hour)
    reward_start = _parse_optional_timestamp(contract.reward_starts_at)
    reward_end = _parse_optional_timestamp(contract.reward_ends_at)
    if reward_start is not None and reward_end is not None and reward_end <= reward_start:
        raise ValueError("invalid reward interval")
    trade_start = _parse_optional_timestamp(contract.trade_starts_at)
    expiry = _parse_optional_timestamp(contract.expiry)

    starts = [(day_start, "eastern_day_conservative_reward_start")]
    # Policy v9 worst case: books trade before the contract's effectiveDate,
    # so Gemini may count the whole scoring day in the denominator.
    if trade_start is not None and not ignore_trade_start:
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


SPORTS_FUTURE_MINIMUM_LIFE = timedelta(days=7)


def sports_future(contract: RewardedContract, reference: datetime) -> bool:
    """Season-long sports market: no single game start, not live, long expiry.

    Policy v9 may quote these like other long-dated events; a game market
    with an unknown start remains unquotable.
    """
    if contract.event_category.casefold() != "sports" or contract.event_is_live:
        return False
    if _parse_optional_timestamp(contract.event_starts_at) is not None:
        return False
    expiry = _parse_optional_timestamp(contract.expiry)
    return expiry is not None and expiry - reference >= SPORTS_FUTURE_MINIMUM_LIFE


def quote_cutoff(
    contract: RewardedContract, venue_end: datetime, *,
    exit_before_expiry_seconds: int = 300, sports_exit_before_start_seconds: int = 1200,
    crypto_entry_before_expiry_seconds: int = 300, allow_sports_futures: bool = False,
) -> datetime:
    """Last local quote opportunity; not evidence that venue scoring ends."""
    ends = [venue_end]
    expiry = _parse_optional_timestamp(contract.expiry)
    if expiry is not None:
        entry_offset = (max(exit_before_expiry_seconds, crypto_entry_before_expiry_seconds)
                        if contract.event_category.casefold() == "crypto"
                        else exit_before_expiry_seconds)
        ends.append(expiry - timedelta(seconds=entry_offset))
    if allow_sports_futures and sports_future(contract, venue_end):
        return min(ends)
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
    market_data_verified_at: datetime | dict[str, datetime | None] | None = None,
    minimum_sides: int = 1,
) -> bool:
    freshness_at = (market_data_verified_at.get(contract.rules.symbol)
                    if isinstance(market_data_verified_at, dict)
                    else market_data_verified_at or (book.observed_at if book is not None else None))
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
    market_data_verified_at: datetime | dict[str, datetime | None] | None = None,
    exit_before_expiry_seconds: int = 300,
    sports_exit_before_start_seconds: int = 1200,
    crypto_entry_before_expiry_seconds: int = 300,
    day_boundary_hour: int = 0,
    allow_sports_futures: bool = False,
    worst_case_denominator: bool = False,
) -> dict[str, UptimeProgress]:
    if now.tzinfo is None:
        raise ValueError("uptime sample time must be timezone-aware")
    progress: dict[str, UptimeProgress] = {}
    grouped: dict[str, list[RewardedContract]] = {}
    for item in contracts:
        grouped.setdefault(uptime_progress_key(item.event_ticker, item.pool.pool_id), []).append(item)
    seen: set[str] = set()
    for contract in contracts:
        key = uptime_progress_key(contract.event_ticker, contract.pool.pool_id)
        if key in seen:
            continue
        seen.add(key)
        peers = grouped[key]
        windows = [(c, estimated_reward_window(c, now, exit_before_expiry_seconds=exit_before_expiry_seconds,
                                               day_boundary_hour=day_boundary_hour,
                                               ignore_trade_start=worst_case_denominator))
                   for c in peers]
        valid = [(c, w) for c, w in windows if w[1] > w[0]]
        if valid:
            start = min(w[0] for _, w in valid)
            end = max(w[1] for _, w in valid)
            start_source = valid[0][1][2] if len(peers) == 1 else "multi_contract_event_window_envelope_proxy"
        else:
            start, end, start_source = windows[0][1]
        if end <= start:
            value = UptimeProgress(start_source=start_source, window_valid=False)
            progress[key] = value
            progress.setdefault(contract.pool.pool_id, value)
            continue
        cutoffs = {c.rules.symbol: quote_cutoff(
            c, w[1], exit_before_expiry_seconds=exit_before_expiry_seconds,
            sports_exit_before_start_seconds=sports_exit_before_start_seconds,
            crypto_entry_before_expiry_seconds=crypto_entry_before_expiry_seconds,
            allow_sports_futures=allow_sports_futures,
        ) for c, w in valid}
        cutoff = max(cutoffs.values())
        if start <= now < end:
            store.sample_reward_uptime(
                event_ticker=contract.event_ticker,
                pool_id=contract.pool.pool_id, window_start=start, window_end=end,
                sample_at=now, eligible=any(c.pool.active and c.rules.active and w[0] <= now < w[1] for c, w in valid),
                qualifying=any(w[0] <= now < cutoffs[c.rules.symbol] and locally_qualifying(
                    c, account, (books or {}).get(c.rules.symbol),
                    now=now, market_data_verified_at=market_data_verified_at,
                ) for c, w in valid),
            )
        eligible, qualifying, first_observed_at = store.load_reward_uptime(
            contract.event_ticker, contract.pool.pool_id, start, end,
        )
        total = max(1, ceil((end - start).total_seconds() / 60))
        elapsed = min(total, max(0, int((min(now, end) - start).total_seconds() // 60) + 1))
        # Inactive/missing observations must not forgive elapsed minutes.
        eligible = max(eligible, elapsed)
        venue_remaining = max(0, total - eligible)
        # We can only quote once the contract is effective, even when the
        # worst-case denominator counts earlier minutes.
        trade_starts = [_parse_optional_timestamp(c.trade_starts_at) for c, _ in valid]
        quote_start = max([now, start] + ([min(t for t in trade_starts if t is not None)]
                                          if any(t is not None for t in trade_starts) else []))
        remaining = min(venue_remaining, max(0, int((cutoff - quote_start).total_seconds() // 60)))
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
