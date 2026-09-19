"""Bounded event reward history for vacant-slot ordering, never quote permission.

Event payouts lack contract/capital attribution. Blend dimensionless preferences,
not dollars from incompatible horizons. All execution/economic gates stay in the
selector. Unknown history is neutral and no learned value is a future payout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal as D

from .reward_uptime_report import EASTERN, event_identity, parse_summaries, parse_reward_results

HISTORY_WEIGHT = D('.30')
REGIME_START = date(2026, 9, 18)


@dataclass(frozen=True)
class EventEvidence:
    event_id: str
    reported_days: int
    qualified_days: int
    mean_reward_per_qualified_day: D
    qualification_reliability: D
    preference: D
    confidence: D


def build_evidence(history: list[dict], *, now: datetime) -> dict[str, EventEvidence]:
    if now.tzinfo is None or not isinstance(history, list) or len(history) > 14:
        raise ValueError('invalid reward history boundary')
    today = now.astimezone(EASTERN).date()
    days = set()
    groups = {}
    for row in history:
        day = date.fromisoformat(row['payout_date'])
        if day in days or day > today:
            raise ValueError('duplicate or future reward date')
        days.add(day)
        raw = dict(payout_date=row['payout_date'], payout_status=row['payout_status'],
                   paid_at=row['paid_at'], total_reward_usd=row['reported_total_reward_usd'],
                   events=row['events'])
        parse_summaries([raw], day, day)
        parsed = parse_reward_results([raw])[0]
        age = (today-day).days
        if age > 13:
            continue
        status = parsed['payout_status']
        if status == 'PAID':
            paid_at = parsed['paid_at']
            if paid_at is None or datetime.fromisoformat(paid_at) > now:
                continue
        elif status not in {'BELOW_THRESHOLD', 'ZERO_AMOUNT'} or day >= today:
            continue
        if parsed['reported_total_reward_usd'] is None:
            continue
        # Reviewed recency weighting; older quote regimes have lower influence.
        weight = D(1) / D(1+age)
        if day < REGIME_START:
            weight *= D('.25')
        for event in parsed['events']:
            if event['total_snapshots'] <= 0 or event['event_reward_usd'] is None:
                continue
            identity = event['event_id']
            qualified = 2*event['snapshot_count'] >= event['total_snapshots']
            groups.setdefault(identity, []).append((day, weight, qualified, D(event['event_reward_usd'])))
    result = {}
    for identity, samples in groups.items():
        # A missing week removes the prior, without pausing trading.
        if (today-max(s[0] for s in samples)).days > 7:
            continue
        qualifying = [s for s in samples if s[2]]
        if not qualifying:
            continue  # No qualified reward observation is not a zero-quality market.
        total_weight = sum((s[1] for s in samples), D(0))
        qualified_weight = sum((s[1] for s in qualifying), D(0))
        mean = sum((s[1]*s[3] for s in qualifying), D(0))/qualified_weight
        reliability = qualified_weight/total_weight
        confidence = min(D(1), qualified_weight/D(2))
        result[identity] = EventEvidence(identity, len(samples), len(qualifying),
            mean, reliability, mean*reliability, confidence)
    return result


class RewardFocus:
    def __init__(self):
        self.evidence = {}
        self.error = None
        self._history = None
        self._day = None

    def update(self, history, *, now):
        day = now.astimezone(EASTERN).date()
        if history is self._history and day == self._day:
            return
        self._history, self._day = history, day
        try:
            self.evidence = build_evidence(history, now=now)
            self.error = None
        except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError):
            self.evidence = {}
            self.error = 'history_unavailable'

    def score(self, candidate, model_normalized, selected):
        """Only call for candidates already passing marginal and safety gates."""
        observation = self.evidence.get(event_identity(candidate.event_id))
        maximum = max((e.preference for e in self.evidence.values()), default=D(0))
        if observation is None or maximum <= 0:
            return model_normalized
        empirical = observation.preference/maximum
        # The event's measured payout cannot be earned again by another sibling.
        # Keep negative evidence, but grant a positive event uplift only once.
        if any(event_identity(c.event_id) == event_identity(candidate.event_id) for c in selected):
            empirical = min(empirical, model_normalized)
        empirical = model_normalized + observation.confidence*(empirical-model_normalized)
        return (D(1)-HISTORY_WEIGHT)*model_normalized + HISTORY_WEIGHT*empirical

    def report(self):
        return {
            'version': 1, 'model_weight': '.70', 'history_weight': '.30',
            'history_confidence_shrunk': True,
            'scope': 'vacant_released_slots_only_healthy_incumbents_preserved',
            'forecast_dollars_calibrated': False,
            'future_payout_or_qualification_guaranteed': False,
            'contract_attribution_available': False,
            'error': self.error,
            'events': {key: {
                'reported_days': e.reported_days, 'qualified_days': e.qualified_days,
                'historical_mean_reward_per_qualified_day_usd': str(e.mean_reward_per_qualified_day),
                'qualification_reliability': str(e.qualification_reliability),
                'confidence': str(e.confidence), 'preference_proxy': str(e.preference),
            } for key, e in self.evidence.items()},
        }
