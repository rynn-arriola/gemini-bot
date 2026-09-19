"""Synthetic fault boundaries; never connect to an exchange or live journal."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from decimal import Decimal as D
from dataclasses import replace

import test_repair_integration as fixtures
import test_continuous_service as retention_fixtures
from gemini_bot.continuity import RestingObservation
from gemini_bot.domain import EngineState
from gemini_bot.execution import IntentState
from gemini_bot.live_ws import LiveWebSocket, TransportChangedBeforeSend, AdmissionTimeoutBeforeSend
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.rate_limits import AdmissionGovernor, Priority
from gemini_bot.recovery_policy import transient_failure
from gemini_bot.rest_client import ApiError, GeminiReadRestClient
from gemini_bot.ws_limits import receive_connection_limits, observe_response
from gemini_bot.ws_requests import RequestTracker


class ResponseTests(unittest.TestCase):
    def test_only_correlated_read_errors_are_recoverable(self):
        for method in ("SUBSCRIBE", "UNSUBSCRIBE", "conninfo"):
            for status in (429, 500, 502, 503, 504):
                with self.subTest(method=method, status=status):
                    tracker = RequestTracker()
                    tracker.register(1, method)
                    with self.assertRaises(ApiError) as caught:
                        tracker.response({"id": 1, "status": status})
                    self.assertTrue(transient_failure(caught.exception))
                    self.assertFalse(tracker.pending)

    def test_mutation_errors_never_become_read_retries(self):
        for method in ("order.place", "order.cancel", "order.cancel_all"):
            for status in (400, 401, 418, 429, 500):
                tracker = RequestTracker()
                tracker.register(1, method)
                with self.assertRaises(RuntimeError) as caught:
                    tracker.response({"id": 1, "status": status})
                self.assertFalse(transient_failure(caught.exception))

    def test_uncorrelated_success_or_error_cannot_establish_readiness(self):
        for status in (200, 429, 500):
            with self.assertRaisesRegex(RuntimeError, "uncorrelated"):
                RequestTracker().response({"id": 9, "status": status})

    def test_read_auth_ban_and_malformed_responses_stay_fatal(self):
        for status in (400, 401, 403, 418, "429", None):
            tracker = RequestTracker()
            tracker.register(1, "SUBSCRIBE")
            with self.assertRaises(RuntimeError) as caught:
                tracker.response({"id": 1, "status": status})
            self.assertFalse(transient_failure(caught.exception))

    def test_correlation_is_bounded_and_stream_events_do_not_consume_it(self):
        tracker = RequestTracker()
        for i in range(256):
            tracker.register(i, "SUBSCRIBE")
        self.assertIsNone(tracker.response({"e": "positionReport"}))
        with self.assertRaises(RuntimeError):
            tracker.register(256, "SUBSCRIBE")
        self.assertEqual(tracker.response({"id": 0, "status": 200}), "SUBSCRIBE")
        tracker.register(256, "SUBSCRIBE")

    def test_longer_server_cooldown_survives_error_classification(self):
        governor = AdmissionGovernor()
        tracker = RequestTracker()
        tracker.register(1, "SUBSCRIBE")
        frame = {"id": 1, "status": 429, "retryAfter": 180}
        observe_response(governor, "private_ws", frame)
        with self.assertRaises(ApiError) as caught:
            tracker.response(frame)
        self.assertEqual(caught.exception.retry_after, 180)
        self.assertGreater(governor.lanes["private_ws"].delay(), 179)
        self.assertGreater(governor.lanes["public_ws"].delay(), 179)


class AvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continuous_service = True
        self.r.account.subscription_acknowledged = True
        self.r.rest.positions = AsyncMock(return_value=[])
        self.r.rest.active_orders = AsyncMock(return_value=[])
        self.r.public.note_message(fixtures.NOW)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def socket(self):
        socket = LiveWebSocket("synthetic", "synthetic", self.r.governor, self.r.ledger)
        socket._connection = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        return socket

    async def test_cancel_timeout_includes_admission(self):
        async def stalled(_):
            await asyncio.Event().wait()
        self.r.private.cancel = AsyncMock(side_effect=stalled)
        with self.assertRaises(TimeoutError):
            await self.r._cancel_order_and_confirm("o", timeout=.01)
        with self.assertRaises(TimeoutError):
            await self.r._cancel_order_and_confirm("o", timeout=.01)
        self.assertEqual(self.r.private.cancel.await_count, 1)

    async def test_correlated_outage_cancel_budget_scales_and_stays_bounded(self):
        from types import SimpleNamespace
        from gemini_bot.domain import Side
        self.r._stream_holds.add("private")
        self.r.account.reducer.orders = {
            str(index): SimpleNamespace(working=True, side=Side.BUY)
            for index in range(40)
        }
        try:
            self.assertEqual(self.r._cancel_confirmation_budget(45), 165)
            self.assertEqual(self.r._cancel_confirmation_budget(.01), .01)
            self.r.account.reducer.orders.update({
                "extra-" + str(index): SimpleNamespace(working=True, side=Side.BUY)
                for index in range(40)
            })
            self.assertEqual(self.r._cancel_confirmation_budget(45), 180)
        finally:
            self.r.account.reducer.orders.clear()
            self.r._stream_holds.clear()
        self.assertEqual(self.r._cancel_confirmation_budget(45), 45)

    async def test_cancel_proof_reads_coalesce_during_full_width_recovery(self):
        active_started, active_release = asyncio.Event(), asyncio.Event()
        async def active_orders():
            active_started.set()
            await active_release.wait()
            return []
        self.r.rest.active_orders = AsyncMock(side_effect=active_orders)
        active_tasks = [
            asyncio.create_task(self.r._coalesced_cancel_active_orders())
            for _ in range(40)
        ]
        await active_started.wait()
        active_release.set()
        self.assertEqual(await asyncio.gather(*active_tasks), [[]] * 40)
        self.assertEqual(self.r.rest.active_orders.await_count, 1)

        history_started, history_release = asyncio.Event(), asyncio.Event()
        async def history(*, symbol):
            history_started.set()
            await history_release.wait()
            return [{"symbol": symbol}]
        self.r.rest.recent_order_history = AsyncMock(side_effect=history)
        history_tasks = [
            asyncio.create_task(self.r._coalesced_cancel_history("X"))
            for _ in range(20)
        ]
        await history_started.wait()
        history_release.set()
        rows = await asyncio.gather(*history_tasks)
        self.assertTrue(all(value == [{"symbol": "X"}] for value in rows))
        self.assertEqual(self.r.rest.recent_order_history.await_count, 1)

    async def test_cancelled_proof_waiter_does_not_orphan_shared_rest_read(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def active_orders():
            started.set()
            await release.wait()
            return []
        self.r.rest.active_orders = AsyncMock(side_effect=active_orders)
        first = asyncio.create_task(self.r._coalesced_cancel_active_orders())
        await started.wait()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = asyncio.create_task(self.r._coalesced_cancel_active_orders())
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await second, [])
        self.assertEqual(self.r.rest.active_orders.await_count, 1)

    async def test_cancel_task_rechecks_terminal_before_send(self):
        order = SimpleNamespace(working=True)
        self.r.account.reducer.orders["o"] = order
        self.r.private.cancel = AsyncMock()
        task = asyncio.create_task(self.r._cancel_order_and_confirm("o"))
        await asyncio.sleep(0)
        order.working = False
        await task
        self.r.private.cancel.assert_not_awaited()
        self.r.account.reducer.orders.clear()

    async def test_drain_cancels_other_entries_before_exit_worker_finishes(self):
        worker = asyncio.create_task(asyncio.Event().wait())
        self.r._exit_workers["X"] = worker
        intent = self.r.ledger.prepare(fixtures.candidate().quote.yes, fixtures.OrderPurpose.ENTRY)
        self.r.account.reducer.orders["o"] = SimpleNamespace(working=True, side=fixtures.Side.BUY, order_id="o",
                                                            client_order_id=intent.client_order_id)
        self.r._cancel_order_and_confirm = AsyncMock()
        self.r._exit_due_positions = AsyncMock()
        try:
            with patch("gemini_bot.orchestrator._DRAIN_JOIN_SECONDS", .02):
                with self.assertRaises(TimeoutError):
                    await self.r.drain()
            self.r._cancel_order_and_confirm.assert_awaited_once_with("o")
            self.r._exit_due_positions.assert_not_awaited()
            self.assertFalse(worker.cancelled())
            self.assertEqual(self.r.gate.state, EngineState.DRAINING)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self.r._exit_workers.clear()
            self.r.account.reducer.orders.clear()

    async def test_drain_preserves_worker_failure_and_refuses_forced_retry(self):
        async def fail():
            raise RuntimeError("synthetic unknown exit")
        worker = asyncio.create_task(fail())
        self.r._exit_workers["X"] = worker
        self.r._exit_due_positions = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "unknown exit"):
            await self.r.drain()
        self.r._exit_due_positions.assert_not_awaited()

    async def test_drain_rest_wait_is_bounded(self):
        async def wait():
            await asyncio.Event().wait()
        self.r.rest.active_orders = AsyncMock(side_effect=wait)
        with patch("gemini_bot.orchestrator._DRAIN_SECONDS", .01):
            with self.assertRaises(TimeoutError):
                await self.r.drain()

    async def test_early_drain_never_cancels_unowned_local_entry(self):
        self.r.account.reducer.orders["foreign"] = SimpleNamespace(
            working=True, side=fixtures.Side.BUY, order_id="foreign", client_order_id="not-owned")
        self.r._cancel_order_and_confirm = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "unowned local"):
            await self.r.drain()
        self.r._cancel_order_and_confirm.assert_not_awaited()
        self.r.account.reducer.orders.clear()

    async def test_read_only_exit_wait_times_out_without_resetting_deadline(self):
        self.r.account.block_entries("X", fill=True)
        self.r.journal.persist_deadline("X", "YES", fixtures.NOW, fixtures.NOW, D(10))
        async def wait(_):
            await asyncio.Event().wait()
        self.r._complete_flat_fill = AsyncMock(side_effect=wait)
        with patch("gemini_bot.orchestrator._EXIT_WORKER_SECONDS", .01):
            await self.r._run_exit_worker("X")
        self.assertIn("X", self.r._exit_waits)
        self.assertEqual(self.r.journal.get_deadline("X", "YES"), fixtures.NOW)
        self.assertFalse(self.r._isolated_exit_proof_current("X"))

    async def test_unknown_exit_worker_timeout_is_not_a_retry_wait(self):
        self.r._exit_uncertain.add("X")
        async def wait(_):
            await asyncio.Event().wait()
        self.r._complete_flat_fill = AsyncMock(side_effect=wait)
        with patch("gemini_bot.orchestrator._EXIT_WORKER_SECONDS", .01):
            with self.assertRaises(TimeoutError):
                await self.r._run_exit_worker("X")
        self.assertNotIn("X", self.r._exit_waits)
        self.assertIn("X", self.r._exit_uncertain)

    async def test_socket_admission_timeout_terminalizes_only_unsent_intent(self):
        socket = self.socket()
        socket.send_timeout_seconds = .01
        socket._governor = SimpleNamespace(admit_websocket=lambda *_: 100)
        with self.assertRaises(AdmissionTimeoutBeforeSend):
            await socket.place_maker(fixtures.candidate().quote.yes)
        socket._connection.send.assert_not_awaited()
        self.assertFalse(self.r.journal.unresolved_ids())

    async def test_attempted_send_timeout_remains_unknown(self):
        socket = self.socket()
        socket.send_timeout_seconds = .01
        async def stalled(_):
            await asyncio.Event().wait()
        socket._connection.send = AsyncMock(side_effect=stalled)
        with self.assertRaises(TimeoutError):
            await socket.place_maker(fixtures.candidate().quote.yes)
        self.assertEqual(len(self.r.journal.unresolved_ids()), 1)

    async def test_outer_cancellation_during_admission_closes_unsent_intent(self):
        socket = self.socket()
        socket._governor = SimpleNamespace(admit_websocket=lambda *_: 100)
        task = asyncio.create_task(socket.place_maker(fixtures.candidate().quote.yes))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        socket._connection.send.assert_not_awaited()
        self.assertFalse(self.r.journal.unresolved_ids())

    async def test_disconnect_during_pacing_is_typed_unsent(self):
        socket = self.socket()
        entered = asyncio.Event()
        def wait(*_):
            entered.set()
            return .01
        socket._governor = SimpleNamespace(admit_websocket=wait)
        task = asyncio.create_task(socket.place_maker(fixtures.candidate().quote.yes))
        await entered.wait()
        await socket.close()
        with self.assertRaises(TransportChangedBeforeSend):
            await task
        self.assertFalse(self.r.journal.unresolved_ids())

    async def test_unsent_quote_transport_change_enters_recovery_without_replay(self):
        self.r._quote_pair_current = AsyncMock(side_effect=TransportChangedBeforeSend("synthetic"))
        self.r._recover_stream = AsyncMock()
        self.r._cancel_symbol_entries = AsyncMock()
        await self.r._quote_pair_locked(fixtures.candidate())
        self.r._recover_stream.assert_awaited_once()
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertEqual(self.r._quote_pair_current.await_count, 1)

    async def test_existing_private_recovery_is_not_started_twice(self):
        self.r._stream_holds.add("private")
        self.r._quote_pair_current = AsyncMock(side_effect=TransportChangedBeforeSend("synthetic"))
        self.r._recover_stream = AsyncMock()
        await self.r._quote_pair_locked(fixtures.candidate())
        self.r._recover_stream.assert_not_awaited()

    async def test_terminal_order_skips_cancel_after_pacing(self):
        socket = self.socket()
        ended = False
        socket.cancel_is_terminal = lambda _: ended
        entered = asyncio.Event()
        def wait(*_):
            entered.set()
            return .01
        socket._governor = SimpleNamespace(admit_websocket=wait)
        task = asyncio.create_task(socket.cancel("o"))
        await entered.wait()
        ended = True
        await task
        socket._connection.send.assert_not_awaited()

    async def test_public_disconnect_after_pacing_never_uses_replacement(self):
        socket = PublicMarketSocket(AdmissionGovernor())
        old = SimpleNamespace(send=AsyncMock())
        replacement = SimpleNamespace(send=AsyncMock())
        socket._connection = old
        calls = 0
        def admit(*_):
            nonlocal calls
            calls += 1
            return .01 if calls == 1 else 0
        socket._governor = SimpleNamespace(admit_websocket=admit)
        task = asyncio.create_task(socket._send("SUBSCRIBE", [], Priority.MARKET_RESYNC))
        await asyncio.sleep(0)
        socket._connection = replacement
        with self.assertRaises(ConnectionError):
            await task
        old.send.assert_not_awaited()
        replacement.send.assert_not_awaited()

    async def test_handshake_429_is_typed_read_failure(self):
        connection = SimpleNamespace(recv=AsyncMock(return_value='{"id":1,"status":429}'))
        with self.assertRaises(ApiError) as caught:
            await receive_connection_limits(connection, self.r.governor, "private_ws", 1)
        self.assertTrue(transient_failure(caught.exception))

    async def test_read_reply_enters_private_stream_recovery(self):
        async def messages():
            raise ApiError(429, "WebSocketReadServiceUnavailable", 60)
            yield
        self.r.private.messages = messages
        entered = asyncio.Event()
        async def recover(*_, **__):
            entered.set()
            await asyncio.Event().wait()
        self.r._recover_stream = AsyncMock(side_effect=recover)
        task = asyncio.create_task(self.r._private_pump())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.r.gate.state, EngineState.QUOTING)
            self.assertEqual(self.r._recover_stream.await_args.args[0], "private")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_rest_admission_bound_keeps_server_cooldown(self):
        client = GeminiReadRestClient(self.r.governor, api_key="synthetic", api_secret="synthetic")
        client.operation_timeout_seconds = .01
        self.r.governor.apply_server_cooldown("private_rest", 180)
        with self.assertRaises(ApiError) as caught:
            await client.positions()
        self.assertTrue(transient_failure(caught.exception))
        self.assertGreater(self.r.governor.lanes["private_rest"].delay(), 179)

    def pause_resume(self):
        self.r._handle_contract_status({"s": "X", "n": "paused"})
        self.r._handle_contract_status({"s": "X", "n": "active"})
        return self.r._market_status_versions["X"]

    async def test_active_event_alone_does_not_release_hold(self):
        self.pause_resume()
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_recovered_market_releases_only_after_terminal_flat_proof(self):
        version = self.pause_resume()
        await self.r._release_recovered_market("X", version)
        self.assertNotIn("X", self.r.account.blocked_entries)
        self.r.rest.active_orders.assert_awaited_once()

    async def test_recovered_hold_preserves_fill_and_manual_holds(self):
        version = self.pause_resume()
        self.r.account.block_entries("X", fill=True)
        self.r.account.block_entries("X")
        await self.r._release_recovered_market("X", version)
        self.assertNotIn("X", self.r.account.entry_hold_reasons)
        self.assertIn("X", self.r.account.blocked_entries)
        self.r.account.release_fill_block("X")
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_new_lifecycle_change_during_proof_keeps_hold(self):
        version = self.pause_resume()
        async def changed():
            self.r._handle_contract_status({"s": "X", "n": "paused"})
            return []
        self.r.rest.active_orders.side_effect = changed
        await self.r._release_recovered_market("X", version)
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_stale_or_unknown_state_cannot_release_hold(self):
        version = self.pause_resume()
        self.r.account.last_position_at = 0
        await self.r._release_recovered_market("X", version)
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_inventory_lifecycle_change_is_scoped_with_complete_proof(self):
        self.r.account.last_balance_at = time.monotonic()
        self.r.account.last_position_at = time.monotonic()
        self.r.account.reducer.positions["X"] = D(1)
        self.r.account.expected_positions = {"X": D(1)}
        self.r.journal.persist_deadline(
            "X", "YES", fixtures.NOW,
            fixtures.NOW + fixtures.timedelta(minutes=5), D(1),
        )
        self.r._handle_contract_status({"s": "X", "n": "paused"})
        self.assertTrue(self.r.gate.may_submit)
        self.assertIn("X", self.r.account.fill_blocked_entries)

    async def test_inventory_lifecycle_change_without_deadline_still_requires_attention(self):
        self.r.account.last_balance_at = time.monotonic()
        self.r.account.last_position_at = time.monotonic()
        self.r.account.reducer.positions["X"] = D(1)
        self.r.account.expected_positions = {"X": D(1)}
        with self.assertRaisesRegex(RuntimeError, "scoped inventory proof"):
            self.r._handle_contract_status({"s": "X", "n": "paused"})

    async def test_temporarily_unrewarded_owner_is_kept_for_recovery(self):
        current = self.r.universe[0]
        self.r.universe = [replace(current, pool=replace(current.pool, active=False))]
        self.r.account.block_entries("X", reason="reward_metadata")
        self.r.public.unsubscribe = AsyncMock()
        with patch("gemini_bot.orchestrator.RewardUniverseLoader.load", new=AsyncMock(return_value=[])):
            await self.r._rollover_flat_markets([])
        self.r.public.unsubscribe.assert_not_awaited()
        self.assertEqual(self.r.universe[0].rules.symbol, "X")

    async def test_successful_metadata_refresh_releases_recovered_hold(self):
        self.pause_resume()
        self.r.rest.reward_config = AsyncMock(return_value={"enabled": True})
        self.r.rest.reward_events = AsyncMock(return_value=[{"event_ticker": "E", "daily_pool_usd": "10"}])
        self.r.rest.event = AsyncMock(return_value={})
        with patch("gemini_bot.orchestrator.join_reward_contracts", return_value=self.r.universe):
            self.assertTrue(await self.r._reward_pools_still_active())
        self.assertNotIn("X", self.r.account.blocked_entries)

    async def test_missing_terminal_intent_keeps_recovered_market_blocked(self):
        version = self.pause_resume()
        intent = self.r.ledger.prepare(fixtures.candidate().quote.yes, fixtures.OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        await self.r._release_recovered_market("X", version)
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_transport_classifies_correlated_subscription_failure(self):
        socket = self.socket()
        class Connection:
            def __aiter__(self):
                return self
            async def __anext__(self):
                return '{"id":1,"status":429}'
        socket._connection = Connection()
        socket._requests.register(1, "SUBSCRIBE")
        with self.assertRaises(ApiError):
            await anext(socket.messages())
        self.assertGreater(self.r.governor.lanes["private_ws"].delay(), 59)

    async def test_late_old_connection_response_uses_old_correlation(self):
        socket = self.socket()
        queue = asyncio.Queue()
        class Connection:
            def __aiter__(self):
                return self
            async def __anext__(self):
                return await queue.get()
            async def close(self):
                pass
        socket._connection = Connection()
        socket._requests.register(1, "SUBSCRIBE")
        frames = socket.messages()
        task = asyncio.create_task(anext(frames))
        await asyncio.sleep(0)
        await socket.close()
        await queue.put('{"id":1,"status":200}')
        self.assertEqual((await task)["status"], 200)
        self.assertFalse(socket._requests.pending)
        await frames.aclose()


class ChurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = retention_fixtures.RestingRetentionTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.r
        self.c = self.fixture.c
        self.r._sticky_pool_ids = {self.c.owner_key}

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_repeated_safe_farther_changes_never_replace(self):
        for order in self.r.account.reducer.orders.values():
            order.price -= D(".01")
        for _ in range(20):
            await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertEqual(self.r._retained_pairs, 20)
        self.assertFalse(self.r._quote_cancel_reasons)

    async def test_adverse_cancellation_is_reason_coded(self):
        self.r.account.reducer.orders["YES"].price += D(".01")
        await self.r._cancel_too_aggressive_quote("X")
        self.assertEqual(self.r._quote_cancel_reasons["inside_configured_tick_distance"], 1)
        self.r._cancel_symbol_entries.assert_awaited_once_with("X")

    async def test_unprofitable_retention_still_replaces_with_reason(self):
        self.r.account.reducer.orders["YES"].price = D(".39")
        await self.r._maintain_candidate(self.c)
        self.assertEqual(self.r._quote_cancel_reasons["reward_distance_or_economics"], 1)

    async def test_short_gap_is_observed_between_clock_ticks(self):
        clock = time.monotonic()
        with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock):
            self.r._observe_resting()
        self.r.account.reducer.orders["YES"].status = "CANCELED"
        with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock + .05):
            self.r._account_event("order_transition", {"symbol": "X", "side": "BUY", "status": "CANCELED"})
        self.r.account.reducer.orders["YES"].status = "NEW"
        with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock + .10):
            self.r._account_event("order_transition", {"symbol": "X", "side": "BUY", "status": "NEW"})
        view = self.r._continuity_view()["two_sided_symbols"]["X"]
        self.assertEqual(view["gap_count"], 1)
        self.assertGreater(view["last_gap_seconds"], 0)
        events = self.r._continuity_view()["symbols"]["X"]
        self.assertEqual(events["order_cancel_events"], 1)
        self.assertEqual(events["order_new_events"], 1)

    def test_missing_observation_is_never_credited_as_uptime(self):
        value = RestingObservation()
        value.observe(0, True)
        value.observe(10, True)
        self.assertEqual(value.qualifying_seconds, 0)
        self.assertEqual(value.unobserved_seconds, 10)


if __name__ == "__main__":
    unittest.main()
