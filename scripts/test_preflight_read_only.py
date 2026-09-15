import unittest
from unittest.mock import AsyncMock

from gemini_bot.execution import IntentLedger
from gemini_bot.rate_limits import AdmissionGovernor, Priority
from preflight_read_only import ReadOnlyAccountSocket


class ReadOnlyPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_methods_are_blocked_before_intents_or_send(self):
        ledger = IntentLedger()
        socket = ReadOnlyAccountSocket("fake", "fake", AdmissionGovernor(), ledger)
        socket._connection = AsyncMock()
        for method in (socket.place_maker, socket.place_ioc_entry, socket.exit_position, socket.cancel, socket.cancel_all):
            with self.assertRaises(RuntimeError):
                await method(None)
        for method in ("order.place", "order.cancel", "order.cancel_all"):
            with self.assertRaises(RuntimeError):
                await socket._send({"method": method, "params": {}}, Priority.ORDER_SAFETY)
        self.assertEqual(ledger.unresolved(), ())
        socket._connection.send.assert_not_awaited()

    async def test_only_account_subscriptions_and_no_disconnect_cancel(self):
        socket = ReadOnlyAccountSocket("fake", "fake", AdmissionGovernor(), IntentLedger())
        socket._connection = AsyncMock()
        self.assertIn("cancelOnDisconnect=false", socket._uri)
        self.assertNotIn("cancelOnDisconnect=true", socket._uri)
        await socket.subscribe_account()
        socket._connection.send.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            await socket._send({"method": "SUBSCRIBE", "params": ["unexpected"]}, Priority.ACCOUNT_RECONCILIATION)
