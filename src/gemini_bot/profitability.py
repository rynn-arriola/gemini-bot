from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
from typing import Any

from .domain import QuotePair, canonical_symbol
from .market_state import LocalBook


MAKER_FEE_RATE = Decimal("0.0175")
TAKER_FEE_RATE = Decimal("0.07")
MAKER_REBATE_FRACTION = Decimal("0.30")
REBATE_PRICE_MINIMUM = Decimal("0.20")
REBATE_PRICE_MAXIMUM = Decimal("0.80")
CENT = Decimal("0.01")
FIVE_MINUTE_SCALE = Decimal("2.2360679775")


def maker_fee_rate_at(now: datetime | None) -> Decimal:
    """Dated estimate only; authenticated execution fees remain authoritative.

Use the unambiguous interior of the published Sep 2–Dec 31 promotion. The
boundary days retain standard fees because the page gives no precise timezone.
Recheck the promotion before deployment; Gemini can amend it early.
"""
    if now is None:
        return MAKER_FEE_RATE
    if now.tzinfo is None:
        raise ValueError("fee estimate time must be timezone-aware")
    utc = now.astimezone(timezone.utc)
    if datetime(2026, 9, 3, tzinfo=timezone.utc) <= utc < datetime(2026, 12, 31, tzinfo=timezone.utc):
        return Decimal("0")
    return MAKER_FEE_RATE


