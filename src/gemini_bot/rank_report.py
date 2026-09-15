from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .catalog import RewardedContract
from .domain import UptimeProgress
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
        inputs.append(RankInput(
            pool=contract.pool,
            rules=contract.rules,
            book=top,
            uptime=progress.get(contract.pool.pool_id, UptimeProgress()),
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
            "symbol": item.symbol,
            "eligible_now": item.eligible_now,
            "uptime_feasible": item.uptime_feasible,
            "target_uptime_proxy": str(target_uptime),
            "target_reachable_proxy": None if item.best_possible_uptime is None else item.best_possible_uptime >= target_uptime,
            "priority_score": str(item.priority_score),
            "quality_proxy": str(item.quality_proxy),
            "capital_proxy_usd": str(item.capital_proxy_usd),
            "reasons": list(item.reasons),
            "local_utc_day_observed_uptime_proxy": str(item.observed_uptime) if item.observed_uptime is not None else None,
            "local_utc_day_best_possible_uptime_proxy": str(item.best_possible_uptime) if item.best_possible_uptime is not None else None,
            "yes_price": str(item.quote.yes.price) if item.quote else None,
            "no_price": str(item.quote.no.price) if item.quote else None,
            "quantity_each": str(item.quote.yes.quantity) if item.quote else None,
        }
        for item in candidates
    ]
