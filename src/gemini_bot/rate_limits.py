from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import time
import math


@dataclass(slots=True)
class WebSocketBucket:
    limit: int
    seconds: float
    used: float
    expires_at: float

    def roll(self, now: float) -> None:
        if now >= self.expires_at:
            self.used = 0
            self.expires_at = now + self.seconds


class Priority(IntEnum):
    EMERGENCY_CANCEL = 0
    ORDER_SAFETY = 10
    ACCOUNT_RECONCILIATION = 20
    MARKET_RESYNC = 30
    REWARD_METADATA = 50
    REPORTING = 90


@dataclass(slots=True)
class Lane:
    minimum_interval_seconds: float
    next_allowed_at: float = 0.0
    cooldown_until: float = 0.0

    def delay(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.next_allowed_at - current, self.cooldown_until - current)

    def consume(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        delay = self.delay(current)
        if delay > 0:
            return delay
        self.next_allowed_at = current + self.minimum_interval_seconds
        return 0.0

    def cool_down(self, seconds: float, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.cooldown_until = max(self.cooldown_until, current + max(0.0, seconds))


class AdmissionGovernor:
    """One shared authority for all outbound Gemini application requests."""

    def __init__(self) -> None:
        self.lanes = {
            "public_rest": Lane(1.0),
            "private_rest": Lane(0.2),
            "public_ws": Lane(0.05),
            "private_ws": Lane(0.05),
        }
        # Share conservative accounting across both sockets because Gemini does
        # not promise that every bucket is exclusively connection-scoped.
        self.ws_buckets: dict[tuple[str, float], WebSocketBucket] = {}
        self.ws_verified: set[str] = set()
        self.ws_next_allowed_at = 0.0
        self.ws_connection_next_at = 0.0

    def admit_websocket_connection(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        delay = max(0.0, self.ws_connection_next_at - current,
                    self.lanes["public_ws"].cooldown_until - current,
                    self.lanes["private_ws"].cooldown_until - current)
        spacing = 1.0
        for (kind, _), bucket in self.ws_buckets.items():
            if kind != "CONNECTION_ATTEMPTS":
                continue
            bucket.roll(current)
            budget = max(1, math.floor(bucket.limit * .7))
            if bucket.used + 1 > budget:
                delay = max(delay, bucket.expires_at - current)
            spacing = max(spacing, bucket.seconds / budget)
        if delay > 0:
            return delay
        self.ws_connection_next_at = current + spacing
        for (kind, _), bucket in self.ws_buckets.items():
            if kind == "CONNECTION_ATTEMPTS":
                bucket.used += 1
        return 0.0

    def observe_websocket_limits(self, lane: str, rows: object, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if lane not in {"public_ws", "private_ws"} or not isinstance(rows, list) or not rows:
            raise ValueError("missing WebSocket rate-limit buckets")
        parsed = []
        units = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid WebSocket rate-limit bucket")
            kind = row.get("rateLimitType")
            if kind not in {"REQUEST_WEIGHT", "ORDERS", "CONNECTION_ATTEMPTS"}:
                raise ValueError("unrecognized WebSocket rate-limit bucket")
            try:
                multiplier = int(row["intervalNum"])
                seconds = units[row["interval"]] * multiplier
                limit, count = int(row["limit"]), int(row["count"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("invalid WebSocket rate-limit values") from exc
            if seconds <= 0 or limit <= 0 or count < 0:
                raise ValueError("invalid WebSocket rate-limit range")
            parsed.append((kind, float(seconds), limit, count))
        kinds = {row[0] for row in parsed}
        if "REQUEST_WEIGHT" not in kinds or (lane == "private_ws" and "ORDERS" not in kinds):
            raise ValueError("required WebSocket rate-limit bucket absent")
        for kind, seconds, limit, count in parsed:
            key = kind, seconds
            bucket = self.ws_buckets.get(key)
            if bucket is None:
                self.ws_buckets[key] = WebSocketBucket(limit, seconds, count, current + seconds)
            else:
                bucket.roll(current)
                bucket.limit = min(bucket.limit, limit)
                # Never forgive locally reserved or other-connection traffic
                # just because one response has a lower reported count.
                bucket.used = max(bucket.used, count)
        self.ws_verified.add(lane)

    def admit_websocket(self, lane: str, priority: Priority, method: str, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        costs = {"REQUEST_WEIGHT": 100, "ORDERS": 1 if method.startswith("order.") else 0}
        if method in {"order.cancel_all", "order.cancel_session"}:
            costs["ORDERS"] = 30
        # Per-method weights are not fully specified. Reserve 100 request units
        # conservatively, retain the local 20/s ceiling, and honor live counters.
        fraction = .9 if priority == Priority.EMERGENCY_CANCEL else .7
        delay = max(self.lanes[lane].delay(current), self.ws_next_allowed_at - current, 0)
        spacing = self.lanes[lane].minimum_interval_seconds
        for (kind, _), bucket in self.ws_buckets.items():
            cost = costs.get(kind, 0)
            if not cost:
                continue
            bucket.roll(current)
            budget = math.floor(bucket.limit * fraction)
            if cost > budget:
                raise RuntimeError("reported WebSocket quota cannot cover conservative request cost")
            if bucket.used + cost > budget:
                delay = max(delay, bucket.expires_at - current)
            spacing = max(spacing, bucket.seconds * cost / budget)
        if delay > 0:
            return delay
        self.lanes[lane].consume(current)
        self.ws_next_allowed_at = current + spacing
        for (kind, _), bucket in self.ws_buckets.items():
            bucket.used += costs.get(kind, 0)
        return 0.0

    def admit(self, lane: str, priority: Priority, now: float | None = None) -> float:
        if lane not in self.lanes:
            raise KeyError(f"unknown rate lane: {lane}")
        delay = self.lanes[lane].consume(now)
        return delay

    def apply_server_cooldown(self, lane: str, seconds: float, now: float | None = None) -> None:
        self.lanes[lane].cool_down(seconds, now)