def active_maker_rebate_fractions(
    rows: list[dict[str, Any]], *, now: datetime,
) -> dict[str, Decimal]:
    """Resolve the latest active category/default rebate rules from Gemini."""
    current = now.astimezone(timezone.utc) if now.tzinfo is not None else None
    if current is None:
        raise ValueError("rebate evaluation time must be timezone-aware")
    selected: dict[str, tuple[datetime, Decimal]] = {}
    for row in rows:
        try:
            starts = datetime.fromisoformat(str(row["effective_from"]).replace("Z", "+00:00"))
            ends_raw = row.get("effective_to")
            ends = (
                datetime.fromisoformat(str(ends_raw).replace("Z", "+00:00"))
                if ends_raw else None
            )
            bps = Decimal(str(row["rebate_multiplier_bps"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid maker rebate rule") from exc
        if starts.tzinfo is None or (ends is not None and ends.tzinfo is None):
            raise ValueError("maker rebate rule timestamp must be timezone-aware")
        if bps < 0 or bps > Decimal("10000"):
            raise ValueError("maker rebate basis points are invalid")
        if current < starts.astimezone(timezone.utc) or (
            ends is not None and current >= ends.astimezone(timezone.utc)
        ):
            continue
        category = str(row.get("category", "*")).strip().casefold() or "*"
        candidate = (starts.astimezone(timezone.utc), bps / Decimal("10000"))
        if category not in selected or candidate[0] > selected[category][0]:
            selected[category] = candidate
    return {category: value for category, (_, value) in selected.items()}


def _fee(rate: Decimal, quantity: Decimal, price: Decimal) -> Decimal:
    if quantity <= 0 or not Decimal("0") < price < Decimal("1"):
        raise ValueError("fee estimate requires positive quantity and a contract price")
    return (rate * quantity * price * (Decimal("1") - price)).quantize(
        CENT, rounding=ROUND_CEILING,
    )


def conservative_taker_fee(quantity: Decimal, price: Decimal) -> Decimal:
    """Return the published standard taker fee, rounded against the account.

    Gemini's authenticated Predictions order-history response does not expose
    fees.  Manual-close reconciliation therefore uses the standard taker fee
    as a conservative upper bound; a maker execution or promotion can only
    make the actual fee smaller.
    """
    return _fee(TAKER_FEE_RATE, quantity, price)


def _maker_rebate(
    quantity: Decimal, price: Decimal, rebate_fraction: Decimal,
) -> Decimal:
    if not REBATE_PRICE_MINIMUM <= price <= REBATE_PRICE_MAXIMUM:
        return Decimal("0")
    return (
        rebate_fraction
        * TAKER_FEE_RATE
        * quantity
        * price
        * (Decimal("1") - price)
    ).quantize(CENT, rounding=ROUND_FLOOR)


def estimated_round_trip_fees(
    *,
    quantity: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    maker_rebate_fraction: Decimal = MAKER_REBATE_FRACTION,
    now: datetime | None = None,
) -> Decimal:
    """Maker entry fee less rebate plus a conservative taker exit fee."""
    if not Decimal("0") <= maker_rebate_fraction <= Decimal("1"):
        raise ValueError("maker rebate fraction must be between zero and one")
    return (
        _fee(maker_fee_rate_at(now), quantity, entry_price)
        - _maker_rebate(quantity, entry_price, maker_rebate_fraction)
        + _fee(TAKER_FEE_RATE, quantity, exit_price)
    )


@dataclass(slots=True)
class _MinuteBook:
    minute: datetime
    minimum_mid: Decimal
    maximum_mid: Decimal
    last_mid: Decimal
    spread_total: Decimal
    samples: int


@dataclass(frozen=True, slots=True)
class _Trade:
    trade_id: int
    observed_at: datetime
    price: Decimal
    quantity: Decimal
    buyer_is_maker: bool


@dataclass(frozen=True, slots=True)
class ProfitabilityEvidence:
    observed_minutes: int = 0
    data_confidence: Decimal = Decimal("0")
    estimated_daily_fills: Decimal = Decimal("1")
    five_minute_move: Decimal = Decimal("0")
    expected_execution_cost_usd: Decimal = Decimal("0")
    fill_hazard: Decimal = Decimal("0")
    exit_risk: Decimal = Decimal("0")
    exit_depth_ratio: Decimal = Decimal("0")


class ProfitabilityTracker:
    """Bounded, O(1)-per-frame public telemetry for risk-adjusted ranking.

    Trades are possible touches, never claimed fills.  The estimator deliberately
    charges a cold-start fill reserve and uses a lower-confidence cost estimate;
    authenticated fills remain the only realized execution evidence.
    """

    STATE_VERSION = 1

    def __init__(self, *, window_minutes: int = 30, maximum_trades_per_symbol: int = 512) -> None:
        if not 5 <= window_minutes <= 240 or maximum_trades_per_symbol <= 0:
            raise ValueError("invalid profitability telemetry bounds")
        self.window_minutes = window_minutes
        self.maximum_trades_per_symbol = maximum_trades_per_symbol
        self._books: dict[str, deque[_MinuteBook]] = defaultdict(deque)
        self._trades: dict[str, deque[_Trade]] = defaultdict(
            lambda: deque(maxlen=self.maximum_trades_per_symbol)
        )
        self._last_trade_ids: dict[str, int] = {}

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("profitability timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _cutoff(self, now: datetime) -> datetime:
        return self._aware(now) - timedelta(minutes=self.window_minutes)

    def _prune(self, symbol: str, now: datetime) -> None:
        cutoff = self._cutoff(now)
        books = self._books[symbol]
        while books and books[0].minute < cutoff.replace(second=0, microsecond=0):
            books.popleft()
        trades = self._trades[symbol]
        while trades and trades[0].observed_at < cutoff:
            trades.popleft()

    def observe_book(self, book: LocalBook, *, observed_at: datetime) -> None:
        top = book.top()
        if top is None:
            return
        at = self._aware(observed_at)
        symbol = canonical_symbol(book.symbol)
        minute = at.replace(second=0, microsecond=0)
        midpoint = top.midpoint
        spread = top.ask - top.bid
        buckets = self._books[symbol]
        if not buckets or buckets[-1].minute != minute:
            buckets.append(_MinuteBook(minute, midpoint, midpoint, midpoint, spread, 1))
        else:
            bucket = buckets[-1]
            bucket.minimum_mid = min(bucket.minimum_mid, midpoint)
            bucket.maximum_mid = max(bucket.maximum_mid, midpoint)
            bucket.last_mid = midpoint
            bucket.spread_total += spread
            bucket.samples += 1
        self._prune(symbol, at)

    def observe_trade(self, frame: dict[str, Any], *, observed_at: datetime) -> None:
        if not all(key in frame for key in ("s", "t", "p", "q", "m")):
            return
        at = self._aware(observed_at)
        symbol = canonical_symbol(str(frame["s"]))
        raw_trade_id = frame["t"]
        if isinstance(raw_trade_id, bool):
            raise ValueError("invalid public trade telemetry")
        trade_id = int(raw_trade_id)
        price = Decimal(str(frame["p"]))
        quantity = Decimal(str(frame["q"]))
        maker = frame["m"]
        if (
            trade_id < 0 or str(raw_trade_id).strip() != str(trade_id)
            or not price.is_finite() or not quantity.is_finite()
            or not Decimal("0") < price < Decimal("1") or quantity <= 0
            or not isinstance(maker, bool)
        ):
            raise ValueError("invalid public trade telemetry")
        if trade_id <= self._last_trade_ids.get(symbol, -1):
            return
        trades = self._trades[symbol]
        if trades and at < trades[-1].observed_at:
            return
        trades.append(_Trade(trade_id, at, price, quantity, maker))
        self._last_trade_ids[symbol] = trade_id
        self._prune(symbol, at)

    @staticmethod
    def _depth_ratio(book: LocalBook, quantity: Decimal) -> Decimal:
        # Production exits already require a 50% depth haircut.  Ranking uses
        # that same conservative coverage on both possible outcomes.
        yes = sum(book.bids.values(), Decimal("0")) * Decimal("0.5") / quantity
        no = sum(book.asks.values(), Decimal("0")) * Decimal("0.5") / quantity
        return min(Decimal("1"), yes, no)

    def estimate(
        self,
        *,
        symbol: str,
        quote: QuotePair,
        book: LocalBook,
        tick_size: Decimal,
        planned_minutes: Decimal,
        now: datetime,
        maker_rebate_fraction: Decimal = MAKER_REBATE_FRACTION,
    ) -> ProfitabilityEvidence:
        symbol = canonical_symbol(symbol)
        self._prune(symbol, now)
        buckets = tuple(self._books[symbol])
        trades = tuple(self._trades[symbol])
        observed = len(buckets)
        confidence = min(Decimal("1"), Decimal(observed) / Decimal(self.window_minutes))

        moves: list[Decimal] = [row.maximum_mid - row.minimum_mid for row in buckets]
        moves.extend(
            abs(current.last_mid - previous.last_mid)
            for previous, current in zip(buckets, buckets[1:])
        )
        mean_move = (
            sum(moves, Decimal("0")) / Decimal(len(moves)) if moves else Decimal("0")
        )
        five_minute_move = min(Decimal("0.10"), mean_move * FIVE_MINUTE_SCALE)

        yes_touch = Decimal("0")
        no_touch = Decimal("0")
        yes_ask = Decimal("1") - quote.no.price
        for trade in trades:
            if trade.buyer_is_maker and trade.price <= quote.yes.price:
                yes_touch += trade.quantity
            elif not trade.buyer_is_maker and trade.price >= yes_ask:
                no_touch += trade.quantity
        observed_span = Decimal(max(1, observed))
        touch_units = (yes_touch + no_touch) / quote.yes.quantity
        projected_fills = touch_units / observed_span * max(Decimal("0"), planned_minutes)
        # Until a full telemetry window is available, reserve the cost of up to
        # one additional fill instead of treating missing observations as safe.
        projected_fills += Decimal("1") - confidence
        projected_fills = min(Decimal("48"), projected_fills)

        top = book.top()
        if top is None:
            return ProfitabilityEvidence()
        adverse_move = min(
            Decimal("0.10"),
            max(tick_size, five_minute_move, (top.ask - top.bid) / Decimal("2")),
        )

        def loss_for(entry: Decimal) -> Decimal:
            exit_price = max(Decimal("0.01"), entry - adverse_move)
            return quote.yes.quantity * adverse_move + estimated_round_trip_fees(
                quantity=quote.yes.quantity, entry_price=entry, exit_price=exit_price,
                maker_rebate_fraction=maker_rebate_fraction,
                now=now,
            )

        yes_loss = loss_for(quote.yes.price)
        no_loss = loss_for(quote.no.price)
        if yes_touch + no_touch > 0:
            loss_per_fill = (
                yes_loss * yes_touch + no_loss * no_touch
            ) / (yes_touch + no_touch)
        else:
            loss_per_fill = max(yes_loss, no_loss)
        depth_ratio = self._depth_ratio(book, quote.yes.quantity)
        exit_risk = min(
            Decimal("1"),
            max(adverse_move / Decimal("0.10"), Decimal("1") - depth_ratio),
        )
        # Missing haircut-adjusted exit depth is charged as additional loss.
        depth_penalty = (Decimal("1") - depth_ratio) * quote.yes.quantity
        expected_cost = projected_fills * (loss_per_fill + depth_penalty)
        fill_hazard = min(
            Decimal("1"), touch_units / max(Decimal("1"), observed_span / Decimal("5")),
        )
        return ProfitabilityEvidence(
            observed_minutes=observed,
            data_confidence=confidence,
            estimated_daily_fills=projected_fills,
            five_minute_move=five_minute_move,
            expected_execution_cost_usd=expected_cost,
            fill_hazard=fill_hazard,
            exit_risk=exit_risk,
            exit_depth_ratio=depth_ratio,
        )

    def dumps(self, *, now: datetime) -> str:
        for symbol in tuple(set(self._books) | set(self._trades)):
            self._prune(symbol, now)
        payload = {
            "version": self.STATE_VERSION,
            "window_minutes": self.window_minutes,
            "books": {
                symbol: [
                    {
                        "minute": row.minute.isoformat(),
                        "minimum_mid": str(row.minimum_mid),
                        "maximum_mid": str(row.maximum_mid),
                        "last_mid": str(row.last_mid),
                        "spread_total": str(row.spread_total),
                        "samples": row.samples,
                    }
                    for row in rows
                ]
                for symbol, rows in self._books.items() if rows
            },
            "trades": {
                symbol: [
                    {
                        "trade_id": row.trade_id,
                        "observed_at": row.observed_at.isoformat(),
                        "price": str(row.price),
                        "quantity": str(row.quantity),
                        "buyer_is_maker": row.buyer_is_maker,
                    }
                    for row in rows
                ]
                for symbol, rows in self._trades.items() if rows
            },
            "last_trade_ids": self._last_trade_ids,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def loads(self, raw: str, *, now: datetime) -> None:
        try:
            payload = json.loads(raw)
            if payload.get("version") != self.STATE_VERSION:
                return
            books = payload.get("books", {})
            trades = payload.get("trades", {})
            last_trade_ids = payload.get("last_trade_ids", {})
            if (
                not isinstance(books, dict) or not isinstance(trades, dict)
                or not isinstance(last_trade_ids, dict)
            ):
                raise ValueError("invalid profitability state shape")
            for symbol, rows in books.items():
                if not isinstance(rows, list) or len(rows) > self.window_minutes + 1:
                    raise ValueError("invalid profitability book history")
                target = self._books[canonical_symbol(symbol)]
                for row in rows:
                    target.append(_MinuteBook(
                        self._aware(datetime.fromisoformat(str(row["minute"]))),
                        Decimal(str(row["minimum_mid"])),
                        Decimal(str(row["maximum_mid"])),
                        Decimal(str(row["last_mid"])),
                        Decimal(str(row["spread_total"])),
                        int(row["samples"]),
                    ))
            for symbol, rows in trades.items():
                if not isinstance(rows, list) or len(rows) > self.maximum_trades_per_symbol:
                    raise ValueError("invalid profitability trade history")
                target = self._trades[canonical_symbol(symbol)]
                for row in rows:
                    maker = row["buyer_is_maker"]
                    if not isinstance(maker, bool):
                        raise ValueError("invalid profitability trade history")
                    target.append(_Trade(
                        int(row["trade_id"]),
                        self._aware(datetime.fromisoformat(str(row["observed_at"]))),
                        Decimal(str(row["price"])),
                        Decimal(str(row["quantity"])),
                        maker,
                    ))
            for symbol, trade_id in last_trade_ids.items():
                if isinstance(trade_id, bool) or int(trade_id) < 0:
                    raise ValueError("invalid profitability trade cursor")
                self._last_trade_ids[canonical_symbol(symbol)] = int(trade_id)
            if any(
                rows and self._last_trade_ids.get(symbol, -1) < rows[-1].trade_id
                for symbol, rows in self._trades.items()
            ):
                raise ValueError("profitability trade cursor precedes history")
        except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
            self._books.clear()
            self._trades.clear()
            self._last_trade_ids.clear()
            return
        for symbol in tuple(set(self._books) | set(self._trades)):
            self._prune(symbol, now)
