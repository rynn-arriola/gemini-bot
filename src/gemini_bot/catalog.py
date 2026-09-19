from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .domain import ContractRules, RewardPool, canonical_symbol, market_owner_key


@dataclass(frozen=True, slots=True)
class RewardedContract:
    event_ticker: str
    pool: RewardPool
    rules: ContractRules
    expiry: str
    reward_starts_at: str = ""
    reward_ends_at: str = ""
    trade_starts_at: str = ""
    pool_source: str = "unspecified"
    pool_event_count: int | None = None
    recurring_duration_value: int | None = None
    recurring_duration_unit: str = ""
    event_starts_at: str = ""
    event_category: str = ""
    event_is_live: bool = False
    event_id: str = ""
    event_contract_count: int = 1

    @property
    def owner_key(self) -> str:
        return market_owner_key(self.pool.pool_id, self.rules.symbol)


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
        if not isinstance(event, dict):
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
        contracts = event.get("contracts")
        if not isinstance(contracts, list):
            continue
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            try:
                raw_symbol = contract["instrumentSymbol"]
                if not isinstance(raw_symbol, str):
                    raise TypeError("contract symbol must be text")
                symbol = canonical_symbol(raw_symbol)
                rules = ContractRules(
                    symbol=symbol,
                    tick_size=Decimal(str(contract["priceIncrement"])),
                    quantity_minimum=Decimal(str(contract["quantityMinimum"])),
                    quantity_increment=Decimal(str(contract["quantityIncrement"])),
                    quote_asset_precision=int(contract["quoteAssetPrecision"]),
                    active=contract.get("status") == "active" and contract.get("marketState") == "open",
                    post_only_mode=bool(contract.get("postOnlyMode", False)),
                )
            except (KeyError, TypeError, ValueError, ArithmeticError):
                # Gemini can briefly return an incomplete contract alongside
                # otherwise usable siblings while event metadata is changing.
                # Treat that contract as unavailable; callers will hold/cancel
                # it if it was already selected, without stopping other markets.
                continue
            if symbol in symbols:
                raise ValueError("duplicate contract symbol in reward catalog")
            symbols.add(symbol)
            result.append(RewardedContract(
                event_ticker=ticker,
                pool=pool,
                rules=rules,
                expiry=str(contract.get("expiryDate") or reward.get("ends_at") or ""),
                reward_starts_at=str(reward.get("starts_at") or ""),
                reward_ends_at=str(reward.get("ends_at") or ""),
                trade_starts_at=str(
                    contract.get("effectiveDate")
                    or event.get("effectiveDate")
                    or ""
                ),
                pool_source=str(reward.get("pool_source", "unspecified")),
                pool_event_count=int(reward["pool_event_count"])
                if reward.get("pool_event_count") is not None else None,
                recurring_duration_value=int(reward["recurring_duration_value"])
                if reward.get("recurring_duration_value") is not None else None,
                recurring_duration_unit=str(reward.get("recurring_duration_unit", "")),
                event_starts_at=str(event.get("startTime") or ""),
                event_category=str(event.get("category", reward.get("category", ""))),
                event_is_live=bool(event.get("isLive", False)),
                event_id=str(event.get("id") or ""),
                event_contract_count=int(event.get("_reward_contract_count", len(event.get("contracts", [])))),
            ))
    return result
