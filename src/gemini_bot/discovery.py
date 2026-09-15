from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .catalog import RewardedContract, join_reward_contracts, reward_pool_key


class DiscoveryRest(Protocol):
    async def reward_events(self, *, ceiling: int = 3000) -> list[dict[str, Any]]: ...
    async def event(self, event_ticker: str) -> dict[str, Any]: ...


def select_pool_representatives(
    rows: list[dict[str, Any]],
    *,
    maximum_pools: int,
) -> list[dict[str, Any]]:
    if not 1 <= maximum_pools <= 32:
        raise ValueError("pool discovery breadth must be between 1 and 32")
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = reward_pool_key(row)
        budget = Decimal(str(row.get("daily_pool_usd", "0")))
        makers = max(1, int(row.get("qualifying_maker_count") or 1))
        if budget <= 0:
            continue
        current = best.get(key)
        score = budget / Decimal(makers)
        current_score = (
            Decimal(str(current.get("daily_pool_usd", "0")))
            / Decimal(max(1, int(current.get("qualifying_maker_count") or 1)))
            if current is not None else Decimal("-1")
        )
        candidate_key = (score, str(row.get("event_ticker", "")))
        current_key = (current_score, str(current.get("event_ticker", ""))) if current else None
        if current_key is None or candidate_key > current_key:
            best[key] = row
    return sorted(
        best.values(),
        key=lambda row: (
            -(Decimal(str(row["daily_pool_usd"])) / Decimal(max(1, int(row.get("qualifying_maker_count") or 1)))),
            reward_pool_key(row),
            str(row.get("event_ticker", "")),
        ),
    )[:maximum_pools]


def _headline_spread(contract: dict[str, Any]) -> Decimal:
    prices = contract.get("prices")
    if not isinstance(prices, dict):
        return Decimal("999")
    try:
        return Decimal(str(prices["bestAsk"])) - Decimal(str(prices["bestBid"]))
    except (KeyError, TypeError, ValueError):
        return Decimal("999")


@dataclass(slots=True)
class RewardUniverseLoader:
    rest: DiscoveryRest

    async def load(self, *, maximum_pools: int = 32) -> list[RewardedContract]:
        rewards = await self.rest.reward_events()
        selected = select_pool_representatives(rewards, maximum_pools=maximum_pools)
        result: list[RewardedContract] = []
        for reward in selected:
            ticker = str(reward["event_ticker"]).upper()
            event_response = await self.rest.event(ticker)
            event = event_response.get("event", event_response)
            if not isinstance(event, dict):
                continue
            if str(event.get("category", reward.get("category", ""))).casefold() == "sports" and bool(event.get("isLive")):
                continue
            contracts = [
                contract for contract in event.get("contracts", [])
                if isinstance(contract, dict)
                and contract.get("status") == "active"
                and contract.get("marketState") == "open"
            ]
            if not contracts:
                continue
            chosen = min(
                contracts,
                key=lambda contract: (
                    _headline_spread(contract),
                    str(contract.get("instrumentSymbol", "")),
                ),
            )
            reduced_event = {**event, "contracts": [chosen]}
            joined = join_reward_contracts([reward], {ticker: reduced_event})
            if joined:
                result.append(joined[0])
        return result
