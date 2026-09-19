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
    """Price one exact-position IOC reducer from synchronized depth.

    Public Gemini depth is normalized in YES-price space. Positive inventory
    sells YES into bids. Negative (NO) inventory buys complementary YES from
    asks, the reduction path required for collateral-return accounts. The IOC
    quantity always remains the complete authenticated position, so it cannot
    over-reduce.  When displayed liquidity cannot fill the whole position, a
    terminal partial fill is still useful: normal residual reconciliation can
    prove the smaller position before another bounded round.  No displayed
    executable depth still returns ``None``.  This never rounds or truncates
    the submitted quantity.
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
        outcome = Outcome.YES
        # Buying YES offsets the exact negative/NO signed position.
        levels = sorted(book.asks.items())
        convert_price = lambda yes_price: yes_price

    best_price: Decimal | None = None
    worst_price: Decimal | None = None
    for yes_price, displayed_quantity in levels:
        usable = displayed_quantity * depth_haircut
        if usable <= 0:
            continue
        if best_price is None:
            best_price = convert_price(yes_price)
        accumulated += usable
        worst_price = convert_price(yes_price)
        if accumulated >= quantity:
            break
    # Requiring enough haircut-adjusted depth for the entire position can
    # strand inventory even though an IOC could safely reduce part of it.  The
    # submitted order is still capped at the exact authenticated position;
    # terminal execution evidence, not advertised depth, controls the residual.
    if accumulated <= 0 or worst_price is None or best_price is None:
        return None
    if accumulated < quantity:
        # Do not turn a partial-reduction fallback into permission to sweep an
        # arbitrarily deep thin book.  Cross only the current best executable
        # level; a later residual round must re-read both account and depth.
        worst_price = best_price
    if not Decimal("0.01") <= worst_price <= Decimal("0.99"):
        return None
    side = Side.SELL if signed_position > 0 else Side.BUY
    return QuoteLeg(book.symbol, outcome, side, worst_price, quantity)
