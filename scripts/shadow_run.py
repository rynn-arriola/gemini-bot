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
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_CEILING
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gemini_bot.account_state import AccountReducer  # noqa: E402
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
    def __init__(self, directory: Path, end: datetime, gross_usd: D | None = None):
        self.dir, self.end = directory, end
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
        self.totals = {"fills": 0, "trading_pnl": D(0), "repositions": 0, "anchors_placed": 0,
                       "pairs_placed": 0, "minutes": 0, "errors": 0}

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
        preferred = {f"{m.pool}\x1e{s}": (m.event, s) for s, m in self.markets.items()}
        self.universe = await loader.load(maximum_pools=self.risk.max_tracked_markets, preferred=preferred,
                                          now=now, quantity=self.strategy.quote_quantity, offset_ticks=0)
        self.universe_at = now

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
            cut = quote_cutoff(c, end, crypto_entry_before_expiry_seconds=900, allow_sports_futures=True)
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
        return now < quote_cutoff(contract, end, crypto_entry_before_expiry_seconds=900, allow_sports_futures=True)

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
            self.write("exits.jsonl", {"at": now, "symbol": fill["symbol"], "entry": p,
                                       "exit_yes_price": exit_price, "pnl": pnl})
        self.exits = remaining

    def count_uptime(self, now: datetime, books: dict) -> None:
        window_start, _ = eastern_day_window(now, self.ledger.scoring_day_boundary_hour)
        seen = set()
        for c in self.universe:
            key = (c.event_ticker, window_start.isoformat())
            entry = self.uptime.setdefault(key, {"counted": 0, "pool": c.pool.daily_budget_usd})
            if key in seen:
                continue
            market = self.markets.get(c.rules.symbol)
            book = books.get(c.rules.symbol)
            if market is None or book is None or book.top() is None:
                continue
            mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
            if any(o.quantity >= 10 and 0 <= mids[o.outcome] - o.price <= D(".10") for o in market.orders.values()):
                entry["counted"] += 1
                seen.add(key)

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
        for cand in ranked:
            contract = by_symbol[cand.symbol]
            state = classify(progress.get(uptime_progress_key(contract.event_ticker, contract.pool.pool_id)),
                             self.ledger)
            incumbent = cand.symbol in self.markets
            if (state.status is UptimeStatus.LOST or now < self.blocked.get(cand.symbol, now)
                    or not self.entry_time_valid(contract, now)):
                continue
            if not incumbent and not admission_allowed(state, self.ledger):
                continue
            tolerated = {UPTIME_PROXY} | ({ECONOMICS} if incumbent else set())
            if cand.quote is not None and set(cand.reasons) <= tolerated:
                q = evaluate_entry_quality(books[cand.symbol], cand.quote, self.strategy.entry_quality)
                if q.allowed:
                    eligible_pairs.append(cand)
                    self.shares[cand.symbol] = (cand.estimated_score_share, cand.daily_pool_usd)
                    continue
            book = books.get(cand.symbol)
            if (incumbent and cand.quote is None and set(cand.reasons) <= tolerated | {"insufficient_shield"}
                    and book is not None and book.top() is not None):
                mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
                target = anchor_target(book, contract.rules.tick_size, mids, self.strategy.anchor)
                if target is not None:
                    anchors_possible[cand.symbol] = target
        keep = {c.symbol for c in eligible_pairs} | set(anchors_possible)
        for symbol in list(self.markets):
            if symbol not in keep:
                self.markets.pop(symbol)
        pairs = {c.symbol: c for c in eligible_pairs}
        # Incumbents first, then new markets by priority, within the caps.
        order = [s for s in self.markets] + [c.symbol for c in sorted(eligible_pairs, key=lambda c: -c.priority_score)
                                             if c.symbol not in self.markets]
        for symbol in order:
            book = books[symbol]
            contract = by_symbol[symbol]
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
        return {"eligible_pairs": len(eligible_pairs), "anchor_capable": len(anchors_possible)}

    def maintain(self, market, book, pair, anchor, now, contract) -> None:
        mids = {Outcome.YES: book.top().midpoint, Outcome.NO: 1 - book.top().midpoint}
        shield = self.strategy.shield
        if pair is not None:
            if any(o.anchor for o in market.orders.values()):
                market.orders.clear()
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
                if order is None or move:
                    price = shielded_price(outcome_levels(book, outcome), midpoint=mids[outcome],
                                           tick=contract.rules.tick_size, required=shield.entry_contracts,
                                           max_distance=shield.max_midpoint_distance)
                    if price is None:
                        market.orders.pop(outcome, None)
                        continue
                    market.orders[outcome] = VOrder(outcome, price, self.strategy.quote_quantity,
                                                    volume_at_or_better(book, outcome, price), now)
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
            market.orders = {outcome: VOrder(outcome, price, self.strategy.anchor.quantity,
                                             volume_at_or_better(book, outcome, price), now, anchor=True)}
            self.totals["anchors_placed"] += 1

    def summary(self, now: datetime, info: dict) -> dict:
        window_start, _ = eastern_day_window(now, self.ledger.scoring_day_boundary_hour)
        windows = {}
        for (event, start), entry in self.uptime.items():
            windows.setdefault(start, {})[event] = entry["counted"]
        return {"updated_at": now, "ends_at": self.end, "window_start": window_start,
                "virtual_capital_usd": self.risk.max_gross_reservation_usd,
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
                         "qualified": ratio >= 0.5})
        reward = D(0)
        for market in self.markets.values():
            share, pool = self.shares.get(market.symbol, (D(0), D(0)))
            if any(r["event"] == market.event and r["qualified"] for r in rows):
                reward += share * pool
        self.write("windows.jsonl", {"window_start": start, "events": rows,
                                     "qualified_events": sum(r["qualified"] for r in rows),
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
                self.settle_exits(now, books)
                info = self.manage(now, books)
                self.count_uptime(now, books)
                self.totals["minutes"] += 1
                summary = self.summary(now, info)
                (self.dir / "summary.json").write_text(json.dumps(summary, default=str, indent=1), encoding="utf-8")
                self.write("minutes.jsonl", {k: summary[k] for k in (
                    "updated_at", "watched", "eligible_pairs", "anchor_capable", "held_markets",
                    "anchors_now", "reserved_usd", "fills")})
            except Exception as exc:  # Measurement only; keep going.
                self.totals["errors"] += 1
                self.write("errors.jsonl", {"at": now, "error": f"{type(exc).__name__}: {exc}"[:300]})
            await asyncio.sleep(max(1, 60 - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--gross-usd", type=D, default=None, help="virtual capital (paper only)")
    args = parser.parse_args()
    directory = args.directory if args.directory.is_absolute() else ROOT / args.directory
    asyncio.run(Shadow(directory, datetime.now(timezone.utc) + timedelta(days=args.days), args.gross_usd).run())


if __name__ == "__main__":
    main()
