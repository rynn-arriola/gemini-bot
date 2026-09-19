"""Exact prediction-market cash effects at the contract's quote precision."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .domain import Outcome, Side
from .execution import OrderPurpose


def _quote_quantum(precision: int) -> Decimal:
    if not 0 <= precision <= 18:
        raise ValueError("quote asset precision must be between 0 and 18")
    return Decimal(1).scaleb(-precision)


def fill_cash_delta(
    *,
    side: Side,
    outcome: Outcome,
    purpose: OrderPurpose,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    quote_asset_precision: int,
) -> Decimal:
    """Return the exact USD ledger change for one authenticated fill.

    Debits round away from zero at Gemini's published quote-asset precision.
    A BUY YES exit against owned NO inventory immediately returns the paired
    collateral, so its net credit is the complementary price rather than an
    ordinary purchase debit.  Gemini rounds that collateral-return credit up
    at quote precision; this also reverses the entry-side debit rounding when
    the two execution prices are exact complements.
    """
    if not all(value.is_finite() for value in (quantity, price, fee)):
        raise ValueError("fill economics must be finite")
    if quantity <= 0 or not Decimal("0") < price < Decimal("1") or fee < 0:
        raise ValueError("fill economics are outside prediction-market bounds")
    quantum = _quote_quantum(quote_asset_precision)
    if purpose is OrderPurpose.EXIT and side is Side.BUY and outcome is Outcome.YES:
        gross = (quantity * (Decimal("1") - price)).quantize(
            quantum, rounding=ROUND_CEILING,
        )
        return gross - fee
    gross = (quantity * price).quantize(
        quantum,
        rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR,
    )
    return (-gross if side is Side.BUY else gross) - fee
