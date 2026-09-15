from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .catalog import RewardedContract
from .domain import UptimeProgress, uptime_progress_key
from .market_state import LocalBook
from .account_state import AccountReducer
from .domain import Outcome
from .reward_ranking import RankInput, RankedCandidate, rank_unique_pools
def rank_live_universe(
    contracts: list[RewardedContract],
    books: dict[str, LocalBook],
    *,
    now: datetime,
    quantity: Decimal,
    offset_ticks: int,
    uptime: dict[str, UptimeProgress] | None = None,
    account: AccountReducer | None = None,
    market_data_verified_at: datetime | None = None,
) -> list[RankedCandidate]:
    inputs: list[RankInput] = []
    progress = uptime or {}
    for contract in contracts:
        book = books.get(contract.rules.symbol)
        if book is not None and account is not None:
            book = LocalBook(book.symbol, dict(book.bids), dict(book.asks), book.last_update_id, book.synchronized, book.observed_at)
            for order in account.orders.values():
                if order.symbol != book.symbol or not order.working:
                    continue
                is_bid = (order.outcome is Outcome.YES) == (order.side.value == "BUY")
                levels = book.bids if is_bid else book.asks
                price = order.price if order.outcome is Outcome.YES else 1 - order.price
                if price in levels:
                    levels[price] -= order.remaining_quantity
                    if levels[price] <= 0:
                        levels.pop(price)
        top = book.top() if book is not None else None
        if top is None:
            continue
        if market_data_verified_at is not None:
            # The book's levels may be unchanged while another message on the
            # same gap-checked WebSocket proves connection continuity.
            top = type(top)(
                top.symbol, top.bid, top.bid_size, top.ask, top.ask_size,
                top.update_id, market_data_verified_at,
            )
        inputs.append(RankInput(
            pool=contract.pool,
            rules=contract.rules,
            book=top,
            uptime=progress.get(
                uptime_progress_key(contract.event_ticker, contract.pool.pool_id),
                progress.get(contract.pool.pool_id, UptimeProgress()),
            ),
            event_ticker=contract.event_ticker,
            pool_source=contract.pool_source,
            pool_event_count=contract.pool_event_count,
            recurring_duration_value=contract.recurring_duration_value,
            recurring_duration_unit=contract.recurring_duration_unit,
        ))
    return rank_unique_pools(
        inputs,
        quantity=quantity,
        offset_ticks=offset_ticks,
        now=now,
    )


def public_rank_rows(candidates: list[RankedCandidate], *, target_uptime: Decimal = Decimal(".75")) -> list[dict[str, object]]:
    return [
        {
            "pool_id": item.pool_id,
            "event_ticker": item.event_ticker,
            "symbol": item.symbol,
            "eligible_now": item.eligible_now,
            "uptime_feasible": item.uptime_feasible,
            "target_uptime_proxy": str(target_uptime),
            "target_reachable_proxy": None if item.best_possible_uptime is None else item.best_possible_uptime >= target_uptime,
            "priority_score": str(item.priority_score),
            "potential_priority_score": str(item.potential_priority_score),
            "quality_proxy": str(item.quality_proxy),
            "capital_proxy_usd": str(item.capital_proxy_usd),
            "reasons": list(item.reasons),
            "local_eastern_day_observed_uptime_proxy": str(item.observed_uptime) if item.observed_uptime is not None else None,
            "local_eastern_day_best_possible_uptime_proxy": str(item.best_possible_uptime) if item.best_possible_uptime is not None else None,
            "observed_start_best_possible_uptime_bound": str(item.observed_start_best_possible_uptime) if item.observed_start_best_possible_uptime is not None else None,
            "uptime_window_start": item.uptime_window_start.isoformat() if item.uptime_window_start else None,
            "uptime_window_end": item.uptime_window_end.isoformat() if item.uptime_window_end else None,
            "uptime_start_source": item.uptime_start_source,
            "pool_source": item.pool_source,
            "pool_event_count": item.pool_event_count,
            "recurring_duration_value": item.recurring_duration_value,
            "recurring_duration_unit": item.recurring_duration_unit,
            "yes_price": str(item.quote.yes.price) if item.quote else None,
            "no_price": str(item.quote.no.price) if item.quote else None,
            "quantity_each": str(item.quote.yes.quantity) if item.quote else None,
        }
        for item in candidates
    ]
