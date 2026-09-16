"""Read-only startup standby, using fake transports and synthetic accounting."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from gemini_bot.cli import (
    _run_live, _run_live_once, _wait_for_reconciliation, _wait_reconciliation_delay,
    LiveRunnerExit,
)
from gemini_bot.reconciliation_errors import (
    ExternalOrderPresent, UnclassifiedCash, UnclassifiedPositions,
)
from gemini_bot.recovery_policy import transient_failure
from gemini_bot.rest_client import ApiError
from gemini_bot.storage import SQLiteJournal, AuthenticatedCashAdjustment


class ReconciliationWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.permit = self.root / 'permit.json'
        self.permit.write_text(json.dumps({'permit_version': 7}))

    async def run_bot(self):
        await _run_live(self.root / '.env', self.root, self.permit,
                        ignore_uptime_proxy=False, supervisor_session_id='test')

    async def test_wait_success_repeats_full_preparation_and_permit_validation(self):
        with patch('gemini_bot.cli._run_live_once', new_callable=AsyncMock) as run, \
                patch('gemini_bot.cli._wait_for_reconciliation', new_callable=AsyncMock) as wait:
            run.side_effect = [ExternalOrderPresent('external'), None]
            wait.return_value = True
            await self.run_bot()
            self.assertEqual(run.await_count, 2)
            wait.assert_awaited_once()
            self.assertEqual(run.await_args_list[0], run.await_args_list[1])

    async def test_stop_in_wait_never_retries_live_attempt(self):
        with patch('gemini_bot.cli._run_live_once', new_callable=AsyncMock) as run, \
                patch('gemini_bot.cli._wait_for_reconciliation', new_callable=AsyncMock) as wait:
            run.side_effect = UnclassifiedCash('cash')
            wait.return_value = False
            await self.run_bot()
            run.assert_awaited_once()

    async def test_legacy_does_not_gain_wait_authority(self):
        self.permit.write_text(json.dumps({'permit_version': 6}))
        with patch('gemini_bot.cli._run_live_once', side_effect=UnclassifiedCash('cash')), \
                patch('gemini_bot.cli._wait_for_reconciliation', new_callable=AsyncMock) as wait:
            with self.assertRaises(UnclassifiedCash):
                await self.run_bot()
            wait.assert_not_awaited()

    async def test_unknown_errors_are_not_retried(self):
        with patch('gemini_bot.cli._run_live_once', side_effect=RuntimeError('unknown')), \
                patch('gemini_bot.cli._wait_for_reconciliation', new_callable=AsyncMock) as wait:
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                await self.run_bot()
            wait.assert_not_awaited()

    async def test_stop_before_start_prevents_prepare_and_activation(self):
        for name in ('STOP', 'SUPERVISOR_STOP'):
            stop = self.root / name
            stop.touch()
            with patch('gemini_bot.cli._run_live_once', new_callable=AsyncMock) as run:
                await self.run_bot()
                run.assert_not_awaited()
            self.assertEqual(json.loads((self.root / 'status.json').read_text())['phase'],
                             'stopped_before_activation')
            stop.unlink()

    async def test_polling_instances_are_read_only_closed_and_never_activate(self):
        runtimes = [AsyncMock(), AsyncMock(), AsyncMock()]
        runtimes[0].prepare.side_effect = ExternalOrderPresent('external')
        runtimes[1].prepare.side_effect = UnclassifiedCash('cash')
        with patch('gemini_bot.cli.load_preflight_config') as config, \
                patch('gemini_bot.cli.ProductionOrchestrator', side_effect=runtimes) as constructor, \
                patch('gemini_bot.cli._wait_reconciliation_delay', new_callable=AsyncMock, return_value=True):
            self.assertTrue(await _wait_for_reconciliation(self.root / '.env', self.root,
                                                          ExternalOrderPresent('external')))
            self.assertEqual(constructor.call_count, 3)
            for call in constructor.call_args_list:
                self.assertIs(call.kwargs['read_only'], True)
            self.assertEqual(config.call_count, 3)
        for runtime in runtimes:
            runtime.close.assert_awaited_once()
            runtime.activate.assert_not_awaited()

    async def test_longer_server_cooldown_is_preserved(self):
        runtime = AsyncMock()
        runtime.prepare.side_effect = ApiError(429, 'limited', retry_after=125)
        with patch('gemini_bot.cli.load_preflight_config'), \
                patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime), \
                patch('gemini_bot.cli._wait_reconciliation_delay', new_callable=AsyncMock) as delay:
            delay.side_effect = [True, False]
            self.assertFalse(await _wait_for_reconciliation(self.root / '.env', self.root,
                                                           UnclassifiedCash('cash')))
            self.assertEqual(delay.await_args_list[-1].args[-1], 125)
        runtime.close.assert_awaited_once()

    async def test_auth_malformed_and_unknown_errors_fail_closed(self):
        for error in (ApiError(401, 'auth'), ValueError('malformed'), RuntimeError('unknown')):
            runtime = AsyncMock()
            runtime.prepare.side_effect = error
            with patch('gemini_bot.cli.load_preflight_config'), \
                    patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime), \
                    patch('gemini_bot.cli._wait_reconciliation_delay', new_callable=AsyncMock, return_value=True):
                with self.assertRaises(type(error)):
                    await _wait_for_reconciliation(self.root / '.env', self.root, UnclassifiedCash('cash'))
            runtime.close.assert_awaited_once()
            runtime.activate.assert_not_awaited()

    async def test_cancellation_closes_readonly_instance(self):
        runtime = AsyncMock()
        runtime.prepare.side_effect = asyncio.CancelledError()
        with patch('gemini_bot.cli.load_preflight_config'), \
                patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime), \
                patch('gemini_bot.cli._wait_reconciliation_delay', new_callable=AsyncMock, return_value=True):
            with self.assertRaises(asyncio.CancelledError):
                await _wait_for_reconciliation(self.root / '.env', self.root, UnclassifiedCash('cash'))
        runtime.close.assert_awaited_once()

    async def test_postactivation_account_error_never_enters_startup_wait(self):
        runtime = AsyncMock()
        runtime._activation_consumed = True
        runtime._continuous_service = True
        runtime._recoverable_exit = False
        runtime.activate.side_effect = UnclassifiedCash('cash')
        with patch('gemini_bot.cli.load_live_config'), \
                patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime), \
                patch('gemini_bot.cli._wait_for_reconciliation', new_callable=AsyncMock) as wait:
            with self.assertRaises(LiveRunnerExit) as error:
                await self.run_bot()
            self.assertEqual(error.exception.code, 3)
            wait.assert_not_awaited()

    async def test_stop_during_final_prepare_prevents_permit_consumption(self):
        runtime = AsyncMock()
        runtime._activation_consumed = False
        async def prepare():
            (self.root / 'STOP').touch()
        runtime.prepare.side_effect = prepare
        with patch('gemini_bot.cli.load_live_config'), \
                patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime):
            await self.run_bot()
        runtime.activate.assert_not_awaited()
        runtime.close.assert_awaited_once()

    async def test_heartbeat_is_redacted_nonactivated_and_stop_responsive(self):
        async def stop_after_tick(seconds):
            self.assertLessEqual(seconds, 1)
            value = json.loads((self.root / 'status.json').read_text())
            self.assertEqual(value['phase'], 'waiting_reconciliation')
            self.assertFalse(value['activated'])
            self.assertEqual(value['counters']['trading_enabled'], 0)
            self.assertGreaterEqual(value['counters']['retry_in_seconds'], 59)
            (self.root / 'STOP').touch()
        with patch('gemini_bot.cli.asyncio.sleep', side_effect=stop_after_tick):
            self.assertFalse(await _wait_reconciliation_delay(self.root, 'UnclassifiedCash', 0))

    def test_evidence_blockers_are_never_transient_failures(self):
        for cls in (UnclassifiedCash, UnclassifiedPositions, ExternalOrderPresent):
            self.assertFalse(transient_failure(cls('unclassified')))

    def test_subcent_cash_blocker_never_writes_balance_or_adjustment(self):
        now = datetime.now(timezone.utc)
        journal = SQLiteJournal(self.root / 'journal.sqlite')
        self.addCleanup(journal.close)
        journal.set_runtime_state('expected_confirmed_usd', '100.0001', updated_at=now)
        with self.assertRaises(UnclassifiedCash):
            journal.reconcile_authenticated_cash_adjustments(
                expected_cash=Decimal('100.0001'), confirmed_cash=Decimal('100'),
                adjustments=(), observed_at=now,
            )
        self.assertEqual(journal.get_runtime_state('expected_confirmed_usd'), '100.0001')
        self.assertEqual(journal.connection.execute(
            'SELECT COUNT(*) FROM authenticated_cash_adjustment').fetchone()[0], 0)

    def test_malformed_cash_evidence_is_not_a_waitable_mismatch(self):
        now = datetime.now(timezone.utc)
        journal = SQLiteJournal(self.root / 'journal.sqlite')
        self.addCleanup(journal.close)
        journal.set_runtime_state('expected_confirmed_usd', '100', updated_at=now)
        with self.assertRaises(RuntimeError) as error:
            journal.reconcile_authenticated_cash_adjustments(
                expected_cash=Decimal('100'), confirmed_cash=Decimal('101'),
                adjustments=(AuthenticatedCashAdjustment('invented', 'id', Decimal(1), now),),
                observed_at=now,
            )
        self.assertNotIsInstance(error.exception, UnclassifiedCash)


if __name__ == '__main__':
    unittest.main()
