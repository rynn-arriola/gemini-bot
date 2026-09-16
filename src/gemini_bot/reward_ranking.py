from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from .domain import BookTop, ContractRules, Outcome, QuoteLeg, QuotePair, RewardPool, Side, UptimeProgress, market_owner_key
from .profitability import ProfitabilityEvidence
from .competition import CompetitionEvidence, UNSEEN_CONTRACT_SCORE_PRIOR, distance_weight


MINIMUM_REWARD_SIZE = Decimal("10")
SIZE_CAP = Decimal("250")
MAX_DISTANCE = Decimal("0.10")
TWO_SIDED_MULTIPLIER = Decimal("1.5")
UPTIME_THRESHOLD = Decimal("0.50")
DEFAULT_TARGET_UPTIME = Decimal("0.75")
MINIMUM_DAILY_PAYOUT = Decimal("1.00")
RANKING_VERSION = 3


@dataclass(frozen=True, slots=True)
class RankInput:
    pool: RewardPool
    rules: ContractRules
    book: BookTop
    uptime: UptimeProgress = UptimeProgress()
    fill_hazard: Decimal = Decimal("0")
    exit_risk: Decimal = Decimal("0")
    profitability: ProfitabilityEvidence = ProfitabilityEvidence()
    event_ticker: str = ""
    event_category: str = ""
    pool_source: str = "unspecified"
    pool_event_count: int | None = None
    recurring_duration_value: int | None = None
    recurring_duration_unit: str = ""
    resting_quote: QuotePair | None = None
    competition: CompetitionEvidence | None = None
    event_contract_count: int = 1
    pool_competition_score: Decimal | None = None
    unobserved_competition_units: int = 1


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    pool_id: str
    daily_pool_usd: Decimal
    qualifying_maker_count: int | None
    event_ticker: str
    symbol: str
    eligible_now: bool
    uptime_feasible: bool
    quote: QuotePair | None
    priority_score: Decimal
    potential_priority_score: Decimal
    quality_proxy: Decimal
    capital_proxy_usd: Decimal
    worst_midpoint_distance: Decimal
    expected_reward_usd: Decimal
    expected_execution_cost_usd: Decimal
    expected_net_profit_usd: Decimal
    profitability_per_capital_hour: Decimal
    estimated_score_share: Decimal
    planned_scoring_minutes: Decimal
    telemetry_minutes: int
    telemetry_confidence: Decimal
    estimated_daily_fills: Decimal
    five_minute_move: Decimal
    fill_hazard: Decimal
    exit_risk: Decimal
    exit_depth_ratio: Decimal
    reasons: tuple[str, ...]
    observed_uptime: Decimal | None
    best_possible_uptime: Decimal | None
    observed_start_best_possible_uptime: Decimal | None
    uptime_window_start: datetime | None
    uptime_window_end: datetime | None
    uptime_start_source: str
    pool_source: str
    pool_event_count: int | None
    recurring_duration_value: int | None
    recurring_duration_unit: str
    event_category: str
    quote_cutoff: datetime | None = None
    own_daily_score_proxy: Decimal = Decimal("0")
    pool_competition_score_proxy: Decimal = UNSEEN_CONTRACT_SCORE_PRIOR
    observed_competition_score_proxy: Decimal | None = None
    unobserved_competition_units: int = 1
    marginal_reward_usd_proxy: Decimal | None = None
    other_pool_daily_score_proxy: Decimal = Decimal("0")
    scoring_yes_price: Decimal | None = None
    scoring_no_price: Decimal | None = None

    @property
    def owner_key(self) -> str:
        return market_owner_key(self.pool_id, self.symbol)


def planned_scoring_minutes(
    uptime: UptimeProgress, target_uptime: Decimal = DEFAULT_TARGET_UPTIME,
) -> Decimal:
    if not Decimal("0") < target_uptime <= Decimal("1"):
        raise ValueError("target uptime must be between zero and one")
    if uptime.window_start is None or uptime.window_end is None:
        return Decimal("1440") * target_uptime
    seconds = Decimal(str((uptime.window_end - uptime.window_start).total_seconds()))
    return max(Decimal("1"), seconds / Decimal("60") * target_uptime)


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


def resting_quote_safe(quote: QuotePair, target: QuotePair, rules: ContractRules, quantity: Decimal) -> bool:
    """Shared retention boundary: farther is allowed, closer/off-size is not."""
    return all(
        leg.symbol == wanted.symbol and leg.outcome == wanted.outcome and leg.side == wanted.side
        and leg.quantity == quantity and Decimal("0") < leg.price <= wanted.price
        and leg.price % rules.tick_size == 0
        for leg, wanted in ((quote.yes, target.yes), (quote.no, target.no))
    )


