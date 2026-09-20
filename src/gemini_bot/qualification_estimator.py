"""Fifty-percent evidence and safe resting-time funding order.

Gemini publishes completed per-event snapshot counts, but not the current
scoring-window start or a scored-through timestamp. Local Eastern-day samples
are therefore an explicitly separate scenario, never venue qualification or
an entry/exit permission.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from .domain import UptimeProgress, uptime_progress_key
from .reward_uptime_report import EASTERN, event_identity


def _reported_event(event_id: object, history: object, now: datetime) -> dict[str, Any] | None:
    identity = event_identity(event_id)
    if identity is None or not isinstance(history, list) or len(history) > 14:
        return None
    today = now.astimezone(EASTERN).date()
    found: list[tuple[date, dict[str, Any]]] = []
    for row in history:
        if not isinstance(row, dict):
            return None
        try:
            day = date.fromisoformat(row['payout_date'])
            if day.isoformat() != row['payout_date'] or day > today:
                return None
            status = row['payout_status']
            if status not in {'PAID', 'PENDING', 'BELOW_THRESHOLD', 'ZERO_AMOUNT', 'UNKNOWN'}:
                return None
            events = row['events']
            if not isinstance(events, list) or len(events) > 3000:
                return None
            matching = [e for e in events if isinstance(e, dict)
                        and event_identity(e.get('event_id')) == identity]
            if len(matching) > 1:
                return None
            if not matching:
                continue
            event = matching[0]
            count, total = event.get('snapshot_count'), event.get('total_snapshots')
            if (type(count) is not int or type(total) is not int
                    or not 0 <= count <= total or total == 0):
                return None
            found.append((day, {'scoring_date': day.isoformat(),
                                'snapshot_count': count, 'total_snapshots': total,
                                'meets_50_on_reported_counts': 2 * count >= total,
                                'payout_status': status,
                                'final_eligibility_confirmed': False,
                                'scored_through_at': None}))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
    if len({day for day, _ in found}) != len(found):
        return None
    return max(found, key=lambda item: item[0])[1] if found else None


def estimate_50_percent(
    event_id: object, progress: UptimeProgress | None, history: object, *, now: datetime,
) -> dict[str, Any]:
    """Expose completed venue truth beside an unaligned local-day scenario."""
    if now.tzinfo is None:
        raise ValueError('estimator time must be aware')
    identity = event_identity(event_id)
    reported = _reported_event(identity, history, now)
    local = None
    if progress is not None and progress.window_valid and progress.window_start and progress.window_end:
        # The local counter can be a useful investment signal, but Gemini has
        # not confirmed that this window is its own scoring interval.
        denominator = (reported['total_snapshots'] if reported is not None else
                       max(1, round((progress.window_end-progress.window_start).total_seconds()/60)))
        threshold = (denominator + 1) // 2
        local = {
            'window_start': progress.window_start.isoformat(),
            'window_end': progress.window_end.isoformat(),
            'qualifying_minute_samples': progress.qualifying_snapshots,
            'elapsed_minute_denominator_proxy': progress.eligible_snapshots,
            'reference_total_snapshots': denominator,
            'reference_source': ('latest_reported_same_event_counts_not_final' if reported is not None
                                 else 'local_window_duration_only'),
            'threshold_at_reference_total': threshold,
            'additional_local_minutes_to_threshold': max(0, threshold-progress.qualifying_snapshots),
            'best_possible_if_local_window_matched':
                progress.qualifying_snapshots+progress.remaining_snapshots_estimate >= threshold,
            'venue_window_alignment_confirmed': False,
            'current_venue_qualification_confirmed': False,
        }
    return {
        'event_id': identity, 'latest_exchange_report': reported,
        'local_eastern_day_scenario': local,
        'current_venue_qualification_confirmed': False,
        'used_as_order_permission': False,
    }


def order_for_resting_focus(
    selected: Iterable[Any], *, progress_by_key: dict[str, UptimeProgress],
    working_buy_counts: dict[str, int], incumbent_symbols: set[str],
) -> list[Any]:
    """Order already-safe owners for maintenance; never release or cancel one.

    Working entries go first, then an incumbent symbol before a new symbol
    that may merely share its event history. Among equally established owners,
    prefer more observed local event minutes. Model order breaks ties. This
    is not a score gate or venue-qualification claim.
    """
    candidates = list(selected)
    def key(index_and_candidate: tuple[int, Any]) -> tuple[int, int, int, int]:
        index, candidate = index_and_candidate
        progress = progress_by_key.get(uptime_progress_key(candidate.event_ticker, candidate.pool_id))
        local_minutes = progress.qualifying_snapshots if progress and progress.window_valid else 0
        return (-min(2, max(0, working_buy_counts.get(candidate.symbol, 0))),
                -int(candidate.symbol in incumbent_symbols), -local_minutes, index)
    return [candidate for _, candidate in sorted(enumerate(candidates), key=key)]
