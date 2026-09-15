from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from .domain import BookTop, ContractRules, Outcome, QuoteLeg, QuotePair, RewardPool, Side, UptimeProgress


MINIMUM_REWARD_SIZE = Decimal("10")
SIZE_CAP = Decimal("250")
MAX_DISTANCE = Decimal("0.10")
TWO_SIDED_MULTIPLIER = Decimal("1.5")
UPTIME_THRESHOLD = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class RankInput:
    pool: RewardPool
    rules: ContractRules
    book: BookTop
    uptime: UptimeProgress = UptimeProgress()
    fill_hazard: Decimal = Decimal("0")
    exit_risk: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    pool_id: str
    symbol: str
    eligible_now: bool
    uptime_feasible: bool
    quote: QuotePair | None
    priority_score: Decimal
    quality_proxy: Decimal
    capital_proxy_usd: Decimal
    reasons: tuple[str, ...]
    observed_uptime: Decimal | None
    best_possible_uptime: Decimal | None


def _floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def build_two_sided_quote(book: BookTop, rules: ContractRules, quantity: Decimal, offset_ticks: int) -> QuotePair:
    offset = rules.tick_size * offset_ticks
    yes_price = _floor_to_tick(book.bid - offset, rules.tick_size)
    no_price = _floor_to_tick((Decimal("1") - book.ask) - offset, rules.tick_size)
    if yes_price <= 0 or no_price <= 0:
        raise ValueError("offset produces an invalid quote price")
    return QuotePair(
        yes=QuoteLeg(book.symbol, Outcome.YES, Side.BUY, yes_price, quantity),
        no=QuoteLeg(book.symbol, Outcome.NO, Side.BUY, no_price, quantity),
    )


def rank_candidate(
    item: RankInput,
    *,
    quantity: Decimal = Decimal("10"),
    offset_ticks: int = 2,
    now: datetime | None = None,
    stale_after_seconds: Decimal = Decimal("3"),
) -> RankedCandidate:
    reasons: list[str] = []
    quote: QuotePair | None = None
    if not item.pool.active or item.pool.daily_budget_usd <= 0:
        reasons.append("inactive reward pool")
    if not item.rules.active:
        reasons.append("inactive contract")
    if item.rules.post_only_mode:
        reasons.append("post-only-mode contracts are excluded from reward scoring")
    if quantity < max(MINIMUM_REWARD_SIZE, item.rules.quantity_minimum):
        reasons.append("quantity below reward or contract minimum")
    if quantity % item.rules.quantity_increment != 0:
        reasons.append("quantity is off increment")
    if now is not None:
        age = Decimal(str((now - item.book.observed_at).total_seconds()))
        if age < 0 or age > stale_after_seconds:
            reasons.append("stale market data")
    try:
        quote = build_two_sided_quote(item.book, item.rules, quantity, offset_ticks)
    except ValueError as exc:
        reasons.append(str(exc))

    quality = Decimal("0")
    capital = Decimal("0")
    if quote is not None:
        midpoint = item.book.midpoint
        yes_distance = midpoint - quote.yes.price
        no_midpoint = Decimal("1") - midpoint
        no_distance = no_midpoint - quote.no.price
        worst_distance = max(yes_distance, no_distance)
        if min(yes_distance, no_distance) < 0:
            reasons.append("quote would cross midpoint")
        if worst_distance > MAX_DISTANCE:
            reasons.append("quote exceeds maximum reward distance")
        # This is an ordering proxy only. Gemini does not publish its exact curve.
        denominator = MAX_DISTANCE + item.rules.tick_size
        spread_proxy = max(Decimal("0"), Decimal("1") - worst_distance / denominator) ** 2
        scored_size = min(quantity, SIZE_CAP)
        quality = spread_proxy * scored_size * TWO_SIDED_MULTIPLIER
        capital = quote.total_reservation_proxy_usd

    best_possible = item.uptime.best_possible_ratio
    uptime_feasible = best_possible is None or best_possible >= UPTIME_THRESHOLD
    if not uptime_feasible:
        reasons.append("50% uptime is no longer reachable in the estimated window")

    eligible = not reasons
    makers = Decimal(max(1, item.pool.qualifying_maker_count or 1))
    hazard_factor = max(Decimal("0"), Decimal("1") - item.fill_hazard)
    exit_factor = max(Decimal("0"), Decimal("1") - item.exit_risk)
    priority = (
        item.pool.daily_budget_usd * quality * hazard_factor * exit_factor / makers
        if eligible
        else Decimal("0")
    )
    return RankedCandidate(
        pool_id=item.pool.pool_id,
        symbol=item.rules.symbol,
        eligible_now=eligible,
        uptime_feasible=uptime_feasible,
        quote=quote,
        priority_score=priority,
        quality_proxy=quality,
        capital_proxy_usd=capital,
        reasons=tuple(reasons),
        observed_uptime=item.uptime.observed_ratio,
        best_possible_uptime=item.uptime.best_possible_ratio,
    )


def rank_unique_pools(items: list[RankInput], **kwargs: object) -> list[RankedCandidate]:
    """Return only the best contract per unique reward budget."""
    best: dict[str, RankedCandidate] = {}
    for item in items:
        ranked = rank_candidate(item, **kwargs)
        current = best.get(ranked.pool_id)
        if current is None or (ranked.priority_score, ranked.quality_proxy, ranked.symbol) > (
            current.priority_score,
            current.quality_proxy,
            current.symbol,
        ):
            best[ranked.pool_id] = ranked
    return sorted(best.values(), key=lambda value: (-value.priority_score, value.pool_id, value.symbol))
