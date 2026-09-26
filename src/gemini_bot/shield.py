"""Depth-shielded maker placement: rest behind other traders' volume.

A sweep must consume every contract at better prices, plus the queue already
ahead of us at our own price, before it can fill our order. This module picks
the price closest to the midpoint that still has the required external volume
in front of it, and measures how much protection a resting order has left.

Aggregate L2 has no queue identities: ``ahead_at_level`` for a resting order is
the queue-scenario estimate, never a proven position. Shields reduce fill
exposure; they cannot guarantee that an order is never filled.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .domain import ContractRules, Outcome, QuoteLeg, QuotePair, Side
from .market_state import LocalBook

ZERO = Decimal("0")
ONE = Decimal("1")
MAX_REWARD_DISTANCE = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class ShieldPolicy:
    entry_contracts: Decimal
    keep_fraction: Decimal
    flow_multiplier: Decimal
    max_midpoint_distance: Decimal
    upgrade_minimum_seconds: Decimal

    def validate(self) -> None:
        values = (
            self.entry_contracts, self.keep_fraction, self.flow_multiplier,
            self.max_midpoint_distance, self.upgrade_minimum_seconds,
        )
        if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
            raise ValueError("shield policy values must be finite decimals")
        if self.entry_contracts < 10:
            raise ValueError("shield entry depth must be at least 10 contracts")
        if not ZERO < self.keep_fraction < ONE:
            raise ValueError("shield keep fraction must be between 0 and 1")
        if self.flow_multiplier < 0:
            raise ValueError("shield flow multiplier cannot be negative")
        if not ZERO < self.max_midpoint_distance <= MAX_REWARD_DISTANCE:
            raise ValueError("shield distance must be within Gemini's 10-cent reward bound")
        if self.upgrade_minimum_seconds < 0:
            raise ValueError("shield upgrade interval cannot be negative")


@dataclass(frozen=True, slots=True)
class AnchorPolicy:
    """Uptime anchor: one small, deeply shielded order that keeps a held event
    counted toward Gemini's 50% rule while its scoring pair cannot rest safely."""

    quantity: Decimal
    entry_contracts: Decimal
    keep_contracts: Decimal
    max_midpoint_distance: Decimal

    def validate(self) -> None:
        values = (self.quantity, self.entry_contracts, self.keep_contracts, self.max_midpoint_distance)
        if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
            raise ValueError("anchor policy values must be finite decimals")
        if self.quantity < 10:
            raise ValueError("anchor quantity must meet Gemini's 10-contract reward minimum")
        if not ZERO < self.keep_contracts < self.entry_contracts:
            raise ValueError("anchor keep level must be positive and below its entry level")
        if not ZERO < self.max_midpoint_distance <= MAX_REWARD_DISTANCE:
            raise ValueError("anchor distance must be within Gemini's 10-cent reward bound")


@dataclass(frozen=True, slots=True)
class ShieldContext:
    """Fresh own-adjusted book facts for one symbol's resting-order advice."""

    policy: ShieldPolicy
    book: LocalBook
    midpoints: dict[Outcome, Decimal]
    targets: dict[Outcome, Decimal | None]
    uptime_first: bool = False
    anchor_quantity: Decimal | None = None
    anchor_keep: Decimal | None = None


def anchor_target(
    book: LocalBook, tick: Decimal, midpoints: dict[Outcome, Decimal], anchor: AnchorPolicy,
) -> tuple[Outcome, Decimal] | None:
    """One side only: the outcome whose anchor-shielded price sits closest to
    its midpoint (most robust to small moves), cheaper price on a tie."""
    choices = []
    for outcome in (Outcome.YES, Outcome.NO):
        price = shielded_price(outcome_levels(book, outcome), midpoint=midpoints[outcome], tick=tick,
                               required=anchor.entry_contracts, max_distance=anchor.max_midpoint_distance)
        if price is not None:
            choices.append((midpoints[outcome] - price, price, outcome))
    if not choices:
        return None
    _, price, outcome = min(choices, key=lambda c: (c[0], c[1], c[2].value))
    return outcome, price


