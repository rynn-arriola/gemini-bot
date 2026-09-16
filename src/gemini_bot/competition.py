"""Relative L2 pressure, not reconstructed Gemini maker/daily scores.

Aggregate depth has no maker identities, individual size caps, two-sided status
or daily eligibility. Never cap an entire price level at one maker's 250 size,
and never sum event maker counts into a supposed pool headcount.
"""
from dataclasses import dataclass
from decimal import Decimal

from .market_state import LocalBook

D = Decimal
MAX_DISTANCE = D(".10")
# An explicit cold/unseen-contract scenario, independent of our chosen size.
# This is neither a measured competitor nor a guaranteed conservative bound.
UNSEEN_CONTRACT_SCORE_PRIOR = D("30")  # 20 contracts * 1.5


def distance_weight(distance: Decimal, tick: Decimal) -> Decimal:
    if distance < 0 or distance > MAX_DISTANCE:
        return D("0")
    return max(D("0"), D("1") - distance / (MAX_DISTANCE + tick)) ** 2


@dataclass(frozen=True, slots=True)
class CompetitionEvidence:
    weighted_depth: Decimal
    bid_depth: Decimal
    ask_depth: Decimal
    levels: int


def measure_competition(book: LocalBook, *, tick: Decimal) -> CompetitionEvidence | None:
    """Input must already have our working quantities subtracted."""
    top = book.top()
    if top is None:
        return None
    weighted = D("0")
    depths = []
    count = 0
    for side in (book.bids, book.asks):
        depth = D("0")
        for price, quantity in side.items():
            if not price.is_finite() or not quantity.is_finite() or quantity < 0:
                raise ValueError("invalid competition depth")
            distance = abs(price - top.midpoint)
            if distance <= MAX_DISTANCE:
                weighted += distance_weight(distance, tick) * quantity
                depth += quantity
                count += 1
        depths.append(depth)
    # Same pair-average scale as our worst-side quality proxy. Assuming the
    # multiplier for external depth deliberately does not infer account pairs.
    return CompetitionEvidence(weighted * D(".75"), depths[0], depths[1], count)
