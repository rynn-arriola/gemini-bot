"""Bounded event reward calibration for vacant-slot ordering.

Daily summaries expose an event's normalized score and qualifying snapshots but
not contract-level attribution.  Use that precise event evidence to correct the
live model's ordering, never to create quote permission or rewrite economics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal as D

from .reward_uptime_report import EASTERN, event_identity, parse_summaries, parse_reward_results

HISTORY_WEIGHT = D('.50')
LOCAL_UNREACHABLE_DISCOUNT = D('.50')
REGIME_START = date(2026, 9, 18)


@dataclass(frozen=True)
class EventEvidence:
    event_id: str
    reported_days: int
    qualified_days: int
    mean_reward_per_qualified_day: D
    mean_normalized_score_per_qualifying_snapshot: D
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
            normalized = event['normalized_score']
            if normalized is None:
                continue
            groups.setdefault(identity, []).append((
                day, weight, qualified, D(event['event_reward_usd']),
                D(normalized), event['snapshot_count'],
            ))
    result = {}
    for identity, samples in groups.items():
        # A missing week removes the prior, without pausing trading.
        if (today-max(s[0] for s in samples)).days > 7:
            continue
        qualifying = [s for s in samples if s[2] and s[5] > 0]
        total_weight = sum((s[1] for s in samples), D(0))
        qualified_weight = sum((s[1] for s in qualifying), D(0))
        mean = (sum((s[1]*s[3] for s in qualifying), D(0))/qualified_weight
                if qualified_weight else D(0))
        score_rate = (sum((s[1]*s[4]/D(s[5]) for s in qualifying), D(0))/qualified_weight
                      if qualified_weight else D(0))
        reliability = qualified_weight/total_weight if total_weight else D(0)
        # An unqualified report is still real qualification evidence.  It may
        # lower priority with bounded confidence, but never makes an event an
        # entry ban or a zero-dollar accounting fact.
        confidence = min(D(1), total_weight/D(2))
        result[identity] = EventEvidence(identity, len(samples), len(qualifying),
            mean, score_rate, reliability, score_rate*reliability, confidence)
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
        if observation is None:
            calibrated = model_normalized
        else:
            empirical = observation.preference/maximum if maximum > 0 else D(0)
            # Event evidence applies to the event as a whole. A positive uplift
            # cannot be collected repeatedly by adding sibling contracts.
            if any(event_identity(c.event_id) == event_identity(candidate.event_id)
                   for c in selected):
                empirical = min(empirical, model_normalized)
            empirical = model_normalized + observation.confidence*(empirical-model_normalized)
            calibrated = ((D(1)-HISTORY_WEIGHT)*model_normalized
                          + HISTORY_WEIGHT*empirical)
        # The local window is not venue truth and therefore cannot reject an
        # entry.  It can, however, haircut a vacant slot whose best local
        # scenario is already below the published threshold.
        if (candidate.best_possible_uptime is not None
                and candidate.best_possible_uptime < D('.50')):
            calibrated *= LOCAL_UNREACHABLE_DISCOUNT
        return calibrated

    def report(self):
        return {
            'version': 2, 'model_weight': '.50', 'history_weight': '.50',
            'history_confidence_shrunk': True,
            'scope': 'vacant_released_slots_only_healthy_incumbents_preserved',
            'history_basis': 'normalized_score_per_qualifying_snapshot_times_qualification_reliability',
            'local_below_50_percent_discount': str(LOCAL_UNREACHABLE_DISCOUNT),
            'local_discount_is_not_an_entry_gate': True,
            'forecast_dollars_calibrated': False,
            'future_payout_or_qualification_guaranteed': False,
            'contract_attribution_available': False,
            'error': self.error,
            'events': {key: {
                'reported_days': e.reported_days, 'qualified_days': e.qualified_days,
                'historical_mean_reward_per_qualified_day_usd': str(e.mean_reward_per_qualified_day),
                'historical_mean_normalized_score_per_qualifying_snapshot': str(
                    e.mean_normalized_score_per_qualifying_snapshot),
                'qualification_reliability': str(e.qualification_reliability),
                'confidence': str(e.confidence), 'preference_proxy': str(e.preference),
            } for key, e in self.evidence.items()},
        }