def rank_candidate(
    item: RankInput,
    *,
    quantity: Decimal = Decimal("10"),
    offset_ticks: int = 2,
    now: datetime | None = None,
    stale_after_seconds: Decimal = Decimal("3"),
    target_uptime: Decimal = DEFAULT_TARGET_UPTIME,
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
        if item.resting_quote is not None:
            target = quote
            quote = item.resting_quote
            if not resting_quote_safe(quote, target, item.rules, quantity):
                raise ValueError("resting quote does not meet approved size/price safety")
    except ValueError as exc:
        quote = None
        reasons.append(str(exc))

    quality = Decimal("0")
    capital = Decimal("0")
    worst_distance = Decimal("0")
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
        spread_proxy = distance_weight(worst_distance, item.rules.tick_size)
        scored_size = min(quantity, SIZE_CAP)
        quality = spread_proxy * scored_size * TWO_SIDED_MULTIPLIER
        capital = quote.total_reservation_proxy_usd

    best_possible = item.uptime.best_possible_ratio
    uptime_feasible = best_possible is None or best_possible >= UPTIME_THRESHOLD
    if not item.uptime.window_valid:
        reasons.append("invalid estimated reward window")
    if not uptime_feasible:
        reasons.append("50% uptime is no longer reachable in the estimated window")

    planned_minutes = planned_scoring_minutes(item.uptime, target_uptime)
    # Local reachability is reporting-only, not a measured scoring horizon.
    # Competitor quantity must never scale when we change our own size.
    own_daily_score = quality * target_uptime
    if item.pool_source == "category_default" and (item.pool_event_count or 1) > 1:
        # Recurring/shared members do not each own a full daily pool. The local
        # window remains a labeled time-exposure proxy, never a 50% veto.
        own_daily_score *= min(
            Decimal("1"), planned_minutes / (Decimal("1440") * target_uptime),
        )
    competing_daily_score = item.pool_competition_score
    if competing_daily_score is None:
        competing_daily_score = (
            item.competition.weighted_depth if item.competition is not None else Decimal("0")
        ) + UNSEEN_CONTRACT_SCORE_PRIOR
    if not competing_daily_score.is_finite() or competing_daily_score <= 0:
        raise ValueError("competition score must be finite and positive")
    denominator = own_daily_score + competing_daily_score
    estimated_share = Decimal("0") if denominator <= 0 else own_daily_score / denominator
    expected_reward = item.pool.daily_budget_usd * estimated_share
    expected_cost = max(Decimal("0"), item.profitability.expected_execution_cost_usd)
    expected_net = expected_reward - expected_cost
    capital_hours = max(
        Decimal("0.01"), capital * planned_minutes / Decimal("60"),
    )
    potential_profitability = expected_net / capital_hours
    # Gemini applies its payout floor to the account daily total, not each
    # contract. It remains a reporting threshold, never a per-market veto.
    if expected_net <= 0:
        reasons.append("non-positive conservative net-profit estimate")
    eligible = not reasons
    potential_priority = potential_profitability
    priority = potential_profitability if eligible else Decimal("0")
    return RankedCandidate(
        pool_id=item.pool.pool_id,
        daily_pool_usd=item.pool.daily_budget_usd,
        qualifying_maker_count=item.pool.qualifying_maker_count,
        event_ticker=item.event_ticker,
        symbol=item.rules.symbol,
        eligible_now=eligible,
        uptime_feasible=uptime_feasible,
        quote=quote,
        priority_score=priority,
        potential_priority_score=potential_priority,
        quality_proxy=quality,
        capital_proxy_usd=capital,
        worst_midpoint_distance=worst_distance,
        expected_reward_usd=expected_reward,
        expected_execution_cost_usd=expected_cost,
        expected_net_profit_usd=expected_net,
        profitability_per_capital_hour=potential_profitability,
        estimated_score_share=estimated_share,
        planned_scoring_minutes=planned_minutes,
        telemetry_minutes=item.profitability.observed_minutes,
        telemetry_confidence=item.profitability.data_confidence,
        estimated_daily_fills=item.profitability.estimated_daily_fills,
        five_minute_move=item.profitability.five_minute_move,
        fill_hazard=max(item.fill_hazard, item.profitability.fill_hazard),
        exit_risk=max(item.exit_risk, item.profitability.exit_risk),
        exit_depth_ratio=item.profitability.exit_depth_ratio,
        reasons=tuple(reasons),
        observed_uptime=item.uptime.observed_ratio,
        best_possible_uptime=item.uptime.best_possible_ratio,
        observed_start_best_possible_uptime=item.uptime.observed_start_best_possible_ratio,
        uptime_window_start=item.uptime.window_start,
        uptime_window_end=item.uptime.window_end,
        uptime_start_source=item.uptime.start_source,
        pool_source=item.pool_source,
        pool_event_count=item.pool_event_count,
        recurring_duration_value=item.recurring_duration_value,
        recurring_duration_unit=item.recurring_duration_unit,
        event_category=item.event_category,
        quote_cutoff=item.uptime.quote_cutoff,
        own_daily_score_proxy=own_daily_score,
        pool_competition_score_proxy=competing_daily_score,
        observed_competition_score_proxy=(
            item.competition.weighted_depth if item.competition is not None else None
        ),
        unobserved_competition_units=item.unobserved_competition_units,
        scoring_yes_price=quote.yes.price if quote is not None else None,
        scoring_no_price=quote.no.price if quote is not None else None,
    )


def _maker_tiebreak(candidate: RankedCandidate) -> tuple[bool, int]:
    count = candidate.qualifying_maker_count
    return count is None, count if count is not None else 0


def rank_market_candidates(items: list[RankInput], **kwargs: object) -> list[RankedCandidate]:
    """Keep market owners separate and share one competition model per pool.

Unwatched contracts/events receive an explicit fixed prior, not zero competition
or an invented sum of account counts. The estimate is not a payout lower bound.
"""
    groups: dict[str, list[RankInput]] = {}
    symbols: set[str] = set()
    for item in items:
        if item.rules.symbol in symbols:
            raise ValueError("duplicate ranked market identity")
        symbols.add(item.rules.symbol)
        groups.setdefault(item.pool.pool_id, []).append(item)
    ranked = []
    for group in groups.values():
        if len({i.pool.daily_budget_usd for i in group}) != 1:
            raise ValueError("shared pool advertises conflicting budgets")
        events: dict[str, list[RankInput]] = {}
        for item in group:
            events.setdefault(item.event_ticker or item.rules.symbol, []).append(item)
        event_count = max(len(events), max((i.pool_event_count or 1) for i in group))
        unseen = max(0, event_count - len(events))
        for event in events.values():
            unseen += max(0, max(i.event_contract_count for i in event) - len(event))
            unseen += sum(i.competition is None for i in event)
        # One additional uncertainty reserve even when the watched scope is full:
        # aggregate L2 cannot establish historical participation/qualification.
        unseen += 1
        pressure = sum(
            (i.competition.weighted_depth for i in group if i.competition is not None),
            Decimal("0"),
        ) + UNSEEN_CONTRACT_SCORE_PRIOR * unseen
        ranked.extend(rank_candidate(replace(
            i, pool_competition_score=pressure, unobserved_competition_units=unseen,
        ), **kwargs) for i in group)
    return sorted(ranked, key=lambda c: (
        -c.priority_score, -c.potential_priority_score,
        _maker_tiebreak(c), c.pool_id, c.symbol,
    ))


def pool_reward_proxy(candidate: RankedCandidate, own_score: Decimal) -> Decimal:
    return candidate.daily_pool_usd * own_score / (
        candidate.pool_competition_score_proxy + own_score
    )


def marginal_reward_proxy(candidate: RankedCandidate, other_score: Decimal) -> Decimal:
    return (
        pool_reward_proxy(candidate, other_score + candidate.own_daily_score_proxy)
        - pool_reward_proxy(candidate, other_score)
    )


def select_sticky_markets(
    candidates: list[RankedCandidate], *, sticky_pool_ids: set[str],
    locked_pool_ids: set[str], permitted_symbols: set[str], maximum_active_pools: int,
    actionable: Callable[[RankedCandidate], bool], entry_time_valid: Callable[[str], bool],
) -> tuple[list[RankedCandidate], set[str]]:
    """Legacy argument names, but all owner sets contain pool+symbol keys.

Reserve held/recovering owners individually, retain healthy incumbents, then
greedily allocate vacant pair slots by marginal net per reserved capital-hour.
No independence/hedging discount is applied to correlated execution costs.
"""
    if not 1 <= maximum_active_pools <= 15:
        raise ValueError("active market breadth must be between 1 and 15")
    if len(locked_pool_ids) > maximum_active_pools:
        raise ValueError("locked market owners exceed authorized breadth")
    usable = [c for c in candidates if c.symbol in permitted_symbols
              and actionable(c) and entry_time_valid(c.symbol)]
    if len({c.owner_key for c in usable}) != len(usable):
        raise ValueError("duplicate market owner")
    selected = [c for c in usable if c.owner_key in sticky_pool_ids]
    owners = locked_pool_ids | {c.owner_key for c in selected}
    if len(owners) > maximum_active_pools:
        raise ValueError("persisted market owners exceed authorized breadth")

    def scores() -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for c in selected:
            result[c.pool_id] = result.get(c.pool_id, Decimal("0")) + c.own_daily_score_proxy
        return result

    # Remove only failed marginal economics, not a challenger with a better rank.
    while selected:
        totals = scores()
        failing = [c for c in selected if marginal_reward_proxy(
            c, totals[c.pool_id] - c.own_daily_score_proxy,
        ) <= c.expected_execution_cost_usd]
        if not failing:
            break
        worst = min(failing, key=lambda c: (
            marginal_reward_proxy(c, totals[c.pool_id] - c.own_daily_score_proxy)
            - c.expected_execution_cost_usd, c.symbol,
        ))
        selected.remove(worst)
        if worst.owner_key not in locked_pool_ids:
            owners.remove(worst.owner_key)
    remaining = [c for c in usable if c.owner_key not in owners]
    while len(owners) < maximum_active_pools and remaining:
        totals = scores()
        def key(c: RankedCandidate) -> tuple:
            reward = marginal_reward_proxy(c, totals.get(c.pool_id, Decimal("0")))
            hours = max(Decimal(".01"), c.capital_proxy_usd * c.planned_scoring_minutes / 60)
            return (-(reward - c.expected_execution_cost_usd) / hours,
                    _maker_tiebreak(c), c.pool_id, c.symbol)
        chosen = min(remaining, key=key)
        remaining.remove(chosen)
        gain = marginal_reward_proxy(chosen, totals.get(chosen.pool_id, Decimal("0")))
        if gain <= chosen.expected_execution_cost_usd:
            continue
        # Adding a member cannot make an already retained member uneconomic.
        if any(c.pool_id == chosen.pool_id and marginal_reward_proxy(
            c, totals[c.pool_id] - c.own_daily_score_proxy + chosen.own_daily_score_proxy,
        ) <= c.expected_execution_cost_usd for c in selected):
            continue
        selected.append(chosen)
        owners.add(chosen.owner_key)
    totals = scores()
    return [replace(c, other_pool_daily_score_proxy=totals[c.pool_id] - c.own_daily_score_proxy,
                    marginal_reward_usd_proxy=marginal_reward_proxy(
        c, totals[c.pool_id] - c.own_daily_score_proxy,
    )) for c in selected], owners


def rank_unique_pools(
    items: list[RankInput], *, preferred_symbols: dict[str, str] | None = None,
    allow_uptime_override: bool = False, **kwargs: object,
) -> list[RankedCandidate]:
    """Return only the best contract per unique reward budget."""
    best: dict[str, RankedCandidate] = {}
    def pinned(candidate: RankedCandidate) -> bool:
        actionable = candidate.eligible_now or (
            allow_uptime_override and candidate.reasons == (
                "50% uptime is no longer reachable in the estimated window",
            )
        )
        return actionable and (preferred_symbols or {}).get(candidate.pool_id) == candidate.symbol

    for item in items:
        ranked = rank_candidate(item, **kwargs)
        current = best.get(ranked.pool_id)
        candidate_key = (
            pinned(ranked),
            ranked.eligible_now,
            ranked.priority_score,
            ranked.potential_priority_score,
            ranked.expected_net_profit_usd,
            ranked.quality_proxy,
            ranked.symbol,
        )
        current_key = None if current is None else (
            pinned(current),
            current.eligible_now,
            current.priority_score,
            current.potential_priority_score,
            current.expected_net_profit_usd,
            current.quality_proxy,
            current.symbol,
        )
        if current_key is None or candidate_key > current_key:
            best[ranked.pool_id] = ranked
    return sorted(
        best.values(),
        key=lambda value: (
            -value.priority_score, -value.potential_priority_score,
            -value.expected_net_profit_usd, -value.quality_proxy,
            value.pool_id, value.symbol,
        ),
    )


def select_sticky_candidates(
    candidates: list[RankedCandidate],
    *,
    sticky_pool_ids: set[str],
    locked_pool_ids: set[str],
    permitted_symbols: set[str],
    maximum_active_pools: int,
    actionable: Callable[[RankedCandidate], bool],
    entry_time_valid: Callable[[str], bool],
) -> tuple[list[RankedCandidate], set[str]]:
    """Keep healthy incumbents, then fill open pool slots in v2 rank order."""
    if not 1 <= maximum_active_pools <= 15:
        raise ValueError("active pool breadth must be between 1 and 15")
    retained = {
        candidate.pool_id
        for candidate in candidates
        if (
            candidate.pool_id in sticky_pool_ids
            and candidate.symbol in permitted_symbols
            and actionable(candidate)
            and entry_time_valid(candidate.symbol)
        )
    }
    desired: list[RankedCandidate] = []
    desired_pool_ids = set(locked_pool_ids) | retained
    for candidate in candidates:
        if (
            candidate.symbol not in permitted_symbols
            or not actionable(candidate)
            or not entry_time_valid(candidate.symbol)
        ):
            continue
        if (
            candidate.pool_id not in desired_pool_ids
            and len(desired_pool_ids) >= maximum_active_pools
        ):
            continue
        desired.append(candidate)
        desired_pool_ids.add(candidate.pool_id)
    return desired, desired_pool_ids
