import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal as D

from gemini_bot.review_adaptive_queue import archive_rows, review


def row(minute=0, flow='0', depth='100', depleted='0', full=True, delay='.1'):
    return {'observed_at': f'2026-09-18T01:{minute:02d}:00+00:00', 'queue': {
        'cancel_measurement': {'maximum_seconds': delay, 'failed_or_interrupted_calls': 0},
        'markets': [{'symbol': 'X', 'availability': 'scenario', 'full_flow_window_observed': full,
            'yes_sell_quantity_60s': flow, 'no_sell_quantity_60s': '0', 'outcomes': {
                o: {'external_depth': depth, 'depth_depletion': depleted, 'joined_at': 'same-anchor'}
                for o in ('YES', 'NO')}}]}}


def run(rows, **kwargs):
    return review(rows, **{**dict(buffer=D(15), burst_multiplier=D(1), depth_multiplier=D(2),
                                cancel_budget=D(5)), **kwargs})


class ArchiveReviewTests(unittest.TestCase):
    def test_peak_depletion_and_flow_are_explicit_stress_not_replay(self):
        result = run([row(), row(1, flow='10', depleted='20')])
        m = result['markets'][0]
        self.assertEqual(m['stress_required_depth'], {'YES': '90', 'NO': '70'})
        self.assertEqual(m['depth_pass_rows_under_full_sample_stress'], 2)
        self.assertFalse(result['live_activation_authorized'])
        self.assertFalse(result['evidence_sufficiency_verified'])
        self.assertIn('not_execution_replay', result['mode'])

    def test_incomplete_and_uncertain_rows_not_counted(self):
        uncertain = row(2); uncertain['queue']['markets'][0]['trade_time_uncertain'] = True
        r = run([row(), row(1, full=False), uncertain])
        self.assertEqual(r['markets'][0]['complete_rows'], 1)
        self.assertEqual(r['markets'][0]['unavailable_rows'], 2)

    def test_cancel_tail_not_latest_rolling_max(self):
        r = run([row(delay='4.9488708'), row(1, delay='.2')], cancel_budget=D(1))
        self.assertEqual(r['cancel_maximum_seconds'], '4.9488708')
        self.assertFalse(r['cancel_envelope_pass'])
        self.assertEqual(r['cancel_maximum_first_archive_at'], row()['observed_at'])

    def test_failures_and_empty_archive_never_pass(self):
        r = row(); r['queue']['cancel_measurement']['failed_or_interrupted_calls'] = 1
        self.assertFalse(run([r])['cancel_envelope_pass'])
        self.assertFalse(run([])['cancel_envelope_pass'])

    def test_chronology_and_nonfinite_rejected(self):
        for rows in ([row(2), row(1)], [row(depth='NaN')], [row(flow='-1')]):
            with self.assertRaises(ValueError): run(rows)

    def test_archive_errors_are_not_clean_evidence(self):
        r = row(); r['queue']['error_type'] = 'ValueError'
        with self.assertRaises(ValueError): run([r])

    def test_archive_rotation_duplicates_and_partial_tail(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p/'queue-shadow.jsonl.1').write_text(json.dumps(row())+'\n')
            (p/'queue-shadow.jsonl').write_text(json.dumps(row())+'\n'+json.dumps(row(1))+'\n'+ '{')
            (p/'credentials.txt').write_text('must never be read')
            self.assertEqual(len(list(archive_rows(p))), 2)

    def test_join_reset_does_not_inherit_depletion(self):
        a, b = row(depleted='500'), row(1, depleted='1000')
        b['queue']['markets'][0]['outcomes']['YES']['joined_at'] = 'new-anchor'
        r = run([a, b])
        self.assertEqual(r['markets'][0]['peak_archived_interval_depletion']['YES'], '0')

    def test_missing_cancellation_samples_not_evidence(self):
        r = run([row(delay=None)])
        self.assertFalse(r['cancel_envelope_pass'])
