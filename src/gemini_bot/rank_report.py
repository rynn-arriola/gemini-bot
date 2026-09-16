from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from decimal import Decimal

from .catalog import RewardedContract
from .domain import UptimeProgress, uptime_progress_key
from .market_state import LocalBook
from .account_state import AccountReducer
from .domain import Outcome, QuotePair, QuoteLeg, Side
from .profitability import ProfitabilityEvidence, ProfitabilityTracker
from .competition import measure_competition
from .reward_ranking import (
    RANKING_VERSION,
    RankInput,
    RankedCandidate,
    build_two_sided_quote,
    planned_scoring_minutes,
    rank_market_candidates,
    pool_reward_proxy,
    resting_quote_safe,
)
def rank_live_universe(
    contracts: list[RewardedContract],
    books: dict[str, LocalBook],
    *,
    now: datetime,
    quantity: Decimal,
    offset_ticks: int,
    uptime: dict[str, UptimeProgress] | None = None,
    account: AccountReducer | None = None,
    market_data_verified_at: datetime | dict[str, datetime | None] | None = None,
    profitability: ProfitabilityTracker | None = None,
    target_uptime: Decimal = Decimal(".75"),
    maker_rebate_fractions: dict[str, Decimal] | None = None,
    preferred_symbols: dict[str, str] | None = None,
    allow_uptime_override: bool = False,
    resting_quotes: dict[str, QuotePair] | None = None,
    pool_competition_scores: dict[str, Decimal] | None = None,
    evaluate_working_quotes: bool = False,
) -> list[RankedCandidate]:
    inputs: list[RankInput] = []
    progress = uptime or {}
    pool_events: dict[str, set[str]] = {}
    for contract in contracts:
        pool_events.setdefault(contract.pool.pool_id, set()).add(contract.event_ticker)
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
        verified_at = (market_data_verified_at.get(contract.rules.symbol)
                       if isinstance(market_data_verified_at, dict) else market_data_verified_at)
        if isinstance(market_data_verified_at, dict) and verified_at is None:
            continue  # Another shard's traffic cannot freshen this book.
        if verified_at is not None:
            # The book's levels may be unchanged while another message on the
            # same gap-checked WebSocket proves connection continuity.
            top = type(top)(
                top.symbol, top.bid, top.bid_size, top.ask, top.ask_size,
                top.update_id, verified_at,
            )
        resting = (resting_quotes or {}).get(contract.rules.symbol)
        if resting is None and evaluate_working_quotes and account is not None:
            working = [o for o in account.orders.values() if o.symbol == contract.rules.symbol
                       and o.working and o.side is Side.BUY]
            if (len(working) == 2 and {o.outcome for o in working} == {Outcome.YES, Outcome.NO}
                    and all(o.cumulative_filled == 0 and o.remaining_quantity == quantity for o in working)):
                by_outcome = {o.outcome: o for o in working}
                pair = QuotePair(*(QuoteLeg(contract.rules.symbol, outcome, Side.BUY,
                                            by_outcome[outcome].price, quantity)
                                   for outcome in (Outcome.YES, Outcome.NO)))
                try:
                    target = build_two_sided_quote(top, contract.rules, quantity, offset_ticks)
                except ValueError:
                    target = None
                if target is not None and resting_quote_safe(pair, target, contract.rules, quantity):
                    resting = pair
        evidence = ProfitabilityEvidence()
        if profitability is not None:
            try:
                quote = resting or build_two_sided_quote(top, contract.rules, quantity, offset_ticks)
            except ValueError:
                quote = None
            if quote is not None:
                evidence = profitability.estimate(
                    symbol=contract.rules.symbol,
                    quote=quote,
                    book=book,
                    tick_size=contract.rules.tick_size,
                    planned_minutes=planned_scoring_minutes(
                        progress.get(
                            uptime_progress_key(contract.event_ticker, contract.pool.pool_id),
                            progress.get(contract.pool.pool_id, UptimeProgress()),
                        ),
                        target_uptime,
                    ),
                    now=now,
                    maker_rebate_fraction=(maker_rebate_fractions or {}).get(
                        contract.event_category.casefold(),
                        (maker_rebate_fractions or {}).get("*", Decimal("0")),
                    ),
                )
        inputs.append(RankInput(
            pool=contract.pool,
            rules=contract.rules,
            book=top,
            uptime=progress.get(
                uptime_progress_key(contract.event_ticker, contract.pool.pool_id),
                progress.get(contract.pool.pool_id, UptimeProgress()),
            ),
            profitability=evidence,
            event_ticker=contract.event_ticker,
            event_category=contract.event_category,
            pool_source=contract.pool_source,
            pool_event_count=max(contract.pool_event_count or 1, len(pool_events[contract.pool.pool_id])),
            recurring_duration_value=contract.recurring_duration_value,
            recurring_duration_unit=contract.recurring_duration_unit,
            resting_quote=resting,
            competition=(
                measure_competition(book, tick=contract.rules.tick_size)
                if Decimal("0") <= Decimal(str((now - top.observed_at).total_seconds())) <= Decimal("3")
                else None
            ),
            event_contract_count=contract.event_contract_count,
        ))
    candidates = rank_market_candidates(
        inputs,
        quantity=quantity,
        offset_ticks=offset_ticks,
        now=now,
        target_uptime=target_uptime,
    )
    if pool_competition_scores:
        # Retention evaluates one actual resting pair against the same pool
        # denominator as allocation, not an artificially smaller one-symbol pool.
        from .reward_ranking import rank_candidate
        return [rank_candidate(replace(
            item, pool_competition_score=pool_competition_scores.get(item.pool.pool_id),
        ), quantity=quantity, offset_ticks=offset_ticks, now=now, target_uptime=target_uptime)
                for item in inputs]
    if evaluate_working_quotes:
        # Economics reflect the actual resting pair, but any replacement must
        # still be submitted at the approved current target, never an old price.
        by_symbol = {i.rules.symbol: i for i in inputs}
        candidates = [replace(c, quote=build_two_sided_quote(
            by_symbol[c.symbol].book, by_symbol[c.symbol].rules, quantity, offset_ticks,
        )) if by_symbol[c.symbol].resting_quote is not None and c.quote is not None else c
                      for c in candidates]
    return candidates


