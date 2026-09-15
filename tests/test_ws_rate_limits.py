from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from gemini_bot.rate_limits import AdmissionGovernor, Priority
from gemini_bot.ws_limits import receive_connection_limits, observe_response
from gemini_bot.public_ws import PublicMarketSocket
import test_repair_integration as fixtures


def limits(weight=7000, orders=3500, count=0):
    return [
        {"rateLimitType": "REQUEST_WEIGHT", "interval": "SECOND", "intervalNum": 10, "limit": weight, "count": count},
        {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": orders, "count": 0},
        {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 10000000, "count": 0},
        {"rateLimitType": "CONNECTION_ATTEMPTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300, "count": 1},
    ]


class ConnectionRateTests(unittest.TestCase):
    def test_smaller_reported_limit_tightens_spacing(self):
        governor = AdmissionGovernor()
        governor.observe_websocket_limits("private_ws", limits(weight=1000), now=100)
        self.assertEqual(governor.admit_websocket("private_ws", Priority.ORDER_SAFETY, "order.place", now=100), 0)
        self.assertGreater(governor.admit_websocket("private_ws", Priority.ORDER_SAFETY, "order.place", now=100.1), 1)

    def test_cancel_uses_headroom_reserved_from_entries(self):
        governor = AdmissionGovernor()
        governor.observe_websocket_limits("private_ws", limits(weight=1000, count=700), now=100)
        self.assertEqual(governor.admit_websocket("private_ws", Priority.ORDER_SAFETY, "order.place", now=100), 10)
        self.assertEqual(governor.admit_websocket("private_ws", Priority.EMERGENCY_CANCEL, "order.cancel", now=100), 0)

    def test_daily_order_bucket_also_limits_requests(self):
        governor = AdmissionGovernor()
        rows = limits()
        rows[2]["count"] = 7000000
        governor.observe_websocket_limits("private_ws", rows, now=100)
        self.assertEqual(governor.admit_websocket("private_ws", Priority.ORDER_SAFETY, "order.place", now=100), 86400)

    def test_reports_never_erase_reserved_usage_or_relax_other_socket_limit(self):
        governor = AdmissionGovernor()
        governor.observe_websocket_limits("public_ws", limits(weight=1000), now=100)
        governor.admit_websocket("public_ws", Priority.MARKET_RESYNC, "SUBSCRIBE", now=100)
        governor.observe_websocket_limits("private_ws", limits(), now=101)
        bucket = governor.ws_buckets[("REQUEST_WEIGHT", 10.0)]
        self.assertEqual(bucket.limit, 1000)
        self.assertEqual(bucket.used, 100)

    def test_server_cooldown_dominates_cancel_headroom(self):
        governor = AdmissionGovernor()
        governor.observe_websocket_limits("private_ws", limits(), now=100)
        governor.apply_server_cooldown("private_ws", 120, now=100)
        self.assertEqual(governor.admit_websocket("private_ws", Priority.EMERGENCY_CANCEL, "order.cancel", now=101), 119)

    def test_connection_bucket_is_not_treated_as_message_allowance(self):
        governor = AdmissionGovernor()
        rows = limits()
        rows[3]["count"] = 210
        governor.observe_websocket_limits("public_ws", rows, now=100)
        self.assertEqual(governor.admit_websocket_connection(now=100), 300)
        self.assertEqual(governor.admit_websocket("public_ws", Priority.MARKET_RESYNC, "SUBSCRIBE", now=100), 0)

    def test_malformed_and_unknown_bucket_fail_closed(self):
        for rows in ([], [{"rateLimitType": "NEW_LIMIT"}], limits(weight=0)):
            with self.assertRaises(ValueError):
                AdmissionGovernor().observe_websocket_limits("private_ws", rows, now=100)


class ConnectionHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_preserves_frames_received_before_conninfo(self):
        connection = AsyncMock()
        event = {"e": "positionReport", "P": []}
        connection.recv.side_effect = [json.dumps(event), json.dumps({"id": 1, "status": 200, "rateLimits": limits()})]
        governor = AdmissionGovernor()
        buffered = await receive_connection_limits(connection, governor, "private_ws", 1)
        self.assertEqual(buffered, [event])
        self.assertIn("private_ws", governor.ws_verified)

    async def test_missing_conninfo_limits_blocks_connection(self):
        connection = AsyncMock()
        connection.recv.return_value = json.dumps({"id": 1, "status": 200})
        with self.assertRaisesRegex(RuntimeError, "conninfo"):
            await receive_connection_limits(connection, AdmissionGovernor(), "private_ws", 1)

    async def test_resync_keeps_100ms_subscription(self):
        socket = PublicMarketSocket(AdmissionGovernor())
        socket._send = AsyncMock()
        await socket.resync("X")
        self.assertEqual(socket._send.await_args_list[0].args[1], ["X@depth@100ms"])
        self.assertEqual(socket._send.await_args_list[1].args[1], ["X@depth@100ms"])

    async def test_websocket_terminal_cancel_avoids_rest(self):
        fixture = fixtures.RuntimeRepairTests()
        await fixture.asyncSetUp()
        runtime = fixture.runtime
        try:
            from gemini_bot.execution import OrderPurpose
            from test_review_regressions import candidate
            intent = runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
            runtime.ledger.mark_sent(intent.client_order_id)
            frame = {"e": "orderUpdate", "i": "order", "c": intent.client_order_id, "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "10", "p": ".46", "X": "OPEN"}
            runtime.account.apply(frame)
            async def cancel(_):
                async def terminal():
                    await asyncio.sleep(.02)
                    runtime.account.apply({**frame, "X": "CANCELED", "Z": "0"})
                asyncio.create_task(terminal())
            runtime.private.cancel = AsyncMock(side_effect=cancel)
            runtime.rest.active_orders = AsyncMock(side_effect=AssertionError("REST must be fallback"))
            await runtime._cancel_order_and_confirm("order")
            runtime.rest.active_orders.assert_not_awaited()
        finally:
            await fixture.asyncTearDown()

