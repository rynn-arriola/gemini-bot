"""Exact prediction-market cash effects at the contract's quote precision."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .domain import Outcome, Side
from .execution import OrderPurpose


def _quote_quantum(precision: int) -> Decimal:
    if not 0 <= precision <= 18:
        raise ValueError("quote asset precision must be between 0 and 18")
    return Decimal(1).scaleb(-precision)


def netted_quantity(
    *, side: Side, outcome: Outcome, quantity: Decimal, prior_signed_position: Decimal,
) -> Decimal:
    """Contracts of a BUY that close opposite-outcome inventory on one symbol.

    Positions are signed per symbol (YES positive, NO negative). Buying the
    opposite outcome nets against held inventory and returns the paired $1
    collateral instead of opening a second position.
    """
    if side is not Side.BUY or prior_signed_position == 0:
        return Decimal("0")
    if outcome is Outcome.YES and prior_signed_position < 0:
        return min(quantity, -prior_signed_position)
    if outcome is Outcome.NO and prior_signed_position > 0:
        return min(quantity, prior_signed_position)
    return Decimal("0")


def fill_cash_delta(
    *,
    side: Side,
    outcome: Outcome,
    purpose: OrderPurpose,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    quote_asset_precision: int,
    prior_signed_position: Decimal | None = None,
) -> Decimal:
    """Return the exact USD ledger change for one authenticated fill.

    Debits round away from zero at Gemini's published quote-asset precision.
    A BUY against owned opposite-outcome inventory immediately returns the
    paired collateral, so the netted contracts earn the complementary price
    rather than an ordinary purchase debit.  Gemini rounds that
    collateral-return credit up at quote precision; this also reverses the
    entry-side debit rounding when the two execution prices are exact
    complements.  Without ``prior_signed_position`` (legacy repairs), only an
    EXIT BUY YES is treated as fully netted.
    """
    if not all(value.is_finite() for value in (quantity, price, fee)):
        raise ValueError("fill economics must be finite")
    if quantity <= 0 or not Decimal("0") < price < Decimal("1") or fee < 0:
        raise ValueError("fill economics are outside prediction-market bounds")
    quantum = _quote_quantum(quote_asset_precision)
    if prior_signed_position is None:
        netted = (
            quantity if purpose is OrderPurpose.EXIT and side is Side.BUY
            and outcome is Outcome.YES else Decimal("0")
        )
    else:
        if not prior_signed_position.is_finite():
            raise ValueError("prior position must be finite")
        netted = netted_quantity(
            side=side, outcome=outcome, quantity=quantity,
            prior_signed_position=prior_signed_position,
        )
    opening = quantity - netted
    cash = Decimal("0")
    if netted:
        cash += (netted * (Decimal("1") - price)).quantize(quantum, rounding=ROUND_CEILING)
    if opening:
        gross = (opening * price).quantize(
            quantum,
            rounding=ROUND_CEILING if side is Side.BUY else ROUND_FLOOR,
        )
        cash += -gross if side is Side.BUY else gross
    return cash - fee
