"""Bounded, reporting-only queue scenarios. No transport or order authority.

Gemini L2 batches price-level quantities. Hidden cancel/add churn means that
even unchanged depth does NOT establish a positive lower bound ahead of us.
The depletion estimate below is a scenario, not a queue position or fill proof.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .account_state import OwnedOrder
from .domain import Outcome, Side
from .market_state import LocalBook

D = Decimal
ZERO = D("0")


def aware(at: datetime) -> None:
    if at.tzinfo is None:
        raise ValueError("queue timestamps must be timezone-aware")


def nonnegative(value: Decimal) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError("invalid queue quantity")


@dataclass(slots=True)
class QueueScenario:
    price: Decimal  # outcome price, NOT always YES space
    joined_at: datetime
    observed_at: datetime
    external_depth: Decimal
    ahead_scenario: Decimal
    depth_depletion: Decimal = ZERO
    trade_depletion: Decimal = ZERO
    trade_since_depth: Decimal = ZERO
    possible_touch: bool = False

    def depth(self, quantity: Decimal, at: datetime) -> None:
        nonnegative(quantity)
        # Trade-before-depth: avoid debiting that execution twice. A trade that
        # arrives after depth can still over-deplete the scenario; do not call
        # this a precise position or a mathematical bound.
        decrease = max(ZERO, self.external_depth - quantity)
        extra = max(ZERO, decrease - self.trade_since_depth)
        self.depth_depletion += decrease
        self.ahead_scenario = min(quantity, max(ZERO, self.ahead_scenario - extra))
        self.trade_since_depth = ZERO
        self.external_depth, self.observed_at = quantity, at
        self.possible_touch |= self.ahead_scenario == 0

    def trade(self, quantity: Decimal) -> None:
        nonnegative(quantity)
        self.trade_depletion += quantity
        self.trade_since_depth += quantity
        self.ahead_scenario = max(ZERO, self.ahead_scenario - quantity)
        self.possible_touch |= self.ahead_scenario == 0

    def report(self) -> dict[str, object]:
        return {
            "price": str(self.price), "joined_at": self.joined_at.isoformat(),
            "depth_observed_at": self.observed_at.isoformat(),
            "external_depth": str(self.external_depth),
            "ahead_scenario": str(self.ahead_scenario),
            "behind_scenario": str(max(ZERO, self.external_depth - self.ahead_scenario)),
            "guaranteed_quantity_ahead": "0",
            "depth_depletion": str(self.depth_depletion),
            "trade_depletion": str(self.trade_depletion),
            "possible_touch_not_fill": self.possible_touch,
        }


@dataclass(slots=True)
class MarketObservation:
    generation: object
    sequence: int
    observed_at: datetime
    started_at: datetime
    scenarios: dict[Outcome, QueueScenario] = field(default_factory=dict)
    trades: deque = field(default_factory=lambda: deque(maxlen=256))
    last_trade_id: int = -1
    trade_overflow: bool = False
    trade_time_uncertain: bool = False
    resets: int = 0
    working_quotes: list[dict[str, str]] = field(default_factory=list)


class QueueShadow:
    def __init__(self, maximum_markets: int = 32) -> None:
        if not 1 <= maximum_markets <= 32:
            raise ValueError("queue shadow supports at most 32 watched markets")
        self.maximum_markets = maximum_markets
        self.markets: dict[str, MarketObservation] = {}
        self.error: str | None = None
        self.cancel_seconds: deque[Decimal] = deque(maxlen=256)
        self.cancel_failures = 0

    def disable(self, exc: Exception) -> None:
        self.error = type(exc).__name__  # No exception messages or account data.
        self.markets.clear()

    def retain(self, symbols: set[str]) -> None:
        self.markets = {s: m for s, m in self.markets.items() if s in symbols}

    def invalidate(self, symbol: str) -> None:
        self.markets.pop(symbol, None)

    def observe_book(
        self, book: LocalBook, *, generation: object, at: datetime,
        orders: tuple[OwnedOrder, ...] = (),
    ) -> None:
        if self.error:
            return
        aware(at)
        if not book.synchronized or book.top() is None:
            self.invalidate(book.symbol)
            return
        previous = self.markets.get(book.symbol)
        if previous is not None and at < previous.observed_at:
            self.invalidate(book.symbol)
            return
        if previous is not None and generation == previous.generation:
            if book.last_update_id <= previous.sequence:
                return  # Repeated frames cannot create new queue evidence.
        bids, asks = dict(book.bids), dict(book.asks)
        for order in orders:
            if order.symbol != book.symbol or not order.working:
                continue
            side = bids if (order.outcome is Outcome.YES) == (order.side is Side.BUY) else asks
            price = order.price if order.outcome is Outcome.YES else 1 - order.price
            # An absent/too-small own level is public/private publication skew,
            # not permission to count our size as external protection.
            if side.get(price, ZERO) < order.remaining_quantity:
                self.invalidate(book.symbol)
                return
            side[price] -= order.remaining_quantity
            if not side[price]:
                del side[price]
        if not bids or not asks:
            self.invalidate(book.symbol)
            return
        if previous is None or generation != previous.generation:
            if previous is None and len(self.markets) >= self.maximum_markets:
                return
            previous = MarketObservation(generation, book.last_update_id, at, at)
            self.markets[book.symbol] = previous
        previous.sequence, previous.observed_at = book.last_update_id, at
        previous.working_quotes = [
            {"side": o.side.value, "outcome": o.outcome.value,
             "price": str(o.price), "remaining_quantity": str(o.remaining_quantity)}
            for o in orders if o.symbol == book.symbol and o.working
        ]
        for outcome, price, quantity in (
            (Outcome.YES, max(bids), bids[max(bids)]),
            (Outcome.NO, 1 - min(asks), asks[min(asks)]),
        ):
            scenario = previous.scenarios.get(outcome)
            if scenario is None or scenario.price != price:
                previous.resets += 1
                # Virtual new join, not a simulated executed/cancelled order.
                previous.scenarios[outcome] = QueueScenario(price, at, at, quantity, quantity)
            else:
                scenario.depth(quantity, at)

    def observe_trade(self, frame: dict, *, generation: object, at: datetime) -> None:
        if self.error:
            return
        aware(at)
        market = self.markets.get(str(frame.get("s", "")).upper())
        if market is None or market.generation != generation:
            return
        raw_id = frame["t"]
        trade_id = int(raw_id)
        price, quantity = D(str(frame["p"])), D(str(frame["q"]))
        maker = frame["m"]
        if (isinstance(raw_id, bool) or str(trade_id) != str(raw_id) or trade_id < 0
                or not isinstance(maker, bool) or not price.is_finite()
                or not ZERO < price < 1 or not quantity.is_finite() or quantity <= 0):
            raise ValueError("invalid queue trade")
        if trade_id <= market.last_trade_id:
            return
        market.last_trade_id = trade_id
        # Trade E is nanoseconds. Without an event time, do not invent ordering
        # against the virtual join. Record no queue depletion from that frame.
        event_ns = frame.get("E")
        if isinstance(event_ns, bool) or not isinstance(event_ns, int) or event_ns <= 0:
            market.trade_time_uncertain = True
            return
        event_time = D(event_ns) / D(1_000_000_000)
        if event_time > D(str(at.timestamp())):
            market.trade_time_uncertain = True
            return
        outcome = Outcome.YES if maker else Outcome.NO
        outcome_price = price if maker else 1 - price
        scenario = market.scenarios.get(outcome)
        if scenario is None or event_time < D(str(scenario.joined_at.timestamp())):
            return
        if market.trades and at < market.trades[-1][0]:
            return
        while market.trades and (at - market.trades[0][0]).total_seconds() > 60:
            market.trades.popleft()
        if len(market.trades) == market.trades.maxlen:
            market.trade_overflow = True
        market.trades.append((at, outcome, quantity))
        # Executions at or below our BUY price could have reached the virtual
        # order. This is NOT a fill, queue proof, or a profit estimate.
        if outcome_price <= scenario.price:
            scenario.trade(quantity)

    def record_cancel(self, elapsed: float, *, succeeded: bool) -> None:
        value = D(str(elapsed))
        nonnegative(value)
        if succeeded:
            self.cancel_seconds.append(value)
        else:
            self.cancel_failures += 1

    def report(self, *, at: datetime, fresh_symbols: set[str]) -> dict[str, object]:
        aware(at)
        rows = []
        for symbol, market in sorted(self.markets.items()):
            fresh = symbol in fresh_symbols and at >= market.observed_at
            trades = [t for t in market.trades if 0 <= (at - t[0]).total_seconds() <= 60]
            rows.append({
                "symbol": symbol, "availability": "scenario" if fresh else "unavailable",
                "depth_observed_at": market.observed_at.isoformat(),
                "virtual_join_resets": market.resets,
                "observed_working_quotes": market.working_quotes if fresh else [],
                "flow_window_seconds": 60,
                "flow_observation_started_at": market.started_at.isoformat(),
                "full_flow_window_observed": (fresh and (at - market.started_at).total_seconds() >= 60
                                              and not market.trade_overflow and not market.trade_time_uncertain),
                "trade_time_uncertain": market.trade_time_uncertain,
                "flow_history_truncated": market.trade_overflow,
                "yes_sell_quantity_60s": str(sum((t[2] for t in trades if t[1] is Outcome.YES), ZERO)) if fresh else None,
                "no_sell_quantity_60s": str(sum((t[2] for t in trades if t[1] is Outcome.NO), ZERO)) if fresh else None,
                "outcomes": {o.value: s.report() for o, s in market.scenarios.items()} if fresh else {},
            })
        samples = sorted(self.cancel_seconds)
        return {
            "version": 1, "mode": "shadow_only", "used_for_trading": False,
            "exact_queue_position_available": False, "live_pilot_ready": False,
            "reason": "calibration_and_pilot_execution_integration_pending",
            "error_type": self.error, "markets": rows,
            "cancel_measurement": {
                "scope": "local_call_to_terminal_confirmation_including_admission_and_proof_not_exchange_latency",
                "successful_samples": len(samples), "failed_or_interrupted_calls": self.cancel_failures,
                "maximum_seconds": str(samples[-1]) if samples else None,
                "p95_seconds": str(samples[max(0, (95 * len(samples) + 99) // 100 - 1)]) if samples else None,
                "history_scope": "latest_256_successful_calls_this_process",
            },
        }


class ShadowArchive:
    """One bounded write per minute, never per depth tick. Failures stay local."""
    def __init__(self, directory: Path, *, max_bytes: int = 16 * 1024 * 1024, backups: int = 3):
        self.path = directory / "queue-shadow.jsonl"
        if not 1024 <= max_bytes <= 64 * 1024 * 1024 or not 1 <= backups <= 4:
            raise ValueError("invalid bounded shadow archive configuration")
        self.handler = RotatingFileHandler(self.path, maxBytes=max_bytes, backupCount=backups,
                                           encoding="utf-8", delay=True)
        self.last_minute: datetime | None = None
        self.error: str | None = None

    def write(self, *, at: datetime, report: dict, forecasts: list[dict]) -> None:
        minute = at.replace(second=0, microsecond=0)
        if self.error or self.last_minute is not None and minute <= self.last_minute:
            return
        try:
            # Fixed allowlist: no credential, order ID, account balance or raw
            # authenticated payload may enter this archive.
            fields = ("symbol", "pool_id", "event_ticker", "daily_pool_usd",
                      "scoring_yes_price", "scoring_no_price", "quantity_each",
                      "own_daily_score_proxy", "pool_competition_score_proxy",
                      "unobserved_competition_units", "estimated_reward_usd_proxy")
            value = {"observed_at": at.isoformat(), "queue": report,
                     "forecasts": [{k: r.get(k) for k in fields} for r in forecasts[:32]],
                     "historical_reward_window_known": False}
            message = json.dumps(value, separators=(",", ":"), sort_keys=True)
            record = logging.LogRecord("queue_shadow", logging.INFO, "", 0, message, (), None)
            # Avoid logging.Handler's stderr path (which can print the record).
            if self.handler.shouldRollover(record):
                self.handler.doRollover()
            if self.handler.stream is None:
                self.handler.stream = self.handler._open()
            self.handler.stream.write(message + "\n")
            self.handler.flush()
            self.last_minute = minute
        except Exception as exc:
            self.error = type(exc).__name__
            self.handler.close()

    def close(self) -> None:
        self.handler.close()
