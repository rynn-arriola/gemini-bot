"""Exchange-reported reward evidence. Never an order-admission authority."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
import time
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
REFRESH_SECONDS = 300
STALE_SECONDS = 900
SOURCE = "authenticated_rest_liquidity_rewards_daily_summary"


def event_identity(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    raw = str(value)
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19:
        return None
    number = int(raw)
    return str(number) if 0 < number <= 2**63 - 1 else None


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid snapshot count")
    return value


def parse_summaries(rows: Any, start: date, end: date) -> dict[tuple[str, str], dict[str, Any]]:
    """Validate atomically; do not infer identities, dates, or missing counts."""
    if not isinstance(rows, list) or len(rows) > 2:
        raise ValueError("invalid daily summaries")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    dates: set[date] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("payout_date"), str):
            raise ValueError("invalid summary date")
        day = date.fromisoformat(row["payout_date"])
        if day.isoformat() != row["payout_date"] or not start <= day <= end or day in dates:
            raise ValueError("duplicate or out-of-window summary date")
        dates.add(day)
        events = row.get("events")
        if not isinstance(events, list) or len(events) > 3000:
            raise ValueError("invalid event summaries")
        status = row.get("payout_status")
        # Status is informational, never evidence that a scoring window is final.
        safe_status = status if status in ("PAID", "PENDING", "ZERO_AMOUNT") else "UNKNOWN"
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("invalid event summary")
            identity = event_identity(event.get("event_id"))
            if identity is None:
                raise ValueError("invalid event identity")
            qualifying = _count(event.get("snapshot_count"))
            total = _count(event.get("total_snapshots"))
            if qualifying > total:
                raise ValueError("qualifying count exceeds total")
            key = (day.isoformat(), identity)
            if key in result:
                raise ValueError("duplicate event summary")
            result[key] = {
                "event_id": identity, "scoring_date": day.isoformat(),
                "snapshot_count": qualifying, "total_snapshots": total,
                "percentage": None if total == 0 else str(
                    (Decimal(qualifying) * 100 / total).quantize(Decimal(".0001"))
                ),
                "required_snapshots_at_reported_total": None if total == 0 else (total + 1) // 2,
                "meets_50_percent_on_reported_counts": None if total == 0 else 2 * qualifying >= total,
                "payout_status": safe_status,
                "final_eligibility_confirmed": False,
                "venue_counts_updated_at": None,
                "scored_through_at": None,
                "snapshot_cadence_seconds": 60,
            }
    return result


class ExchangeRewardUptime:
    """Bounded two-date cache with one paced background fetch for all events."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.fetched_at: datetime | None = None
        self.attempted_at: datetime | None = None
        self.error: str | None = None
        self._next_attempt = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, rest: Any, *, now: datetime, clock: float | None = None) -> bool:
        if now.tzinfo is None:
            raise ValueError("timezone-aware fetch time required")
        clock = time.monotonic() if clock is None else clock
        if self._lock.locked() or clock < self._next_attempt:
            return False
        async with self._lock:
            self._next_attempt = clock + REFRESH_SECONDS
            self.attempted_at = now
            today = now.astimezone(EASTERN).date()
            yesterday = today - timedelta(days=1)
            request_started = time.monotonic()
            try:
                rows = await rest.liquidity_reward_daily(
                    date_from=yesterday.isoformat(), date_to=today.isoformat(),
                )
                parsed = parse_summaries(rows, yesterday, today)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never expose response text, credentials, or exception payloads.
                self.error = "request_or_response_failed"
                return False
            self.rows = parsed
            self.fetched_at = now + timedelta(seconds=max(0, time.monotonic() - request_started))
            self.error = None
            return True

    def metadata(self, *, now: datetime) -> dict[str, Any]:
        age = None if self.fetched_at is None else (now - self.fetched_at).total_seconds()
        stale = self.fetched_at is None or age is None or not 0 <= age <= STALE_SECONDS or self.error is not None
        return {
            "source": SOURCE,
            "fetch_status": "failed" if self.error else ("ok" if self.fetched_at else "not_fetched"),
            "last_attempt_at": self.attempted_at.isoformat() if self.attempted_at else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "stale": stale, "refresh_interval_seconds": REFRESH_SECONDS,
            "live_updates_guaranteed": False,
            "used_for_trading_decisions": False,
            "error": self.error,
        }

    def event_view(self, identity: object, *, now: datetime) -> dict[str, Any]:
        metadata = self.metadata(now=now)
        today = now.astimezone(EASTERN).date().isoformat()
        identity = event_identity(identity)
        current = self.rows.get((today, identity)) if identity else None
        latest = max(
            (row for (day, event_id), row in self.rows.items() if event_id == identity and day <= today),
            key=lambda row: row["scoring_date"], default=None,
        )
        if identity is None:
            reason = "event_id_unavailable"
        elif self.error:
            reason = "fetch_failed"
        elif metadata["stale"]:
            reason = "fetch_failed_or_stale" if self.fetched_at else "not_fetched"
        elif current is None:
            reason = "no_current_day_report"
        elif current["total_snapshots"] == 0:
            reason = "zero_denominator"
        else:
            reason = "reported"
        available = reason == "reported"
        return {
            **metadata, "event_id": identity, "requested_scoring_date": today,
            "availability": reason,
            "label": "Exchange-reported uptime" if available else "Exchange uptime unavailable",
            "current_percentage": current["percentage"] if available else None,
            "current_report": dict(current) if current is not None else None,
            "last_report": dict(latest) if latest is not None else None,
        }
