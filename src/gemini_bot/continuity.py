"""Operational continuity estimates, never exchange snapshot/reward evidence."""
from dataclasses import dataclass


@dataclass
class RestingObservation:
    observed_seconds: float = 0
    qualifying_seconds: float = 0
    unobserved_seconds: float = 0
    current_gap_seconds: float = 0
    longest_gap_seconds: float = 0
    last_clock: float | None = None
    last_qualifying: bool = False
    gap_count: int = 0
    completed_gap_seconds: float = 0
    last_gap_seconds: float = 0
    order_new_events: int = 0
    order_cancel_events: int = 0

    def observe(self, clock: float, qualifying: bool) -> None:
        if self.last_clock is not None:
            elapsed = max(0, clock - self.last_clock)
            # Never credit a blocked loop, restart, or missing observation as uptime.
            if elapsed > 3:
                self.unobserved_seconds += elapsed
            else:
                self.observed_seconds += elapsed
                if qualifying and self.last_qualifying:
                    self.qualifying_seconds += elapsed
            if not qualifying or not self.last_qualifying or elapsed > 3:
                if self.last_qualifying:
                    self.gap_count += 1
                self.current_gap_seconds += elapsed
                self.longest_gap_seconds = max(self.longest_gap_seconds, self.current_gap_seconds)
            if qualifying:
                if self.current_gap_seconds:
                    self.last_gap_seconds = self.current_gap_seconds
                    self.completed_gap_seconds += self.current_gap_seconds
                self.current_gap_seconds = 0
        self.last_clock, self.last_qualifying = clock, qualifying

    def view(self) -> dict[str, float | bool]:
        return {name: round(getattr(self, name), 3) for name in (
            "observed_seconds", "qualifying_seconds", "unobserved_seconds",
            "current_gap_seconds", "longest_gap_seconds",
            "completed_gap_seconds", "last_gap_seconds", "gap_count",
            "order_new_events", "order_cancel_events",
        )} | {"qualifying_at_last_observation": self.last_qualifying}
