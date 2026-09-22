"""Maintenance fault injection stays offline; no production connections."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from websockets.exceptions import InvalidMessage
import test_continuous_service as fixtures
from gemini_bot.recovery_policy import transient_failure
from gemini_bot.domain import EngineState
from gemini_bot.live_ws import AdmissionTimeoutBeforeSend, TransportChangedBeforeSend
from decimal import Decimal as D
from datetime import datetime, timezone, timedelta
from gemini_bot.account_state import OwnedOrder
from gemini_bot.domain import Outcome, Side
from gemini_bot.execution import OrderPurpose
from gemini_bot.stream_recovery import RecoveryPending


def handshake_eof():
    error = InvalidMessage('did not receive a valid HTTP response')
    error.__cause__ = EOFError('connection closed while reading HTTP status line')
    return error


class HandshakeClassificationTests(unittest.TestCase):
    def test_eof_wrapped_by_websockets_is_retryable(self):
        self.assertTrue(transient_failure(handshake_eof()))

    def test_malformed_complete_response_is_not_retryable(self):
        for cause in (None, ValueError('invalid header'), PermissionError('auth')):
            error = InvalidMessage('invalid response')
            error.__cause__ = cause
            self.assertFalse(transient_failure(error))


class MaintenanceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.LongRecoveryTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.r

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_simultaneous_restart_and_four_handshake_eofs_resume_same_runner(self):
        r = self.r
        r.private.connect.side_effect = [handshake_eof() for _ in range(4)] + [None]
        sleep = asyncio.sleep
        async def fast_sleep(_):
            await sleep(0)
        with patch('gemini_bot.orchestrator.asyncio.sleep', side_effect=fast_sleep):
            await asyncio.gather(r._recover_stream('public', ConnectionError('1012 service restart')),
                                 r._recover_stream('private', ConnectionError('1012 service restart')))
            await asyncio.wait_for(r._stream_recovery_task, 2)
        self.assertEqual(r.private.connect.await_count, 5)
        self.assertEqual(r.gate.state, EngineState.QUOTING)
        self.assertFalse(r._stream_holds)
        r.private.place_maker.assert_not_awaited()

    async def test_unknown_execution_still_cannot_be_replayed(self):
        await self.fixture.test_unknown_execution_is_still_not_replayed()

    async def test_drain_reconnects_before_quarantine_and_never_resumes_entry(self):
        r = self.r
        r._stream_holds = {'private', 'public'}
        r._wait_ready = AsyncMock()
        r._recovery_account_proof = AsyncMock()
        async def pump():
            await asyncio.Event().wait()
        r._public_pump = AsyncMock(side_effect=pump)
        r._private_pump = AsyncMock(side_effect=pump)
        r._drain_bounded = AsyncMock()
        await r.drain()
        r.private.connect.assert_awaited_once()
        r.public.connect.assert_awaited_once()
        r._recovery_account_proof.assert_awaited_once()
        r._drain_bounded.assert_awaited_once()
        self.assertEqual(r.gate.state, EngineState.DRAINING)
        self.assertEqual(len(r._tasks), 2)
        self.assertFalse(r._stream_holds)
        r.private.place_maker.assert_not_awaited()

    async def test_drain_clears_only_definitely_unsent_cancel_failures(self):
        r = self.r
        async def fail(error):
            raise error
        unsent = asyncio.create_task(fail(TransportChangedBeforeSend('socket absent')))
        unknown = asyncio.create_task(fail(TimeoutError('send outcome unknown')))
        await asyncio.gather(unsent, unknown, return_exceptions=True)
        r._cancel_tasks.update(unsent=unsent, unknown=unknown)
        r._wait_ready = AsyncMock()
        r._recovery_account_proof = AsyncMock()
        r._public_pump = AsyncMock()
        r._private_pump = AsyncMock()
        r.gate.state = EngineState.DRAINING
        await r._restore_drain_streams()
        self.assertNotIn('unsent', r._cancel_tasks)
        self.assertIs(r._cancel_tasks['unknown'], unknown)

    async def test_shared_cancel_survives_private_rotation_when_first_send_was_proven_unsent(self):
        r = self.r
        leg = fixtures.recovery.candidate().quote.yes
        intent = r.ledger.prepare(leg, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, 'o')
        frame = {'e':'orderUpdate','i':'o','c':intent.client_order_id,'s':'X','S':'BUY',
                 'O':'YES','q':'10','p':str(leg.price),'z':'10','Z':'0','X':'OPEN','T':1}
        r.account.apply(frame)
        attempts = 0
        active = [dict(orderId='o',clientOrderId=intent.client_order_id,symbol='X',side='BUY',
                       outcome='YES',quantity='10',remainingQuantity='10',price=str(leg.price),status='OPEN')]
        async def active_orders():
            return active if attempts < 2 else []
        r.rest.active_orders = AsyncMock(side_effect=active_orders)
        r.rest.recent_order_history = AsyncMock(return_value=[
            dict(orderId='o',clientOrderId=intent.client_order_id,symbol='X',side='BUY',outcome='YES',
                 quantity='10',filledQuantity='0',price=str(leg.price),status='CANCELED'),
        ])
        async def cancel(_):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TransportChangedBeforeSend('rotated before send')
            r.account.apply({**frame,'z':'0','X':'CANCELED','T':2})
        r.private.cancel = AsyncMock(side_effect=cancel)

        await asyncio.gather(r._cancel_order_and_confirm('o'), r._cancel_order_and_confirm('o'))
        await asyncio.wait_for(r._stream_recovery_task, 2)

        self.assertEqual(r.private.cancel.await_count, 2)
        r.private.connect.assert_awaited_once()
        self.assertGreaterEqual(r.rest.active_orders.await_count, 2)
        self.assertFalse(r.account.reducer.orders['o'].working)
        self.assertNotIn('o', r._cancel_tasks)
        self.assertFalse(r._stream_holds)
        self.assertEqual(r.gate.state, EngineState.QUOTING)

    async def test_adverse_cancel_does_not_halt_while_proven_unsent_reconnect_exceeds_old_clock(self):
        r = self.r
        leg = fixtures.recovery.candidate().quote.yes
        intent = r.ledger.prepare(leg, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, 'o')
        frame = {'e':'orderUpdate','i':'o','c':intent.client_order_id,'s':'X','S':'BUY',
                 'O':'YES','q':'10','p':str(leg.price),'z':'10','Z':'0','X':'OPEN','T':1}
        r.account.apply(frame)
        active = [dict(orderId='o',clientOrderId=intent.client_order_id,symbol='X',side='BUY',
                       outcome='YES',quantity='10',remainingQuantity='10',price=str(leg.price),status='OPEN')]
        r.rest.active_orders = AsyncMock(return_value=active)
        attempts = 0

        async def cancel(_):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TransportChangedBeforeSend('rotated before send')
            r.account.apply({**frame,'z':'0','X':'CANCELED','T':2})

        async def delayed_replacement(_):
            # Longer than the original attempt budget: this is an availability
            # wait after a proven-unsent mutation, not an unknown send.
            await asyncio.sleep(.03)

        async def adverse_cancel(_):
            await r._cancel_order_and_confirm('o', timeout=.01)
            return True

        r.private.cancel = AsyncMock(side_effect=cancel)
        r._await_private_cancel_replacement = AsyncMock(side_effect=delayed_replacement)
        r._cancel_too_aggressive_quote = AsyncMock(side_effect=adverse_cancel)

        self.assertTrue(await r._run_adverse_cancel('X'))

        self.assertEqual(r.private.cancel.await_count, 2)
        self.assertFalse(r.account.reducer.orders['o'].working)
        self.assertNotIn('o', r._cancel_tasks)
        self.assertEqual(r.gate.state, EngineState.QUOTING)

    async def test_unsent_cancel_reconciliation_remains_stop_responsive(self):
        r = self.r
        r.directory.joinpath('STOP').touch()
        with self.assertRaisesRegex(RuntimeError, 'interrupted by stop or expiry'):
            await r._reconcile_unsent_cancel('o')
        r.rest.active_orders.assert_not_awaited()

    async def test_unsent_cancel_absent_from_rest_still_requires_terminal_history_proof(self):
        r = self.r
        leg = fixtures.recovery.candidate().quote.yes
        intent = r.ledger.prepare(leg, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, 'o')
        r.account.reducer.orders['o'] = OwnedOrder(
            'o', intent.client_order_id, 'X', Side.BUY, Outcome.YES,
            leg.price, D(10), D(10), D(0), 'OPEN', 1,
        )
        r.private.cancel = AsyncMock(side_effect=AdmissionTimeoutBeforeSend('not sent'))
        r.rest.active_orders = AsyncMock(return_value=[])
        r.rest.recent_order_history = AsyncMock(return_value=[
            dict(orderId='o',clientOrderId=intent.client_order_id,symbol='X',side='BUY',
                 outcome='YES',quantity='10',filledQuantity='0',price=str(leg.price),
                 status='CANCELED'),
        ])

        await r._cancel_order_and_confirm('o', timeout=.5)

        r.private.cancel.assert_awaited_once_with('o')
        r.rest.recent_order_history.assert_awaited()
        self.assertFalse(r.account.reducer.orders['o'].working)

    async def test_repeated_unsent_admission_failure_keeps_original_total_clock(self):
        r = self.r
        leg = fixtures.recovery.candidate().quote.yes
        intent = r.ledger.prepare(leg, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, 'o')
        r.account.reducer.orders['o'] = OwnedOrder(
            'o', intent.client_order_id, 'X', Side.BUY, Outcome.YES,
            leg.price, D(10), D(10), D(0), 'OPEN', 1,
        )
        r.private.cancel = AsyncMock(side_effect=AdmissionTimeoutBeforeSend('not sent'))
        r.rest.active_orders = AsyncMock(return_value=[
            dict(orderId='o',clientOrderId=intent.client_order_id,symbol='X',side='BUY',
                 outcome='YES',quantity='10',remainingQuantity='10',price=str(leg.price),
                 status='OPEN'),
        ])

        with self.assertRaises(TimeoutError):
            await r._cancel_order_and_confirm('o', timeout=.02)

        self.assertGreater(r.private.cancel.await_count, 1)
        self.assertIn('o', r._cancel_tasks)

    async def test_unsent_cancel_refuses_retry_when_rest_identity_disagrees(self):
        r = self.r
        leg = fixtures.recovery.candidate().quote.yes
        intent = r.ledger.prepare(leg, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, 'o')
        r.account.reducer.orders['o'] = OwnedOrder(
            'o', intent.client_order_id, 'X', Side.BUY, Outcome.YES,
            leg.price, D(10), D(10), D(0), 'OPEN', 1,
        )
        r.private.cancel = AsyncMock(side_effect=AdmissionTimeoutBeforeSend('not sent'))
        r.rest.active_orders = AsyncMock(return_value=[
            dict(orderId='o',clientOrderId='wrong',symbol='X',side='BUY',outcome='YES',
                 quantity='10',remainingQuantity='10',price=str(leg.price),status='OPEN'),
        ])

        with self.assertRaisesRegex(RuntimeError, 'identity is not proven'):
            await r._cancel_order_and_confirm('o')

        r.private.cancel.assert_awaited_once_with('o')

    async def test_drain_transport_wait_remains_bounded(self):
        r = self.r
        r._stream_holds = {'private'}
        r.private.connect.side_effect = asyncio.Event().wait
        r._drain_bounded = AsyncMock()
        with patch('gemini_bot.orchestrator._DRAIN_SECONDS', .02):
            with self.assertRaises(TimeoutError):
                await r.drain()
        r._drain_bounded.assert_not_awaited()
        self.assertTrue(r._stream_holds)

    async def test_missed_partial_maker_fill_reconciles_before_resume(self):
        r = self.r
        now = datetime(2026,9,16,12,tzinfo=timezone.utc)
        leg = fixtures.recovery.candidate().quote.yes
        intent = r.ledger.prepare(leg, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id,'o')
        r.account.reducer.orders['o'] = OwnedOrder('o',intent.client_order_id,'X',Side.BUY,Outcome.YES,
                                                   leg.price,leg.quantity,D(0),D(0),'CANCELED',1)
        r._private_recovery_orders.add('o')
        r.account.reducer.positions={'X':D(1)}
        r.account.confirmed_usd = r.account.expected_confirmed_usd - leg.price
        r.rest.positions.return_value=[{'symbol':'X','outcome':'yes','totalQuantity':'1','avgPrice':str(leg.price),'marketValue':str(leg.price)}]
        r.rest.recent_order_history.return_value=[dict(orderId='o',clientOrderId=intent.client_order_id,
            symbol='X',side='buy',outcome='yes',quantity=str(leg.quantity),filledQuantity='1',status='cancelled',
            price=str(leg.price),avgExecutionPrice=str(leg.price),timeInForce='maker-or-cancel',
            createdAt=(now-timedelta(hours=1)).isoformat(),updatedAt=now.isoformat())]
        r.rest.recent_account_trades = AsyncMock(return_value=[dict(tid='actual-fill',order_id='o',
            client_order_id=intent.client_order_id,symbol='X',type='Buy',amount='1',price=str(leg.price),
            fee_amount='0',fee_currency='USD',timestampms=int((now-timedelta(minutes=10)).timestamp()*1000))])
        with patch('gemini_bot.orchestrator._utc_now',return_value=now):
            with self.assertRaises(RecoveryPending):
                await r._recovery_account_proof()
            await r._recovery_account_proof()
        self.assertTrue(r.account.equity_classification_complete)
        self.assertLess(r.journal.get_deadline('X','YES'),now)
        self.assertIn('X',r.account.blocked_entries)
        r.private.place_maker.assert_not_awaited()
