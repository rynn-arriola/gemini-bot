"""Public, WebSocket-first paper observations. No credentials or trading routes.

One socket streams best prices/trades/status; its depth method supplies sampled
exit books. REST is limited to catalog, reward configuration and metadata.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict, deque
import contextlib
import ctypes
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.parse
import urllib.error
import urllib.request

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
QTY = D(10)
MAX_MARKETS = 50
REST_SPACING = 1.1
WS_SPACING = 0.55
REQUEST_TIMEOUT = 3.0
CYCLE_BUDGET = 50.0
BUFFER_LIMIT = 10000
SUPERVISOR_BACKOFF = 60


def retry_delay(value, default=60.0):
    """Honor Retry-After seconds, HTTP dates and WS epoch timestamps."""
    try:
        number = float(value)
        if number > 1e12:
            number = number / 1000 - time.time()
        elif number > 1e9:
            number -= time.time()
        return max(default, number) if number == number else default
    except (ValueError, TypeError):
        try:
            return max(default, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, AttributeError, OverflowError):
            return default


class RateCooldown(ConnectionError):
    pass


def diverse_selection(candidates, count):
    """Cover distinct pools first, then add siblings without duplicate symbols."""
    selected, seen = [], set()
    queues = [deque(group) for group in candidates if group]
    while queues and len(selected) < count:
        remaining = []
        for queue in queues:
            while queue and queue[0]["symbol"] in seen:
                queue.popleft()
            if queue and len(selected) < count:
                item = queue.popleft()
                selected.append(item)
                seen.add(item["symbol"])
            if queue:
                remaining.append(queue)
        queues = remaining
    return selected


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def utc(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def pool_key(row):
    if row.get("pool_source") == "event_override":
        return "event:" + row["event_ticker"]
    if row.get("pool_source") == "category_default" and type(row.get("pool_id")) is int:
        return "shared:" + str(row["pool_id"])
    raise ValueError("unknown_pool_identity")


def group_pools(rows):
    groups = {}
    for row in rows:
        key = pool_key(row)
        amount = D(row["daily_pool_usd"])
        if not amount.is_finite() or amount < 0:
            raise ValueError("invalid_pool_budget")
        if key in groups and D(groups[key][0]["daily_pool_usd"]) != amount:
            raise ValueError("conflicting_pool_budget")
        groups.setdefault(key, []).append(row)
    return groups


def levels(raw, reverse):
    parsed = []
    for item in raw:
        p, q = map(D, item)
        if not p.is_finite() or not q.is_finite() or not D(0) < p < D(1) or q < 0:
            raise ValueError("invalid_book_level")
        if q:
            parsed.append((p, q))
    if len({p for p, _ in parsed}) != len(parsed):
        raise ValueError("duplicate_book_level")
    return sorted(parsed, reverse=reverse)


def liquidation_cash(book, outcome):
    remaining, cash = QTY, D(0)
    for price, quantity in book:
        take = min(remaining, quantity)
        cash += take * (price if outcome == "yes" else 1 - price)
        remaining -= take
        if remaining == 0:
            return cash
    return None  # Partial exit depth must not appear to be a complete exit.


def quote_models(bids, asks, tick, max_distance):
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return []
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    if tick <= 0 or bid % tick or ask % tick:
        raise ValueError("off_tick_book")
    models = []
    for offset in (0, 1, 2):
        buy_yes, sell_yes = bid - offset * tick, ask + offset * tick
        if not 0 < buy_yes < sell_yes < 1:
            continue
        yes_distance, no_distance = mid - buy_yes, sell_yes - mid
        # Exclude the outer boundary: the exact quadratic curve is unpublished,
        # and a quote at the boundary must not be assumed to earn positive score.
        eligible = yes_distance < max_distance and no_distance < max_distance
        models.append({"offset_ticks": offset, "yes_bid": float(buy_yes),
                       "role": "primary_cautious_candidate" if offset == 2 else "comparison_only",
                       "yes_ask": float(sell_yes), "eligible_geometry": eligible,
                       "reserved_quote_value": float(QTY * (buy_yes + 1 - sell_yes)),
                       "yes_distance_cents": float(100 * yes_distance),
                       "no_distance_cents": float(100 * no_distance),
                       "possible_yes_touch": False, "possible_no_touch": False})
    return models


class PublicRest:
    def __init__(self):
        self.last_call = 0.0
        self.calls = 0
        self.throttles = 0
        self.blocked_until = 0.0
        self.until = time.time() + 24 * 3600

    def get(self, path):
        allowed = ("/v1/prediction-markets/events", "/v1/prediction-markets/liquidity-rewards/config",
                   "/v1/prediction-markets/liquidity-rewards/events")
        clean = path.split("?", 1)[0]
        if clean not in allowed and not (clean.startswith(allowed[0] + "/") and clean.count("/") == 4):
            raise ValueError("non_readonly_route")
        if time.monotonic() < self.blocked_until:
            raise RateCooldown("rest_rate_limit_cooldown")
        time.sleep(max(0, REST_SPACING - (time.monotonic() - self.last_call)))
        self.last_call = time.monotonic()
        self.calls += 1
        req = urllib.request.Request("https://api.gemini.com" + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 418):
                self.throttles += 1
                default = max(60, self.until - time.time()) if exc.code == 418 else min(900, 60 * 2 ** min(self.throttles - 1, 4))
                self.blocked_until = time.monotonic() + retry_delay(exc.headers.get('Retry-After'), default)
            raise  # No immediate retries; startup fails or hourly refresh waits.

    def catalog(self):
        rows, seen = [], set()
        for offset in range(0, 3000, 100):
            page = self.get("/v1/prediction-markets/liquidity-rewards/events?limit=100&sort=title_asc&offset=" + str(offset))
            for row in page["events"]:
                if row["event_ticker"] in seen:
                    raise ValueError("catalog_changed_duplicate")
                seen.add(row["event_ticker"])
                rows.append(row)
            if offset + 100 >= page["pagination"]["total"]:
                return rows
        raise ValueError("catalog_page_ceiling")


def discover(rest, until, count):
    config = rest.get("/v1/prediction-markets/liquidity-rewards/config")
    if config.get("enabled") is not True:
        raise ValueError("rewards_disabled")
    rows = rest.catalog()
    grouped = group_pools(rows)
    priorities = {"Zcash": 0, "Commodities": 1, "Bitcoin": 2, "Ether": 3,
                  "Weather": 4, "Midterms": 5}
    ordered = sorted(grouped.items(), key=lambda pair: (
        priorities.get(pair[1][0].get("pool_category_name"), 6 if pair[0].startswith("event:") else 7),
        -D(pair[1][0]["daily_pool_usd"])))
    candidates, evaluated = [], []
    reads = 0
    for key, members in ordered:
        usable_members = [r for r in members if r.get("ends_at") and timestamp(r["ends_at"]) > until + 1800]
        usable_members.sort(key=lambda r: (timestamp(r["ends_at"]), r["qualifying_maker_count"]))
        pool_choices = []
        for reward in usable_members[:8]:
            if reads >= 160:
                break
            event = rest.get("/v1/prediction-markets/events/" + urllib.parse.quote(reward["event_ticker"], safe=""))
            reads += 1
            if event.get("event"):
                event = event["event"]
            if event.get("status") != "active":
                continue
            choices = []
            for contract in event.get("contracts", []):
                prices = contract.get("prices", {})
                try:
                    bid, ask = D(prices["bestBid"]), D(prices["bestAsk"])
                    tick = D(contract["priceIncrement"])
                    if not all(v.is_finite() for v in (bid, ask, tick)):
                        continue
                    if contract.get("marketState") != "open" or contract.get("status") != "active":
                        continue
                    if not D("0.20") <= bid < ask <= D("0.80") or ask - bid > D("0.20"):
                        continue
                    if D(contract["quantityMinimum"]) > QTY or QTY % D(contract["quantityIncrement"]):
                        continue
                    expiry = contract.get("expiryDate", reward["ends_at"])
                    if timestamp(expiry) <= until:
                        continue
                    effective = contract.get("effectiveDate", event.get("effectiveDate"))
                    if effective and timestamp(effective) > time.time():
                        continue
                    choices.append({"symbol": contract["instrumentSymbol"].upper(), "event_ticker": reward["event_ticker"],
                        "title": event["title"], "label": contract.get("label"), "pool_key": key,
                        "category": reward.get("pool_category_name", reward["category"]),
                        "reward": reward, "expiry": expiry, "tick": str(tick), "market_state": "open",
                        "effective": effective, "metadata_checked_at": time.time(), "discovery_mid": float((bid + ask) / 2)})
                except (KeyError, InvalidOperation, TypeError, ValueError):
                    continue
            choices.sort(key=lambda c: abs(c["discovery_mid"] - 0.5))
            pool_choices.extend(choices[:4])
            evaluated.append({"event": reward["event_ticker"], "candidate_count": len(choices)})
            if len(pool_choices) >= 8:
                break
        if pool_choices:
            candidates.append(pool_choices)
        if reads >= 160:
            break
    selected = diverse_selection(candidates, count)
    if not selected:
        raise ValueError("no_qualified_discovery_candidates")
    return {"captured_at": utc(), "config": config, "events": rows, "selected": selected,
            "evaluated": evaluated, "pool_count": len(grouped), "requested_markets": count,
            "candidate_contracts": sum(len(group) for group in candidates), "metadata_read_ceiling": 160,
            "distinct_daily_budget": str(sum(D(v[0]["daily_pool_usd"]) for v in grouped.values()))}


class Socket:
    ALLOWED = {"SUBSCRIBE", "depth", "ping", "conninfo"}

    def __init__(self, observer):
        self.observer = observer
        self.ws = None
        self.pending = {}
        self.request_id = 0
        self.send_lock = asyncio.Lock()
        self.last_send = 0.0
        self.blocked_until = 0.0
        self.throttle_count = 0

    def throttle(self, status, retry=None):
        self.observer.metrics["ws_throttles"] += 1
        self.throttle_count += 1
        default = min(900, 60 * 2 ** min(self.throttle_count - 1, 4))
        if status == 418 or self.throttle_count >= 5:
            default = max(default, self.observer.until - time.time())
        self.blocked_until = max(self.blocked_until, time.monotonic() + retry_delay(retry, default))
        if hasattr(self.observer, 'active'):
            self.observer.active.clear()

    async def call(self, method, params):
        if method not in self.ALLOWED:
            raise ValueError("non_readonly_method")
        async with self.send_lock:
            if time.monotonic() < self.blocked_until:
                raise RateCooldown("websocket_rate_limit_cooldown")
            await asyncio.sleep(max(0, WS_SPACING - (time.monotonic() - self.last_send)))
            if self.ws is None:
                raise ConnectionError("not_connected")
            self.request_id += 1
            request_id = self.request_id
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            self.last_send = time.monotonic()
            try:
                await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
                self.observer.metrics["ws_requests"] += 1
                return await asyncio.wait_for(future, REQUEST_TIMEOUT)
            finally:
                self.pending.pop(request_id, None)

    async def reader(self, ws):
        async for raw in ws:
            self.observer.metrics["ws_frames"] += 1
            try:
                frame = json.loads(raw)
                limits = frame.get("rateLimits", [])
                if limits:
                    self.observer.rate_limits = limits
                # Resume only after the venue cooldown, with exponential backoff.
                # An explicit ban (418) remains blocked for this bounded run.
                if frame.get("status") in (429, 418):
                    error = frame.get("error") or {}
                    detail = (error.get("data") or {}) if isinstance(error, dict) else {}
                    retry = frame.get("retryAfter", detail.get("retryAfter") if isinstance(detail, dict) else None)
                    self.throttle(frame['status'], retry)
                for limit in limits:
                    if (limit.get("rateLimitType") == "REQUEST_WEIGHT"
                            and limit.get("count", 0) >= 0.7 * limit.get("limit", float("inf"))):
                        self.blocked_until = max(self.blocked_until, time.monotonic() + 60)
                        self.observer.metrics["ws_weight_cooldowns"] += 1
                if "id" in frame:
                    future = self.pending.get(frame["id"])
                    if future and not future.done():
                        if "error" in frame or frame.get("status", 200) >= 400:
                            future.set_exception(ValueError("websocket_request_rejected"))
                        else:
                            future.set_result(frame.get("result"))
                else:
                    self.observer.on_frame(frame)
            except (ValueError, KeyError, TypeError, InvalidOperation):
                self.observer.metrics["unparsed_frames"] += 1

    async def run(self):
        backoff = 2
        while time.time() < self.observer.until and not self.observer.stop_requested():
            if time.monotonic() < self.blocked_until:
                await asyncio.sleep(min(30, self.blocked_until - time.monotonic()))
                continue
            try:
                async with connect("wss://ws.gemini.com", open_timeout=15, close_timeout=3,
                                   ping_interval=20, ping_timeout=10, max_queue=128,
                                   max_size=2**20, compression=None, proxy=None) as ws:
                    self.ws = ws
                    self.observer.generation += 1
                    self.observer.active.clear()
                    reader = asyncio.create_task(self.reader(ws))
                    try:
                        streams = [s + suffix for s in self.observer.selected for suffix in ("@bookTicker", "@trade")]
                        await self.call("conninfo", {})
                        streams.append("contractStatus")
                        for start in range(0, len(streams), 20):
                            await self.call("SUBSCRIBE", streams[start:start + 20])
                        self.observer.metrics["ws_connections"] += 1
                        self.observer.ready.set()
                        backoff = 2
                        await reader
                    finally:
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, InvalidStatus) and exc.response.status_code in (429, 418):
                    self.throttle(exc.response.status_code, exc.response.headers.get('Retry-After'))
                self.observer.error("socket", exc)
            finally:
                self.ws = None
                self.observer.active.clear()
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError("socket_closed"))
            await asyncio.sleep(min(backoff, max(0, self.observer.until - time.time())))
            backoff = min(60, backoff * 2)


class Observer:
    def __init__(self, directory, until, selected, config, rest, interval=60):
        self.directory, self.until, self.rest = directory, until, rest
        self.selected = {c["symbol"]: c for c in selected}
        self.config = config
        self.interval = interval
        self.metrics = Counter()
        self.generation = 0
        self.rate_limits = []
        self.buffer = []
        self.sample_cursor = 0
        self.last_limit_check = time.monotonic()
        self.active = {}
        self.history = defaultdict(lambda: deque(maxlen=20))
        self.last_trade = {}
        self.last_ticker = {}
        self.last_errors = deque(maxlen=12)
        self.db = sqlite3.connect(directory / "observations.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, observed_at REAL, symbol TEXT, payload TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS records_lookup ON records(kind,symbol,observed_at)")
        self.started = time.time()
        self.cpu_start = time.process_time()
        self.last_cpu, self.last_wall = self.cpu_start, time.monotonic()
        self.cpu_peak = 0.0
        self.socket = Socket(self)
        self.ready = asyncio.Event()

    def stop_requested(self):
        return (self.directory / "STOP").exists()

    def error(self, stage, exc):
        self.metrics["errors"] += 1
        self.last_errors.append({"at": utc(), "stage": stage, "type": type(exc).__name__})

    def on_frame(self, frame):
        symbol = str(frame.get("s", "")).upper()
        if symbol not in self.selected:
            return
        if frame.get("e") == "contractStatus":
            self.selected[symbol]["market_state"] = "open" if frame.get("n", "").lower() == "active" else "unknown_or_closed"
            self.metrics["lifecycle_updates"] += 1
            return
        if "t" in frame and "p" in frame and "q" in frame:
            # Trade IDs are monotonic for this observation scope; duplicates/backward events do not count.
            trade_id = int(frame["t"])
            if trade_id <= self.last_trade.get(symbol, -1):
                return
            self.last_trade[symbol] = trade_id
            self.metrics["trades"] += 1
            price = D(frame["p"])
            active = self.active.get(symbol)
            if active and active["generation"] == self.generation and time.time() - active["at"] <= self.interval + 1:
                for quote in active["models"]:
                    if frame.get("m") is True and price <= D(str(quote["yes_bid"])):
                        quote["possible_yes_touch"] = True
                    if frame.get("m") is False and price >= D(str(quote["yes_ask"])):
                        quote["possible_no_touch"] = True
            return
        if all(k in frame for k in ("b", "a", "B", "A")):
            # Coalesce quotes in memory; no per-frame database write, sorting or reward scan.
            self.last_ticker[symbol] = (time.time(), frame["b"], frame["a"])
            self.metrics["ticker_updates"] += 1

    def store(self, kind, at, symbol, payload):
        if len(self.buffer) >= BUFFER_LIMIT:
            self.metrics["records_dropped_storage_overflow"] += 1
            return
        self.buffer.append((kind, at, symbol, json.dumps(payload, separators=(",", ":"))))

    async def flush(self):
        for attempt in range(3):
            try:
                with self.db:
                    self.db.executemany("INSERT INTO records VALUES (?,?,?,?)", self.buffer)
                self.metrics["records_committed"] += len(self.buffer)
                self.buffer.clear()
                return True
            except (sqlite3.Error, OSError) as exc:
                self.error("storage_flush", exc)
                await asyncio.sleep((0.25, 0.5, 1)[attempt])
        return False  # Retain bounded pending rows for the next cycle.

    async def publish(self, phase):
        for attempt in range(3):
            try:
                return self.state(phase)
            except OSError as exc:
                self.error("status_write", exc)
                await asyncio.sleep((0.25, 0.5, 1)[attempt])
        return None

    def state(self, phase):
        elapsed = max(0.001, time.time() - self.started)
        cpu_now, wall_now = time.process_time(), time.monotonic()
        recent = 100 * (cpu_now - self.last_cpu) / max(0.001, wall_now - self.last_wall)
        self.cpu_peak = max(self.cpu_peak, recent)
        self.last_cpu, self.last_wall = cpu_now, wall_now
        value = {"phase": phase, "mode": "paper_read_only", "pid": os.getpid(), "updated_at": utc(),
                 "started_at": utc(self.started), "stop_at": utc(self.until), "symbols": list(self.selected),
                 "distinct_selected_pools": len({c["pool_key"] for c in self.selected.values()}),
                 "ws_connected": self.socket.ws is not None, "generation": self.generation,
                 "cpu_percent_one_core_average": round(100 * (cpu_now - self.cpu_start) / elapsed, 3),
                 "cpu_percent_one_core_recent": round(recent, 3), "cpu_percent_one_core_peak_sample": round(self.cpu_peak, 3),
                 "rest_calls": self.rest.calls, "rest_throttles": self.rest.throttles,
                 "pending_storage_records": len(self.buffer),
                 "ws_cooldown_seconds": max(0, round(self.socket.blocked_until - time.monotonic())),
                 "rest_cooldown_seconds": max(0, round(self.rest.blocked_until - time.monotonic())),
                 "ws_rate_limits": self.rate_limits,
                 "metrics": dict(self.metrics), "recent_errors": list(self.last_errors),
                 "reward_income": "not_estimated", "actual_fills": "no_orders_submitted"}
        temp = self.directory / "status.tmp"
        temp.write_text(json.dumps(value, indent=2))
        temp.replace(self.directory / "status.json")
        return value

    async def sample(self, symbol, contract):
        generation = self.generation
        book = await self.socket.call("depth", {"symbol": symbol, "limit": 100})
        at = time.time()
        if generation != self.generation:
            raise ValueError("connection_changed_during_snapshot")
        bids, asks = levels(book["bids"], True), levels(book["asks"], False)
        market_ok = (contract["market_state"] == "open" and timestamp(contract["expiry"]) > at
                     and (not contract.get("effective") or timestamp(contract["effective"]) <= at)
                     and at - contract["metadata_checked_at"] < 4200 and contract.get("reward_current", True)
                     and self.config.get("enabled") is True)
        models = quote_models(bids, asks, D(contract["tick"]), D(self.config["max_spread_cents"]) / 100) if market_ok else []
        valid = bool(bids and asks and bids[0][0] < asks[0][0])
        mid = (bids[0][0] + asks[0][0]) / 2 if valid else None
        yes_exit = liquidation_cash(bids, "yes")
        no_exit = liquidation_cash(asks, "no")
        previous = self.active.pop(symbol, None)
        if previous:
            self.store("quote_interval", at, symbol, {**previous, "ended_at": at,
                "censored": generation != previous["generation"] or at - previous["at"] > self.interval + 10})
        current = {"at": at, "generation": generation, "models": models, "evaluated_horizons": []}
        for old in self.history[symbol]:
            age = at - old["at"]
            for seconds in (60, 300, 900):
                if seconds not in old["evaluated_horizons"] and age >= seconds - 2:
                    old["evaluated_horizons"].append(seconds)
                    if age > seconds + 12 or old["generation"] != generation or mid is None or not market_ok:
                        self.metrics["censored_markout_windows"] += 1
                        continue
                    for quote in old["models"]:
                        if not quote["eligible_geometry"]:
                            continue
                        # Hypothetical entry at the proposed price. These are not inferred fills or P&L.
                        yes_cost, no_cost = QTY * D(str(quote["yes_bid"])), QTY * (1 - D(str(quote["yes_ask"])))
                        self.store("markout", at, symbol, {"horizon_seconds": seconds, "actual_age_seconds": age,
                            "offset_ticks": quote["offset_ticks"], "entry_at": old["at"],
                            "yes_mark_change_usd": float(QTY * mid - yes_cost),
                            "no_mark_change_usd": float(QTY * (1 - mid) - no_cost),
                            "yes_executable_gross_change_usd": None if yes_exit is None else float(yes_exit - yes_cost),
                            "no_executable_gross_change_usd": None if no_exit is None else float(no_exit - no_cost)})
        self.history[symbol].append(current)
        if models:
            self.active[symbol] = current
        self.store("snapshot", at, symbol, {"pool_key": contract["pool_key"], "generation": generation,
            "reward": contract["reward"], "market_eligible": market_ok, "models": models,
            "spread_cents": None if mid is None else float(100 * (asks[0][0] - bids[0][0])),
            "yes_exit_cash_10": None if yes_exit is None else float(yes_exit),
            "no_exit_cash_10": None if no_exit is None else float(no_exit),
            "bids": book["bids"], "asks": book["asks"], "lastUpdateId": book.get("lastUpdateId")})
        self.metrics["snapshots"] += 1
        self.metrics["market_eligible_snapshots"] += int(market_ok and any(m["eligible_geometry"] for m in models))
        self.metrics["primary_eligible_snapshots"] += int(market_ok and any(m["offset_ticks"] == 2 and m["eligible_geometry"] for m in models))

    def refresh(self):
        config = self.rest.get("/v1/prediction-markets/liquidity-rewards/config")
        rows = self.rest.catalog()
        group_pools(rows)
        by_event = {row["event_ticker"]: row for row in rows}
        updates = {}
        for ticker in {c["event_ticker"] for c in self.selected.values()}:
            event = self.rest.get("/v1/prediction-markets/events/" + urllib.parse.quote(ticker, safe=""))
            event = event.get("event", event)
            for c in event.get("contracts", []):
                symbol = c.get("instrumentSymbol", "").upper()
                if symbol in self.selected:
                    updates[symbol] = {"market_state": c.get("marketState") if event.get("status") == "active" else "closed",
                        "metadata_checked_at": time.time(), "reward_current": ticker in by_event}
                    if ticker in by_event:
                        updates[symbol]["reward"] = by_event[ticker]
                        updates[symbol]["pool_key"] = pool_key(by_event[ticker])
        return config, updates

    async def metadata_loop(self):
        delay = 60 if getattr(self, "seeded", False) else 3600
        failures = 0
        while True:
            await asyncio.sleep(max(delay, self.rest.blocked_until - time.monotonic()))
            try:
                config, updates = await asyncio.to_thread(self.refresh)
                self.config = config
                for symbol, update in updates.items():
                    self.selected[symbol].update(update)
                self.metrics["metadata_refreshes"] += 1
                failures, delay = 0, 3600
            except Exception as exc:
                self.error("metadata_refresh", exc)
                failures += 1
                delay = min(600, 60 * 2 ** min(failures - 1, 4))

    async def sample_cycle(self):
        contracts = list(self.selected.items())
        deadline = min(time.monotonic() + CYCLE_BUDGET, time.monotonic() + self.until - time.time())
        if time.monotonic() - self.last_limit_check >= 300 and self.socket.ws is not None:
            self.last_limit_check = time.monotonic()
            try:
                await self.socket.call('conninfo', {})
            except Exception as exc:
                self.error('limits_refresh', exc)
        visited = 0
        for offset in range(len(contracts)):
            if self.stop_requested() or time.monotonic() >= deadline:
                break
            symbol, contract = contracts[(self.sample_cursor + offset) % len(contracts)]
            visited += 1
            self.metrics["sample_attempts"] += 1
            if self.socket.ws is None or time.monotonic() < self.socket.blocked_until:
                self.active.pop(symbol, None)
                self.metrics["missing_samples"] += 1
                continue
            try:
                await asyncio.wait_for(self.sample(symbol, contract), min(REQUEST_TIMEOUT + 0.6, max(0.01, deadline - time.monotonic())))
            except Exception as exc:
                self.active.pop(symbol, None)
                self.metrics["missing_samples"] += 1
                self.error("sample", exc)
        skipped = len(contracts) - visited
        for offset in range(visited, len(contracts)):
            self.active.pop(contracts[(self.sample_cursor + offset) % len(contracts)][0], None)
        self.metrics["cycle_budget_skipped"] += skipped
        self.metrics["missing_samples"] += skipped
        self.sample_cursor = (self.sample_cursor + visited) % max(1, len(contracts))
        self.store("cycle_health", time.time(), "", {"visited": visited, "budget_skipped": skipped,
            "ws_cooldown_seconds": max(0, self.socket.blocked_until - time.monotonic())})

    async def run(self):
        socket_task = asyncio.create_task(self.socket.run())
        metadata_task = asyncio.create_task(self.metadata_loop())
        try:
            await self.publish("starting")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.ready.wait(), min(20, max(0.01, self.until - time.time())))
            next_cycle = time.monotonic()
            while time.time() < self.until and not self.stop_requested():
                await self.sample_cycle()
                await self.flush()
                state = await self.publish("running")
                if state:
                    try:
                        print(json.dumps({k: state[k] for k in ("updated_at", "phase", "metrics", "cpu_percent_one_core_recent")}), flush=True)
                    except OSError as exc:
                        self.error("stdout", exc)
                next_cycle = max(next_cycle + self.interval, time.monotonic())
                await asyncio.sleep(min(max(0, next_cycle - time.monotonic()), max(0, self.until - time.time())))
        finally:
            socket_task.cancel()
            metadata_task.cancel()
            for task in (socket_task, metadata_task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await self.flush()
            await self.publish("stopped_early" if self.stop_requested() or time.time() < self.until else "completed")
            self.db.close()
            try:
                report(self.directory)
            except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
                self.error("report", exc)


def report(directory):
    state = json.loads((directory / "status.json").read_text())
    selected = json.loads((directory / "discovery.json").read_text())["selected"]
    db = sqlite3.connect(f"file:{(directory / 'observations.sqlite').as_posix()}?mode=ro", uri=True)
    lines = ["# Gemini overnight paper observations", "", f"State: {state['phase']}. Updated: {state['updated_at']}.",
        f"Window: {state['started_at']} through {state['stop_at']} (UTC).", "",
        "No orders were submitted. Trade touches are possible executions, not fills. Markouts assume an entry and exclude fees/rewards; they are not realized P&L.",
        "Sampled quote eligibility is not proven venue reward uptime. Missing samples and observation outside this window cannot count as eligible.",
        "Actual daily reward share is unknown because public data does not expose complete competitor daily scores or the precise scoring curve.", "",
        "Primary candidate: two ticks behind both best prices; touch and one-tick quotes are comparisons only. A wider quote is not guaranteed safe.", "",
        f"CPU average: {state['cpu_percent_one_core_average']}% of one core; peak sampled interval: {state['cpu_percent_one_core_peak_sample']}%.",
        f"Metrics: {json.dumps(state['metrics'], sort_keys=True)}. Public REST calls: {state['rest_calls']}.", "",
        "| Contract | Pool | Snapshots | Primary two-tick qualifying geometry | Mean spread (cents) | Full 10-contract exit, both sides |",
        "| --- | --- | ---: | ---: | ---: | ---: |"]
    for contract in selected:
        rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE kind='snapshot' AND symbol=?", (contract["symbol"],))]
        eligible = sum(r["market_eligible"] and any(m["offset_ticks"] == 2 and m["eligible_geometry"] for m in r["models"]) for r in rows)
        spreads = [r["spread_cents"] for r in rows if r["spread_cents"] is not None]
        exits = sum(r["yes_exit_cash_10"] is not None and r["no_exit_cash_10"] is not None for r in rows)
        mean = f"{sum(spreads)/len(spreads):.2f}" if spreads else "unknown"
        lines.append(f"| {contract['symbol']} | {contract['pool_key']} | {len(rows)} | {eligible} | {mean} | {exits} |")
    lines += ["", "## Hypothetical subsequent marks", "",
        "Each side assumes a separate ten-contract entry at the quoted price; the two side outcomes are not added into profit. Quote offset is ticks behind the current best price.", "",
        "| Offset | Horizon | Observations | Mean YES mark change ($) | Mean NO mark change ($) |",
        "| ---: | ---: | ---: | ---: | ---: |"]
    buckets = defaultdict(list)
    for (raw,) in db.execute("SELECT payload FROM records WHERE kind='markout'"):
        row = json.loads(raw)
        buckets[(row["offset_ticks"], row["horizon_seconds"])].append(row)
    for (offset, horizon), rows in sorted(buckets.items()):
        yes = sum(r["yes_mark_change_usd"] for r in rows)/len(rows)
        no = sum(r["no_mark_change_usd"] for r in rows)/len(rows)
        lines.append(f"| {offset} | {horizon}s | {len(rows)} | {yes:.4f} | {no:.4f} |")
    lines += ["", "Full books, proposed quotes, possible trade-touch flags and depth-based markouts are retained in observations.sqlite.",
        "A paper test cannot demonstrate received rewards, reliable fill probability, queue priority or profitable live execution."]
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.close()


async def supervise(directory, until, selected, config, rest, seeded=False):
    """Bounded in-process recovery; preserve cooldowns, rows and connection epochs."""
    carry, failures = None, 0
    while time.time() < until and not (directory / "STOP").exists():
        observer = None
        try:
            observer = Observer(directory, until, selected, config, rest)
            observer.seeded = seeded
            if carry:
                observer.metrics.update(carry["metrics"])
                observer.generation = carry["generation"] + 1
                observer.buffer = carry["buffer"]
                observer.socket.blocked_until = carry["blocked_until"]
                observer.socket.throttle_count = carry["throttle_count"]
            observer.store("observer_session", time.time(), "", {"restart": failures, "pid": os.getpid()})
            await observer.run()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            if observer:
                observer.metrics["supervisor_restarts"] += 1
                carry = {"metrics": observer.metrics, "generation": observer.generation,
                    "buffer": observer.buffer, "blocked_until": observer.socket.blocked_until,
                    "throttle_count": observer.socket.throttle_count}
                selected, config = list(observer.selected.values()), observer.config
                with contextlib.suppress(sqlite3.Error):
                    observer.db.close()
            with contextlib.suppress(OSError):
                print(json.dumps({"supervisor_failure": type(exc).__name__, "attempt": failures}), flush=True)
            if failures > 3:
                raise
            resume_at = min(until, time.time() + SUPERVISOR_BACKOFF * 2 ** (failures - 1))
            while time.time() < resume_at and not (directory / "STOP").exists():
                await asyncio.sleep(min(5, max(0, resume_at - time.time())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", help="Absolute UTC stop time, required unless generating a report")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--markets", type=int, default=10)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--seed-from", help="Preserve a recent local cohort when starting a new run")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.relative_to((ROOT / "data").resolve())
    if args.report:
        report(directory)
        return
    until = timestamp(args.until)
    if not time.time() < until <= time.time() + 24 * 3600 or not 1 <= args.markets <= MAX_MARKETS:
        raise ValueError("invalid_observation_bounds")
    directory.mkdir(parents=True, exist_ok=True)
    # OS-held file lock prevents two observers writing the same run, including after crashes.
    lock = (directory / "observer.lock").open("a+b")
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    if (directory / "status.json").exists():
        raise ValueError("existing_run_requires_new_directory")
    awake = False
    try:
        if os.name == "nt":
            awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        rest = PublicRest()
        rest.until = until
        if args.seed_from:
            source = Path(args.seed_from).resolve()
            source.relative_to((ROOT / 'data').resolve())
            discovery = json.loads((source / 'discovery.json').read_text())
            selected = discovery['selected']
            if not 0 < len(selected) <= args.markets or time.time() - timestamp(discovery['captured_at']) > 3600:
                raise ValueError('invalid_or_old_seed_cohort')
            if any(timestamp(c['expiry']) <= until for c in selected):
                raise ValueError('seed_expires_before_deadline')
            discovery['seed_source'] = str(source)
            discovery['reused_at'] = utc()
        else:
            discovery = discover(rest, until, args.markets)
        (directory / "discovery.json").write_text(json.dumps(discovery, indent=2), encoding="utf-8")
        print(json.dumps({"selected": discovery["selected"], "distinct_daily_budget": discovery["distinct_daily_budget"]}), flush=True)
        asyncio.run(supervise(directory, until, discovery["selected"], discovery["config"], rest, bool(args.seed_from)))
    finally:
        if awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        lock.close()


if __name__ == "__main__":
    main()
