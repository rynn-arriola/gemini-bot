"""Offline/shadow best-bid planner; deliberately NOT an execution authority.

Thresholds must come from reviewed measurements. This pure function owns no
transport; the separately configured pilot applies its advice through real guards.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .queue_shadow import aware, nonnegative
from .capacity import MAX_ACTIVE_MARKETS

D = Decimal


@dataclass(frozen=True)
class QueueCalibration:
    minimum_depth: Decimal
    warning_ahead: Decimal
    minimum_requeue_gain: Decimal
    maximum_sell_flow_60s: Decimal
    refresh_cooldown_seconds: Decimal
    expires_at: datetime
    evidence_reference: str

    def __post_init__(self):
        aware(self.expires_at)
        for v in (self.minimum_depth, self.warning_ahead, self.minimum_requeue_gain,
                  self.maximum_sell_flow_60s, self.refresh_cooldown_seconds):
            nonnegative(v)
            if v <= 0:
                raise ValueError("calibration thresholds must be positive")
        if self.warning_ahead >= self.minimum_depth or not self.evidence_reference.strip():
            raise ValueError("calibration requires evidence and distinct depth thresholds")


@dataclass(frozen=True)
class BidObservation:
    symbol: str
    at: datetime
    external_depth: Decimal
    ahead_scenario: Decimal
    sell_flow_60s: Decimal
    quote_quantity: Decimal = D("15")
    working: bool = False
    best_price_changed: bool = False
    observation_complete: bool = False
    safety_checks_pass: bool = False
    any_fill: bool = False
    inventory_or_exit_pending: bool = False
    cancellation_pending_or_unknown: bool = False
    since_last_refresh_seconds: Decimal = D("0")


@dataclass(frozen=True)
class BidAdvice:
    action: str
    reason: str
    requires_cancel_then_fresh_proof: bool = False
    used_for_trading: bool = False


def recommend_bid(
    observation: BidObservation, *, pilot_symbols: frozenset[str],
    calibration: QueueCalibration | None,
    maximum_symbols: int = 2,
) -> BidAdvice:
    o = observation
    aware(o.at)
    if (maximum_symbols not in (2, MAX_ACTIVE_MARKETS)
            or (maximum_symbols == 2 and len(pilot_symbols) != 2)
            or (maximum_symbols == MAX_ACTIVE_MARKETS and not 1 <= len(pilot_symbols) <= MAX_ACTIVE_MARKETS)
            or any(not isinstance(s, str) or not s or s.strip().upper() != s for s in pilot_symbols)):
        raise ValueError("invalid bounded queue strategy scope")
    if o.symbol not in pilot_symbols:
        return BidAdvice("unchanged_configured_offset_policy", "outside_pilot")
    for value in (o.external_depth, o.ahead_scenario, o.sell_flow_60s, o.since_last_refresh_seconds):
        nonnegative(value)
    # Lifecycle evidence wins over attractive depth, rate cooldown or ranking.
    if o.any_fill or o.inventory_or_exit_pending:
        return BidAdvice("existing_fill_exit_flow", "no_requeue_after_fill")
    if o.cancellation_pending_or_unknown:
        return BidAdvice("wait_for_reconciliation", "never_replace_unknown_cancel")
    if not o.safety_checks_pass or not o.observation_complete or o.quote_quantity != D("15"):
        return BidAdvice("withdraw_if_working_else_wait", "safety_or_evidence_unavailable", o.working)
    if calibration is None or o.at >= calibration.expires_at:
        return BidAdvice("withdraw_if_working_else_wait", "missing_or_expired_calibration", o.working)
    if (o.external_depth < calibration.minimum_depth
            or o.sell_flow_60s > calibration.maximum_sell_flow_60s):
        return BidAdvice("withdraw_if_working_else_wait", "thin_depth_or_fast_selling", o.working)
    cooldown = o.since_last_refresh_seconds < calibration.refresh_cooldown_seconds
    if o.working and o.best_price_changed:
        # Withdraw stale price even during cooldown; cooldown gates re-entry,
        # never safety cancellation. Re-evaluate all prices/risk after proof.
        return BidAdvice("cancel_then_reassess", "best_price_changed", True)
    if not o.working:
        return BidAdvice("wait" if cooldown else "join_best_bid_moc",
                         "refresh_cooldown" if cooldown else "depth_and_flow_scenario_pass")
    if o.ahead_scenario <= calibration.warning_ahead:
        gain = max(D("0"), o.external_depth - o.ahead_scenario)
        if gain >= calibration.minimum_requeue_gain and not cooldown:
            return BidAdvice("cancel_then_reassess", "estimated_queue_refresh_benefit", True)
        return BidAdvice("withdraw_if_working_else_wait", "depleted_without_useful_refresh", True)
    return BidAdvice("keep", "no_material_reason_to_churn")


@dataclass(frozen=True)
class ReplacementProof:
    requested_order_ids: frozenset[str]
    zero_fill_cancelled_order_ids: frozenset[str]
    request_at: datetime
    proven_at: datetime
    expected_public_generation: object
    actual_public_generation: object
    expected_private_generation: int
    actual_private_generation: int
    fresh_classified_flat: bool = False
    no_working_or_unresolved_orders: bool = False
    no_fill_since_request: bool = False


def replacement_proven(proof: ReplacementProof, *, now: datetime, max_age_seconds: Decimal) -> bool:
    """Additional *offline* guard, never a substitute for real account proofs.

No price/size is cached here. Even a True result needs normal fresh admission,
intent journaling, ownership, budget, HARD, MOC, first-leg and final-send guards.
"""
    aware(now)
    aware(proof.request_at)
    aware(proof.proven_at)
    nonnegative(max_age_seconds)
    return bool(
        max_age_seconds > 0
        and 1 <= len(proof.requested_order_ids) <= 2
        and all(proof.requested_order_ids)
        and proof.requested_order_ids == proof.zero_fill_cancelled_order_ids
        and proof.request_at <= proof.proven_at <= now
        and D(str((now - proof.proven_at).total_seconds())) <= max_age_seconds
        and proof.expected_public_generation == proof.actual_public_generation
        and proof.expected_private_generation == proof.actual_private_generation
        and proof.fresh_classified_flat and proof.no_working_or_unresolved_orders
        and proof.no_fill_since_request
    )
