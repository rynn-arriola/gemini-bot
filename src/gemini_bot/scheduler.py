from __future__ import annotations

from collections import deque


class DirtySymbolScheduler:
    """Bounded coalescing queue: one pending calculation per symbol."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._queue: deque[str] = deque()
        self._dirty: set[str] = set()
        self.coalesced = 0
        self.dropped = 0

    def mark(self, symbol: str) -> bool:
        if symbol in self._dirty:
            self.coalesced += 1
            return False
        if len(self._queue) >= self.capacity:
            self.dropped += 1
            return False
        self._dirty.add(symbol)
        self._queue.append(symbol)
        return True

    def pop(self) -> str | None:
        if not self._queue:
            return None
        symbol = self._queue.popleft()
        self._dirty.remove(symbol)
        return symbol

    def __len__(self) -> int:
        return len(self._queue)

