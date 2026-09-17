"""Durable recovery grace for owner slots, never permission to keep unsafe orders."""
from dataclasses import dataclass
from datetime import datetime
import json
import math
from .capacity import MAX_ACTIVE_MARKETS

RECOVERY_GRACE_SECONDS = 600
MAX_OBSERVATION_GAP_SECONDS = 30


@dataclass
class Failure:
    reason: str
    observed_seconds: float
    last_observed: datetime


class StickyRecovery:
    def __init__(self) -> None:
        self.failures: dict[str, Failure] = {}
        self._restart_pending: set[str] = set()

    def observe(self, owner: str, reason: str | None, now: datetime, *, evidence_available: bool = True) -> bool:
        if now.tzinfo is None:
            raise ValueError("owner recovery time must be timezone-aware")
        if reason is None:
            self.failures.pop(owner, None)
            self._restart_pending.discard(owner)
            return False
        previous = self.failures.get(owner)
        delta = (now - previous.last_observed).total_seconds() if previous else 0
        continuous = (previous is not None and owner not in self._restart_pending
                      and evidence_available and 0 <= delta <= MAX_OBSERVATION_GAP_SECONDS)
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
