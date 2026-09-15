from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import time


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

    def admit(self, lane: str, priority: Priority, now: float | None = None) -> float:
        if lane not in self.lanes:
            raise KeyError(f"unknown rate lane: {lane}")
        delay = self.lanes[lane].consume(now)
        return delay

    def apply_server_cooldown(self, lane: str, seconds: float, now: float | None = None) -> None:
        self.lanes[lane].cool_down(seconds, now)
