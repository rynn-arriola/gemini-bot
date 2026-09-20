"""Entry-only, own-order-adjusted exit-book protection.

This is a hypothetical immediate reduction, not a promise of execution after
the book moves. Missing executable depth has zero value. Actual inventory
reducers never consult this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from .account_state import AccountReducer
from .domain import Outcome, QuotePair, Side
from .market_state import LocalBook
from .profitability import conservative_taker_fee, MAKER_FEE_RATE


@dataclass(frozen=True, slots=True)
class EntryQualityPolicy:
    minimum_exit_depth_fraction: Decimal
    minimum_retained_value_fraction: Decimal
    depth_haircut: Decimal
    crypto_entry_before_expiry_seconds: int

    def validate(self) -> None:
        if not all(
            value.is_finite() and Decimal("0") < value <= Decimal("1")
            for value in (
                self.minimum_exit_depth_fraction,
                self.minimum_retained_value_fraction,
                self.depth_haircut,
            )
        ):
            raise ValueError("entry-quality fractions must be in (0, 1]")
        if not 300 <= self.crypto_entry_before_expiry_seconds <= 3600:
            raise ValueError("crypto entry cutoff must be between 5 and 60 minutes")


@dataclass(frozen=True, slots=True)
class ExitSideQuality:
    covered_quantity: Decimal
    depth_fraction: Decimal
    retained_value_fraction: Decimal


@dataclass(frozen=True, slots=True)
class EntryQuality:
    allowed: bool
    reason: str
    yes: ExitSideQuality
    no: ExitSideQuality


def external_book(book: LocalBook, account: AccountReducer | None) -> LocalBook:
    """Remove our acknowledged working orders from the public L2 copy."""
    adjusted = LocalBook(
        book.symbol, dict(book.bids), dict(book.asks), book.last_update_id,
        book.synchronized, book.observed_at,
    )
    if account is None:
        return adjusted
    for order in account.orders.values():
        if order.symbol != book.symbol or not order.working:
            continue
        is_bid = (order.outcome is Outcome.YES) == (order.side is Side.BUY)
        levels = adjusted.bids if is_bid else adjusted.asks
        price = order.price if order.outcome is Outcome.YES else Decimal("1") - order.price
        if price in levels:
            levels[price] -= order.remaining_quantity
            if levels[price] <= 0:
                levels.pop(price)
    return adjusted


def _side_quality(
    levels: dict[Decimal, Decimal], *, yes: bool, quantity: Decimal,
    entry_price: Decimal, haircut: Decimal,
) -> ExitSideQuality:
    remaining = quantity
    covered = Decimal("0")
    proceeds = Decimal("0")
    for price, displayed in sorted(levels.items(), reverse=yes):
        fillable = min(remaining, displayed * haircut)
        if fillable <= 0:
            continue
        # A NO holding is reduced by BUY YES. Its collateral return is $1
        # minus the YES purchase price; taker fees are charged on that BUY.
        unit_value = price if yes else Decimal("1") - price
        proceeds += fillable * unit_value - conservative_taker_fee(fillable, price)
        covered += fillable
        remaining -= fillable
        if remaining <= 0:
            break
    # Do not anticipate a dated fee promotion or future maker rebate in a
    # safety gate. The published standard maker fee is the conservative cost.
    entry_fee = (MAKER_FEE_RATE * quantity * entry_price * (Decimal("1") - entry_price)).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING,
    )
    cost = quantity * entry_price + entry_fee
    return ExitSideQuality(covered, covered / quantity, max(Decimal("0"), proceeds) / cost)


def evaluate_entry_quality(
    book: LocalBook, quote: QuotePair, policy: EntryQualityPolicy,
    *, account: AccountReducer | None = None,
) -> EntryQuality:
    policy.validate()
    if not book.synchronized or book.top() is None:
        zero = ExitSideQuality(Decimal("0"), Decimal("0"), Decimal("0"))
        return EntryQuality(False, "exit_book_unavailable", zero, zero)
    if quote.yes.quantity <= 0 or quote.no.quantity <= 0:
        raise ValueError("entry quality requires positive quote quantities")
    adjusted = external_book(book, account)
    yes = _side_quality(
        adjusted.bids, yes=True, quantity=quote.yes.quantity,
        entry_price=quote.yes.price, haircut=policy.depth_haircut,
    )
    no = _side_quality(
        adjusted.asks, yes=False, quantity=quote.no.quantity,
        entry_price=quote.no.price, haircut=policy.depth_haircut,
    )
    if min(yes.depth_fraction, no.depth_fraction) < policy.minimum_exit_depth_fraction:
        reason = "exit_depth_below_minimum"
    elif min(yes.retained_value_fraction, no.retained_value_fraction) < policy.minimum_retained_value_fraction:
        reason = "exit_value_below_minimum"
    else:
        reason = "passed"
    return EntryQuality(reason == "passed", reason, yes, no)