def portfolio_reward_report(candidates: list[RankedCandidate]) -> dict[str, object]:
    """Report selected owners only; never add standalone shared-pool forecasts."""
    groups: dict[str, list[RankedCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.pool_id, []).append(candidate)
    pools = []
    total, cost = Decimal("0"), Decimal("0")
    for pool_id, members in sorted(groups.items()):
        if len({(c.daily_pool_usd, c.pool_competition_score_proxy) for c in members}) != 1:
            raise ValueError("inconsistent selected pool model")
        score = sum((c.own_daily_score_proxy for c in members), Decimal("0"))
        reward = pool_reward_proxy(members[0], score)
        costs = sum((c.expected_execution_cost_usd for c in members), Decimal("0"))
        sensitivity = [
            members[0].daily_pool_usd * score / (
                members[0].pool_competition_score_proxy * factor + score
            ) if score else Decimal("0")
            for factor in (Decimal("4"), Decimal(".25"))
        ]
        total += reward
        cost += costs
        pools.append({
            "pool_id": pool_id, "market_count": len(members),
            "symbols": [c.symbol for c in members],
            "daily_pool_budget_usd_counted_once": str(members[0].daily_pool_usd),
            "reward_usd_proxy": str(reward), "execution_cost_usd_proxy": str(costs),
            "net_usd_proxy": str(reward - costs),
            "competition_sensitivity_4x_to_quarter_usd": [str(v) for v in sensitivity],
            "sensitivity_is_not_confidence_interval_or_bound": True,
            "combined_quote_reservation_usd": str(sum((c.capital_proxy_usd for c in members), Decimal("0"))),
            "correlated_fills_assumed_possible_no_hedging_credit": True,
        })
    return {
        "scope": "selected_market_owners_not_all_ranked_candidates",
        "pools": pools, "reward_usd_proxy": str(total), "execution_cost_usd_proxy": str(cost),
        "net_usd_proxy": str(total - cost),
        "account_daily_payout_floor_usd": "1.00",
        "projected_above_payout_floor": total >= Decimal("1"),
        "payout_floor_used_for_admission": False,
        "qualified_or_paid_guaranteed": False,
        "forecast_window_is_proxy": True,
    }


def public_rank_rows(candidates: list[RankedCandidate], *, target_uptime: Decimal = Decimal(".75")) -> list[dict[str, object]]:
    return [
        {
            "pool_id": item.pool_id,
            "ranking_version": RANKING_VERSION,
            "daily_pool_usd": str(item.daily_pool_usd),
            "qualifying_maker_count": item.qualifying_maker_count,
            "maker_count_scope": "event_recent_snapshot_window_may_include_us",
            "owner_key": item.owner_key,
            "competition_model": "aggregate_L2_with_fixed_unseen_contract_prior_not_venue_scores",
            "observed_competition_score_proxy": (
                str(item.observed_competition_score_proxy) if item.observed_competition_score_proxy is not None else None
            ),
            "pool_competition_score_proxy": str(item.pool_competition_score_proxy),
            "unobserved_competition_units": item.unobserved_competition_units,
            "own_daily_score_proxy": str(item.own_daily_score_proxy),
            "scoring_yes_price": str(item.scoring_yes_price) if item.scoring_yes_price is not None else None,
            "scoring_no_price": str(item.scoring_no_price) if item.scoring_no_price is not None else None,
            "standalone_reward_not_additive_within_pool": True,
            "marginal_reward_usd_proxy": (
                str(item.marginal_reward_usd_proxy) if item.marginal_reward_usd_proxy is not None else None
            ),
            "payout_floor_scope": "account_daily_total_reporting_only",
            "exact_shared_pool_allocation_known": False,
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
            "worst_midpoint_distance_cents": str(item.worst_midpoint_distance * Decimal("100")),
            "estimated_score_share_proxy": str(item.estimated_score_share),
            "estimated_reward_usd_proxy": str(item.expected_reward_usd),
            "expected_execution_cost_usd_proxy": str(item.expected_execution_cost_usd),
            "expected_net_profit_usd_proxy": str(item.expected_net_profit_usd),
            "profitability_per_capital_hour_proxy": str(item.profitability_per_capital_hour),
            "planned_scoring_minutes": str(item.planned_scoring_minutes),
            "telemetry_minutes": item.telemetry_minutes,
            "telemetry_confidence": str(item.telemetry_confidence),
            "estimated_daily_fills_proxy": str(item.estimated_daily_fills),
            "five_minute_mid_move_proxy": str(item.five_minute_move),
            "fill_hazard_proxy": str(item.fill_hazard),
            "exit_risk_proxy": str(item.exit_risk),
            "exit_depth_ratio": str(item.exit_depth_ratio),
            "reasons": list(item.reasons),
            "local_eastern_day_observed_uptime_proxy": str(item.observed_uptime) if item.observed_uptime is not None else None,
            "local_eastern_day_best_possible_uptime_proxy": str(item.best_possible_uptime) if item.best_possible_uptime is not None else None,
            "observed_start_best_possible_uptime_bound": str(item.observed_start_best_possible_uptime) if item.observed_start_best_possible_uptime is not None else None,
            "uptime_window_start": item.uptime_window_start.isoformat() if item.uptime_window_start else None,
            "uptime_window_end": item.uptime_window_end.isoformat() if item.uptime_window_end else None,
            "uptime_start_source": item.uptime_start_source,
            "local_quote_cutoff": item.quote_cutoff.isoformat() if item.quote_cutoff else None,
            "uptime_window_is_estimate": True,
            "reward_estimate_conditional_on_qualification": True,
            "reward_estimate_counterfactual": not item.uptime_feasible,
            "venue_reward_start_confirmed": item.uptime_start_source == "published_reward_start",
            "exact_venue_snapshot_phase_known": False,
            "pool_source": item.pool_source,
            "pool_event_count": item.pool_event_count,
            "recurring_duration_value": item.recurring_duration_value,
            "recurring_duration_unit": item.recurring_duration_unit,
            "event_category": item.event_category,
            "yes_price": str(item.quote.yes.price) if item.quote else None,
            "no_price": str(item.quote.no.price) if item.quote else None,
            "quantity_each": str(item.quote.yes.quantity) if item.quote else None,
        }
        for item in candidates
    ]
