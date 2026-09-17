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


DISCOVERY_RANKING_VERSION = 2


def _maker_tiebreak(row: dict[str, Any]) -> tuple[bool, int]:
    count = row.get("qualifying_maker_count")
    # Missing/zero historical qualification is not proof of an empty pool.
    return count is None or int(count) <= 0, max(0, int(count or 0))


@dataclass(frozen=True, slots=True)
class PoolDiscoveryRank:
    pool_id: str
    daily_budget_usd: Decimal
    event_count: int
    observed_contract_events: int
    estimated_contract_count: int
    priority_proxy: Decimal
    representative: dict[str, Any]


def rank_discovery_pools(
    rows: list[dict[str, Any]],
    contracts: list[RewardedContract] | None = None,
) -> list[PoolDiscoveryRank]:
    """Bounded research priority, NOT a payout estimate or eligibility rule.

    Budget / sqrt(breadth) softly prefers smaller sharing scopes without
    assuming equal event payouts or banning broad pools. Before event reads,
    breadth is the larger of published count and distinct catalog events.
    Afterwards use known active contract counts and a rounded-up sample mean
    for unseen events. That imputation is uncertainty, not measured liquidity.
    Final allocation still uses actual book pressure and marginal economics.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    known: dict[str, dict[str, int]] = {}
    for contract in contracts or []:
        events = known.setdefault(contract.pool.pool_id, {})
        events[contract.event_ticker] = max(
            events.get(contract.event_ticker, 0), contract.event_contract_count,
        )
    for row in rows:
        groups.setdefault(reward_pool_key(row), []).append(row)
    ranked = []
    for pool_id, group in groups.items():
        budgets = {Decimal(str(r.get("daily_pool_usd", "0"))) for r in group}
        if len(budgets) != 1 or not all(b.is_finite() for b in budgets):
            raise ValueError("shared pool advertises conflicting or invalid budgets")
        budget = next(iter(budgets))
        if budget <= 0:
            continue
        published = [int(r["pool_event_count"]) for r in group if r.get("pool_event_count") is not None]
        if any(n <= 0 for n in published):
            raise ValueError("invalid pool event count")
        event_count = max(1, len({str(r["event_ticker"]).upper() for r in group}), max(published, default=1))
        counts = known.get(pool_id, {})
        event_count = max(event_count, len(counts))
        total = sum(counts.values())
        typical = max(1, (total + len(counts) - 1) // len(counts)) if counts else 1
        breadth = max(event_count, total + (event_count - len(counts)) * typical)
        representative = min(group, key=lambda r: (_maker_tiebreak(r), str(r.get("event_ticker", ""))))
        ranked.append(PoolDiscoveryRank(
            pool_id, budget, event_count, len(counts), breadth,
            budget / Decimal(breadth).sqrt(), representative,
        ))
    return sorted(ranked, key=lambda p: (
        -p.priority_proxy, -p.daily_budget_usd, p.estimated_contract_count,
        _maker_tiebreak(p.representative), p.pool_id,
    ))


def select_pool_representatives(
    rows: list[dict[str, Any]],
    *,
    maximum_pools: int,
) -> list[dict[str, Any]]:
    if not 1 <= maximum_pools <= 32:
        raise ValueError("pool discovery breadth must be between 1 and 32")
    return [p.representative for p in rank_discovery_pools(rows)[:maximum_pools]]


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
                _maker_tiebreak(r),
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
        # Incumbents were added first. Their pool's low catalog position must
        # not crowd higher-ranked challengers out of the reserve frontier.
        candidate_order = [p.pool_id for p in rank_discovery_pools(
            rewards, [item for choices in buckets.values() for item in choices],
        ) if buckets.get(p.pool_id)]
        frontier = candidate_order[:max(1, (maximum_pools + 1) // 2)]
        for index in range(maximum_pools):
            for pool_id in frontier:
                choices = buckets.get(pool_id, [])
                if index < len(choices):
                    add(choices[index])
            if len(result) >= maximum_pools:
                break
        for pool_id in candidate_order:
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
