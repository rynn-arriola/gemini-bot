"""Entry-quality safety is offline-tested; no venue mutations are made."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import test_repair_integration as repair_fixtures
from test_repair_integration import NOW, candidate
from gemini_bot.account_state import AccountReducer
from gemini_bot.config import DEFAULT_POLICY_PATH, ConfigurationError, load_policy
from gemini_bot.domain import Outcome, QuoteLeg, QuotePair, Side
from gemini_bot.entry_quality import EntryQualityPolicy, evaluate_entry_quality
from gemini_bot.live_ws import QuoteChangedBeforeSend
from gemini_bot.market_state import LocalBook
from gemini_bot.exit_pricing import exact_crossing_exit
from gemini_bot.uptime import quote_cutoff


POLICY = EntryQualityPolicy(D('.70'), D('.70'), D('.50'), 900)


def pair(quantity=D(30), yes=D('.49'), no=D('.49')):
    return QuotePair(
        QuoteLeg('X', Outcome.YES, Side.BUY, yes, quantity),
        QuoteLeg('X', Outcome.NO, Side.BUY, no, quantity),
    )


def depth(bid=D('.49'), ask=D('.51'), bid_qty=D(100), ask_qty=D(100)):
    value = LocalBook('X')
    value.apply_snapshot([(bid, bid_qty)], [(ask, ask_qty)], 1, 1, NOW)
    return value


class EntryQualityFormulaTests(unittest.TestCase):
    def test_both_sides_pass_stressed_depth_and_value(self):
        result = evaluate_entry_quality(depth(), pair(), POLICY)
        self.assertTrue(result.allowed)
        self.assertEqual(result.yes.covered_quantity, D(30))
        self.assertEqual(result.no.covered_quantity, D(30))
        self.assertGreater(result.no.retained_value_fraction, D('.70'))

    def test_quantity_only_is_insufficient_when_bid_value_collapses(self):
        result = evaluate_entry_quality(depth(bid=D('.20'), ask=D('.80')), pair(), POLICY)
        self.assertEqual(result.yes.depth_fraction, D(1))
        self.assertEqual(result.no.depth_fraction, D(1))
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'exit_value_below_minimum')

    def test_insufficient_haircut_depth_fails_only_this_pair(self):
        result = evaluate_entry_quality(depth(bid_qty=D(40)), pair(), POLICY)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'exit_depth_below_minimum')
        self.assertEqual(result.yes.depth_fraction, D(2) / D(3))

    def test_own_working_bid_does_not_count_as_exit_buyer(self):
        account = AccountReducer()
        account.apply_order_update({
            'e': 'orderUpdate', 'i': 'own', 'c': 'own-client', 's': 'X',
            'S': 'BUY', 'O': 'YES', 'p': '.49', 'q': '30', 'z': '30', 'X': 'NEW',
        })
        value = depth(bid_qty=D(60))
        original = dict(value.bids)
        result = evaluate_entry_quality(value, pair(), POLICY, account=account)
        self.assertEqual(result.yes.covered_quantity, D(15))
        self.assertFalse(result.allowed)
        self.assertEqual(value.bids, original)

    def test_no_exit_values_complementary_yes_ask_and_fee(self):
        result = evaluate_entry_quality(depth(bid=D('.49'), ask=D('.61')), pair(), POLICY)
        self.assertEqual(result.no.covered_quantity, D(30))
        self.assertLess(result.no.retained_value_fraction, D(1))
        self.assertGreater(result.no.retained_value_fraction, D('.70'))

    def test_absent_book_blocks_entry_without_touching_exit_code(self):
        value = depth()
        value.synchronized = False
        self.assertEqual(evaluate_entry_quality(value, pair(), POLICY).reason, 'exit_book_unavailable')

    def test_existing_position_reduction_does_not_consult_entry_gate(self):
        thin = depth(bid_qty=D(1), ask_qty=D(1))
        self.assertFalse(evaluate_entry_quality(thin, pair(), POLICY).allowed)
        self.assertIsNotNone(exact_crossing_exit(thin, D(30)))
        self.assertIsNotNone(exact_crossing_exit(thin, D(-30)))


class EntryQualityPolicyTests(unittest.TestCase):
    def test_v8_policy_is_explicit_and_legacy_v7_has_no_gate(self):
        strategy, risk = load_policy()
        self.assertEqual(strategy.entry_quality, POLICY)
        self.assertEqual(risk.force_exit_before_expiry_seconds, 300)
        raw = json.loads(DEFAULT_POLICY_PATH.read_text())
        raw['policy_version'] = 7
        raw.pop('entry_quality')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.json'
            path.write_text(json.dumps(raw))
            self.assertIsNone(load_policy(path)[0].entry_quality)
            raw['entry_quality'] = {'minimum_exit_depth_fraction': '0.70'}
            path.write_text(json.dumps(raw))
            with self.assertRaises(ConfigurationError):
                load_policy(path)

class EntryQualityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = repair_fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.runtime = self.fixture.runtime
        self.runtime.config = replace(self.runtime.config, strategy=replace(
            self.runtime.config.strategy, quote_quantity=D(30), entry_quality=POLICY,
        ))

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_crypto_quote_window_ends_before_non_crypto_window(self):
        contract = self.runtime.universe[0]
        expiry = NOW + timedelta(hours=1)
        crypto = replace(contract, event_category='Crypto', expiry=expiry.isoformat())
        politics = replace(crypto, event_category='Politics')
        self.assertEqual(
            quote_cutoff(crypto, expiry, crypto_entry_before_expiry_seconds=900),
            expiry - timedelta(minutes=15),
        )
        self.assertEqual(
            quote_cutoff(politics, expiry, crypto_entry_before_expiry_seconds=900),
            expiry - timedelta(minutes=5),
        )

    async def test_market_local_gate_and_other_market_remains_actionable(self):
        first = candidate(D(30))
        self.assertTrue(self.runtime._candidate_actionable(first))
        self.runtime.public.books['X'] = depth(bid=D('.48'), ask=D('.52'), bid_qty=D(20))
        self.assertFalse(self.runtime._candidate_actionable(first))
        self.assertEqual(self.runtime._owner_failure_reason('X'), 'entry_quality:exit_depth_below_minimum')
        self.assertTrue(self.runtime.gate.may_submit)

    async def test_final_maker_send_rechecks_book_after_pacing(self):
        payload = self.fixture.payload()
        self.runtime._validate_send(payload)
        self.runtime.public.books['X'] = depth(bid=D('.48'), ask=D('.52'), bid_qty=D(20))
        with self.assertRaisesRegex(QuoteChangedBeforeSend, 'entry_quality'):
            self.runtime._validate_send(payload)
        self.assertTrue(self.runtime.gate.may_submit)

    async def test_crypto_cutoff_blocks_new_entry_fifteen_minutes_before_expiry_only(self):
        contract = self.runtime.universe[0]
        self.runtime.universe = [replace(contract, event_category='Crypto')]
        self.runtime._expiry['X'] = NOW + timedelta(minutes=14, seconds=59)
        self.assertFalse(self.runtime._entry_time_valid('X'))
        self.runtime._expiry['X'] = NOW + timedelta(minutes=15, seconds=1)
        self.assertTrue(self.runtime._entry_time_valid('X'))
        self.runtime.universe = [replace(contract, event_category='Politics')]
        self.runtime._expiry['X'] = NOW + timedelta(minutes=10)
        self.assertTrue(self.runtime._entry_time_valid('X'))

    async def test_gate_failure_cancels_only_affected_resting_market(self):
        current = candidate(D(30))
        self.runtime.account.reducer.apply_order_update({
            'e': 'orderUpdate', 'i': 'owned-yes', 'c': 'owned-client', 's': 'X',
            'S': 'BUY', 'O': 'YES', 'p': '.46', 'q': '30', 'z': '30', 'X': 'NEW',
        })
        self.runtime.public.books['X'] = depth(bid=D('.48'), ask=D('.52'), bid_qty=D(20))
        self.runtime._cancel_symbol_entries = AsyncMock()
        await self.runtime._cancel_ineligible_working_entries({'X': current})
        self.runtime._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertTrue(self.runtime.gate.may_submit)

    async def test_report_exposes_point_in_time_gate_count(self):
        self.runtime.ranked = [candidate(D(30))]
        self.runtime._write_rank_report(NOW)
        report = json.loads((self.runtime.directory / 'rank-report.json').read_text())
        self.assertEqual(report['entry_quality_summary']['ranked_passed'], 1)
        self.assertEqual(report['entry_quality_summary']['ranked_considered'], 1)
        self.assertTrue(report['candidates'][0]['entry_quality']['passed'])
        self.assertIsNone(report['candidates'][0]['qualification_estimate']['local_eastern_day_scenario'])
