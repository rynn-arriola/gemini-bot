"""Offline September 17 dead-reader/drain regression; no venue mutation."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from decimal import Decimal as D

import test_maintenance_recovery as fixtures
from gemini_bot.domain import EngineState, QuoteLeg, Outcome, Side
from gemini_bot.execution import OrderPurpose
from gemini_bot.stream_recovery import RecoveryPending
from gemini_bot.recovery_policy import transient_failure
from gemini_bot.ws_requests import RequestTracker, WebSocketRequestRejected


class RejectionDiagnosticsTests(unittest.TestCase):
    def test_correlated_rejection_keeps_safe_code_and_remains_fatal(self):
        tracker = RequestTracker()
        tracker.register(1, 'order.place')
        with self.assertRaises(WebSocketRequestRejected) as caught:
            tracker.response({'id': 1, 'status': 400,
                              'error': {'code': -2010, 'msg': 'SECRET', 'data': {'balance': 'SECRET'}}})
        error = caught.exception
        self.assertEqual((error.method, error.status, error.code), ('order.place', 400, -2010))
        self.assertNotIn('SECRET', str(error))
        self.assertNotIn('SECRET', repr(vars(error)))
        self.assertFalse(transient_failure(error))
        self.assertFalse(tracker.pending)

    def test_malformed_metadata_is_never_logged(self):
        error = WebSocketRequestRejected({'status': 'SECRET', 'error': {'code': 'SECRET'}}, 'SECRET')
        self.assertEqual((error.method, error.status, error.code), ('unknown', None, None))
        self.assertNotIn('SECRET', str(error))

    def test_mutation_service_error_is_not_retry_authority(self):
        for method in ('order.place', 'order.cancel', 'order.cancel_all'):
            tracker = RequestTracker(); tracker.register(1, method)
            with self.assertRaises(WebSocketRequestRejected) as caught:
                tracker.response({'id': 1, 'status': 503, 'error': {'code': -1000}})
            self.assertFalse(transient_failure(caught.exception))

    def test_only_explicit_service_codes_allow_post_proof_recovery(self):
        for status, code in ((429, -1003), (500, -1000), (503, -1000)):
            self.assertTrue(WebSocketRequestRejected({'status': status, 'error': {'code': code}}, 'order.place').service_failure)
        for status, code in ((400, -2010), (401, -1002), (418, -1003), (503, None), (503, -1002)):
            self.assertFalse(WebSocketRequestRejected({'status': status, 'error': {'code': code}}, 'order.place').service_failure)


class DeadReaderDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = fixtures.MaintenanceFlowTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.r

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    async def test_fatal_reader_without_hold_restores_for_drain_only(self):
        async def rejected():
            raise WebSocketRequestRejected({'status': 400, 'error': {'code': -2010}}, 'order.place')
        dead = asyncio.create_task(rejected())
        await asyncio.gather(dead, return_exceptions=True)
        self.r._tasks = [dead]
        self.assertFalse(self.r._stream_holds)
        self.r._wait_ready = AsyncMock()
        self.r._recovery_account_proof = AsyncMock()
        async def pump():
            await asyncio.Event().wait()
        self.r._public_pump = AsyncMock(side_effect=pump)
        self.r._private_pump = AsyncMock(side_effect=pump)
        self.r._drain_bounded = AsyncMock()
        await self.r.drain()
        self.r.private.connect.assert_awaited_once()
        self.r._recovery_account_proof.assert_awaited_once()
        self.r._drain_bounded.assert_awaited_once()
        self.assertEqual(self.r.gate.state, EngineState.DRAINING)
        self.r.private.place_maker.assert_not_awaited()

    async def test_failed_fresh_proof_does_not_drain_or_resume_blindly(self):
        dead = asyncio.create_task(asyncio.sleep(0))
        await dead
        self.r._tasks = [dead]
        self.r._restore_drain_streams = AsyncMock(side_effect=RuntimeError('unowned order'))
        self.r._drain_bounded = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'unowned order'):
            await self.r.drain()
        self.r._drain_bounded.assert_not_awaited()
        self.r.private.place_maker.assert_not_awaited()

    async def test_healthy_reader_does_not_reconnect_on_normal_stop(self):
        alive = asyncio.create_task(asyncio.Event().wait())
        self.r._tasks = [alive]
        self.r._restore_drain_streams = AsyncMock()
        self.r._drain_bounded = AsyncMock()
        await self.r.drain()
        self.r._restore_drain_streams.assert_not_awaited()
        self.r._drain_bounded.assert_awaited_once()

    def missing_ack(self):
        intent = self.r.ledger.prepare(QuoteLeg('X', Outcome.YES, Side.BUY, D('.4'), D(15)), OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        row = dict(clientOrderId=intent.client_order_id, orderId='missing-new', symbol='X',
                   side='buy', outcome='yes', price='.4', quantity='15', filledQuantity='0', status='cancelled')
        self.r.rest.recent_order_history.return_value = [row]
        return intent, row

    async def test_missing_new_resolves_only_exact_zero_fill_terminal(self):
        intent, row = self.missing_ack()
        await self.r._resolve_zero_fill_unacknowledged_entries()
        self.assertFalse(self.r.journal.unresolved_ids())
        self.assertFalse(self.r.journal.nonterminal_sent_ids())
        self.r.private.place_maker.assert_not_awaited()
        self.r.private.cancel.assert_not_awaited()

    async def test_missing_history_never_means_no_order(self):
        intent, _ = self.missing_ack()
        self.r.rest.recent_order_history.return_value = []
        with self.assertRaises(RecoveryPending):
            await self.r._resolve_zero_fill_unacknowledged_entries()
        self.assertIn(intent.client_order_id, self.r.journal.unresolved_ids())

    async def test_partial_fill_cannot_be_erased_by_cancel_history(self):
        intent, row = self.missing_ack(); row['filledQuantity'] = '7'
        with self.assertRaisesRegex(RuntimeError, 'missing execution'):
            await self.r._resolve_zero_fill_unacknowledged_entries()
        self.assertIn(intent.client_order_id, self.r.journal.unresolved_ids())

    async def test_identity_mismatch_stays_unknown(self):
        intent, row = self.missing_ack(); row['outcome'] = 'no'
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            await self.r._resolve_zero_fill_unacknowledged_entries()
        self.assertIn(intent.client_order_id, self.r.journal.unresolved_ids())

    async def test_duplicate_history_stays_unknown(self):
        intent, row = self.missing_ack(); self.r.rest.recent_order_history.return_value = [row, row]
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            await self.r._resolve_zero_fill_unacknowledged_entries()
        self.assertIn(intent.client_order_id, self.r.journal.unresolved_ids())

    async def test_service_recovery_requires_flat_classified_terminal_proof(self):
        failure = WebSocketRequestRejected({'status': 503, 'error': {'code': -1000}}, 'order.place')
        self.r._private_request_failure = failure
        self.assertTrue(self.r._request_failure_proven_recoverable(failure, drained_flat=True))
        self.assertTrue(self.r._request_failure_proven_recoverable(TimeoutError(), drained_flat=True))
        self.assertFalse(self.r._request_failure_proven_recoverable(failure, drained_flat=False))
        self.assertFalse(self.r._request_failure_proven_recoverable(ValueError('other failure'), drained_flat=True))
        self.r._dynamic_pending_cap = 0
        self.assertFalse(self.r._request_failure_proven_recoverable(failure, drained_flat=True))
        self.r._dynamic_pending_cap = 40
        self.missing_ack()
        self.assertFalse(self.r._request_failure_proven_recoverable(failure, drained_flat=True))

    async def test_permanent_rejection_never_becomes_restart_permission(self):
        for status, code in ((400, -2010), (401, -1002), (418, -1003), (503, None)):
            self.r._private_request_failure = WebSocketRequestRejected({'status': status, 'error': {'code': code}}, 'order.place')
            self.assertFalse(self.r._request_failure_proven_recoverable(TimeoutError(), drained_flat=True))

    async def test_full_width_dead_reader_drains_exact_terminal_orders_without_replay(self):
        rows = []
        for i in range(40):
            leg = QuoteLeg('X', Outcome.YES if i % 2 else Outcome.NO, Side.BUY, D('.4'), D(15))
            intent = self.r.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.r.ledger.mark_sent(intent.client_order_id)
            self.r.account.apply(dict(e='orderUpdate', i=str(i), c=intent.client_order_id, s='X',
                                      S='BUY', O=leg.outcome.value, p='.4', q='15', z='15', X='NEW'))
            rows.append(dict(orderId=str(i), clientOrderId=intent.client_order_id, symbol='X',
                             side='buy', outcome=leg.outcome.value.lower(), quantity='15',
                             filledQuantity='0', price='.4', status='cancelled'))
        self.r.rest.recent_order_history.return_value = rows
        dead = asyncio.create_task(asyncio.sleep(0)); await dead; self.r._tasks = [dead]
        async def pump():
            await asyncio.Event().wait()
        self.r._public_pump = AsyncMock(side_effect=pump)
        self.r._private_pump = AsyncMock(side_effect=pump)
        self.r._wait_ready = AsyncMock()
        await self.r.drain()
        self.assertFalse(self.r.account.reducer.pending_order_count())
        self.assertFalse(self.r.journal.nonterminal_sent_ids())
        self.assertEqual(self.r.gate.state, EngineState.HALTED)
        self.r.private.cancel.assert_not_awaited()
        self.r.private.place_maker.assert_not_awaited()
        self.assertLessEqual(self.r.rest.recent_order_history.await_count, 2)