def entry_required(
    policy: ShieldPolicy, peak_sell_flow_60s: Decimal = ZERO, *, uptime_first: bool = False,
) -> Decimal:
    """External contracts required ahead before a fresh order may rest.

    ``uptime_first`` lowers the fixed floor to the keep level so an event at
    risk of missing Gemini's 50% rule can stay quoted; the flow floor remains.
    """
    base = policy.entry_contracts * (policy.keep_fraction if uptime_first else ONE)
    return max(base, policy.flow_multiplier * max(ZERO, peak_sell_flow_60s))


def keep_required(policy: ShieldPolicy, peak_sell_flow_60s: Decimal = ZERO) -> Decimal:
    """Resting protection below which the order is near a fill and must move."""
    return policy.keep_fraction * max(
        policy.entry_contracts, policy.flow_multiplier * max(ZERO, peak_sell_flow_60s),
    )


def outcome_levels(book: LocalBook, outcome: Outcome) -> list[tuple[Decimal, Decimal]]:
    """External bids for one outcome in its own price space, best first.

    NO bids are published as YES asks at the complementary price.
    """
    if outcome is Outcome.YES:
        return sorted(((p, q) for p, q in book.bids.items() if q > 0), reverse=True)
    return [(ONE - p, q) for p, q in sorted(book.asks.items()) if q > 0]


def shielded_price(
    levels: list[tuple[Decimal, Decimal]],
    *,
    midpoint: Decimal,
    tick: Decimal,
    required: Decimal,
    max_distance: Decimal,
) -> Decimal | None:
    """Highest price at or behind the external best with ``required`` ahead.

    Joining price ``p`` puts every external contract at prices >= ``p`` in
    front of us. The first level where that cumulative volume reaches the
    requirement is the closest safe price; ticks between levels add nothing.
    """
    if tick <= 0 or required < 0:
        raise ValueError("invalid shield inputs")
    ahead = ZERO
    for price, quantity in levels:
        ahead += quantity
        if ahead < required:
            continue
        candidate = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        if candidate <= 0 or midpoint - candidate < 0 or midpoint - candidate > max_distance:
            return None
        return candidate
    return None


def shielded_quote(
    book: LocalBook,
    rules: ContractRules,
    quantity: Decimal,
    required: dict[Outcome, Decimal],
    max_distance: Decimal,
) -> QuotePair:
    """Two BUY legs, each behind its required external volume."""
    top = book.top()
    if top is None:
        raise ValueError("insufficient_shield")
    midpoints = {Outcome.YES: top.midpoint, Outcome.NO: ONE - top.midpoint}
    prices: dict[Outcome, Decimal] = {}
    for outcome in (Outcome.YES, Outcome.NO):
        price = shielded_price(
            outcome_levels(book, outcome), midpoint=midpoints[outcome],
            tick=rules.tick_size, required=required[outcome], max_distance=max_distance,
        )
        if price is None:
            raise ValueError("insufficient_shield")
        prices[outcome] = price
    return QuotePair(
        yes=QuoteLeg(book.symbol, Outcome.YES, Side.BUY, prices[Outcome.YES], quantity),
        no=QuoteLeg(book.symbol, Outcome.NO, Side.BUY, prices[Outcome.NO], quantity),
    )


def resting_shield(
    book: LocalBook, outcome: Outcome, price: Decimal, ahead_at_level: Decimal | None,
) -> Decimal:
    """External volume a sweep must consume before reaching our resting order.

    A missing queue estimate counts as nothing ahead at our own price.
    """
    better = sum((q for p, q in outcome_levels(book, outcome) if p > price), ZERO)
    return better + max(ZERO, ahead_at_level or ZERO)
