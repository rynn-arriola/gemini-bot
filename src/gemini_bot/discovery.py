from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .catalog import RewardedContract, join_reward_contracts, reward_pool_key
from .uptime import estimated_reward_window, quote_cutoff


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

    async def load(
        self, *, maximum_pools: int = 32,
        preferred: dict[str, tuple[str, str]] | None = None,
        now: datetime | None = None,
        rewards: list[dict[str, Any]] | None = None,
    ) -> list[RewardedContract]:
        rewards = await self.rest.reward_events() if rewards is None else rewards
        preferred = preferred or {}
        selected = select_pool_representatives(rewards, maximum_pools=maximum_pools)
        # Restore incumbent pools even when another pool's score has risen.
        pinned = [r for r in rewards if reward_pool_key(r) in preferred
                  and str(r.get("event_ticker", "")).upper() == preferred[reward_pool_key(r)][0]
                  and Decimal(str(r.get("daily_pool_usd", "0"))) > 0]
        pool_order = list(dict.fromkeys(reward_pool_key(r) for r in pinned + selected))[:maximum_pools]
        result: list[RewardedContract] = []
        reads = 0
        for pool_id in pool_order:
            rows = [r for r in rewards if reward_pool_key(r) == pool_id
                    and Decimal(str(r.get("daily_pool_usd", "0"))) > 0]
            rows.sort(key=lambda r: (
                str(r.get("event_ticker", "")).upper() != preferred.get(pool_id, ("", ""))[0],
                -Decimal(str(r["daily_pool_usd"])) / Decimal(max(1, int(r.get("qualifying_maker_count") or 1))),
                str(r.get("event_ticker", "")),
            ))
            # Bounded fallback across recurring instances; no unbounded REST scan.
            for reward in rows[:3]:
                if reads >= 64:
                    break
                reads += 1
                chosen = await self._load_event(reward, preferred.get(pool_id), now)
                if chosen is not None:
                    result.append(chosen)
                    break
        return result

    async def _load_event(
        self, reward: dict[str, Any], preferred: tuple[str, str] | None, now: datetime | None,
    ) -> RewardedContract | None:
        ticker = str(reward["event_ticker"]).upper()
        event_response = await self.rest.event(ticker)
        event = event_response.get("event", event_response)
        if not isinstance(event, dict):
            return None
        if str(event.get("category", reward.get("category", ""))).casefold() == "sports" and bool(event.get("isLive")):
            return None
        contracts = [
            contract for contract in event.get("contracts", [])
            if isinstance(contract, dict)
            and contract.get("status") == "active"
            and contract.get("marketState") == "open"
        ]
        contracts.sort(key=lambda contract: (
            str(contract.get("instrumentSymbol", "")).upper() != (preferred[1] if preferred else ""),
            _headline_spread(contract),
            str(contract.get("instrumentSymbol", "")),
        ))
        for chosen in contracts:
            reduced_event = {**event, "contracts": [chosen]}
            joined = join_reward_contracts([reward], {ticker: reduced_event})
            if not joined:
                continue
            item = joined[0]
            if not item.pool.active or item.rules.post_only_mode:
                continue
            if now is not None:
                start, end, _ = estimated_reward_window(item, now)
                if end <= start or now < start or now >= quote_cutoff(item, end):
                    continue
            return item
        return None
