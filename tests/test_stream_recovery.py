"""Offline transport recovery and no-replay boundaries. Never contact Gemini."""
import asyncio
from decimal import Decimal as D
from datetime import timedelta
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as fixtures
from test_review_regressions import candidate
from gemini_bot.account_state import OwnedOrder
from gemini_bot.domain import EngineState, Outcome, Side
from gemini_bot.execution import OrderPurpose
from gemini_bot.live_ws import MutationNotSentError
from gemini_bot.stream_recovery import RecoveryPending, terminal_history_status


class HistoryProofTests(unittest.TestCase):
    def setUp(self):
        self.order = OwnedOrder("o", "c", "X", Side.BUY, Outcome.YES,
                                D(".4"), D(10), D(10), D(0), "OPEN", 1)
        self.row = dict(orderId="o", clientOrderId="c", symbol="X", side="buy",
                        outcome="yes", quantity="10", filledQuantity="0", status="cancelled")

    def test_exact_terminal_is_accepted(self):
        self.assertEqual(terminal_history_status(self.order, self.row), "CANCELED")

    def test_missing_fill_is_never_invented(self):
        self.row["filledQuantity"] = "1"
        with self.assertRaisesRegex(RuntimeError, "execution evidence"):
            terminal_history_status(self.order, self.row)

    def test_identity_mismatch_blocks(self):
        self.row["outcome"] = "no"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            terminal_history_status(self.order, self.row)

    def test_working_history_waits(self):
        self.row["status"] = "OPEN"
        with self.assertRaises(RecoveryPending):
            terminal_history_status(self.order, self.row)

    def test_missing_or_nonfinite_quantities_block(self):
        for value in (None, "NaN", "Infinity", "-1", "11"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                terminal_history_status(self.order, dict(self.row, filledQuantity=value))


class StreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.runtime = r = self.fixture.runtime
        self.no_network = patch("websockets.connect", side_effect=AssertionError("network prohibited"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        r.account.subscription_acknowledged = True
        r.public.close = AsyncMock()
        r.private.close = AsyncMock()
        r.public.connect = AsyncMock()
        r.private.connect = AsyncMock()

        async def public_ready(*args):
            r.public.books["X"] = fixtures.book()
            r.public.last_message_at = fixtures.NOW

        async def private_ready():
            r.account.apply({"status": 200})
            r.account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "100", "f": "100"}]})
            r.account.apply({"e": "positionReport", "P": [], "u": 1})

        r.public.subscribe = AsyncMock(side_effect=public_ready)
        r.private.subscribe_account = AsyncMock(side_effect=private_ready)
        r.rest.positions = AsyncMock(return_value=[])
        r.rest.active_orders = AsyncMock(return_value=[])
        r.rest.recent_order_history = AsyncMock(return_value=[])
        r.private.cancel = AsyncMock(side_effect=AssertionError("unexpected cancel"))
        r.private.place_maker = AsyncMock(side_effect=AssertionError("unexpected entry"))
        r.private.exit_position = AsyncMock(side_effect=AssertionError("unexpected exit"))

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_public_disconnect_resumes_only_after_proof(self):
        r = self.runtime
        await r._recover_stream("public", ConnectionError("synthetic"))
        await asyncio.wait_for(r._stream_recovery_task, 2)
        self.assertFalse(r._stream_holds)
        self.assertEqual(r.gate.state, EngineState.QUOTING)
        r.rest.positions.assert_awaited()
        r.public.connect.assert_awaited_once()
        r.private.place_maker.assert_not_awaited()

    async def test_private_disconnect_rebuilds_snapshot_generation(self):
        r = self.runtime
        r.account.reducer.position_update_ns = 999
        await r._recover_stream("private", ConnectionError("synthetic"))
        await asyncio.wait_for(r._stream_recovery_task, 2)
        self.assertFalse(r._stream_holds)
        self.assertEqual(r.account.reducer.position_update_ns, 1)
        r.private.subscribe_account.assert_awaited_once()

    async def test_concurrent_faults_share_reconnect(self):
        r = self.runtime
        await asyncio.gather(*(r._recover_stream("public", ConnectionError("synthetic"), generation=0) for _ in range(2)))
        await asyncio.wait_for(r._stream_recovery_task, 2)
        r.public.connect.assert_awaited_once()

    async def test_public_and_private_recover_together(self):
        r = self.runtime
        await asyncio.gather(r._recover_stream("public", ConnectionError()),
                             r._recover_stream("private", ConnectionError()))
        await asyncio.wait_for(r._stream_recovery_task, 2)
        self.assertFalse(r._stream_holds)

    async def test_unknown_mutation_never_replayed(self):
        r = self.runtime
        intent = r.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        with self.assertRaisesRegex(RuntimeError, "unknown mutation"):
            await r._recover_stream("private", ConnectionError())
        r.private.connect.assert_not_awaited()
        self.assertIn(intent.client_order_id, r.journal.unresolved_ids())

    async def test_stop_or_expired_permit_cannot_resume(self):
        r = self.runtime
        r._permit_expires_at = fixtures.NOW
        with self.assertRaisesRegex(RuntimeError, "authority"):
            await r._recover_stream("public", ConnectionError())
        r.public.connect.assert_not_awaited()

    async def test_timeout_and_ownership_errors_keep_hold(self):
        r = self.runtime
        r._recovery_account_proof = AsyncMock(side_effect=RuntimeError("unowned order"))
        await r._recover_stream("public", ConnectionError())
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            await r._stream_recovery_task
        self.assertTrue(r._stream_holds)
        self.assertEqual(r.gate.state, EngineState.HALTED)

    async def test_readonly_recovery_cannot_mutate(self):
        r = self.runtime
        r.read_only = True
        r.gate.state = EngineState.READY
        await r._recover_stream("public", ConnectionError())
        await asyncio.wait_for(r._stream_recovery_task, 2)
        r.private.cancel.assert_not_awaited()
        r.private.exit_position.assert_not_awaited()
        r.private.place_maker.assert_not_awaited()

    async def test_hold_blocks_final_entry_and_preserves_deadline(self):
        r = self.runtime
        r._stream_holds.add("public")
        with self.assertRaises(MutationNotSentError):
            r._validate_send(self.fixture.payload())
        deadline = fixtures.NOW - timedelta(seconds=1)
        r.journal.persist_deadline("X", "YES", deadline - timedelta(seconds=300), deadline, D(10))
        r.account.reducer.positions = {"X": D(10)}
        r.account.expected_positions = {"X": D(10)}
        r.public.books["X"].synchronized = False
        await r._exit_due_positions()
        self.assertEqual(r.journal.get_deadline("X", "YES"), deadline)
        r.private.exit_position.assert_not_awaited()

    async def test_reconnect_attempts_are_bounded(self):
        r = self.runtime
        r.public.connect.side_effect = ConnectionError("synthetic")
        with self.assertRaises(ConnectionError):
            await r._recover_stream("public", ConnectionError())
        with self.assertRaises(ConnectionError):
            await r._stream_recovery_task
        self.assertEqual(r.public.connect.await_count, 3)
        self.assertEqual(r.gate.state, EngineState.HALTED)

    async def test_storage_failure_is_not_transport_recovery(self):
        r = self.runtime
        async def frames():
            yield {"status": 200}
        r.private.messages = frames
        r.account.apply = lambda _: (_ for _ in ()).throw(OSError("synthetic disk failure"))
        with self.assertRaisesRegex(OSError, "disk failure"):
            await r._private_pump()
        r.private.connect.assert_not_awaited()

    async def test_pump_eof_reconnects_without_replaying_data(self):
        r = self.runtime
        received = asyncio.Event()
        async def frames():
            if r._stream_generation["public"] == 0:
                return
            yield {"e": "depthUpdate", "s": "X", "U": 1, "u": 1,
                   "b": [[".48", "100"]], "a": [[".52", "100"]]}
            received.set()
            await asyncio.Event().wait()
        r.public.frames = frames
        task = asyncio.create_task(r._public_pump())
        try:
            await asyncio.wait_for(received.wait(), 2)
            await asyncio.wait_for(r._stream_recovery_task, 2)
            r.public.connect.assert_awaited_once()
            self.assertFalse(r._stream_holds)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_private_missed_fill_prevents_resume(self):
        r = self.runtime
        intent = r.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, "o")
        r.account.reducer.orders["o"] = OwnedOrder("o", intent.client_order_id, "X", Side.BUY,
                                                   Outcome.YES, D(".46"), D(10), D(0), D(0), "CANCELED", 1)
        r._private_recovery_orders.add("o")
        r.rest.recent_order_history.return_value = [dict(orderId="o", clientOrderId=intent.client_order_id,
            symbol="X", side="BUY", outcome="YES", quantity="10", filledQuantity="1", status="CANCELED")]
        with self.assertRaisesRegex(RuntimeError, "execution evidence"):
            await r._recovery_account_proof()

    async def test_close_cancels_recovery_tasks(self):
        r = self.runtime
        task = asyncio.create_task(asyncio.Event().wait())
        r._stream_restarts["public"] = task
        await r.close()
        self.assertTrue(task.cancelled())

    async def test_proof_timeout_does_not_resume(self):
        r = self.runtime
        r._recovery_account_proof = AsyncMock(side_effect=RecoveryPending("not published"))
        with patch("gemini_bot.orchestrator._STREAM_RECOVERY_SECONDS", .03):
            await r._recover_stream("public", ConnectionError())
            with self.assertRaises(TimeoutError):
                await r._stream_recovery_task
        self.assertTrue(r._stream_holds)
        self.assertEqual(r.gate.state, EngineState.HALTED)

    async def test_private_staleness_initiates_bounded_recovery(self):
        r = self.runtime
        r.account.last_balance_at = 0
        async def recover(*args, **kwargs):
            r.gate.state = EngineState.DRAINING
            raise asyncio.CancelledError()
        r._recover_stream = AsyncMock(side_effect=recover)
        with self.assertRaises(asyncio.CancelledError):
            await r._live_safety_clock()
        self.assertEqual(r._recover_stream.call_args.args[0], "private")

    async def test_expiry_precedes_recovery(self):
        r = self.runtime
        r.account.last_balance_at = 0
        r._permit_expires_at = fixtures.NOW
        r._recover_stream = AsyncMock()
        await r._live_safety_clock()
        self.assertEqual(r.gate.state, EngineState.DRAINING)
        r._recover_stream.assert_not_awaited()

    async def test_cancel_on_disconnect_terminal_is_proved_without_duplicate_cancel(self):
        r = self.runtime
        intent = r.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        r.ledger.mark_sent(intent.client_order_id)
        r.ledger.acknowledge(intent.client_order_id, "o")
        r.account.reducer.orders["o"] = OwnedOrder("o", intent.client_order_id, "X", Side.BUY,
                                                   Outcome.YES, D(".46"), D(10), D(10), D(0), "OPEN", 1)
        r.rest.recent_order_history.return_value = [dict(orderId="o", clientOrderId=intent.client_order_id,
            symbol="X", side="BUY", outcome="YES", quantity="10", filledQuantity="0", status="CANCELED")]
        await r._recover_stream("private", ConnectionError())
        await asyncio.wait_for(r._stream_recovery_task, 2)
        self.assertFalse(r._stream_holds)
        self.assertFalse(r.account.reducer.orders["o"].working)
        self.assertFalse(r.journal.nonterminal_sent_ids())
        r.private.cancel.assert_not_awaited()

    async def test_missing_terminal_history_keeps_recovery_pending(self):
        r = self.runtime
        r.account.reducer.orders["o"] = OwnedOrder("o", "c", "X", Side.BUY,
                                                   Outcome.YES, D(".46"), D(10), D(10), D(0), "OPEN", 1)
        r._private_recovery_orders.add("o")
        with self.assertRaisesRegex(RecoveryPending, "unavailable"):
            await r._recovery_account_proof()
        r.private.cancel.assert_not_awaited()
