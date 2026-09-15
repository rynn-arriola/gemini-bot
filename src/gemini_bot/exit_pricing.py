from __future__ import annotations

from decimal import Decimal

from .domain import Outcome, QuoteLeg, Side
from .market_state import LocalBook


def exact_crossing_exit(
    book: LocalBook,
    signed_position: Decimal,
    *,
    depth_haircut: Decimal = Decimal("0.50"),
) -> QuoteLeg | None:
    """Price one exact IOC reducer from synchronized full depth.

    Public Gemini depth is normalized in YES-price space. Positive position is
    YES; negative position is NO. This never rounds or truncates quantity.
    """
    if signed_position == 0:
        return None
    if not Decimal("0") < depth_haircut <= Decimal("1"):
        raise ValueError("depth_haircut must be in (0, 1]")
    if not book.synchronized:
        return None
    quantity = abs(signed_position)
    accumulated = Decimal("0")

    if signed_position > 0:
        outcome = Outcome.YES
        levels = sorted(book.bids.items(), reverse=True)
        convert_price = lambda yes_price: yes_price
    else:
        outcome = Outcome.NO
        # Lowest YES ask is the highest complementary NO bid.
        levels = sorted(book.asks.items())
        convert_price = lambda yes_price: Decimal("1") - yes_price

    worst_price: Decimal | None = None
    for yes_price, displayed_quantity in levels:
        usable = displayed_quantity * depth_haircut
        if usable <= 0:
            continue
        accumulated += usable
        worst_price = convert_price(yes_price)
        if accumulated >= quantity:
            break
    if accumulated < quantity or worst_price is None:
        return None
    if not Decimal("0.01") <= worst_price <= Decimal("0.99"):
        return None
    return QuoteLeg(book.symbol, outcome, Side.SELL, worst_price, quantity)

