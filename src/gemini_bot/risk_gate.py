from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import RiskConfig
from .domain import QuotePair, canonical_symbol


@dataclass(slots=True)
class ExposureState:
    gross_reservation_usd: Decimal = Decimal("0")
    market_exposure_usd: dict[str, Decimal] = field(default_factory=dict)
    pending_orders: int = 0
    two_sided_quotes: int = 0
    tracked_markets: int = 0


@dataclass(frozen=True, slots=True)
class EntryAdmission:
    allowed: bool
    reasons: tuple[str, ...]
    resulting_gross_usd: Decimal
    resulting_market_usd: Decimal


def admit_quote_pair(
    risk: RiskConfig,
    state: ExposureState,
    quote: QuotePair,
    *,
    stressed_exit_loss_usd: Decimal,
    opens_new_owner: bool,
) -> EntryAdmission:
    symbol = canonical_symbol(quote.yes.symbol)
    reservation = quote.total_reservation_proxy_usd
    resulting_gross = state.gross_reservation_usd + reservation
    resulting_market = state.market_exposure_usd.get(symbol, Decimal("0")) + reservation
    reasons: list[str] = []
    if state.pending_orders + 2 > risk.max_pending_orders:
        reasons.append("pending-order cap")
    if state.two_sided_quotes + 1 > risk.max_two_sided_quotes:
        reasons.append("two-sided quote cap")
    if opens_new_owner and state.tracked_markets + 1 > risk.max_tracked_markets:
        reasons.append("tracked-market cap")
    if resulting_gross > risk.max_gross_reservation_usd:
        reasons.append("gross exposure cap")
    if resulting_market > risk.max_market_exposure_usd:
        reasons.append("market exposure cap")
    if stressed_exit_loss_usd > risk.max_stressed_exit_loss_usd:
        reasons.append("stressed-exit entry gate")
    return EntryAdmission(not reasons, tuple(reasons), resulting_gross, resulting_market)

