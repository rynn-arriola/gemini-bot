"""Paper run of policy v9 on live public Gemini data. No account access, no orders.

Every minute: read the public order books of the v9-discovered reward markets,
apply the real v9 selection (shield, 50% ledger, anchors, caps) to *virtual*
orders, and record what would have happened: eligible markets, virtual fills
and their 5-minute exit P&L, per-event 5 pm-window uptime and a rough reward
estimate. Fill detection is conservative: a virtual bid counts as filled when
the book trades through its price or every contract at or above it is gone.

Usage: python scripts/shadow_run.py --days 7 --directory data/shadow-YYYYMMDD
Stop early by creating a STOP file in the directory.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gemini_bot.account_state import AccountReducer  # noqa: E402
from gemini_bot.competition import distance_weight  # noqa: E402
from gemini_bot.config import load_policy  # noqa: E402
from gemini_bot.discovery import RewardUniverseLoader  # noqa: E402
from gemini_bot.domain import Outcome, UptimeProgress, uptime_progress_key  # noqa: E402
from gemini_bot.entry_quality import evaluate_entry_quality  # noqa: E402
from gemini_bot.market_state import LocalBook  # noqa: E402
from gemini_bot.rank_report import rank_live_universe  # noqa: E402
from gemini_bot.shield import anchor_target, entry_required, outcome_levels, shielded_price  # noqa: E402
from gemini_bot.uptime import eastern_day_window, estimated_reward_window, quote_cutoff  # noqa: E402
from gemini_bot.uptime_ledger import UptimeStatus, admission_allowed, classify  # noqa: E402

API = "https://api.gemini.com"
ET = ZoneInfo("America/New_York")
UPTIME_PROXY = "50% uptime is no longer reachable in the estimated window"
ECONOMICS = "non-positive conservative net-profit estimate"


def get(path: str):
    for attempt in range(3):
        try:
            req = urllib.request.Request(API + path, headers={"User-Agent": "gemini-bot-shadow"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


class PublicRest:
    async def reward_events(self, ceiling=3000):
        rows, offset = [], 0
        while True:
            page = get(f"/v1/prediction-markets/liquidity-rewards/events?limit=100&offset={offset}")
            rows += page["events"]
            offset += 100
            if offset >= page["pagination"]["total"]:
                return rows

    async def event(self, ticker):
        time.sleep(0.1)
        return get("/v1/prediction-markets/events/" + ticker)


@dataclass
class VOrder:
    outcome: Outcome
    price: D
    quantity: D
    ahead: D
    placed_at: datetime
    anchor: bool = False


@dataclass
class Market:
    symbol: str
    event: str
    pool: str
    orders: dict = field(default_factory=dict)
    last_upgrade: datetime | None = None


def taker_fee(q: D, p: D) -> D:
    return (D("0.07") * q * p * (1 - p)).quantize(D("0.01"), rounding=ROUND_CEILING)


def volume_at_or_better(book: LocalBook, outcome: Outcome, price: D) -> D:
    return sum((q for p, q in outcome_levels(book, outcome) if p >= price), D(0))


class Shadow:
    def __init__(self, directory: Path, end: datetime, gross_usd: D | None = None,
                 ignore_admission: bool = False):
        self.dir, self.end = directory, end
        # Behaviour-only mode: skip the 50% LOST/admission gates to observe
        # fills, repositions and anchors; its uptime numbers are not meaningful.
        self.ignore_admission = ignore_admission
        self.strategy, self.risk = load_policy()
        if gross_usd is not None:
            # Paper-only capital override; the live policy is untouched.
            from dataclasses import replace
            self.risk = replace(self.risk, max_gross_reservation_usd=gross_usd)
        self.ledger = self.strategy.uptime_ledger
        self.markets: dict[str, Market] = {}
        self.blocked: dict[str, datetime] = {}
        self.exits: list[dict] = []
        self.uptime: dict[tuple[str, str], dict] = {}
        self.universe = []
        self.universe_at: datetime | None = None
        self.shares: dict[str, tuple[D, D]] = {}
        # Like the bot's sticky owners: a held market survives 10 minutes of
        # continuous trouble before release (LOST/timing release at once).
        self.fail_since: dict[str, datetime] = {}
        self.fill_cooldown: dict[str, datetime] = {}  # event -> until
        self.swept: dict[str, datetime] = {}  # symbol -> last sweep-like depletion
        self.prev_tops: dict[str, dict] = {}
        self.exit_log: list[tuple[datetime, D]] = []
        self.totals = {"fills": 0, "trading_pnl": D(0), "repositions": 0, "anchors_placed": 0,
                       "pairs_placed": 0, "minutes": 0, "errors": 0}
        self.load_state()

    def load_state(self) -> None:
        path = self.dir / "state.json"
        if not path.exists():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.uptime = {tuple(k.split("|", 1)): {"counted": v["counted"], "pool": D(v["pool"]),
                                                "score": float(v.get("score", 0))}
                       for k, v in raw.get("uptime", {}).items()}
        for key in ("fills", "repositions", "anchors_placed", "pairs_placed", "minutes", "errors"):
            self.totals[key] = raw.get("totals", {}).get(key, 0)
        self.totals["trading_pnl"] = D(raw.get("totals", {}).get("trading_pnl", "0"))
        self.fill_cooldown = {k: datetime.fromisoformat(v) for k, v in raw.get("fill_cooldown", {}).items()}
        self.swept = {k: datetime.fromisoformat(v) for k, v in raw.get("swept", {}).items()}
        self.exit_log = [(datetime.fromisoformat(a), D(b)) for a, b in raw.get("exit_log", [])]
        self.blocked = {k: datetime.fromisoformat(v) for k, v in raw.get("blocked", {}).items()}
        for symbol, m in raw.get("markets", {}).items():  # Held markets survive restarts.
            self.markets[symbol] = Market(symbol, m["event"], m["pool"], {
                Outcome(o["outcome"]): VOrder(Outcome(o["outcome"]), D(o["price"]), D(o["quantity"]), D(o["ahead"]),
                                              datetime.fromisoformat(o["placed_at"]), o["anchor"])
                for o in m["orders"]})

    def save_state(self) -> None:
        state = {"uptime": {f"{k[0]}|{k[1]}": {"counted": v["counted"], "pool": str(v["pool"]),
                                               "score": round(v.get("score", 0.0), 3)}
                            for k, v in self.uptime.items()},
                 "totals": {k: str(v) if isinstance(v, D) else v for k, v in self.totals.items()},
                 "fill_cooldown": {k: v.isoformat() for k, v in self.fill_cooldown.items()},
                 "swept": {k: v.isoformat() for k, v in self.swept.items()},
                 "exit_log": [(a.isoformat(), str(b)) for a, b in self.exit_log[-200:]],
                 "blocked": {k: v.isoformat() for k, v in self.blocked.items()},
                 "markets": {s: {"event": m.event, "pool": m.pool, "orders": [
                     {"outcome": o.outcome.value, "price": str(o.price), "quantity": str(o.quantity),
                      "ahead": str(o.ahead), "placed_at": o.placed_at.isoformat(), "anchor": o.anchor}
                     for o in m.orders.values()]} for s, m in self.markets.items()}}
        (self.dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def action(self, now: datetime, symbol: str, kind: str, **fields) -> None:
        self.write("actions.jsonl", {"at": now, "symbol": symbol, "action": kind, **fields})

    def write(self, name: str, row: dict) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    async def refresh_universe(self, now: datetime) -> None:
        loader = RewardUniverseLoader(PublicRest(), None, self.strategy.entry_quality.crypto_entry_before_expiry_seconds)
        loader.excluded_categories = self.strategy.excluded_categories
        loader.per_pool_reads = 60
        loader.prefer_uncontested = True
        loader.day_boundary_hour = self.ledger.scoring_day_boundary_hour
        loader.allow_sports_futures = True
        loader.max_contracts_per_event = 2
        loader.worst_case_denominator = True
        loader.trust_start_time = True
        preferred = {f"{m.pool}\x1e{s}": (m.event, s) for s, m in self.markets.items()}
        held = {s for s in self.markets}
        self.universe = await loader.load(maximum_pools=self.risk.max_tracked_markets, preferred=preferred,
                                          now=now, quantity=self.strategy.quote_quantity, offset_ticks=0)
        self.universe_at = now
        missing = held - {c.rules.symbol for c in self.universe}
        if missing:
            self.write("drops.jsonl", {"at": now, "note": "held markets missing from refreshed universe",
                                       "symbols": sorted(missing)})

    def books(self, now: datetime) -> dict[str, LocalBook]:
        books = {}
        for c in self.universe:
            try:
                raw = get(f"/v1/book/{c.rules.symbol}?limit_bids=0&limit_asks=0")
                b = LocalBook(c.rules.symbol)
                b.apply_snapshot([(D(x["price"]), D(x["amount"])) for x in raw["bids"]],
                                 [(D(x["price"]), D(x["amount"])) for x in raw["asks"]], 1, 1, now)
                books[c.rules.symbol] = b
            except Exception:
                self.totals["errors"] += 1
            time.sleep(0.05)
        return books

    def progress(self, now: datetime) -> dict:
        result = {}
        window_start, _ = eastern_day_window(now, self.ledger.scoring_day_boundary_hour)
        for c in self.universe:
            start, end, _ = estimated_reward_window(c, now, day_boundary_hour=self.ledger.scoring_day_boundary_hour,
                                                    ignore_trade_start=True)
            if end <= start:
                continue
            cut = quote_cutoff(c, end, crypto_entry_before_expiry_seconds=900, allow_sports_futures=True,
                            trust_start_time=True)
            trade = (datetime.fromisoformat(c.trade_starts_at.replace("Z", "+00:00"))
                     if c.trade_starts_at else start)
            total = max(1, int((end - start).total_seconds() // 60))
            elapsed = min(total, max(0, int((now - start).total_seconds() // 60)))
            counted = self.uptime.get((c.event_ticker, window_start.isoformat()), {}).get("counted", 0)
            quotable = max(0, int((min(cut, end) - max(now, start, trade)).total_seconds() // 60))
            result[uptime_progress_key(c.event_ticker, c.pool.pool_id)] = UptimeProgress(
                elapsed, min(counted, elapsed), min(quotable, total - elapsed),
                remaining_venue_snapshots_estimate=total - elapsed)
        return result

    def entry_time_valid(self, contract, now: datetime) -> bool:
        """Same timing gate as the bot: after effectiveDate, before its own cutoff."""
        if contract.trade_starts_at and now < datetime.fromisoformat(contract.trade_starts_at.replace("Z", "+00:00")):
            return False
        _, end, _ = estimated_reward_window(contract, now, day_boundary_hour=self.ledger.scoring_day_boundary_hour,
                                            ignore_trade_start=True)
        return now < quote_cutoff(contract, end, crypto_entry_before_expiry_seconds=900, allow_sports_futures=True,
                            trust_start_time=True)

    def detect_fills(self, now: datetime, books: dict) -> None:
        for symbol, market in list(self.markets.items()):
            book = books.get(symbol)
            if book is None or book.top() is None:
                continue
            filled = None
            for order in market.orders.values():
                levels = outcome_levels(book, order.outcome)
                # Best price someone would sell this outcome at (NO ask = 1 - best YES bid).
                opposite_best = (1 - max(book.bids)) if order.outcome is Outcome.NO else min(book.asks)
                best_same = levels[0][0] if levels else D(0)
                if opposite_best <= order.price:
                    filled = order
                elif best_same < order.price:
                    filled = order
                else:
                    order.ahead = min(order.ahead, volume_at_or_better(book, order.outcome, order.price))
                if filled:
                    break
            if filled:
                self.totals["fills"] += 1
                row = {"at": now, "symbol": symbol, "outcome": filled.outcome.value, "price": filled.price,
                       "quantity": filled.quantity, "anchor": filled.anchor,
                       "minutes_resting": int((now - filled.placed_at).total_seconds() // 60)}
                self.write("fills.jsonl", row)
                self.exits.append({**row, "exit_at": now + timedelta(minutes=5)})
                market.orders.clear()
                self.blocked[symbol] = now + timedelta(minutes=6)
                self.fill_cooldown[market.event] = now + timedelta(hours=24)

    def settle_exits(self, now: datetime, books: dict) -> None:
        remaining = []
        for fill in self.exits:
            book = books.get(fill["symbol"])
            if now < fill["exit_at"] or book is None or book.top() is None:
                remaining.append(fill)
                continue
            q, p = D(fill["quantity"]), D(fill["price"])
            if fill["outcome"] == "YES":
                exit_price = max(book.bids)
                pnl = (exit_price - p) * q - taker_fee(q, exit_price)
            else:
                exit_price = min(book.asks)  # BUY YES closes NO: credit 1 - ask
                pnl = ((1 - exit_price) - p) * q - taker_fee(q, exit_price)
            self.totals["trading_pnl"] += pnl
            self.exit_log.append((now, pnl))
            self.write("exits.jsonl", {"at": now, "symbol": fill["symbol"], "entry": p,
                                       "exit_yes_price": exit_price, "pnl": pnl})
        self.exits = remaining

    def count_uptime(self, now: datetime, books: dict) -> None:
        """Per-event qualifying minutes and a Gemini-style score proxy.

        Score per snapshot = quadratic distance weight x size (max 250) x 1.5
        when both sides qualify (Gemini's published shape; exact curve and
        size aggregation are not published).
        """
        window_start, _ = eastern_day_window(now, self.ledger.scoring_day_boundary_hour)
        seen = set()
        for c in self.universe:
            key = (c.event_ticker, window_start.isoformat())
            entry = self.uptime.setdefault(key, {"counted": 0, "pool": c.pool.daily_budget_usd, "score": 0.0})
            market = self.markets.get(c.rules.symbol)
            book = books.get(c.rules.symbol)
            if market is None or book is None or book.top() is None:
                continue
            mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
            qualifying = [o for o in market.orders.values()
                          if o.quantity >= 10 and 0 <= mids[o.outcome] - o.price <= D(".10")]
            if not qualifying:
                continue
            score = sum(float(distance_weight(mids[o.outcome] - o.price, D(".01")) * min(o.quantity, D(250)))
                        for o in qualifying)
            if {o.outcome for o in qualifying} == {Outcome.YES, Outcome.NO}:
                score *= 1.5
            entry["score"] = entry.get("score", 0.0) + score
            if key not in seen:
                entry["counted"] += 1
                seen.add(key)

    def detect_sweeps(self, now: datetime, books: dict) -> None:
        """Book-based sweep proxy: >=150 contracts gone at/above the old best
        while the best price fell (cannot separate trades from cancels)."""
        for symbol, book in books.items():
            if book.top() is None:
                continue
            previous = self.prev_tops.get(symbol, {})
            current = {}
            for outcome in Outcome:
                levels = outcome_levels(book, outcome)
                if not levels:
                    continue
                best = levels[0][0]
                current[outcome] = (best, volume_at_or_better(book, outcome, best))
                if outcome in previous:
                    old_best, old_volume = previous[outcome]
                    if best < old_best and old_volume - volume_at_or_better(book, outcome, old_best) >= 150:
                        self.swept[symbol] = now
                        self.write("sweeps.jsonl", {"at": now, "symbol": symbol, "outcome": outcome.value,
                                                    "from": old_best, "to": best, "removed": old_volume})
            self.prev_tops[symbol] = current

    def window_loss(self, now: datetime) -> D:
        """Realized virtual loss in the current six-hour ET risk window."""
        local = now.astimezone(ET)
        start = local.replace(hour=(local.hour // 6) * 6, minute=0, second=0, microsecond=0)
        realized = sum((pnl for at, pnl in self.exit_log if at.astimezone(ET) >= start), D(0))
        return max(D(0), -realized)

    def reserved(self) -> D:
        return sum((o.price * o.quantity for m in self.markets.values() for o in m.orders.values()), D(0))

    def manage(self, now: datetime, books: dict) -> dict:
        progress = self.progress(now)
        shield = self.strategy.shield
        requirements = {c.rules.symbol: {o: entry_required(shield) for o in Outcome} for c in self.universe}
        ranked = rank_live_universe(self.universe, books, now=now, quantity=self.strategy.quote_quantity,
                                    offset_ticks=0, uptime=progress, account=AccountReducer(),
                                    shield_requirements=requirements, shield_distance=shield.max_midpoint_distance,
                                    allow_uptime_override=True)
        by_symbol = {c.rules.symbol: c for c in self.universe}
        eligible_pairs, anchors_possible = [], {}
        why: dict[str, str] = {}
        for cand in ranked:
            contract = by_symbol[cand.symbol]
            state = classify(progress.get(uptime_progress_key(contract.event_ticker, contract.pool.pool_id)),
                             self.ledger)
            incumbent = cand.symbol in self.markets
            if state.status is UptimeStatus.LOST and not self.ignore_admission:
                why[cand.symbol] = "lost"
                continue
            if now < self.blocked.get(cand.symbol, now):
                why[cand.symbol] = "fill_block"
                continue
            if not self.entry_time_valid(contract, now):
                why[cand.symbol] = "entry_time"
                continue
            if not incumbent and not self.ignore_admission and not admission_allowed(state, self.ledger):
                why[cand.symbol] = "admission"
                continue
            tolerated = {UPTIME_PROXY} | ({ECONOMICS} if incumbent else set())
            if cand.quote is not None and set(cand.reasons) <= tolerated:
                q = evaluate_entry_quality(books[cand.symbol], cand.quote, self.strategy.entry_quality)
                if q.allowed:
                    eligible_pairs.append(cand)
                    self.shares[cand.symbol] = (cand.estimated_score_share, cand.daily_pool_usd)
                    continue
                why[cand.symbol] = "entry_quality:" + q.reason
            else:
                why[cand.symbol] = "; ".join(r for r in cand.reasons if r != UPTIME_PROXY)[:80] or "no_quote"
            book = books.get(cand.symbol)
            if (incumbent and cand.quote is None and set(cand.reasons) <= tolerated | {"insufficient_shield"}
                    and book is not None and book.top() is not None):
                mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
                target = anchor_target(book, contract.rules.tick_size, mids, self.strategy.anchor)
                if target is not None:
                    anchors_possible[cand.symbol] = target
        keep = {c.symbol for c in eligible_pairs} | set(anchors_possible)
        grace = set()
        for symbol in list(self.markets):
            if symbol in keep:
                self.fail_since.pop(symbol, None)
                continue
            reason = why.get(symbol, "not_in_universe_or_book")
            first = self.fail_since.setdefault(symbol, now)
            immediate = reason in {"lost", "entry_time", "fill_block"}
            if immediate or now - first >= timedelta(minutes=10):
                self.markets.pop(symbol)
                self.fail_since.pop(symbol, None)
                self.write("drops.jsonl", {"at": now, "symbol": symbol, "reason": reason,
                                           "failing_minutes": int((now - first).total_seconds() // 60)})
            else:
                if first == now:
                    self.action(now, symbol, "grace_start", reason=reason)
                grace.add(symbol)  # Existing virtual orders keep resting, as in the bot.
        pairs = {c.symbol: c for c in eligible_pairs}
        # Incumbents first, then new markets by priority, within the caps.
        order = [s for s in self.markets if s not in grace] + [
            c.symbol for c in sorted(eligible_pairs, key=lambda c: -c.priority_score) if c.symbol not in self.markets]
        for symbol in order:
            book = books.get(symbol)
            contract = by_symbol.get(symbol)
            if book is None or contract is None or book.top() is None:
                continue
            market = self.markets.get(symbol)
            if market is None:
                if len(self.markets) >= self.risk.max_two_sided_quotes:
                    continue
                cost = pairs[symbol].quote.total_reservation_proxy_usd
                if (self.reserved() + cost > self.risk.max_gross_reservation_usd
                        or cost > self.risk.max_market_exposure_usd
                        or max(pairs[symbol].quote.yes.price, pairs[symbol].quote.no.price) * self.strategy.quote_quantity
                        > self.risk.max_stressed_exit_loss_usd):
                    continue
                market = self.markets[symbol] = Market(symbol, contract.event_ticker, contract.pool.pool_id)
            self.maintain(market, book, pairs.get(symbol), anchors_possible.get(symbol), now, contract)
        return {"eligible_pairs": len(eligible_pairs), "anchor_capable": len(anchors_possible),
                "in_grace": len(grace)}

    def maintain(self, market, book, pair, anchor, now, contract) -> None:
        mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
        shield = self.strategy.shield
        if pair is not None:
            if any(o.anchor for o in market.orders.values()):
                market.orders.clear()
                self.action(now, market.symbol, "anchor_to_pair")
            targets = {Outcome.YES: pair.quote.yes.price, Outcome.NO: pair.quote.no.price}
            for outcome in Outcome:
                order = market.orders.get(outcome)
                move = False
                if order is not None:
                    protection = order.ahead  # Updated every minute in detect_fills.
                    if protection < shield.entry_contracts * shield.keep_fraction or mids[outcome] - order.price > D(".10"):
                        move = True
                    elif (targets[outcome] > order.price and (market.last_upgrade is None or
                          (now - market.last_upgrade).total_seconds() >= float(shield.upgrade_minimum_seconds))):
                        move, market.last_upgrade = True, now
                    if move:
                        self.totals["repositions"] += 1
                        reason = ("shield_thin" if protection < shield.entry_contracts * shield.keep_fraction
                                  else "outside_distance" if mids[outcome] - order.price > D(".10") else "upgrade")
                        self.action(now, market.symbol, "reposition", outcome=outcome.value, reason=reason,
                                    old_price=order.price, protection=protection)
                if order is None or move:
                    price = shielded_price(outcome_levels(book, outcome), midpoint=mids[outcome],
                                           tick=contract.rules.tick_size, required=shield.entry_contracts,
                                           max_distance=shield.max_midpoint_distance)
                    if price is None:
                        market.orders.pop(outcome, None)
                        continue
                    market.orders[outcome] = VOrder(outcome, price, self.strategy.quote_quantity,
                                                    volume_at_or_better(book, outcome, price), now)
                    self.action(now, market.symbol, "place" if order is None else "replaced", outcome=outcome.value,
                                price=price, quantity=self.strategy.quote_quantity,
                                ahead=market.orders[outcome].ahead)
                    if order is None:
                        self.totals["pairs_placed"] += 1
        elif anchor is not None:
            existing = next(iter(market.orders.values()), None)
            if existing is not None and not existing.anchor:
                return  # A leftover pair leg still counts; leave it.
            if existing is not None:
                if existing.ahead >= self.strategy.anchor.keep_contracts and mids[existing.outcome] - existing.price <= D(".10"):
                    return
                self.totals["repositions"] += 1
            outcome, price = anchor
            self.action(now, market.symbol, "anchor" if existing is None else "anchor_reposition",
                        outcome=outcome.value, price=price, quantity=self.strategy.anchor.quantity,
                        reason="pair_unsafe_reduced_to_anchor")
            market.orders = {outcome: VOrder(outcome, price, self.strategy.anchor.quantity,
                                             volume_at_or_better(book, outcome, price), now, anchor=True)}
            self.totals["anchors_placed"] += 1

    def summary(self, now: datetime, info: dict) -> dict:
        window_start, _ = eastern_day_window(now, self.ledger.scoring_day_boundary_hour)
        windows = {}
        for (event, start), entry in self.uptime.items():
            windows.setdefault(start, {})[event] = entry["counted"]
        current = [v for (e, st), v in self.uptime.items() if st == window_start.isoformat()]
        return {"updated_at": now, "ends_at": self.end, "window_start": window_start,
                "mode": type(self).__name__,
                "score_units_current_window": round(sum(v.get("score", 0.0) for v in current), 1),
                "events_with_coverage_current_window": sum(1 for v in current if v["counted"] > 0),
                "window_realized_loss": self.window_loss(now),
                "virtual_capital_usd": self.risk.max_gross_reservation_usd,
                "behaviour_only_ignores_50_percent_gate": self.ignore_admission,
                "watched": len(self.universe), **info, "held_markets": len(self.markets),
                "anchors_now": sum(1 for m in self.markets.values() if any(o.anchor for o in m.orders.values())),
                "reserved_usd": self.reserved(), **self.totals,
                "current_window_counted_minutes": windows.get(window_start.isoformat(), {})}

    def close_window(self, start_iso: str) -> None:
        """At 5 pm, record which events would have reached 50% and a rough reward."""
        start = datetime.fromisoformat(start_iso)
        rows = []
        for (event, s), entry in self.uptime.items():
            if s != start_iso:
                continue
            total = 1440
            ratio = entry["counted"] / total
            rows.append({"event": event, "counted": entry["counted"], "ratio": round(ratio, 3),
                         "qualified": ratio >= 0.5, "score_units": round(entry.get("score", 0.0), 1)})
        reward = D(0)
        for market in self.markets.values():
            share, pool = self.shares.get(market.symbol, (D(0), D(0)))
            if any(r["event"] == market.event and r["qualified"] for r in rows):
                reward += share * pool
        self.write("windows.jsonl", {"window_start": start, "events": rows,
                                     "qualified_events": sum(r["qualified"] for r in rows),
                                     "qualified_score_units": round(sum(r["score_units"] for r in rows
                                                                        if r["qualified"]), 1),
                                     "rough_reward_estimate_usd": reward.quantize(D("0.01"))})

    async def run(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        last_window = None
        while datetime.now(timezone.utc) < self.end and not (self.dir / "STOP").exists():
            started = time.monotonic()
            now = datetime.now(timezone.utc)
            try:
                window = eastern_day_window(now, self.ledger.scoring_day_boundary_hour)[0].isoformat()
                if last_window is not None and window != last_window:
                    self.close_window(last_window)
                last_window = window
                if self.universe_at is None or now - self.universe_at >= timedelta(minutes=10):
                    await self.refresh_universe(now)
                books = self.books(now)
                self.detect_fills(now, books)
                self.detect_sweeps(now, books)
                self.settle_exits(now, books)
                info = self.manage(now, books)
                self.count_uptime(now, books)
                self.totals["minutes"] += 1
                self.save_state()
                summary = self.summary(now, info)
                (self.dir / "summary.json").write_text(json.dumps(summary, default=str, indent=1), encoding="utf-8")
                self.write("minutes.jsonl", {k: summary[k] for k in (
                    "updated_at", "watched", "eligible_pairs", "anchor_capable", "held_markets",
                    "anchors_now", "reserved_usd", "fills")})
            except Exception as exc:  # Measurement only; keep going.
                self.totals["errors"] += 1
                self.write("errors.jsonl", {"at": now, "error": f"{type(exc).__name__}: {exc}"[:300]})
            await asyncio.sleep(max(1, 60 - (time.monotonic() - started)))


# Game symbols: GEMI-NCAAF-2609270230-AF-NEV-M-AF (M winner, S spread, T total).
GAME_SYMBOL = re.compile(r"^GEMI-(?:NCAAF|NFL)-\d+-[A-Z0-9]+-[A-Z0-9]+-([A-Z])-")


class LayeredShadow(Shadow):
    """Two layers tuned to Gemini's rewards.

    Layer 1 (eligibility): every admissible event keeps a 10-contract anchor
    per side where >= 1,000 external contracts rest ahead within 8 cents, so
    its 50% clock runs at almost no fill risk. Layer 2 (score): 25/leg pairs
    near the midpoint only where safe: game-winner (or non-game) markets, no
    fill in the event for 24 h, no sweep-like depletion in 60 min, both legs
    at most 80 cents. Filters and loss step-downs only move a market between
    layers; they never abandon it, so its 50% count keeps running.
    """

    def score_allowed(self, contract, cand, book, now: datetime) -> tuple[bool, str]:
        match = GAME_SYMBOL.match(contract.rules.symbol)
        if match and match.group(1) != "M":
            return False, "not_winner_market"
        if now < self.fill_cooldown.get(contract.event_ticker, now):
            return False, "fill_cooldown"
        swept = self.swept.get(contract.rules.symbol)
        if swept is not None and now - swept < timedelta(minutes=60):
            return False, "recent_sweep"
        if max(cand.quote.yes.price, cand.quote.no.price) > D("0.80"):
            return False, "price_above_80c"
        quality = evaluate_entry_quality(book, cand.quote, self.strategy.entry_quality)
        if not quality.allowed:
            return False, "entry_quality"
        return True, ""

    def anchor_targets(self, contract, book) -> dict:
        anchor = self.strategy.anchor
        mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
        targets = {}
        for outcome in Outcome:
            price = shielded_price(outcome_levels(book, outcome), midpoint=mids[outcome],
                                   tick=contract.rules.tick_size, required=anchor.entry_contracts,
                                   max_distance=anchor.max_midpoint_distance)
            if price is not None:
                targets[outcome] = price
        return targets

    def manage(self, now: datetime, books: dict) -> dict:
        progress = self.progress(now)
        shield, anchor = self.strategy.shield, self.strategy.anchor
        loss = self.window_loss(now)
        if loss >= self.risk.risk_window_loss_usd:
            for symbol in list(self.markets):  # HARD: like the bot, no orders at all.
                self.markets.pop(symbol)
                self.action(now, symbol, "hard_stop")
            return {"eligible_pairs": 0, "anchor_capable": 0, "pairs_now": 0, "anchor_markets": 0}
        requirements = {c.rules.symbol: {o: entry_required(shield) for o in Outcome} for c in self.universe}
        ranked = rank_live_universe(self.universe, books, now=now, quantity=self.strategy.quote_quantity,
                                    offset_ticks=0, uptime=progress, account=AccountReducer(),
                                    shield_requirements=requirements, shield_distance=shield.max_midpoint_distance,
                                    allow_uptime_override=True)
        cand_by = {c.symbol: c for c in ranked}
        events: dict[str, list] = {}
        for c in self.universe:
            events.setdefault(c.event_ticker, []).append(c)
        held_by_event = {m.event: s for s, m in self.markets.items()}
        plans, why = [], {}
        for event, contracts in events.items():
            state = classify(progress.get(uptime_progress_key(event, contracts[0].pool.pool_id)), self.ledger)
            held = held_by_event.get(event)
            if state.status is UptimeStatus.LOST and not self.ignore_admission:
                if held:
                    why[held] = "lost"
                continue
            if held is None and not self.ignore_admission and not admission_allowed(state, self.ledger):
                continue
            best = None
            for c in contracts:
                symbol = c.rules.symbol
                if held is not None and symbol != held:
                    continue
                book = books.get(symbol)
                if book is None or book.top() is None:
                    continue
                if not self.entry_time_valid(c, now):
                    why[symbol] = "entry_time"
                    continue
                if now < self.blocked.get(symbol, now):
                    why[symbol] = "fill_block"
                    continue
                cand = cand_by.get(symbol)
                pair_ok, reason = False, "no_pair_quote"
                tolerated = {UPTIME_PROXY} | ({ECONOMICS} if held else set())
                if cand is not None and cand.quote is not None and set(cand.reasons) <= tolerated:
                    pair_ok, reason = self.score_allowed(c, cand, book, now)
                targets = self.anchor_targets(c, book)
                if not pair_ok and not targets:
                    why[symbol] = reason + "+no_anchor_depth"
                    continue
                option = (pair_ok, float(cand.priority_score) if cand else 0.0, symbol, cand, targets, c, reason)
                if best is None or option[:2] > best[:2]:
                    best = option
            if best is not None:
                plans.append((held is not None,) + best)
        planned = {p[3] for p in plans}
        grace = set()
        for symbol in list(self.markets):
            if symbol in planned:
                self.fail_since.pop(symbol, None)
                continue
            reason = why.get(symbol, "not_in_universe_or_book")
            first = self.fail_since.setdefault(symbol, now)
            if reason in {"lost", "entry_time", "fill_block"} or now - first >= timedelta(minutes=10):
                self.markets.pop(symbol)
                self.fail_since.pop(symbol, None)
                self.write("drops.jsonl", {"at": now, "symbol": symbol, "reason": reason})
            else:
                if first == now:
                    self.action(now, symbol, "grace_start", reason=reason)
                grace.add(symbol)
        capital = sum((o.price * o.quantity for s in grace for o in self.markets[s].orders.values()), D(0))
        plans.sort(key=lambda p: (not p[0], not p[1], -p[2]))
        selected = []
        for plan in plans:
            if len(selected) + len(grace) >= self.risk.max_two_sided_quotes:
                break
            anchor_cost = sum(price * anchor.quantity for price in plan[5].values())
            if not plan[5] and plan[1]:
                anchor_cost = plan[4].quote.total_reservation_proxy_usd  # pair-only market
            if capital + anchor_cost > self.risk.max_gross_reservation_usd:
                continue
            selected.append((plan, anchor_cost))
            capital += anchor_cost
        steps = [(D(8), D(1)), (D(16), D("0.75")), (D(24), D("0.5")), (D(30), D("0.25"))]
        fraction = next((f for limit, f in steps if loss < limit), D(0))
        pair_budget = int(len(selected) * fraction)
        pairs = anchors_only = 0
        for plan, anchor_cost in selected:
            incumbent, pair_ok, _, symbol, cand, targets, contract, reason = plan
            book = books[symbol]
            market = self.markets.get(symbol) or self.markets.setdefault(
                symbol, Market(symbol, contract.event_ticker, contract.pool.pool_id))
            upgrade_cost = cand.quote.total_reservation_proxy_usd - anchor_cost if pair_ok else D(0)
            if (pair_ok and pairs < pair_budget and capital + upgrade_cost <= self.risk.max_gross_reservation_usd
                    and cand.quote.total_reservation_proxy_usd <= self.risk.max_market_exposure_usd):
                capital += upgrade_cost
                pairs += 1
                self.shares[symbol] = (cand.estimated_score_share, cand.daily_pool_usd)
                if any(o.anchor for o in market.orders.values()):
                    market.orders.clear()
                    self.action(now, symbol, "promote_to_pair")
                self.maintain(market, book, cand, None, now, contract)
            else:
                anchors_only += 1
                if not targets:
                    continue
                if pair_ok:
                    reason = "loss_step_down_or_capital"
                if any(not o.anchor for o in market.orders.values()):
                    market.orders.clear()
                    self.action(now, symbol, "demote_to_anchor", reason=reason)
                self.maintain_anchor(market, book, targets, now)
        return {"eligible_pairs": sum(1 for p in plans if p[1]), "anchor_capable": sum(1 for p in plans if p[5]),
                "pairs_now": pairs, "anchor_markets": anchors_only, "in_grace": len(grace),
                "pair_budget_fraction": fraction}

    def maintain_anchor(self, market, book, targets: dict, now: datetime) -> None:
        anchor = self.strategy.anchor
        mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
        for outcome in Outcome:
            existing = market.orders.get(outcome)
            if (existing is not None and existing.anchor and existing.ahead >= anchor.keep_contracts
                    and 0 <= mids[outcome] - existing.price <= D(".10")):
                continue
            target = targets.get(outcome)
            if target is None:
                if existing is not None:
                    market.orders.pop(outcome)
                    self.action(now, market.symbol, "anchor_side_removed", outcome=outcome.value)
                continue
            market.orders[outcome] = VOrder(outcome, target, anchor.quantity,
                                            volume_at_or_better(book, outcome, target), now, anchor=True)
            if existing is None:
                self.totals["anchors_placed"] += 1
                self.action(now, market.symbol, "anchor", outcome=outcome.value, price=target)
            else:
                self.totals["repositions"] += 1
                self.action(now, market.symbol, "anchor_reposition", outcome=outcome.value,
                            old_price=existing.price, price=target, protection=existing.ahead)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--gross-usd", type=D, default=None, help="virtual capital (paper only)")
    parser.add_argument("--until", type=datetime.fromisoformat, default=None, help="end time (ISO, overrides --days)")
    parser.add_argument("--ignore-admission", action="store_true", help="behaviour-only: skip 50%% gates")
    parser.add_argument("--layered", action="store_true", help="two-layer anchor + score strategy")
    args = parser.parse_args()
    directory = args.directory if args.directory.is_absolute() else ROOT / args.directory
    end = args.until or datetime.now(timezone.utc) + timedelta(days=args.days)
    runner = LayeredShadow if args.layered else Shadow
    asyncio.run(runner(directory, end, args.gross_usd, args.ignore_admission).run())


if __name__ == "__main__":
    main()
