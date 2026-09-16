from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .catalog import RewardedContract, join_reward_contracts, reward_pool_key
from .uptime import estimated_reward_window, quote_cutoff
from .domain import BookTop
from .reward_ranking import build_two_sided_quote, MAX_DISTANCE, MINIMUM_REWARD_SIZE


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
        quantity: Decimal = Decimal("20"),
        offset_ticks: int = 2,
        excluded_symbols: set[str] | None = None,
    ) -> list[RewardedContract]:
        rewards = await self.rest.reward_events() if rewards is None else rewards
        preferred = preferred or {}
        selected = select_pool_representatives(rewards, maximum_pools=maximum_pools)
        # Restore incumbent pools even when another pool's score has risen.
        pinned = [r for r in rewards if any(
                  str(r.get("event_ticker", "")).upper() == pair[0] for pair in preferred.values())
                  and Decimal(str(r.get("daily_pool_usd", "0"))) > 0]
        pool_order = list(dict.fromkeys(reward_pool_key(r) for r in pinned + selected))[:maximum_pools]
        buckets: dict[str, list[RewardedContract]] = {}
        identities: dict[str, tuple[str, str]] = {}
        preferred_pairs = set(preferred.values())
        budgets: dict[str, Decimal] = {}
        for row in rewards:
            key, budget = reward_pool_key(row), Decimal(str(row["daily_pool_usd"]))
            if budgets.setdefault(key, budget) != budget:
                raise ValueError("shared pool advertises conflicting budgets")
        reads = 0
        for pool_id in pool_order:
            rows = [r for r in rewards if reward_pool_key(r) == pool_id
                    and Decimal(str(r.get("daily_pool_usd", "0"))) > 0]
            rows.sort(key=lambda r: (
                not any(str(r.get("event_ticker", "")).upper() == p[0] for p in preferred_pairs),
                -Decimal(str(r["daily_pool_usd"])) / Decimal(max(1, int(r.get("qualifying_maker_count") or 1))),
                str(r.get("event_ticker", "")),
            ))
            # Read all pinned events first, then a bounded alternative frontier.
            pinned_count = sum(any(str(r.get("event_ticker", "")).upper() == p[0]
                                   for p in preferred_pairs) for r in rows)
            for reward in rows[:max(3, pinned_count)]:
                if reads >= 64:
                    break
                reads += 1
                choices = await self._load_event_candidates(
                    reward, preferred_pairs, now, quantity, offset_ticks,
                )
                for item in choices:
                    identity = (item.pool.pool_id, item.event_ticker)
                    if identities.setdefault(item.rules.symbol, identity) != identity:
                        raise ValueError("conflicting contract identity in reward discovery")
                choices = [item for item in choices if item.rules.symbol not in (excluded_symbols or set())
                           or (item.event_ticker, item.rules.symbol) in preferred_pairs]
                buckets.setdefault(pool_id, []).extend(choices)
        result: list[RewardedContract] = []
        used: set[str] = set()
        def add(item: RewardedContract) -> None:
            if item.rules.symbol not in used and len(result) < maximum_pools:
                result.append(item)
                used.add(item.rules.symbol)
        for choices in buckets.values():
            for item in choices:
                if (item.event_ticker, item.rules.symbol) in preferred_pairs:
                    add(item)
        # Reserve a frontier for alternatives rather than spend every watched
        # slot on one representative of a different pool.
        frontier = pool_order[:max(1, (maximum_pools + 1) // 2)]
        for index in range(maximum_pools):
            for pool_id in frontier:
                choices = buckets.get(pool_id, [])
                if index < len(choices):
                    add(choices[index])
            if len(result) >= maximum_pools:
                break
        for pool_id in pool_order:
            for item in buckets.get(pool_id, []):
                add(item)
        return result

    async def _load_event(
        self, reward: dict[str, Any], preferred: tuple[str, str] | None, now: datetime | None,
    ) -> RewardedContract | None:
        choices = await self._load_event_candidates(
            reward, {preferred} if preferred else set(), now, Decimal("20"), 2,
        )
        return choices[0] if choices else None

    async def _load_event_candidates(
        self, reward: dict[str, Any], preferred: set[tuple[str, str]],
        now: datetime | None, quantity: Decimal, offset_ticks: int,
    ) -> list[RewardedContract]:
        ticker = str(reward["event_ticker"]).upper()
        event_response = await self.rest.event(ticker)
        event = event_response.get("event", event_response)
        if not isinstance(event, dict):
            return []
        if str(event.get("category", reward.get("category", ""))).casefold() == "sports" and bool(event.get("isLive")):
            return []
        contracts = [
            contract for contract in event.get("contracts", [])
            if isinstance(contract, dict)
            and contract.get("status") == "active"
            and contract.get("marketState") == "open"
        ]
        contracts.sort(key=lambda contract: (
            (ticker, str(contract.get("instrumentSymbol", "")).upper()) not in preferred,
            _headline_spread(contract),
            str(contract.get("instrumentSymbol", "")),
        ))
        result = []
        for chosen in contracts:
            reduced_event = {**event, "contracts": [chosen], "_reward_contract_count": len(contracts)}
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
            is_incumbent = (ticker, item.rules.symbol) in preferred
            if not is_incumbent:
                if quantity < max(MINIMUM_REWARD_SIZE, item.rules.quantity_minimum) or quantity % item.rules.quantity_increment:
                    continue
                prices = chosen.get("prices")
                if isinstance(prices, dict) and prices.get("bestBid") is not None and prices.get("bestAsk") is not None:
                    try:
                        top = BookTop(item.rules.symbol, Decimal(str(prices["bestBid"])), Decimal("0"),
                                      Decimal(str(prices["bestAsk"])), Decimal("0"), 0,
                                      now or datetime.now().astimezone())
                        pair = build_two_sided_quote(top, item.rules, quantity, offset_ticks)
                        if max(top.midpoint - pair.yes.price, 1 - top.midpoint - pair.no.price) > MAX_DISTANCE:
                            continue
                    except (ValueError, ArithmeticError):
                        continue
            # Headlines screen new candidates only; live sequenced books and
            # normal safety gates still decide admission/retention.
            result.append(item)
        return result
