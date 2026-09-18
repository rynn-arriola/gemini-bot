"""Two-market live decision state. No transport, accounting or mutation owner.

Anchors start at the final pre-send check, not at a hypothetical shadow join.
L2 cannot prove queue position. These deliberately pessimistic scenarios never
prove a fill or release a cancellation/reconciliation barrier.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal as D

from .domain import Outcome, Side
from .guarded_bid import BidObservation, QueueCalibration, recommend_bid
from .queue_shadow import QueueScenario, aware


@dataclass(frozen=True)
class PilotPolicy:
    # Immutable exact symbol/outcome calibration, included in the policy hash.
    markets: tuple[tuple[str, QueueCalibration, QueueCalibration], ...]

    def __post_init__(self):
        symbols = [s for s, _, _ in self.markets]
        if (len(symbols) != 2 or len(set(symbols)) != 2
                or any(not s or s != s.strip().upper() for s in symbols)):
            raise ValueError("queue pilot requires exactly two canonical markets")
        if not all(isinstance(c, QueueCalibration) for _, y, n in self.markets for c in (y, n)):
            raise ValueError("queue pilot requires both outcome calibrations")

    @property
    def symbols(self):
        return frozenset(s for s, _, _ in self.markets)

    def calibration(self, symbol, outcome):
        return next(y if outcome is Outcome.YES else n for s, y, n in self.markets if s == symbol)

    def expired(self, now):
        return any(now >= c.expires_at for _, y, n in self.markets for c in (y, n))


def parse_pilot_policy(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"markets"} or not isinstance(raw["markets"], list):
        raise ValueError("invalid queue pilot schema")
    markets = []
    names = {"minimum_depth", "warning_ahead", "minimum_requeue_gain", "maximum_sell_flow_60s",
             "refresh_cooldown_seconds", "expires_at", "evidence_reference"}
    for row in raw["markets"]:
        if not isinstance(row, dict) or set(row) != {"symbol", "YES", "NO"}:
            raise ValueError("invalid queue pilot market")
        values = []
        for outcome in ("YES", "NO"):
            c = row[outcome]
            if not isinstance(c, dict) or set(c) != names:
                raise ValueError("invalid queue calibration schema")
            values.append(QueueCalibration(**{
                k: datetime.fromisoformat(v) if k == "expires_at" else
                v if k == "evidence_reference" else D(v) for k, v in c.items()
            }))
        markets.append((row["symbol"], *values))
    return PilotPolicy(tuple(markets))


@dataclass
class Anchor:
    client_id: str
    scenario: QueueScenario


@dataclass
class PilotMarket:
    generation: object
    started_at: datetime
    observed_at: datetime
    sequence: int = -1
    prices: dict = field(default_factory=dict)
    depths: dict = field(default_factory=dict)
    anchors: dict = field(default_factory=dict)
    trades: deque = field(default_factory=lambda: deque(maxlen=4096))
    last_trade_id: int = -1
    uncertain_until: datetime | None = None


class QueuePilot:
    def __init__(self, policy: PilotPolicy):
        self.policy = policy
        self.markets = {}
        self.cooldown_at = {}
        # Barriers are owned by the orchestrator; observation cannot clear them.
        self.proof_required = set(policy.symbols)
        self.disabled = {}
        self.reasons = {}
        self.cancel_sequence = {}

    def calibration(self, symbol, outcome):
        return self.policy.calibration(symbol, outcome)

    def invalidate(self, symbol):
        self.markets.pop(symbol, None)
        self.proof_required.add(symbol)

    def fail(self, symbol, exc):
        self.invalidate(symbol)
        self.disabled[symbol] = type(exc).__name__

    def observe_book(self, book, *, generation, at, orders=()):
        if book.symbol not in self.policy.symbols or book.symbol in self.disabled:
            return
        aware(at)
        symbol = book.symbol
        if not book.synchronized or book.top() is None:
            self.invalidate(symbol)
            return
        m = self.markets.get(symbol)
        if m is not None and (m.generation != generation or at < m.observed_at):
            self.invalidate(symbol)
            m = None
        if m is None:
            m = PilotMarket(generation, at, at)
            self.markets[symbol] = m
        if book.last_update_id <= m.sequence:
            return
        bids, asks = dict(book.bids), dict(book.asks)
        for order in orders:
            if order.symbol != symbol or not order.working:
                continue
            levels = bids if (order.outcome is Outcome.YES) == (order.side is Side.BUY) else asks
            price = order.price if order.outcome is Outcome.YES else 1 - order.price
            if levels.get(price, D(0)) < order.remaining_quantity:
                self.invalidate(symbol)  # Public/private publication skew.
                return
            levels[price] -= order.remaining_quantity
            if not levels[price]:
                del levels[price]
        if not bids or not asks:
            self.invalidate(symbol)
            return
        m.sequence, m.observed_at = book.last_update_id, at
        m.prices = {Outcome.YES: max(bids), Outcome.NO: 1 - min(asks)}
        m.depths = {Outcome.YES: bids[max(bids)], Outcome.NO: asks[min(asks)]}
        for outcome, anchor in m.anchors.items():
            # Never transplant an actual order's anchor onto a new touch.
            levels = bids if outcome is Outcome.YES else asks
            price = anchor.scenario.price if outcome is Outcome.YES else 1 - anchor.scenario.price
            anchor.scenario.depth(levels.get(price, D(0)), at)

    def observe_trade(self, frame, *, generation, at):
        symbol = str(frame.get("s", "")).upper()
        m = self.markets.get(symbol)
        if m is None or m.generation != generation:
            return
        aware(at)
        from datetime import timedelta
        trade_id = frame["t"]
        price, qty = D(str(frame["p"])), D(str(frame["q"]))
        if (isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id < 0
                or not isinstance(frame["m"], bool) or not price.is_finite() or not 0 < price < 1
                or not qty.is_finite() or qty <= 0):
            raise ValueError("invalid pilot trade")
        if trade_id <= m.last_trade_id:
            return
        m.last_trade_id = trade_id
        stamp = frame.get("E")
        if (not isinstance(stamp, int) or isinstance(stamp, bool)
                or stamp <= 0 or D(stamp) / D(10**9) > D(str(at.timestamp()))
                or at < m.observed_at):
            m.uncertain_until = at + timedelta(seconds=60)
            return
        event_time = D(stamp) / D(10**9)
        if event_time < D(str(m.started_at.timestamp())):
            return
        while m.trades and (at - m.trades[0][0]).total_seconds() > 60:
            m.trades.popleft()
        if len(m.trades) == m.trades.maxlen:
            m.uncertain_until = at + timedelta(seconds=60)
        outcome = Outcome.YES if frame["m"] else Outcome.NO
        m.trades.append((at, outcome, qty))
        a = m.anchors.get(outcome)
        if (a is not None and event_time >= D(str(a.scenario.joined_at.timestamp()))
                and (price if outcome is Outcome.YES else 1 - price) <= a.scenario.price):
            a.scenario.trade(qty)

    def advice(self, symbol, *, now, generation, orders, safe, allow_entry=False):
        if symbol not in self.policy.symbols:
            return "outside_pilot"
        m = self.markets.get(symbol)
        if (symbol in self.disabled or m is None or m.generation != generation or not safe
                or now < m.observed_at or (now - m.started_at).total_seconds() < 60
                or (m.uncertain_until is not None and now < m.uncertain_until)):
            self.reasons[symbol] = "evidence_unavailable"
            return "withdraw"
        working = [o for o in orders if o.symbol == symbol and o.working]
        if (len(working) > 2 or len({o.outcome for o in working}) != len(working)
                or any(o.side is not Side.BUY or o.cumulative_filled or o.remaining_quantity != D(15)
                       or o.original_quantity != D(15) for o in working)):
            self.reasons[symbol] = "fill_or_invalid_pair"
            return "withdraw"
        if not working and symbol in self.proof_required:
            self.reasons[symbol] = "terminal_flat_proof_required"
            return "wait"
        if not working and m.sequence <= self.cancel_sequence.get(symbol, -1):
            self.reasons[symbol] = "post_cancel_depth_required"
            return "wait"
        for outcome in (Outcome.YES, Outcome.NO):
            order = next((o for o in working if o.outcome is outcome), None)
            a = m.anchors.get(outcome)
            if order is not None and (a is None or a.client_id != order.client_order_id):
                self.reasons[symbol] = "actual_order_anchor_missing"
                return "withdraw"
            c = self.calibration(symbol, outcome)
            since = (D(str((now - self.cooldown_at[symbol]).total_seconds()))
                     if symbol in self.cooldown_at else c.refresh_cooldown_seconds)
            observation = BidObservation(
                symbol, now, m.depths[outcome],
                a.scenario.ahead_scenario if order is not None else m.depths[outcome],
                sum((q for at, o, q in m.trades if o is outcome and 0 <= (now - at).total_seconds() <= 60), D(0)),
                working=order is not None, best_price_changed=order is not None and order.price != m.prices[outcome],
                observation_complete=True, safety_checks_pass=safe,
                since_last_refresh_seconds=max(D(0), since),
            )
            advice = recommend_bid(observation, pilot_symbols=self.policy.symbols, calibration=c)
            if advice.action not in {"keep", "join_best_bid_moc"}:
                self.reasons[symbol] = advice.reason
                return "withdraw" if working else "wait"
        self.reasons[symbol] = "depth_flow_and_actual_anchor_pass"
        return "allow" if allow_entry else "keep"

    def anchor_send(self, symbol, outcome, client_id, price, now):
        m = self.markets[symbol]
        if price != m.prices[outcome] or not client_id:
            raise ValueError("pilot final-send target mismatch")
        m.anchors[outcome] = Anchor(client_id, QueueScenario(
            price, now, now, m.depths[outcome], m.depths[outcome],
        ))

    def canceled(self, symbol, at):
        self.proof_required.add(symbol)
        self.cooldown_at[symbol] = at
        if symbol in self.markets:
            self.cancel_sequence[symbol] = self.markets[symbol].sequence
            self.markets[symbol].anchors.clear()

    def report(self):
        return {"configured": True, "symbols": sorted(self.policy.symbols),
                "exact_queue_position_available": False, "disabled": dict(self.disabled),
                "proof_required": sorted(self.proof_required), "reasons": dict(self.reasons)}
