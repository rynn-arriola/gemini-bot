"""No network, active policy, permit, real account or trading process touched."""
import policy_v8  # noqa: F401  (pins the v8-era default policy)
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gemini_bot.config import DEFAULT_POLICY_PATH, ConfigurationError, load_policy
from gemini_bot import stage_queue_pilot as staging
from test_queue_pilot import policy, NOW


def review():
    calibration = {k: v.isoformat() if hasattr(v, 'isoformat') else str(v)
                   for k, v in asdict(policy().markets[0][1]).items()}
    return {'reviewed_at': NOW.isoformat(), 'incumbent_symbols': ['X','Y','OTHER'],
            'queue_pilot': {'markets': [dict(symbol=s, YES=dict(calibration), NO=dict(calibration))
                                      for s in ('X','Y')]}}


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / 'config' / 'live_policy.json'
        self.base.parent.mkdir()
        baseline = json.loads(DEFAULT_POLICY_PATH.read_text())
        baseline.pop('queue_pilot', None)
        baseline.pop('entry_quality', None)
        baseline['policy_version'] = 2
        baseline['quote_offset_ticks'] = 1
        self.baseline = json.dumps(baseline).encode()
        self.base.write_bytes(self.baseline)
        self.source = self.root / 'review.json'
        self.source.write_text(json.dumps(review()))
        self.destination = patch.object(staging, 'DEFAULT_POLICY_PATH', self.base)
        self.destination.start()

    def tearDown(self):
        self.assertEqual(self.base.read_bytes(), self.baseline)
        self.destination.stop()
        self.tmp.cleanup()

    def build(self, document=None):
        return staging.build_candidate(self.base, review() if document is None else document, now=NOW)

    def test_candidate_changes_only_pilot_and_version(self):
        candidate = self.build()
        base = json.loads(self.baseline)
        self.assertEqual(candidate['quote_offset_ticks'], 1)
        self.assertEqual({k:v for k,v in candidate.items() if k != 'queue_pilot'}, {**base,'policy_version':3})

    def test_draft_roundtrip_keeps_one_tick_and_all_risk(self):
        output = staging.stage_candidate(self.base,self.source,draft_name='reviewed',now=NOW)
        self.assertEqual(output.parent, self.root / 'data' / 'queue-pilot-drafts')
        strategy, risk = load_policy(output)
        self.assertEqual(strategy.quote_offset_ticks, 1)
        self.assertEqual(strategy.queue_pilot.symbols, {'X','Y'})
        self.assertEqual(risk, load_policy(self.base)[1])

    def test_old_two_tick_nonpilot_compatibility_and_reject_zero_or_three(self):
        strategy,_ = load_policy(self.base)
        for offset in (1,2):
            replace(strategy,quote_offset_ticks=offset,queue_pilot=policy()).validate()
        for offset in (0,3):
            with self.assertRaises(ConfigurationError):
                replace(strategy,quote_offset_ticks=offset,queue_pilot=policy()).validate()

    def test_disabled_policy_stays_disabled(self):
        self.assertIsNone(load_policy(self.base)[0].queue_pilot)
        self.build()
        self.assertIsNone(load_policy(self.base)[0].queue_pilot)

    def test_expired_either_outcome_rejected(self):
        for outcome in ('YES','NO'):
            value=review()
            value['queue_pilot']['markets'][1][outcome]['expires_at']=NOW.isoformat()
            with self.assertRaises(ValueError): self.build(value)

    def test_future_or_naive_review_rejected(self):
        for stamp in ((NOW+timedelta(seconds=1)).isoformat(), NOW.replace(tzinfo=None).isoformat()):
            value=review(); value['reviewed_at']=stamp
            with self.assertRaises(ValueError): self.build(value)

    def test_unreviewed_boolean_or_missing_side_rejected(self):
        for pilot in ({'enabled':True}, None, {'markets':[{'symbol':'X','YES':{}}]}):
            value=review(); value['queue_pilot']=pilot
            with self.assertRaises((ValueError,TypeError)): self.build(value)

    def test_no_defaults_for_empty_reference_or_threshold(self):
        for key, invalid in [('evidence_reference',' '),('minimum_depth','0'),('maximum_sell_flow_60s','NaN')]:
            value=review(); value['queue_pilot']['markets'][0]['YES'][key]=invalid
            with self.assertRaises((ValueError,ArithmeticError)): self.build(value)

    def test_only_two_existing_distinct_symbols(self):
        for names in (['X','OTHER'], ['X','X'], ['x','Y']):
            value=review(); value['incumbent_symbols']=names
            with self.assertRaises(ValueError): self.build(value)
        value=review(); value['queue_pilot']['markets'].append(deepcopy(value['queue_pilot']['markets'][0]))
        with self.assertRaises(ValueError): self.build(value)

    def test_no_overwrite_or_path_traversal(self):
        output=staging.stage_candidate(self.base,self.source,draft_name='reviewed',now=NOW)
        before=output.read_bytes()
        with self.assertRaises(FileExistsError):
            staging.stage_candidate(self.base,self.source,draft_name='reviewed',now=NOW)
        self.assertEqual(output.read_bytes(), before)
        for name in ('../live_policy','..\\live_policy','C:\\live_policy','', 'x.json'):
            with self.assertRaises(ValueError):
                staging.stage_candidate(self.base,self.source,draft_name=name,now=NOW)

    def test_duplicate_fields_and_oversized_input_rejected(self):
        self.source.write_text('{"a":1,"a":2}')
        with self.assertRaises(ValueError): staging.read_document(self.source)
        self.source.write_text(' '*(1024*1024+1))
        with self.assertRaises(ValueError): staging.read_document(self.source)

    def test_cli_reports_no_live_or_evidence_authority(self):
        with patch.object(staging,'datetime') as clock, redirect_stdout(io.StringIO()) as output:
            clock.now.return_value=NOW
            clock.fromisoformat.side_effect=type(NOW).fromisoformat
            result=staging.main(['--base-policy',str(self.base),'--review',str(self.source),'--draft-name','cli'])
        self.assertEqual(result,0)
        status=json.loads(output.getvalue())
        self.assertFalse(status['live_activation_authorized'])
        self.assertFalse(status['evidence_sufficiency_verified'])
        self.assertFalse(status['active_policy_changed'])
