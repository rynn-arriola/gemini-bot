"""Launch-blocking regression cases found in the September 15 code review.

All exchange transports are fake. Failures intentionally expose current defects;
these assertions describe the required behavior before commissioning.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.account_state import AccountReducer
from gemini_bot.catalog import RewardedContract
from gemini_bot.config import LiveConfig, load_policy
from gemini_bot.domain import BookTop, ContractRules, EngineState, Outcome, RewardPool
from gemini_bot.execution import IntentLedger, OrderPurpose
from gemini_bot.market_state import LocalBook
from gemini_bot.orchestrator import ProductionOrchestrator
from gemini_bot.rate_limits import AdmissionGovernor
from gemini_bot.rest_client import ApiError, GeminiReadRestClient
from gemini_bot.reward_ranking import RankInput, rank_candidate
from gemini_bot.storage import SQLiteJournal, WindowsOwnerLock
from gemini_bot.uptime import sample_and_load_progress


NOW = datetime(2026, 9, 15, 16, tzinfo=timezone.utc)


def candidate():
    return rank_candidate(RankInput(
        RewardPool("pool", D("100")),
        ContractRules("X", D(".01"), D("1"), D("1")),
        BookTop("X", D(".48"), D("100"), D(".52"), D("100"), 1, NOW),
    ), now=NOW)


class RuntimeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        strategy, risk = load_policy()
        self.runtime = ProductionOrchestrator(
            LiveConfig("account-review-fake", "fake-review-secret", "", True, strategy, risk),
            Path(self.temporary.name),
        )
        self.runtime.control_directory = Path(self.temporary.name) / "isolated-control"
        self.runtime.lock = WindowsOwnerLock(self.runtime.control_directory / "mutation-owner.lock")
        self.runtime.account.available_usd = D("100")
        self.runtime.account.confirmed_usd = D("100")
        self.runtime.account.expected_confirmed_usd = D("100")
        self.runtime.account.expected_positions = {}
        self.runtime.account.last_balance_at = time.monotonic()
        self.runtime.account.last_position_at = time.monotonic()
        self.runtime.gate.state = EngineState.QUOTING
        for field in ("terms_accepted", "balances_known", "positions_known", "open_orders_known",
                      "account_stream_live", "public_stream_live"):
            setattr(self.runtime.gate.reconciliation, field, True)
        self.runtime.private = SimpleNamespace(close=AsyncMock())
        self.time_patch = patch("gemini_bot.orchestrator._utc_now", return_value=NOW)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.runtime._expiry["X"] = NOW + timedelta(hours=2)
        book = LocalBook("X")
        book.apply_snapshot([(D(".48"), D("100"))], [(D(".52"), D("100"))], 1, 1, NOW)
        self.runtime.public.books["X"] = book
        self.runtime.universe = [RewardedContract(
            "E", RewardPool("pool", D("100")),
            ContractRules("X", D(".01"), D("1"), D("1")),
            (NOW + timedelta(hours=2)).isoformat(),
        )]

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temporary.cleanup()

    async def test_normal_depth_frame_does_not_trigger_resubscribe(self):
        async def frames():
            yield {"e": "depthUpdate", "s": "X", "U": 1, "u": 1,
                   "b": [[".48", "100"]], "a": [[".52", "100"]]}
        self.runtime.public.frames = frames
        self.runtime.public.resync = AsyncMock()
        self.runtime._recover_stream = AsyncMock(side_effect=RuntimeError("synthetic EOF"))
        with self.assertRaisesRegex(RuntimeError, "synthetic EOF"):
            await self.runtime._public_pump()
        self.runtime.public.resync.assert_not_awaited()
        self.assertIsInstance(self.runtime._recover_stream.call_args.args[1], StopAsyncIteration)
        telemetry = json.loads(self.runtime.profitability.dumps(now=NOW))
        self.assertEqual(len(telemetry["books"]["X"]), 1)

    async def test_clean_private_socket_close_removes_quoting_permission(self):
        async def messages():
            if False:
                yield {}
        self.runtime.private.messages = messages
        self.runtime._recover_stream = AsyncMock(side_effect=RuntimeError("recovery unavailable"))
        with self.assertRaisesRegex(RuntimeError, "recovery unavailable"):
            await self.runtime._private_pump()
        self.assertFalse(self.runtime.gate.may_submit)

    async def test_partial_first_fill_prevents_second_entry_leg(self):
        sent = []
        async def place(leg):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            sent.append(leg.outcome)
            partial = leg.outcome is Outcome.YES
            self.runtime.account.apply({
                "e": "orderUpdate", "i": str(len(sent)), "c": intent.client_order_id,
                "s": "X", "S": "BUY", "O": leg.outcome.value, "q": "10", "p": ".46",
                "z": "7" if partial else "10", "Z": "3" if partial else "0",
                "X": "PARTIALLY_FILLED" if partial else "OPEN", "t": "fake-trade",
            })
            return intent.client_order_id
        self.runtime.private.place_maker = place
        self.runtime._cancel_symbol_entries = AsyncMock()
        await self.runtime._quote_pair(candidate())
        self.assertEqual(sent, [Outcome.YES])
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")

    async def test_clean_zero_fill_second_leg_moc_cancel_cools_symbol_without_global_failure(self):
        sent = []
        async def place(leg):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            sent.append(leg.outcome)
            order_id = str(len(sent))
            frame = {
                "e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                "s": "X", "S": "BUY", "O": leg.outcome.value, "q": "10",
                "p": str(leg.price), "z": "10", "Z": "0", "X": "NEW",
            }
            self.runtime.account.apply(frame)
            if leg.outcome is Outcome.NO:
                self.runtime.account.apply({**frame, "z": "0", "X": "CANCELED"})
            return intent.client_order_id
        self.runtime.private.place_maker = AsyncMock(side_effect=place)
        self.runtime._cancel_symbol_entries = AsyncMock()
        self.runtime.rest = SimpleNamespace(positions=AsyncMock(return_value=[]))
        await self.runtime._quote_pair(candidate())
        self.assertEqual(sent, [Outcome.YES, Outcome.NO])
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")
        self.assertGreater(self.runtime._quote_cooldown_until["X"], 0)

    async def test_rejected_second_leg_remains_fatal(self):
        async def place(leg):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            order_id = leg.outcome.value
            self.runtime.account.apply({
                "e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                "s": "X", "S": "BUY", "O": leg.outcome.value, "q": "10",
                "p": str(leg.price), "z": "10" if leg.outcome is Outcome.YES else "0",
                "Z": "0", "X": "NEW" if leg.outcome is Outcome.YES else "REJECTED",
            })
            return intent.client_order_id
        self.runtime.private.place_maker = AsyncMock(side_effect=place)
        self.runtime._cancel_symbol_entries = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "second maker leg"):
            await self.runtime._quote_pair(candidate())
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")

    async def test_expired_contract_cannot_receive_new_quotes(self):
        self.runtime._expiry["X"] = NOW - timedelta(seconds=1)
        async def place(leg):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            self.runtime.account.apply({
                "e": "orderUpdate", "i": leg.outcome.value, "c": intent.client_order_id,
                "s": "X", "S": "BUY", "O": leg.outcome.value, "q": "10", "p": ".46",
                "z": "10", "X": "OPEN",
            })
            return intent.client_order_id
        self.runtime.private.place_maker = AsyncMock(side_effect=place)
        with patch("gemini_bot.orchestrator._utc_now", return_value=NOW):
            await self.runtime._quote_pair(candidate())
        self.runtime.private.place_maker.assert_not_awaited()

    def install_exit(self):
        local = LocalBook("X")
        local.apply_snapshot([(D(".48"), D("100"))], [(D(".52"), D("100"))], 1, 1, NOW)
        self.runtime.public.books["X"] = local
        self.runtime.account.reducer.positions["X"] = D("10")
        self.runtime._cancel_symbol_entries = AsyncMock()
        async def exit_position(leg, *, signed_position):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            order_id = f"exit-{intent.client_order_id}"
            frame = {
                "e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                "s": leg.symbol, "S": "SELL", "O": leg.outcome.value,
                "q": str(leg.quantity), "p": str(leg.price), "z": str(leg.quantity),
                "Z": "0", "X": "NEW",
            }
            self.runtime.account.apply(frame)
            self.runtime.account.apply({
                **frame, "z": "0", "Z": str(leg.quantity), "X": "CANCELED",
                "t": f"fill-{intent.client_order_id}", "L": str(leg.price),
            })
            self.runtime.account.reducer.positions.pop(leg.symbol, None)
            return intent.client_order_id
        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock()
        async def positions():
            return [{"symbol": symbol, "outcome": "YES" if signed > 0 else "NO",
                     "totalQuantity": str(abs(signed)), "avgPrice": ".4", "marketValue": str(abs(signed) * D(".4"))}
                    for symbol, signed in self.runtime.account.reducer.positions.items() if signed]
        self.runtime.rest = SimpleNamespace(positions=AsyncMock(side_effect=positions))

    async def test_exit_rechecks_quantity_after_cancel_fill_race(self):
        self.install_exit()
        async def cancel(symbol):
            self.runtime.account.reducer.positions[symbol] = D("15")
        self.runtime._cancel_symbol_entries = cancel
        with patch("gemini_bot.orchestrator._utc_now", return_value=NOW):
            await self.runtime._exit_due_positions(force=True)
        sent = self.runtime.private.exit_position.call_args.args[0]
        self.assertEqual(sent.quantity, D("15"))

    async def test_exit_rejects_stale_book(self):
        self.install_exit()
        try:
            with patch("gemini_bot.orchestrator._utc_now", return_value=NOW + timedelta(hours=1)):
                await self.runtime._exit_due_positions(force=True)
        except (ValueError, RuntimeError):
            pass
        self.runtime.private.exit_position.assert_not_awaited()

    async def test_ambiguous_exit_is_not_submitted_again_by_drain(self):
        self.install_exit()
        async def exit_position(leg, *, signed_position):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            return intent.client_order_id
        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock(side_effect=TimeoutError("fake missing ack"))
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                await self.runtime._exit_due_positions(force=True)
        self.assertEqual(self.runtime.private.exit_position.await_count, 1)

    async def test_terminal_unfilled_ioc_retries_exact_residual_then_flattens(self):
        self.install_exit()
        attempts = 0

        async def exit_position(leg, *, signed_position):
            nonlocal attempts
            attempts += 1
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            order_id = f"exit-{attempts}"
            frame = {
                "e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                "s": "X", "S": "SELL", "O": "YES", "q": "10",
                "p": str(leg.price), "z": "10", "Z": "0", "X": "NEW",
            }
            self.runtime.account.apply(frame)
            if attempts == 1:
                self.runtime.account.apply({**frame, "z": "0", "X": "CANCELED"})
            else:
                self.runtime.account.apply({
                    **frame, "z": "0", "Z": "10", "X": "CANCELED",
                    "t": "retry-fill", "L": str(leg.price),
                })
                self.runtime.account.reducer.positions.pop("X", None)
            return intent.client_order_id

        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock()
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            await self.runtime._exit_due_positions(force=True)
        self.assertEqual(self.runtime.private.exit_position.await_count, 2)
        self.assertNotIn("X", self.runtime.account.reducer.positions)
        self.assertFalse(self.runtime.journal.pending_exit("X"))
        self.assertNotIn("X", self.runtime._exit_uncertain)

    async def test_terminal_partial_ioc_retries_only_authenticated_residual(self):
        self.install_exit()
        quantities = []

        async def exit_position(leg, *, signed_position):
            quantities.append(leg.quantity)
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            order_id = f"partial-{len(quantities)}"
            frame = {
                "e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                "s": "X", "S": "SELL", "O": "YES", "q": str(leg.quantity),
                "p": str(leg.price), "z": str(leg.quantity), "Z": "0", "X": "NEW",
            }
            self.runtime.account.apply(frame)
            if len(quantities) == 1:
                self.runtime.account.apply({
                    **frame, "z": "6", "Z": "4", "X": "CANCELED",
                    "t": "partial-fill", "L": str(leg.price),
                })
                self.runtime.account.reducer.positions["X"] = D("6")
            else:
                self.runtime.account.apply({
                    **frame, "z": "0", "Z": "6", "X": "CANCELED",
                    "t": "residual-fill", "L": str(leg.price),
                })
                self.runtime.account.reducer.positions.pop("X", None)
            return intent.client_order_id

        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock()
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            await self.runtime._exit_due_positions(force=True)
        self.assertEqual(quantities, [D("10"), D("6")])

    async def test_rejected_exit_is_not_automatically_retried(self):
        self.install_exit()

        async def exit_position(leg, *, signed_position):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            frame = {
                "e": "orderUpdate", "i": "rejected-exit", "c": intent.client_order_id,
                "s": "X", "S": "SELL", "O": "YES", "q": "10",
                "p": str(leg.price), "z": "0", "Z": "0", "X": "REJECTED",
            }
            self.runtime.account.apply(frame)
            return intent.client_order_id

        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await self.runtime._exit_due_positions(force=True)
        self.runtime.private.exit_position.assert_awaited_once()
        self.assertNotIn("X", self.runtime._exit_uncertain)

    async def test_terminal_unfilled_ioc_stops_after_three_attempts(self):
        self.install_exit()

        async def exit_position(leg, *, signed_position):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            order_id = f"unfilled-{intent.client_order_id}"
            frame = {
                "e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                "s": "X", "S": "SELL", "O": "YES", "q": "10",
                "p": str(leg.price), "z": "10", "Z": "0", "X": "NEW",
            }
            self.runtime.account.apply(frame)
            self.runtime.account.apply({**frame, "z": "0", "X": "CANCELED"})
            return intent.client_order_id

        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock()
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "bounded retry authority"):
                await self.runtime._exit_due_positions(force=True)
        self.assertEqual(self.runtime.private.exit_position.await_count, 3)
        self.assertFalse(self.runtime.journal.pending_exit("X"))
        self.assertNotIn("X", self.runtime._exit_uncertain)

    async def test_terminal_exit_inventory_increase_is_not_retried(self):
        self.install_exit()

        async def exit_position(leg, *, signed_position):
            intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
            self.runtime.ledger.mark_sent(intent.client_order_id)
            frame = {
                "e": "orderUpdate", "i": "bad-residual", "c": intent.client_order_id,
                "s": "X", "S": "SELL", "O": "YES", "q": "10",
                "p": str(leg.price), "z": "0", "Z": "0", "X": "CANCELED",
            }
            self.runtime.account.apply(frame)
            self.runtime.account.reducer.positions["X"] = D("11")
            return intent.client_order_id

        self.runtime.private.exit_position = AsyncMock(side_effect=exit_position)
        self.runtime.account.wait_for_order_event = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "away from flat"):
            await self.runtime._exit_due_positions(force=True)
        self.runtime.private.exit_position.assert_awaited_once()

    async def test_position_without_deadline_does_not_silently_skip_safety(self):
        self.install_exit()
        with patch("gemini_bot.orchestrator._utc_now", return_value=NOW + timedelta(hours=1)):
            await self.runtime._exit_due_positions()
        self.assertTrue(
            self.runtime.private.exit_position.await_count or not self.runtime.gate.may_submit,
            "Missing position deadline was silently ignored while quoting remained enabled",
        )

    def test_rest_cancelled_history_resolves_unknown_intent(self):
        intent = self.runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime._resolve_prior_intents([{
            "clientOrderId": intent.client_order_id, "orderId": "fake-1", "status": "cancelled",
        }])
        self.assertEqual(self.runtime.journal.unresolved_ids(), ())

    async def test_cancel_confirmation_retries_transient_rest_without_resending_cancel(self):
        intent = self.runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.account.apply({
            "e": "orderUpdate", "i": "cancel-1", "c": intent.client_order_id,
            "s": "X", "S": "BUY", "O": "YES", "q": "10", "p": ".46",
            "z": "10", "Z": "0", "X": "OPEN",
        })
        self.runtime.private.cancel = AsyncMock()
        self.runtime.rest = SimpleNamespace(
            active_orders=AsyncMock(side_effect=[ApiError(None, "TimeoutError"), []]),
            recent_order_history=AsyncMock(return_value=[{
                "orderId": "cancel-1", "status": "cancelled",
            }]),
        )
        await self.runtime._cancel_order_and_confirm("cancel-1", timeout=2)
        self.runtime.private.cancel.assert_awaited_once_with("cancel-1")
        self.assertEqual(self.runtime.rest.active_orders.await_count, 2)
        self.runtime.rest.recent_order_history.assert_awaited_once_with(symbol="X")
        self.assertFalse(self.runtime.account.reducer.orders["cancel-1"].working)

    async def test_full_recent_history_page_is_valid_terminal_lookup_input(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        rows = [{"orderId": str(index)} for index in range(1000)]
        client.private_read = AsyncMock(return_value={"orders": rows})
        self.assertEqual(
            len(await client.recent_order_history(symbol="X")),
            1000,
        )
        client.private_read.assert_awaited_once_with(
            "/v1/prediction-markets/orders/history",
            {"limit": 1000, "offset": 0, "symbol": "X"},
        )

    def test_same_account_cannot_acquire_two_directory_locks(self):
        with tempfile.TemporaryDirectory() as other:
            other_runtime = ProductionOrchestrator(self.runtime.config, Path(other))
            other_runtime.control_directory = self.runtime.control_directory
            other_runtime.lock = WindowsOwnerLock(other_runtime.control_directory / "mutation-owner.lock")
            try:
                self.runtime.lock.acquire()
                with self.assertRaises(RuntimeError):
                    other_runtime.lock.acquire()
            finally:
                other_runtime.lock.release()
                other_runtime._logging.close()
                other_runtime.journal.close()


class StorageAndTransportReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_maker_rebate_rate_shape_is_validated(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client.public_get = AsyncMock(return_value={"rate_rules": "not-a-list"})
        with self.assertRaisesRegex(ApiError, "invalid_maker_rebate_rates_shape"):
            await client.maker_rebate_rates()

    async def test_reward_catalog_restarts_after_transient_page_duplicate(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        responses = [
            {"events": [{"event_ticker": "A"}, {"event_ticker": "B"}],
             "pagination": {"total": 3}},
            {"events": [{"event_ticker": "B"}], "pagination": {"total": 3}},
            {"events": [{"event_ticker": "A"}, {"event_ticker": "B"}],
             "pagination": {"total": 2}},
        ]
        client.public_get = AsyncMock(side_effect=responses)
        self.assertEqual(
            [row["event_ticker"] for row in await client.reward_events()],
            ["A", "B"],
        )
        self.assertEqual(client.public_get.await_count, 3)

    async def test_reward_catalog_fails_after_repeated_unstable_pages(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        responses = []
        for _ in range(3):
            responses.extend([
                {"events": [{"event_ticker": "A"}, {"event_ticker": "B"}],
                 "pagination": {"total": 3}},
                {"events": [{"event_ticker": "B"}], "pagination": {"total": 3}},
            ])
        client.public_get = AsyncMock(side_effect=responses)
        with self.assertRaisesRegex(ApiError, "unstable_reward_catalog"):
            await client.reward_events()

    async def test_rest_429_installs_server_cooldown(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client._open_json = Mock(side_effect=ApiError(429, "RateLimit", retry_after=120))
        with self.assertRaises(ApiError):
            await client.reward_config()
        self.assertGreater(client.governor.lanes["public_rest"].delay(), 119)

    async def test_idempotent_rest_read_retries_transient_failures_through_governor(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client._open_json = Mock(side_effect=[
            ApiError(None, "TimeoutError"), ApiError(503, "Unavailable"),
            {"enabled": True},
        ])
        with patch("gemini_bot.rest_client.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await client.reward_config(), {"enabled": True})
        self.assertEqual(client._open_json.call_count, 3)

    async def test_private_read_retry_rebuilds_authentication_nonce(self):
        client = GeminiReadRestClient(
            AdmissionGovernor(), api_key="account-fake", api_secret="fake-secret",
        )
        requests = []
        def read(request):
            requests.append(request)
            if len(requests) == 1:
                raise ApiError(None, "TimeoutError")
            return {"ok": True}
        client._open_json = read
        with (
            patch("gemini_bot.rest_client.time.time", side_effect=[100, 101]),
            patch("gemini_bot.rest_client.asyncio.sleep", new=AsyncMock()),
        ):
            self.assertEqual(await client.terms_status(), {"ok": True})
        nonces = [
            json.loads(base64.b64decode(request.get_header("X-gemini-payload")))["nonce"]
            for request in requests
        ]
        self.assertEqual(nonces, [100, 101])

    async def test_count_is_page_length_not_total_result_count(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        async def page(path, fields):
            offset = fields["offset"]
            rows = [{"orderId": index} for index in range(offset, min(offset + 100, 150))]
            return {"orders": rows, "pagination": {"limit": 100, "offset": offset, "count": len(rows)}}
        client.private_read = AsyncMock(side_effect=page)
        self.assertEqual(len(await client.order_history()), 150)

    def test_account_snapshot_replacement_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(Path(directory) / "review.sqlite")
            try:
                row = {"symbol": "X", "outcome": "YES", "signed_quantity": "10",
                       "average_price": ".4", "market_value": "4"}
                journal.replace_account_snapshot([row], [], observed_at=NOW)
                with self.assertRaises(KeyError):
                    journal.replace_account_snapshot([], [{}], observed_at=NOW)
                self.assertEqual(journal.load_position_quantities(), {"X": D("10")})
            finally:
                journal.close()

    def test_unrelated_cash_change_near_fill_is_not_classified_as_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(Path(directory) / "review.sqlite")
            try:
                account = AccountRuntime(IntentLedger(journal), journal)
                with patch("gemini_bot.account_runtime.time.monotonic", return_value=100):
                    account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "100", "f": "100"}]})
                    account._known_account_activity()
                with patch("gemini_bot.account_runtime.time.monotonic", return_value=101):
                    account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "1100", "f": "1100"}]})
                self.assertFalse(account.equity_classification_complete)
            finally:
                journal.close()

    def test_local_uptime_does_not_forgive_unobserved_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(Path(directory) / "review.sqlite")
            try:
                contract = RewardedContract(
                    "E", RewardPool("pool", D("100")),
                    ContractRules("X", D(".01"), D("1"), D("1")),
                    "2026-09-16T00:00:00Z", "2026-09-15T16:00:00Z", "2026-09-15T18:00:00Z",
                )
                account = AccountReducer()
                sample_and_load_progress(journal, [contract], account, now=NOW)
                progress = sample_and_load_progress(
                    journal, [contract], account, now=NOW + timedelta(minutes=61),
                )["pool"]
                self.assertGreaterEqual(progress.eligible_snapshots, 61)
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
