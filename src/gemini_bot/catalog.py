from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .domain import ContractRules, RewardPool, canonical_symbol


@dataclass(frozen=True, slots=True)
class RewardedContract:
    event_ticker: str
    pool: RewardPool
    rules: ContractRules
    expiry: str
    reward_starts_at: str = ""
    reward_ends_at: str = ""


def reward_pool_key(row: dict[str, Any]) -> str:
    pool_id = row.get("pool_id")
    if pool_id is not None:
        return f"shared:{pool_id}"
    ticker = str(row.get("event_ticker", "")).strip().upper()
    if not ticker:
        raise ValueError("reward row lacks pool and event identity")
    return f"event:{ticker}"


def join_reward_contracts(
    rewards: list[dict[str, Any]],
    events: dict[str, dict[str, Any]],
) -> list[RewardedContract]:
    result: list[RewardedContract] = []
    budgets: dict[str, Decimal] = {}
    symbols: set[str] = set()
    for reward in rewards:
        ticker = str(reward.get("event_ticker", "")).strip().upper()
        event = events.get(ticker)
        if event is None:
            continue
        if isinstance(event.get("event"), dict):
            event = event["event"]
        pool_id = reward_pool_key(reward)
        budget = Decimal(str(reward["daily_pool_usd"]))
        previous = budgets.setdefault(pool_id, budget)
        if previous != budget:
            raise ValueError("shared pool advertises conflicting budgets")
        pool = RewardPool(
            pool_id,
            budget,
            active=event.get("status") == "active",
            qualifying_maker_count=int(reward["qualifying_maker_count"])
            if reward.get("qualifying_maker_count") is not None else None,
        )
        for contract in event.get("contracts", []):
            symbol = canonical_symbol(str(contract["instrumentSymbol"]))
            if symbol in symbols:
                raise ValueError("duplicate contract symbol in reward catalog")
            symbols.add(symbol)
            rules = ContractRules(
                symbol=symbol,
                tick_size=Decimal(str(contract["priceIncrement"])),
                quantity_minimum=Decimal(str(contract["quantityMinimum"])),
                quantity_increment=Decimal(str(contract["quantityIncrement"])),
                active=contract.get("status") == "active" and contract.get("marketState") == "open",
                post_only_mode=bool(contract.get("postOnlyMode", False)),
            )
            result.append(RewardedContract(
                event_ticker=ticker,
                pool=pool,
                rules=rules,
                expiry=str(contract.get("expiryDate", reward.get("ends_at", ""))),
                reward_starts_at=str(reward.get("starts_at", "")),
                reward_ends_at=str(reward.get("ends_at", "")),
            ))
    return result
