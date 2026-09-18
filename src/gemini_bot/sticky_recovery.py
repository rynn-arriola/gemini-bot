"""Durable recovery grace for owner slots, never permission to keep unsafe orders."""
from dataclasses import dataclass
from datetime import datetime
import json
import math
from .capacity import MAX_ACTIVE_MARKETS
from .uptime import eastern_day_window

RECOVERY_GRACE_SECONDS = 600
MAX_OBSERVATION_GAP_SECONDS = 30
ECONOMIC_FAILURES = frozenset({
    "non-positive conservative net-profit estimate",
    "non_positive_marginal_pool_economics",
})


@dataclass
class Failure:
    reason: str
    observed_seconds: float
    last_observed: datetime


class StickyRecovery:
    def __init__(self) -> None:
        self.failures: dict[str, Failure] = {}
        self._restart_pending: set[str] = set()
        # First economic failure, not a renewable daily deadline. Keep expired
        # records until genuine recovery/release so a restart cannot renew them.
        self.economic_holds: dict[str, datetime] = {}

    def economic_hold_until(self, owner: str) -> datetime | None:
        started = self.economic_holds.get(owner)
        return eastern_day_window(started)[1] if started is not None else None

    def protect_economics(self, owner: str, reason: str, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("owner recovery time must be timezone-aware")
        if reason not in ECONOMIC_FAILURES:
            return False
        if owner not in self.economic_holds:
            if len(self.economic_holds) >= MAX_ACTIVE_MARKETS:
                raise ValueError("economic reservation capacity exceeded")
            self.economic_holds[owner] = now
        return now < self.economic_hold_until(owner)

    def observe(self, owner: str, reason: str | None, now: datetime, *, evidence_available: bool = True,
                preserve_economic_hold: bool = False) -> bool:
        if now.tzinfo is None:
            raise ValueError("owner recovery time must be timezone-aware")
        if reason is None:
            self.failures.pop(owner, None)
            self._restart_pending.discard(owner)
            if not preserve_economic_hold:
                self.economic_holds.pop(owner, None)
            return False
        previous = self.failures.get(owner)
        delta = (now - previous.last_observed).total_seconds() if previous else 0
        continuous = (previous is not None and previous.reason == reason and owner not in self._restart_pending
                      and evidence_available and 0 <= delta <= MAX_OBSERVATION_GAP_SECONDS)
        until = self.economic_hold_until(owner)
        if until is not None and previous is not None and previous.last_observed < until <= now:
            continuous = False  # Reassess after midnight; never credit the protected day.
        self._restart_pending.discard(owner)
        elapsed = min(RECOVERY_GRACE_SECONDS, previous.observed_seconds + delta) if continuous else 0
        if owner not in self.failures and len(self.failures) >= MAX_ACTIVE_MARKETS:
            raise ValueError("owner recovery capacity exceeded")
        self.failures[owner] = Failure(reason, elapsed, now)
        return elapsed >= RECOVERY_GRACE_SECONDS

    def dumps(self) -> str:
        return json.dumps({k: {"reason": v.reason, "observed_seconds": v.observed_seconds,
                              "last_observed": v.last_observed.isoformat()}
                           for k, v in self.failures.items()}, sort_keys=True)

    def dumps_state(self) -> str:
        return json.dumps({"version": 2, "failures": json.loads(self.dumps()),
                           "economic_holds": {k: v.isoformat() for k, v in self.economic_holds.items()}},
                          sort_keys=True)

    def restore_state(self, raw: str, owners: set[str]) -> None:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"version", "failures", "economic_holds"}
                or type(value['version']) is not int or value['version'] != 2
                or not isinstance(value['economic_holds'], dict)
                or len(value['economic_holds']) > MAX_ACTIVE_MARKETS):
            raise ValueError("invalid daily owner recovery state")
        parsed = {}
        for owner, stamp in value['economic_holds'].items():
            if not isinstance(owner, str) or not owner or not isinstance(stamp, str):
                raise ValueError("invalid daily owner reservation")
            started = datetime.fromisoformat(stamp)
            if started.tzinfo is None:
                raise ValueError("invalid daily owner reservation timestamp")
            eastern_day_window(started)  # Validate derivable expiry before adopting state.
            if owner in owners:
                parsed[owner] = started
        self.restore(json.dumps(value['failures']), owners)
        self.economic_holds = parsed

    def restore(self, raw: str | None, owners: set[str]) -> None:
        if raw is None:
            return
        rows = json.loads(raw)
        if not isinstance(rows, dict) or len(rows) > MAX_ACTIVE_MARKETS:
            raise ValueError("invalid owner recovery state")
        parsed = {}
        for owner, row in rows.items():
            if not isinstance(owner, str) or not isinstance(row, dict) or set(row) != {"reason", "observed_seconds", "last_observed"}:
                raise ValueError("invalid owner recovery record")
            seconds = row['observed_seconds']
            if (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds)
                    or not 0 <= seconds <= RECOVERY_GRACE_SECONDS or not isinstance(row['reason'], str)
                    or not row['reason'] or len(row['reason']) > 512):
                raise ValueError("invalid owner recovery evidence")
            at = datetime.fromisoformat(row['last_observed'])
            if at.tzinfo is None:
                raise ValueError("invalid owner recovery timestamp")
            if owner in owners:
                parsed[owner] = Failure(row['reason'], seconds, at)
        self.failures = parsed
        self._restart_pending = set(parsed)
