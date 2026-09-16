"""Exchange-reported reward evidence. Never an order-admission authority."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
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
        safe_status = status if status in ("PAID", "PENDING", "ZERO_AMOUNT", "BELOW_THRESHOLD") else "UNKNOWN"
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


def parse_reward_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Account totals and per-event attribution; optional fields stay unavailable.

Input has already passed parse_summaries. Do not divide an event's reward between
our contracts, equate normalized_score with shared-pool share, or sum rounded
event amounts to overwrite the authenticated account total.
"""
    def amount(value: object, maximum: Decimal | None = None) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("invalid reward amount")
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0 or (maximum is not None and parsed > maximum):
            raise ValueError("invalid reward amount")
        return str(parsed)
    result = []
    for row in rows:
        total = amount(row.get("total_reward_usd"))
        paid_at = row.get("paid_at")
        if paid_at is not None:
            paid = datetime.fromisoformat(str(paid_at).replace("Z", "+00:00"))
            if paid.tzinfo is None:
                raise ValueError("invalid paid timestamp")
            paid_at = paid.isoformat()
        status = row.get("payout_status")
        status = status if status in ("PAID", "PENDING", "ZERO_AMOUNT", "BELOW_THRESHOLD") else "UNKNOWN"
        result.append({
            "payout_date": row["payout_date"], "payout_status": status,
            "reported_total_reward_usd": total, "paid_at": paid_at,
            "paid_reward_usd": total if status == "PAID" and paid_at is not None else None,
            "reported_below_daily_minimum": None if total is None else Decimal(total) < 1,
            "exact_scoring_window_known": False,
            "net_profit_after_trading_usd": None,
            "net_profit_unavailable_reason": "reward_window_and_trading_cost_attribution_not_proven",
            "events": [{
                "event_id": event_identity(e["event_id"]),
                "snapshot_count": e["snapshot_count"], "total_snapshots": e["total_snapshots"],
                "normalized_score": amount(e.get("normalized_score"), Decimal("1")),
                "event_reward_usd": amount(e.get("event_reward_usd")),
                "contract_reward_attribution_available": False,
            } for e in row["events"]],
        })
    return result


class ExchangeRewardUptime:
    """Bounded two-date cache with one paced background fetch for all events."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.fetched_at: datetime | None = None
        self.attempted_at: datetime | None = None
        self.error: str | None = None
        self.reward_results: list[dict[str, Any]] = []
        self.history_incomplete = True
        self._next_attempt = 0.0
        self._lock = asyncio.Lock()

    def restore_reward_history(self, payload: str | None, *, now: datetime) -> None:
        """Restore bounded reporting evidence only, never account/risk state."""
        if payload is None:
            return
        try:
            rows = json.loads(payload)
            if not isinstance(rows, list) or len(rows) > 14:
                raise ValueError("invalid reward history")
            restored = []
            seen = set()
            for row in rows:
                day = date.fromisoformat(row["payout_date"])
                if day > now.astimezone(EASTERN).date() or day in seen:
                    raise ValueError("invalid reward history date")
                seen.add(day)
                raw = {
                    "payout_date": row["payout_date"], "payout_status": row["payout_status"],
                    "paid_at": row["paid_at"], "total_reward_usd": row["reported_total_reward_usd"],
                    "events": row["events"],
                }
                parse_summaries([raw], day, day)
                restored.extend(parse_reward_results([raw]))
            self.reward_results = sorted(restored, key=lambda r: r["payout_date"])
        except (ValueError, TypeError, KeyError, ArithmeticError):
            self.reward_results = []
            self.history_incomplete = True
            return
        self.history_incomplete = False

    def reward_history_payload(self) -> str:
        return json.dumps(self.reward_results, sort_keys=True)

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
                results = parse_reward_results(rows)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never expose response text, credentials, or exception payloads.
                self.error = "request_or_response_failed"
                return False
            self.rows = parsed
            history = {r["payout_date"]: r for r in self.reward_results}
            history.update({r["payout_date"]: r for r in results})
            self.reward_results = [history[key] for key in sorted(history)[-14:]]
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
