"""Per-event progress toward Gemini's 50% daily snapshot rule.

Gemini publishes no live snapshot counts; the authenticated summary appears
only after its scoring day closes. This classifies the bot's own per-minute
count (``UptimeProgress``) so quoting can protect the events that can still
qualify and stop spending fill risk on events that cannot. Local counts are an
estimate of the venue count, compared nightly against the published summary.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import Enum

from .domain import UptimeProgress


class UptimeStatus(str, Enum):
    LOCKED = "locked"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    LOST = "lost"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UptimeLedgerPolicy:
    scoring_day_boundary_hour: int = 17
    threshold: Decimal = Decimal("0.50")
    margin_minutes: int = 30
    at_risk_spare_minutes: int = 120
    admission_spare_minutes: int = 60

    def validate(self) -> None:
        if not 0 <= self.scoring_day_boundary_hour <= 23:
            raise ValueError("scoring-day boundary hour must be 0-23")
        if not Decimal("0") < self.threshold <= Decimal("1"):
            raise ValueError("uptime threshold must be in (0, 1]")
        if min(self.margin_minutes, self.at_risk_spare_minutes, self.admission_spare_minutes) < 0:
            raise ValueError("uptime ledger minutes cannot be negative")


@dataclass(frozen=True, slots=True)
class UptimeLedgerState:
    status: UptimeStatus
    total_minutes: int
    counted: int
    target: int
    needed: int
    remaining: int
    spare: int

    @property
    def admission_allowed(self) -> bool:
        return self.status is not UptimeStatus.LOST and self.status is not UptimeStatus.UNKNOWN

    @property
    def uptime_first(self) -> bool:
        return self.status is UptimeStatus.AT_RISK


def classify(progress: UptimeProgress | None, policy: UptimeLedgerPolicy) -> UptimeLedgerState:
    """Classify one event's current scoring window.

    ``eligible_snapshots`` is elapsed minutes, ``remaining_venue_snapshots_estimate``
    the rest of the venue window, and ``remaining_snapshots_estimate`` the
    minutes we can still quote before our own cutoff.
    """
    if (progress is None or not progress.window_valid
            or progress.remaining_venue_snapshots_estimate is None):
        return UptimeLedgerState(UptimeStatus.UNKNOWN, 0, 0, 0, 0, 0, 0)
    total = progress.eligible_snapshots + progress.remaining_venue_snapshots_estimate
    if total <= 0:
        return UptimeLedgerState(UptimeStatus.UNKNOWN, 0, 0, 0, 0, 0, 0)
    counted = progress.qualifying_snapshots
    remaining = progress.remaining_snapshots_estimate
    bare = int((policy.threshold * total).to_integral_value(rounding=ROUND_CEILING))
    target = min(total, bare + policy.margin_minutes)
    needed = max(0, target - counted)
    spare = remaining - needed
    if counted >= target:
        status = UptimeStatus.LOCKED
    elif remaining < max(0, bare - counted):
        status = UptimeStatus.LOST
    elif spare < policy.at_risk_spare_minutes:
        status = UptimeStatus.AT_RISK
    else:
        status = UptimeStatus.ON_TRACK
    return UptimeLedgerState(status, total, counted, target, needed, remaining, spare)


def admission_allowed(state: UptimeLedgerState, policy: UptimeLedgerPolicy) -> bool:
    """A new event must be able to reach its target with spare minutes left."""
    if state.status is UptimeStatus.LOCKED:
        return True
    return state.admission_allowed and state.spare >= policy.admission_spare_minutes
