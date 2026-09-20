"""Offline estimator and funding-order regressions; no venue mutations."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from gemini_bot.domain import UptimeProgress, uptime_progress_key
from gemini_bot.qualification_estimator import estimate_50_percent, order_for_resting_focus


NOW = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def progress(minutes):
    return UptimeProgress(eligible_snapshots=1080, qualifying_snapshots=minutes,
                          remaining_snapshots_estimate=360,
                          window_start=START, window_end=END)


def history(count=700, total=1440):
    return [{'payout_date': '2026-09-19', 'payout_status': 'PAID',
             'paid_at': '2026-09-19T21:30:00+00:00',
             'events': [{'event_id': '123', 'snapshot_count': count,
                         'total_snapshots': total}]}]


def candidate(symbol, pool_id, event_id='123'):
    return SimpleNamespace(symbol=symbol, pool_id=pool_id, event_ticker=symbol,
                           event_id=event_id)


class EstimatorTests(unittest.TestCase):
    def test_paid_counts_are_reported_not_final_or_current_qualification(self):
        result = estimate_50_percent('123', progress(500), history(), now=NOW)
        reported = result['latest_exchange_report']
        local = result['local_eastern_day_scenario']
        self.assertEqual((reported['snapshot_count'], reported['total_snapshots']), (700, 1440))
        self.assertFalse(reported['meets_50_on_reported_counts'])
        self.assertFalse(reported['final_eligibility_confirmed'])
        self.assertEqual(local['additional_local_minutes_to_threshold'], 220)
        self.assertEqual(local['reference_source'], 'latest_reported_same_event_counts_not_final')
        self.assertFalse(local['venue_window_alignment_confirmed'])
        self.assertFalse(result['current_venue_qualification_confirmed'])
        self.assertFalse(result['used_as_order_permission'])

    def test_unknown_history_uses_only_local_window_and_never_claims_venue_50(self):
        result = estimate_50_percent(None, progress(720), [], now=NOW)
        self.assertIsNone(result['latest_exchange_report'])
        self.assertEqual(result['local_eastern_day_scenario']['reference_total_snapshots'], 1440)
        self.assertEqual(result['local_eastern_day_scenario']['additional_local_minutes_to_threshold'], 0)
        self.assertFalse(result['current_venue_qualification_confirmed'])

    def test_malformed_and_zero_denominator_reports_do_not_make_up_counts(self):
        for rows in (history(True), history(5, 0), history(5, 4),
                     history() * 2, [{'payout_date': 'bad', 'events': []}]):
            with self.subTest(rows=rows):
                self.assertIsNone(estimate_50_percent('123', None, rows, now=NOW)['latest_exchange_report'])

    def test_funding_order_keeps_existing_quotes_then_prioritizes_invested_time(self):
        a, b, c = (candidate('A', 'P1'), candidate('B', 'P2'), candidate('C', 'P3'))
        observations = {uptime_progress_key(x.event_ticker, x.pool_id): progress(minutes)
                        for x, minutes in ((a, 400), (b, 100), (c, 700))}
        selected = [a, b, c]
        result = order_for_resting_focus(selected, progress_by_key=observations,
                                         working_buy_counts={'B': 2},
                                         incumbent_symbols={'A', 'B'})
        self.assertEqual([x.symbol for x in result], ['B', 'A', 'C'])
        self.assertEqual([x.symbol for x in selected], ['A', 'B', 'C'])

    def test_invalid_local_window_has_no_priority_and_ties_retain_model_order(self):
        a, b = candidate('A', 'P1'), candidate('B', 'P2')
        observations = {uptime_progress_key('A', 'P1'): UptimeProgress(window_valid=False),
                        uptime_progress_key('B', 'P2'): progress(0)}
        result = order_for_resting_focus([b, a], progress_by_key=observations,
                                         working_buy_counts={}, incumbent_symbols=set())
        self.assertEqual([x.symbol for x in result], ['B', 'A'])

    def test_more_invested_incumbent_wins_when_both_need_funding(self):
        a, b = candidate('A', 'P1'), candidate('B', 'P2')
        observations = {uptime_progress_key('A', 'P1'): progress(400),
                        uptime_progress_key('B', 'P2'): progress(100)}
        result = order_for_resting_focus([b, a], progress_by_key=observations,
                                         working_buy_counts={}, incumbent_symbols={'A', 'B'})
        self.assertEqual([x.symbol for x in result], ['A', 'B'])


if __name__ == '__main__':
    unittest.main()
