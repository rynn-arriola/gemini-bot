"""Bounded public-data fan-in. This adapter has no account/order authority."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from .domain import canonical_symbol
from .market_state import LocalBook
from .public_ws import PublicMarketSocket
from .scheduler import DirtySymbolScheduler


PUBLIC_SHARD_COUNT = 5


@dataclass(frozen=True)
class ShardEvent:
    shard: int
    generation: int
    frame: dict[str, Any] | None = None
    error: Exception | None = None


class PublicMarketShards:
    def __init__(self, governor, *, count=PUBLIC_SHARD_COUNT, socket_factory=PublicMarketSocket):
        if not 1 <= count <= PUBLIC_SHARD_COUNT:
            raise ValueError('public shard count exceeds reviewed architecture')
        self.sockets = [socket_factory(governor) for _ in range(count)]
        self.generations = [0] * count
        self.assignments: dict[str, int] = {}
        self.held: set[int] = set()
        self.connected: set[int] = set()
        self.books: dict[str, LocalBook] = {}
        self.resync_required: set[str] = set()
        self.dirty = DirtySymbolScheduler(2048)
        self.adverse_dirty = DirtySymbolScheduler(2048)
        self.depth_changed = asyncio.Event()
        self._events: asyncio.Queue[ShardEvent] = asyncio.Queue(2048)
        self._readers: dict[int, asyncio.Task] = {}
        self._consuming = False
        self._subscribed_shards: set[int] = set()

    def restore_assignments(self, raw: str | None) -> None:
        if raw is None:
            return
        mapping = json.loads(raw)
        if (not isinstance(mapping, dict) or len(mapping) > 32
                or any(not isinstance(s, str) or canonical_symbol(s) != s
                       or type(i) is not int or not 0 <= i < len(self.sockets)
                       for s, i in mapping.items())):
            raise ValueError('invalid persisted public shard assignment')
        self.assignments = mapping

    def symbols(self, shard: int) -> tuple[str, ...]:
        return tuple(sorted(s for s, i in self.assignments.items() if i == shard))

    def generation(self, symbol: str) -> int:
        return self.generations[self.assignments[canonical_symbol(symbol)]]

    def verified_at(self, symbol: str) -> datetime | None:
        shard = self.assignments.get(canonical_symbol(symbol))
        if shard is None or shard in self.held or shard not in self.connected:
            return None
        return self.sockets[shard].last_message_at

    def accepts(self, event: ShardEvent) -> bool:
        return event.generation == self.generations[event.shard]

    def owns_frame(self, event: ShardEvent) -> bool:
        frame = event.frame or {}
        symbol = frame.get('s')
        return symbol is None or self.assignments.get(str(symbol).upper()) == event.shard

    def invalidate(self, shard: int) -> None:
        self.held.add(shard)
        self.generations[shard] += 1
        self.connected.discard(shard)
        self.sockets[shard].last_message_at = None
        for symbol in self.symbols(shard):
            if symbol in self.books:
                self.books[symbol].synchronized = False
            self.dirty.mark(symbol)
            self.adverse_dirty.mark(symbol)
        self.depth_changed.set()

    async def _read(self, shard: int, generation: int) -> None:
        try:
            async for frame in self.sockets[shard].frames():
                await self._events.put(ShardEvent(shard, generation, frame=frame))
            raise ConnectionError('public shard stream ended')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._events.put(ShardEvent(shard, generation, error=exc))

    def _start_reader(self, shard: int) -> None:
        if self._consuming and shard in self.connected:
            if shard in self._readers and not self._readers[shard].done():
                raise RuntimeError('public shard already has a reader')
            self._readers[shard] = asyncio.create_task(
                self._read(shard, self.generations[shard]), name=f'gemini-public-shard-{shard}',
            )

    async def _stop_reader(self, shard: int) -> None:
        task = self._readers.pop(shard, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def connect(self) -> None:
        # Shared governor staggers attempts. Startup remains all-or-nothing.
        try:
            self._subscribed_shards.clear()
            for shard, socket in enumerate(self.sockets):
                socket.books = {}
                async with asyncio.timeout(15):
                    await socket.connect()
                self.connected.add(shard)
            self.held.clear()
        except BaseException:
            await self.close()
            raise

    async def reconnect(self, shard: int) -> None:
        await self._stop_reader(shard)
        socket = self.sockets[shard]
        await socket.close()
        socket.books = {s: LocalBook(s) for s in self.symbols(shard)}
        socket.resync_required.clear()
        socket.last_message_at = None
        self.books.update(socket.books)
        self.resync_required.difference_update(self.symbols(shard))
        await socket.connect()
        await socket.subscribe(self.symbols(shard))
        self._subscribed_shards.add(shard)
        self.connected.add(shard)
        self._start_reader(shard)

    async def subscribe(self, symbols: tuple[str, ...]) -> None:
        symbols = tuple(sorted(set(canonical_symbol(s) for s in symbols)))
        if len(set(self.assignments) | set(symbols)) > 32:
            raise ValueError('public subscription owner bound exceeded')
        additions: dict[int, list[str]] = {}
        for symbol in symbols:
            if symbol not in self.assignments:
                shard = min(range(len(self.sockets)), key=lambda i: (len(self.symbols(i)), i))
                self.assignments[symbol] = shard
            shard = self.assignments[symbol]
            if shard in self.held:
                raise RuntimeError('cannot change subscriptions on a recovering shard')
            additions.setdefault(shard, []).append(symbol)
        # Subscribe lifecycle on empty shards too; membership filters duplicates.
        for shard, socket in enumerate(self.sockets):
            if shard in self.held or (shard not in additions and shard in self._subscribed_shards):
                continue
            await socket.subscribe(tuple(additions.get(shard, ())))
            self._subscribed_shards.add(shard)
            self.books.update(socket.books)

    async def unsubscribe(self, symbols: tuple[str, ...]) -> None:
        grouped: dict[int, list[str]] = {}
        for symbol in symbols:
            symbol = canonical_symbol(symbol)
            shard = self.assignments[symbol]
            if shard in self.held:
                raise RuntimeError('cannot retire an owner on a recovering shard')
            grouped.setdefault(shard, []).append(symbol)
        for shard, group in grouped.items():
            await self.sockets[shard].unsubscribe(tuple(group))
            for symbol in group:
                self.assignments.pop(symbol)
                self.books.pop(symbol, None)
                self.resync_required.discard(symbol)

    async def frames(self):
        if self._consuming:
            raise RuntimeError('only one public shard consumer is permitted')
        self._consuming = True
        try:
            for shard in sorted(self.connected):
                self._start_reader(shard)
            while True:
                yield await self._events.get()
        finally:
            self._consuming = False
            await asyncio.gather(*(self._stop_reader(i) for i in tuple(self._readers)))

    def apply_frame(self, event: ShardEvent) -> str | None:
        if not self.accepts(event) or not self.owns_frame(event):
            return None
        socket = self.sockets[event.shard]
        symbol = socket.apply_frame(event.frame or {})
        if symbol is not None:
            self.books[symbol] = socket.books[symbol]
            if symbol in socket.resync_required:
                self.resync_required.add(symbol)
            self.dirty.mark(symbol)
            self.adverse_dirty.mark(symbol)
            self.depth_changed.set()
        return symbol

    async def prove_liveness(self, *, timeout=2):
        async def ping(shard):
            generation = self.generations[shard]
            try:
                await self.sockets[shard].prove_liveness(timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._events.put(ShardEvent(shard, generation, error=exc))
        await asyncio.gather(*(ping(i) for i in sorted(self.connected)))

    def status(self):
        return [{'shard': i, 'symbols': list(self.symbols(i)), 'generation': self.generations[i],
                 'held': i in self.held, 'connected': i in self.connected,
                 'last_message_at': (s.last_message_at.isoformat() if s.last_message_at else None)}
                for i, s in enumerate(self.sockets)]

    async def close(self):
        for i in range(len(self.sockets)):
            self.invalidate(i)
        await asyncio.gather(*(self._stop_reader(i) for i in tuple(self._readers)))
        await asyncio.gather(*(s.close() for s in self.sockets))
        while not self._events.empty():
            self._events.get_nowait()
