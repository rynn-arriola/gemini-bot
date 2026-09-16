from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.cash_exception import (
    EXPIRES, apply_approval, apply_with_fresh_proof, confirmed_usd, record_approval,
)
from gemini_bot.execution import IntentLedger
from gemini_bot.storage import SQLiteJournal


NOW = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
IDENTITY = hashlib.sha256(b'synthetic-key').hexdigest()


class CashExceptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite'
        self.journal = SQLiteJournal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.journal.set_runtime_state('credential_identity', IDENTITY, updated_at=NOW)
        self.journal.set_runtime_state('expected_confirmed_usd', '100.123', updated_at=NOW)
        self.journal.set_runtime_state('expected_positions', '{}', updated_at=NOW)
        self.journal.set_runtime_state('cash_reconciliation_watermark', 'verified', updated_at=NOW)
        self.journal.connection.execute(
            "INSERT INTO realized_risk_window_state VALUES (?,?,?,?,?,?,?)",
            ('window', 'end', '-31', '31', 0, 1, NOW.isoformat()))

    def approve(self, **changes):
        args = dict(identity=IDENTITY, observed_cash=D('100.12'), positions={}, now=NOW)
        args.update(changes)
        record_approval(self.journal, **args)

    def apply(self, **changes):
        args = dict(identity=IDENTITY, observed_cash=D('100.12'), now=NOW)
        args.update(changes)
        apply_approval(self.journal, **args)

    def protected_state(self):
        return {table: self.journal.connection.execute(f'SELECT * FROM {table}').fetchall()
                for table in ('execution_fill', 'position_deadline', 'risk_window_state',
                              'realized_risk_window_state', 'authenticated_cash_adjustment',
                              'order_intent', 'reward_uptime_v2')}

    def test_once_preserves_history_and_watermark_across_restart(self):
        self.journal.set_runtime_state('expected_positions', '{"TEST":"-1.01"}', updated_at=NOW)
        self.journal.connection.execute('INSERT INTO position_deadline VALUES (?,?,?,?,?)',
                                        ('TEST', 'NO', NOW.isoformat(), NOW.isoformat(), '1.01'))
        self.journal.connection.execute('INSERT INTO execution_fill VALUES (?,?,?,?,?,?,?,?,?,?)',
                                        ('trade', 'order', 'client', 'TEST', 'BUY', 'NO', '1.01', '.2', '.01', NOW.isoformat()))
        before = self.protected_state()
        watermark = self.journal.get_runtime_state_record('cash_reconciliation_watermark')
        self.approve(positions={'TEST': D('-1.01')})
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '100.123')
        self.journal.close()
        self.journal = SQLiteJournal(self.path)
        self.apply()
        self.assertEqual(self.protected_state(), before)
        self.assertEqual(self.journal.get_runtime_state_record('cash_reconciliation_watermark'), watermark)
        self.assertEqual(self.journal.get_runtime_state('expected_positions'), '{"TEST":"-1.01"}')
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '100.12')
        with self.assertRaises(RuntimeError): self.apply()
        with self.assertRaises(RuntimeError): self.approve()
        audit = str(self.journal.connection.execute('SELECT details_json FROM audit_event').fetchall())
        self.assertNotIn('100.12', audit)

    def test_rejects_missing_approval_and_duplicate_binding(self):
        with self.assertRaises(RuntimeError): self.apply()
        self.approve()
        with self.assertRaises(RuntimeError): self.approve(observed_cash=D('100.124'))

    def test_rejects_invalid_or_out_of_scope_amounts(self):
        for cash in ('100.123', '100.133', '99', 'NaN', 'Infinity', '-1'):
            with self.subTest(cash=cash), self.assertRaises(RuntimeError):
                self.approve(observed_cash=D(cash))

    def test_signed_ledger_cash_keeps_exact_delta_without_new_credit_authority(self):
        self.journal.set_runtime_state('expected_confirmed_usd', '-.123', updated_at=NOW)
        self.approve(observed_cash=D('-.12'))
        self.apply(observed_cash=D('-.12'))
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '-0.12')
        self.assertEqual(self.journal.connection.execute(
            'SELECT loss_high_water,pending_order_cap FROM realized_risk_window_state').fetchone(), ('31', 0))

    def test_rejects_account_position_and_date_mismatches(self):
        for changes in (dict(identity='other'), dict(positions={'TEST': D(1)}),
                        dict(now=EXPIRES), dict(now=NOW.replace(tzinfo=None))):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError): self.approve(**changes)

    def test_exact_observed_cash_and_expiry_enforced_at_consumption(self):
        self.approve()
        for changes in (dict(observed_cash=D('100.121')), dict(identity='other'), dict(now=EXPIRES)):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError): self.apply(**changes)
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '100.123')

    def test_exact_distinct_rest_and_ws_surfaces_are_bound_once(self):
        self.approve()
        self.apply(ws_cash=D('100.121'))
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '100.121')
        evidence = json.loads(self.journal.get_runtime_state('one_time_cash_exception_20260916_surfaces'))
        self.assertEqual(evidence['rest_confirmed'], '100.12')
        self.assertEqual(evidence['ws_confirmed'], '100.121')
        with self.assertRaises(RuntimeError): self.apply(ws_cash=D('100.122'))

    def test_surface_difference_must_remain_subcent(self):
        self.approve()
        with self.assertRaises(RuntimeError): self.apply(ws_cash=D('100.14'))

    def test_changed_history_cannot_be_forgiven(self):
        self.approve()
        self.journal.connection.execute("UPDATE realized_risk_window_state SET loss_high_water='32'")
        with self.assertRaises(RuntimeError): self.apply()

    def test_changed_cash_or_position_binding_refuses_even_same_delta(self):
        self.approve()
        self.journal.set_runtime_state('expected_confirmed_usd', '101.123', updated_at=NOW)
        with self.assertRaises(RuntimeError): self.apply(observed_cash=D('101.12'))

    def test_new_fill_invalidates_approval(self):
        self.approve()
        self.journal.connection.execute('INSERT INTO execution_fill VALUES (?,?,?,?,?,?,?,?,?,?)',
                                        ('new', 'o', 'c', 'TEST', 'BUY', 'NO', '1', '.2', '.01', NOW.isoformat()))
        with self.assertRaises(RuntimeError): self.apply()

    def test_nonterminal_intent_refuses_approval(self):
        self.journal.connection.execute('INSERT INTO order_intent VALUES (?,?,?,?,?,?,?,?,?,?)',
                                        ('c', 'TEST', 'YES', 'BUY', 'entry', '.1', '10', 'sent_unknown', None, NOW.isoformat()))
        with self.assertRaises(RuntimeError): self.approve()

    def test_atomic_rollback_if_audit_fails(self):
        self.approve()
        with patch.object(self.journal, 'audit', side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError): self.apply()
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '100.123')
        self.apply()  # Failed transaction consumed nothing.

    def test_future_discrepancy_still_blocks_runtime(self):
        self.approve()
        self.apply()
        account = AccountRuntime(IntentLedger(self.journal), self.journal)
        account.confirmed_usd = D('100.12')
        self.assertTrue(account.equity_classification_complete)
        account.confirmed_usd += D('.001')
        self.assertFalse(account.equity_classification_complete)
        with self.assertRaises(RuntimeError): self.apply(observed_cash=account.confirmed_usd)

    def test_balance_parser_requires_unique_finite_confirmed_amount(self):
        self.assertEqual(confirmed_usd([{'currency': 'USD', 'amount': '100.12', 'available': '20'}]), D('100.12'))
        for rows in ([], [{'currency': 'USD'}], [{'currency': 'USD', 'amount': 'NaN'}],
                     [{'currency': 'USD', 'amount': '1'}] * 2):
            with self.assertRaises(RuntimeError): confirmed_usd(rows)

    def runtime(self):
        account = AccountRuntime(IntentLedger(self.journal), self.journal)
        account.subscription_acknowledged = account.balances_seen = account.positions_seen = True
        account.last_balance_at = account.last_position_at = time.monotonic()
        account.confirmed_usd = D('100.12')
        return SimpleNamespace(
            read_only=True, _activation_consumed=False, lock=SimpleNamespace(_file=object()),
            account=account, journal=self.journal, config=SimpleNamespace(api_key='synthetic-key'), _tasks=[],
            rest=SimpleNamespace(positions=AsyncMock(return_value=[]), active_orders=AsyncMock(return_value=[]),
                                 account_balances=AsyncMock(return_value=[{'currency': 'USD', 'amount': '100.12'}])))

    async def test_fresh_order_free_proof_applies_without_activation(self):
        self.approve()
        runtime = self.runtime()
        with patch('gemini_bot.cash_exception.datetime') as clock:
            clock.now.return_value = NOW
            # Date constructor is also used to bound the incident date.
            clock.side_effect = datetime
            await apply_with_fresh_proof(runtime)
        self.assertTrue(runtime.account.equity_classification_complete)
        self.assertFalse(runtime._activation_consumed)

    async def test_stale_unknown_missing_fill_nonreadonly_fail_closed(self):
        self.approve()
        for fault in ('stale', 'unowned', 'missing_fill', 'position', 'cash', 'live', 'lock'):
            runtime = self.runtime()
            if fault == 'stale': runtime.account.last_balance_at = time.monotonic() - 10
            if fault == 'unowned': runtime.account.unowned_order_seen = True
            if fault == 'missing_fill': runtime.account.missing_execution_orders.add('unknown')
            if fault == 'position': runtime.account.expected_positions = {'TEST': D(1)}
            if fault == 'cash': runtime.account.confirmed_usd = D('100.14')
            if fault == 'live': runtime.read_only = False
            if fault == 'lock': runtime.lock._file = None
            with self.subTest(fault=fault), self.assertRaises(RuntimeError):
                await apply_with_fresh_proof(runtime)
        self.assertEqual(self.journal.get_runtime_state('expected_confirmed_usd'), '100.123')

    async def test_active_external_order_blocks_consumption(self):
        self.approve()
        runtime = self.runtime()
        runtime.rest.active_orders.return_value = [dict(orderId='external', clientOrderId='manual', symbol='TEST',
            side='sell', outcome='no', price='.1', quantity='1', remainingQuantity='1', status='open')]
        with self.assertRaises(RuntimeError):
            await apply_with_fresh_proof(runtime)

    async def test_fill_during_read_bracket_refuses_consumption(self):
        self.approve()
        runtime = self.runtime()
        async def balances():
            runtime.account.fill_version += 1
            return [{'currency': 'USD', 'amount': '100.12'}]
        runtime.rest.account_balances.side_effect = balances
        with self.assertRaises(RuntimeError): await apply_with_fresh_proof(runtime)

    async def test_cli_does_not_bypass_external_order_error(self):
        from gemini_bot.cli import _apply_cash_exception
        from gemini_bot.reconciliation_errors import ExternalOrderPresent
        runtime = SimpleNamespace(prepare=AsyncMock(side_effect=ExternalOrderPresent('external')),
                                  close=AsyncMock())
        with patch('gemini_bot.cli.load_preflight_config'), patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime) as constructor:
            with patch('gemini_bot.cash_exception.apply_with_fresh_proof', new=AsyncMock()) as apply:
                with self.assertRaises(ExternalOrderPresent):
                    await _apply_cash_exception(Path('synthetic'), Path(self.tmp.name))
                apply.assert_not_awaited()
            self.assertTrue(constructor.call_args.kwargs['read_only'])
        runtime.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
