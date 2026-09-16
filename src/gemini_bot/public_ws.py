from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .market_state import LocalBook, SequenceGap
from .domain import canonical_symbol
from .rate_limits import AdmissionGovernor, Priority
from .scheduler import DirtySymbolScheduler
from .ws_limits import observe_response, receive_connection_limits, pace_connection


PRODUCTION_WS = "wss://ws.gemini.com?snapshot=-1"


def _levels(value: Any) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list):
        raise ValueError("depth levels must be a list")
    result: list[tuple[Decimal, Decimal]] = []
    for level in value:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("invalid depth level")
        result.append((Decimal(str(level[0])), Decimal(str(level[1]))))
    return result


class PublicMarketSocket:
    """One public differential-depth connection with per-symbol resync."""

    def __init__(self, governor: AdmissionGovernor, *, queue_capacity: int = 2048) -> None:
        self._governor = governor
        self._connection: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._ping_lock = asyncio.Lock()
        self._request_id = 0
        self._initial_frames: list[dict[str, Any]] = []
        self.books: dict[str, LocalBook] = {}
        self._subscribed_symbols: set[str] | None = None
        self.dirty = DirtySymbolScheduler(queue_capacity)
        self.adverse_dirty = DirtySymbolScheduler(queue_capacity)
        self.depth_changed = asyncio.Event()
        self.resync_required: set[str] = set()
        # Differential depth is event-driven in observed production traffic: a
        # quiet book can be unchanged for minutes.  Connection activity proves
        # that the shared sequenced stream is alive; LocalBook.observed_at keeps
        # its separate meaning of "last state change for this symbol".
        self.last_message_at: datetime | None = None

    def note_message(self, observed_at: datetime | None = None) -> datetime:
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("WebSocket observation timestamp must be timezone-aware")
        self.last_message_at = timestamp.astimezone(timezone.utc)
        return self.last_message_at

    def stream_fresh(self, *, now: datetime, stale_after_seconds: Decimal) -> bool:
        if now.tzinfo is None or stale_after_seconds <= 0 or self.last_message_at is None:
            return False
        age = Decimal(str((now - self.last_message_at).total_seconds()))
        return Decimal("0") <= age <= stale_after_seconds

    async def connect(self) -> None:
        await pace_connection(self._governor)
        self._connection = await websockets.connect(
            PRODUCTION_WS,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_queue=2048,
            max_size=2**20,
            compression=None,
            proxy=None,
        )
        try:
            await self._send("conninfo", {}, Priority.MARKET_RESYNC)
            self._initial_frames = await receive_connection_limits(
                self._connection, self._governor, "public_ws", self._request_id,
            )
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def prove_liveness(self, *, timeout: float = 2.0) -> float:
        """Use protocol ping/pong to prove a quiet event-driven stream is live."""
        if timeout <= 0:
            raise ValueError("ping timeout must be positive")
        async with self._ping_lock:
            connection = self._connection
            if connection is None:
                raise ConnectionError("public WebSocket is not connected")
            pong = await connection.ping()
            latency = await asyncio.wait_for(pong, timeout=timeout)
            self.note_message()
            return float(latency)

    async def _send(self, method: str, params: object, priority: Priority) -> None:
        if self._connection is None:
            raise ConnectionError("public WebSocket is not connected")
        async with self._send_lock:
            delay = self._governor.admit_websocket("public_ws", priority, method)
            while delay:
                await asyncio.sleep(delay)
                delay = self._governor.admit_websocket("public_ws", priority, method)
            self._request_id += 1
            await self._connection.send(json.dumps(
                {"id": self._request_id, "method": method, "params": params},
                separators=(",", ":"),
            ))

    async def subscribe(self, symbols: tuple[str, ...]) -> None:
        await self._send("SUBSCRIBE", ["contractStatus"], Priority.MARKET_RESYNC)
        symbols = tuple(canonical_symbol(symbol) for symbol in symbols)
        if self._subscribed_symbols is None:
            self._subscribed_symbols = set()
        self._subscribed_symbols.update(symbols)
        for start in range(0, len(symbols), 20):
            batch = symbols[start:start + 20]
            for symbol in batch:
                self.books.setdefault(symbol, LocalBook(symbol))
            await self._send(
                "SUBSCRIBE",
                [f"{symbol}@depth@100ms" for symbol in batch],
                Priority.MARKET_RESYNC,
            )
            # Trade prints feed the profitability estimator.  Keep this as a
            # separate bounded request so subscription message width remains
            # at the already-tested 20-stream batch size.
            await self._send(
                "SUBSCRIBE",
                [f"{symbol}@trade" for symbol in batch],
                Priority.MARKET_RESYNC,
            )

    async def unsubscribe(self, symbols: tuple[str, ...]) -> None:
        """Retire proven-flat owners; late frames cannot recreate their books."""
        symbols = tuple(canonical_symbol(symbol) for symbol in symbols)
        if self._subscribed_symbols is not None:
            self._subscribed_symbols.difference_update(symbols)
        for symbol in symbols:
            self.books.pop(symbol, None)
            self.resync_required.discard(symbol)
        for start in range(0, len(symbols), 10):
            await self._send("UNSUBSCRIBE", [stream for symbol in symbols[start:start + 10]
                             for stream in (f"{symbol}@depth@100ms", f"{symbol}@trade")], Priority.MARKET_RESYNC)

    async def resync(self, symbol: str) -> None:
        symbol = canonical_symbol(symbol)
        book = self.books.setdefault(symbol, LocalBook(symbol))
        book.synchronized = False
        await self._send("UNSUBSCRIBE", [f"{symbol}@depth@100ms"], Priority.MARKET_RESYNC)
        await self._send("SUBSCRIBE", [f"{symbol}@depth@100ms"], Priority.MARKET_RESYNC)

    def apply_frame(self, frame: dict[str, Any], observed_at: datetime | None = None) -> str | None:
        timestamp = self.note_message(observed_at)
        if "status" in frame and frame["status"] != 200:
            if frame["status"] in {418, 429}:
                self._governor.apply_server_cooldown("public_ws", 60)
            raise RuntimeError("public WebSocket request rejected")
        if frame.get("e") != "depthUpdate":
            return None
        symbol = canonical_symbol(str(frame["s"]))
        if self._subscribed_symbols is not None and symbol not in self._subscribed_symbols:
            return None
        if symbol in self.resync_required:
            return symbol
        local = self.books.setdefault(symbol, LocalBook(symbol))
        first = int(frame["U"])
        last = int(frame["u"])
        bids = _levels(frame.get("b", []))
        asks = _levels(frame.get("a", []))
        try:
            if not local.synchronized:
                local.apply_snapshot(bids, asks, first, last, timestamp)
            else:
                local.apply_delta(bids, asks, first, last, timestamp)
        except SequenceGap:
            self.resync_required.add(symbol)
            return symbol
        self.dirty.mark(symbol)
        self.adverse_dirty.mark(symbol)
        self.depth_changed.set()
        return symbol

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        if self._connection is None:
            raise ConnectionError("public WebSocket is not connected")
        for frame in self._initial_frames:
            yield frame
        self._initial_frames.clear()
        async for raw in self._connection:
            if isinstance(raw, str):
                value = json.loads(raw)
                if isinstance(value, dict):
                    observe_response(self._governor, "public_ws", value)
                    self.note_message()
                    yield value
