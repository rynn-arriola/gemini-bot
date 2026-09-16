"""Optional REST valuation must never erase or block known inventory."""
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from gemini_bot.domain import Outcome
from gemini_bot.storage import SQLiteJournal
from gemini_bot.reconciliation import (
    build_account_snapshot, parse_positions, exposure_from_snapshot,
    assert_position_consistency, assert_stable_account_bracket,
)
import test_fill_continuity as flow


def position(**changes):
    return dict(symbol='X', outcome='no', totalQuantity='1.01', avgPrice='.46', **changes)


class OptionalValuationTests(unittest.TestCase):
    def test_absent_or_null_value_preserves_exact_position_and_full_basis(self):
        for row in (position(), position(marketValue=None)):
            snapshot = build_account_snapshot([row], [])
            self.assertEqual(snapshot.signed_positions, {'X': D('-1.01')})
            self.assertIsNone(snapshot.positions[0].market_value)
            self.assertIsNone(snapshot.position_market_value)
            exposure = exposure_from_snapshot(snapshot)
            self.assertEqual(exposure.gross_reservation_usd, D('.4646'))
            self.assertEqual(exposure.tracked_markets, 1)
            assert_position_consistency(snapshot, {'X': D('-1.01')})

    def test_unavailable_is_not_zero_or_last_trade_price(self):
        row = position(prices={'lastTradePrice': '.99'})
        parsed = parse_positions([row])[0]
        self.assertEqual(parsed.as_storage()['market_value'], 'unavailable')
        zero = build_account_snapshot([position(marketValue='0')], [])
        self.assertEqual(zero.position_market_value, D(0))
        self.assertEqual(zero.positions[0].as_storage()['market_value'], '0')
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(Path(directory) / 'snapshot.sqlite')
            try:
                journal.replace_account_snapshot([parsed.as_storage()], [], observed_at=flow.NOW)
                self.assertEqual(journal.load_position_quantities(), {'X': D('-1.01')})
                self.assertEqual(journal.connection.execute(
                    'SELECT market_value FROM account_position').fetchone()[0], 'unavailable')
            finally:
                journal.close()

    def test_liquidity_change_is_not_inventory_change(self):
        before = build_account_snapshot([position()], [])
        after = build_account_snapshot([position(marketValue='.03')], [])
        assert_stable_account_bracket(before, after)
        self.assertEqual(after.position_market_value, D('.03'))

    def test_empty_snapshot_is_flat_but_unknown_mark_is_not(self):
        empty = build_account_snapshot([], [])
        self.assertEqual(empty.position_market_value, D(0))
        held = build_account_snapshot([position()], [])
        with self.assertRaises(ValueError):
            assert_position_consistency(held, {})

    def test_malformed_and_nonfinite_numbers_remain_invalid(self):
        for value in ('', 'garbage', 'NaN', 'Infinity', '-Infinity', '-.01'):
            with self.subTest(value=value), self.assertRaises((ValueError, InvalidOperation)):
                parse_positions([position(marketValue=value)])
        for field in ('totalQuantity', 'avgPrice'):
            for value in ('NaN', 'Infinity', '-Infinity'):
                row = position()
                row[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    parse_positions([row])


class NoBuyerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = flow.FillContinuityTests()
        await self.fixture.asyncSetUp()
        self.fixture.inventory(outcome=Outcome.NO, quantity=D('1.01'))
        self.r = self.fixture.r
        self.fixture.book()
        self.r.public.books['X'].asks.clear()
        self.r.rest.positions = AsyncMock(return_value=[position()])

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_startup_hands_missing_valuation_to_same_process_no_buyer_wait(self):
        r = self.r
        deadline = r.journal.get_deadline('X', 'NO')
        r._last_snapshot = build_account_snapshot([position()], [])
        await r._prepare_activation_inventory()
        await r._run_exit_worker('X')
        self.assertEqual(r._exit_waits['X'].reason, 'exit_depth_unavailable')
        self.assertEqual(r.journal.get_deadline('X', 'NO'), deadline)
        self.assertIn('X', r.account.fill_blocked_entries)
        self.assertEqual(r.account.reducer.positions, {'X': D('-1.01')})
        r.private.exit_position.assert_not_awaited()
        self.assertTrue(r.gate.may_submit)
        # Natural liquidity return restores the existing exact-exit path;
        # no restart, deadline reset or replacement position is required.
        self.fixture.book()
        r.rest.positions = AsyncMock(side_effect=lambda: (
            [position(marketValue='.03')] if r.account.reducer.positions else []))
        async def exit_now(leg, **kwargs):
            self.assertEqual(leg.quantity, D('1.01'))
            self.assertEqual(leg.outcome, Outcome.NO)
            return self.fixture.order(leg)
        r.private.exit_position.side_effect = exit_now
        await r._run_exit_worker('X')
        self.assertFalse(r.account.reducer.positions)
        self.assertNotIn('X', r.account.blocked_entries)
        self.assertIsNone(r.journal.get_deadline('X', 'NO'))

    async def test_missing_quote_does_not_bypass_cash_or_external_order_check(self):
        r = self.r
        r.account.expected_confirmed_usd -= D('.0001')
        with self.assertRaisesRegex(RuntimeError, 'exactly reconciled'):
            await r._prepare_activation_inventory()
        r.account.expected_confirmed_usd = r.account.confirmed_usd
        r.rest.active_orders.return_value = [dict(
            orderId='manual', clientOrderId='not-owned', symbol='X', side='sell',
            outcome='no', price='.02', quantity='1.01', remainingQuantity='1.01', status='open',
        )]
        with self.assertRaisesRegex(RuntimeError, 'changed during authenticated proof'):
            await r._prepare_activation_inventory()
        r.private.exit_position.assert_not_awaited()
