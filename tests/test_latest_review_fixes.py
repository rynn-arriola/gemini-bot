"""Regression evidence for the September 15 follow-up review; offline only."""
from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as repair_fixtures
from test_repair_integration import NOW
from test_review_regressions import candidate
from gemini_bot.cli import _write_live_runner_result
from gemini_bot.domain import EngineState
from gemini_bot.execution import IntentState, OrderPurpose
from gemini_bot.live_ws import EntryStateChangedBeforeSend, QuoteChangedBeforeSend
from gemini_bot.persistent_risk import PersistentRiskWindow
from gemini_bot.rate_limits import AdmissionGovernor
from gemini_bot.rest_client import ApiError, GeminiReadRestClient
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.storage import AuthenticatedCashAdjustment, SQLiteJournal


class SupervisorResultTests(unittest.TestCase):
    def test_runner_result_is_atomic_and_session_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            _write_live_runner_result(
                path, session_id="session-a", exit_code=5,
                outcome="recoverable_flat",
            )
            result = json.loads((path / "runner-result.json").read_text())
            self.assertEqual(result, {
                "session_id": "session-a",
                "exit_code": 5,
                "outcome": "recoverable_flat",
            })


class NonTradingCashTests(unittest.TestCase):
    def test_authenticated_rebate_reconciles_cash_without_changing_realized_pnl(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                store.set_runtime_state(
                    "expected_confirmed_usd", "100", updated_at=NOW,
                )
                store.record_fill(
                    trade_id="buy", order_id="buy-order", client_order_id="buy-client",
                    symbol="X", side="BUY", outcome="YES", quantity=D("10"),
                    price=D(".40"), fee=D(".01"), event_at=NOW,
                )
                store.record_fill(
                    trade_id="sell", order_id="sell-order", client_order_id="sell-client",
                    symbol="X", side="SELL", outcome="YES", quantity=D("10"),
                    price=D(".45"), fee=D(".01"), event_at=NOW + timedelta(seconds=1),
                )
                window = RiskWindowPolicy(D("30")).bounds(NOW)
                pnl_before = store.realized_pnl_for_window(*window)
                count = store.reconcile_authenticated_cash_adjustments(
                    expected_cash=D("100"), confirmed_cash=D("100.08"),
                    adjustments=(AuthenticatedCashAdjustment(
                        "maker_rebate", "19922", D(".08"),
                        NOW + timedelta(minutes=1),
                    ),),
                    observed_at=NOW + timedelta(minutes=2),
                )
                self.assertEqual(count, 1)
                self.assertEqual(store.get_runtime_state("expected_confirmed_usd"), "100.08")
                self.assertEqual(store.realized_pnl_for_window(*window), pnl_before)
            finally:
                store.close()

    def test_authenticated_deposit_and_withdrawal_are_nontrading_cash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                store.set_runtime_state(
                    "expected_confirmed_usd", "100", updated_at=NOW,
                )
                self.assertEqual(store.reconcile_authenticated_cash_adjustments(
                    expected_cash=D("100"), confirmed_cash=D("110"),
                    adjustments=(AuthenticatedCashAdjustment(
                        "deposit", "deposit-1", D("10"),
                        NOW + timedelta(minutes=1),
                    ),),
                    observed_at=NOW + timedelta(minutes=2),
                ), 1)
                self.assertEqual(store.reconcile_authenticated_cash_adjustments(
                    expected_cash=D("110"), confirmed_cash=D("104.50"),
                    adjustments=(AuthenticatedCashAdjustment(
                        "withdrawal", "withdrawal-1", D("-5.50"),
                        NOW + timedelta(minutes=3),
                    ),),
                    observed_at=NOW + timedelta(minutes=4),
                ), 1)
                self.assertEqual(store.get_runtime_state("expected_confirmed_usd"), "104.50")
                self.assertEqual(
                    store.realized_pnl_for_window(*RiskWindowPolicy(D("30")).bounds(NOW)),
                    (D("0"), True),
                )
            finally:
                store.close()

    def test_unmatched_external_cash_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                store.set_runtime_state(
                    "expected_confirmed_usd", "100", updated_at=NOW,
                )
                with self.assertRaisesRegex(RuntimeError, "exactly match"):
                    store.reconcile_authenticated_cash_adjustments(
                        expected_cash=D("100"), confirmed_cash=D("90"),
                        adjustments=(AuthenticatedCashAdjustment(
                            "maker_rebate", "irrelevant", D(".08"),
                            NOW + timedelta(minutes=1),
                        ),),
                        observed_at=NOW + timedelta(minutes=2),
                    )
                self.assertEqual(store.get_runtime_state("expected_confirmed_usd"), "100")
            finally:
                store.close()

    def test_duplicate_or_unapproved_cash_credit_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                store.set_runtime_state(
                    "expected_confirmed_usd", "100", updated_at=NOW,
                )
                duplicate = AuthenticatedCashAdjustment(
                    "maker_rebate", "same", D(".04"), NOW + timedelta(minutes=1),
                )
                with self.assertRaisesRegex(RuntimeError, "duplicate identity"):
                    store.reconcile_authenticated_cash_adjustments(
                        expected_cash=D("100"), confirmed_cash=D("100.08"),
                        adjustments=(duplicate, duplicate),
                        observed_at=NOW + timedelta(minutes=2),
                    )
                with self.assertRaisesRegex(RuntimeError, "unapproved source"):
                    store.reconcile_authenticated_cash_adjustments(
                        expected_cash=D("100"), confirmed_cash=D("100.08"),
                        adjustments=(AuthenticatedCashAdjustment(
                            "mystery", "not-an-incentive", D(".08"),
                            NOW + timedelta(minutes=1),
                        ),),
                        observed_at=NOW + timedelta(minutes=2),
                    )
            finally:
                store.close()


class IncentiveRestTests(unittest.IsolatedAsyncioTestCase):
    async def test_incentive_reads_use_documented_query_parameters(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client.private_read = AsyncMock(side_effect=[
            {"payouts": [{"id": 1}]},
            {"daily_summaries": [{"payout_date": "2026-09-15"}]},
        ])
        self.assertEqual(await client.maker_rebate_payouts(), [{"id": 1}])
        self.assertEqual(
            await client.liquidity_reward_daily(
                date_from="2026-09-08", date_to="2026-09-15",
            ),
            [{"payout_date": "2026-09-15"}],
        )
        self.assertEqual(
            client.private_read.await_args_list[0].kwargs,
            {"method": "POST", "query_params": {"limit": 100, "offset": 0}},
        )
        self.assertEqual(
            client.private_read.await_args_list[1].kwargs,
            {
                "method": "GET",
                "query_params": {"dateFrom": "2026-09-08", "dateTo": "2026-09-15"},
            },
        )

    async def test_transfer_history_uses_bounded_slow_lane(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client._private_list_read = AsyncMock(return_value=[{"eid": 1}])
        self.assertEqual(await client.past_transfers(since=NOW), [{"eid": 1}])
        client._private_list_read.assert_awaited_once_with(
            "/v2/transfers",
            {"timestamp": int(NOW.timestamp() * 1000), "limit_transfers": 50},
            lane="transfer_rest",
        )
        self.assertEqual(client.governor.lanes["transfer_rest"].minimum_interval_seconds, 5)

    async def test_full_payout_or_transfer_window_fails_closed(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client.private_read = AsyncMock(return_value={
            "payouts": [{"id": value} for value in range(2)],
        })
        with self.assertRaisesRegex(ApiError, "window_ceiling"):
            await client.maker_rebate_payouts(limit=2)
        client._private_list_read = AsyncMock(
            return_value=[{"eid": value} for value in range(50)]
        )
        with self.assertRaisesRegex(ApiError, "window_ceiling"):
            await client.past_transfers(since=NOW)

    async def test_private_reward_query_is_in_url_not_signed_payload(self):
        client = GeminiReadRestClient(
            AdmissionGovernor(), api_key="account-fake", api_secret="fake-secret",
        )
        requests = []
        def read(request):
            requests.append(request)
            return {"payouts": []}
        client._open_json = read
        await client.maker_rebate_payouts(limit=25, offset=50)
        self.assertTrue(requests[0].full_url.endswith("?limit=25&offset=50"))
        payload = json.loads(base64.b64decode(
            requests[0].get_header("X-gemini-payload")
        ))
        self.assertEqual(payload["request"], "/v1/prediction-markets/maker-rebate/payouts")
        self.assertNotIn("limit", payload)
        self.assertNotIn("offset", payload)


class RecentRuntimeFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = repair_fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.runtime = self.fixture.runtime

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_send_rejects_price_that_moved_inside_two_ticks(self):
        payload = self.fixture.payload()
        self.runtime.public.books["X"].apply_snapshot(
            [(D(".46"), D("100"))], [(D(".52"), D("100"))], 2, 2, NOW,
        )
        with self.assertRaises(QuoteChangedBeforeSend):
            self.runtime._validate_send(payload)

    async def test_exact_paid_rebate_repairs_cash_classification_only(self):
        self.runtime.journal.set_runtime_state(
            "expected_confirmed_usd", "100", updated_at=NOW,
        )
        self.runtime.journal.set_runtime_state(
            "cash_reconciliation_watermark", "baseline", updated_at=NOW,
        )
        self.runtime.account.expected_confirmed_usd = D("100")
        self.runtime.account.confirmed_usd = D("100.08")
        self.runtime.rest.maker_rebate_payouts = AsyncMock(return_value=[{
            "id": 19922,
            "total_rebate_usd": ".08",
            "status": "PAID",
            "paid_at": (NOW + timedelta(minutes=1)).isoformat(),
        }])
        self.runtime.rest.liquidity_reward_daily = AsyncMock(return_value=[])
        self.runtime.rest.past_transfers = AsyncMock(return_value=[])
        self.runtime.rest.positions = AsyncMock(return_value=[])
        self.runtime.rest.active_orders = AsyncMock(return_value=[])
        self.assertEqual(await self.runtime._risk_mark(), 30)
        self.assertFalse(self.runtime.account.equity_classification_complete)
        with patch(
            "gemini_bot.orchestrator._utc_now",
            return_value=NOW + timedelta(minutes=2),
        ):
            self.assertEqual(
                await self.runtime._reconcile_authenticated_nontrading_cash(), 1,
            )
        self.assertTrue(self.runtime.account.equity_classification_complete)
        self.assertEqual(await self.runtime._risk_mark(), 30)
        self.assertEqual(self.runtime.journal.realized_pnl_for_window(
            *RiskWindowPolicy(D("30")).bounds(NOW)
        ), (D("0"), True))

    async def test_transport_terminals_unsent_stale_maker_intent(self):
        transport = self.runtime.private
        transport._connection = AsyncMock()
        self.runtime.public.books["X"].apply_snapshot(
            [(D(".46"), D("100"))], [(D(".52"), D("100"))], 2, 2, NOW,
        )
        with self.assertRaises(QuoteChangedBeforeSend):
            await transport.place_maker(candidate().quote.yes)
        transport._connection.send.assert_not_awaited()
        self.assertEqual(self.runtime.journal.nonterminal_sent_ids(), ())

    async def test_second_leg_book_move_cancels_sibling_without_global_halt(self):
        transport = self.runtime.private
        transport._connection = AsyncMock()
        async def send(raw):
            params = json.loads(raw)["params"]
            self.runtime.account.apply({
                "e": "orderUpdate", "i": "yes-order", "c": params["clientOrderId"],
                "s": "X", "S": "BUY", "O": "YES",
                "q": params["quantity"], "z": params["quantity"],
                "p": params["price"], "X": "OPEN",
            })
            self.runtime.public.books["X"].apply_snapshot(
                [(D(".48"), D("100"))], [(D(".50"), D("100"))], 2, 2, NOW,
            )
        transport._connection.send.side_effect = send
        self.runtime._cancel_symbol_entries = AsyncMock()
        await self.runtime._quote_pair(candidate())
        self.assertEqual(transport._connection.send.await_count, 1)
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")
        self.assertTrue(self.runtime.gate.may_submit)
        rows = self.runtime.journal.connection.execute(
            "SELECT outcome,state FROM order_intent ORDER BY outcome"
        ).fetchall()
        self.assertIn(("NO", IntentState.TERMINAL.value), rows)

    async def test_second_leg_account_transition_cancels_sibling_without_global_halt(self):
        transport = self.runtime.private
        transport._connection = AsyncMock()

        async def send(raw):
            params = json.loads(raw)["params"]
            self.runtime.account.apply({
                "e": "orderUpdate", "i": "yes-order", "c": params["clientOrderId"],
                "s": "X", "S": "BUY", "O": "YES",
                "q": params["quantity"], "z": params["quantity"],
                "p": params["price"], "X": "OPEN",
            })
            # Model the brief cash/position mismatch after a fill while the
            # second leg is waiting for its private-WebSocket send slot.
            self.runtime.account.expected_confirmed_usd = D("99")

        transport._connection.send.side_effect = send
        self.runtime._cancel_symbol_entries = AsyncMock()
        await self.runtime._quote_pair(candidate())
        self.assertEqual(transport._connection.send.await_count, 1)
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")
        self.assertTrue(self.runtime.gate.may_submit)
        rows = self.runtime.journal.connection.execute(
            "SELECT outcome,state FROM order_intent ORDER BY outcome"
        ).fetchall()
        self.assertIn(("NO", IntentState.TERMINAL.value), rows)

    async def test_entry_cutoff_change_is_typed_as_recoverable_unsent_state(self):
        self.runtime._expiry["X"] = NOW + timedelta(minutes=5)
        with self.assertRaises(EntryStateChangedBeforeSend):
            self.runtime._validate_send(self.fixture.payload())

    async def test_first_leg_fill_before_waiter_uses_fill_safety_not_global_halt(self):
        intent = self.runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.private.place_maker = AsyncMock(return_value=intent.client_order_id)
        async def fill_before_waiter_returns(_client_order_id):
            quantity = candidate().quote.yes.quantity
            self.runtime.account.apply({
                "e": "orderUpdate", "i": "filled-order", "c": intent.client_order_id,
                "s": "X", "S": "BUY", "O": "YES", "q": str(quantity), "z": "0",
                "p": ".46", "X": "FILLED", "Z": str(quantity), "L": ".46",
                "n": "0", "t": "fill-before-waiter", "T": int(NOW.timestamp() * 1000),
            })
        self.runtime.account.wait_for_order_event = AsyncMock(
            side_effect=fill_before_waiter_returns,
        )
        self.runtime._cancel_symbol_entries = AsyncMock()
        await self.runtime._quote_pair(candidate())
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")
        self.assertTrue(self.runtime.gate.may_submit)

    async def test_stale_rank_candidate_is_skipped_until_reranked(self):
        self.runtime.private.place_maker = AsyncMock()
        self.runtime.public.books["X"].apply_snapshot(
            [(D(".46"), D("100"))], [(D(".52"), D("100"))], 2, 2, NOW,
        )
        await self.runtime._quote_pair(candidate())
        self.runtime.private.place_maker.assert_not_awaited()

    async def test_urgent_cancels_bypass_lifecycle_lock_and_other_symbols_waits(self):
        first_started, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def cancel(symbol):
            if symbol == "X":
                first_started.set()
                await release.wait()
            else:
                second_started.set()
            return True
        self.runtime._cancel_too_aggressive_quote = AsyncMock(side_effect=cancel)
        async with self.runtime._lifecycle_lock:
            guard = asyncio.create_task(self.runtime._adverse_quote_guard())
            try:
                self.runtime.public.adverse_dirty.mark("X")
                self.runtime.public.adverse_dirty.mark("Y")
                self.runtime.public.depth_changed.set()
                await asyncio.wait_for(first_started.wait(), .5)
                await asyncio.wait_for(second_started.wait(), .5)
            finally:
                release.set()
                guard.cancel()
                await asyncio.gather(guard, return_exceptions=True)

    async def test_concurrent_cancel_requests_share_one_mutation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def cancel(*args, **kwargs):
            entered.set()
            await release.wait()
        self.runtime._cancel_order_and_confirm_once = AsyncMock(side_effect=cancel)
        first = asyncio.create_task(self.runtime._cancel_order_and_confirm("order"))
        await entered.wait()
        second = asyncio.create_task(self.runtime._cancel_order_and_confirm("order"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.runtime._cancel_order_and_confirm_once.await_count, 1)

    async def test_failed_cancel_is_not_resent_by_drain(self):
        self.runtime._cancel_order_and_confirm_once = AsyncMock(side_effect=TimeoutError("unknown"))
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                await self.runtime._cancel_order_and_confirm("order")
        self.assertEqual(self.runtime._cancel_order_and_confirm_once.await_count, 1)

    async def test_emergency_cancel_takes_next_slot_ahead_of_paced_entry(self):
        transport = self.runtime.private
        transport._connection = AsyncMock()
        self.runtime.governor.apply_server_cooldown("private_ws", .06)
        entry = asyncio.create_task(transport.place_maker(candidate().quote.yes))
        await asyncio.sleep(.01)
        cancel = asyncio.create_task(transport.cancel("existing-order"))
        await asyncio.gather(entry, cancel)
        methods = [json.loads(call.args[0])["method"] for call in transport._connection.send.await_args_list]
        self.assertEqual(methods, ["order.cancel", "order.place"])

    async def test_flat_drain_does_not_scan_full_order_history(self):
        self.runtime.rest.active_orders = AsyncMock(return_value=[])
        self.runtime.rest.positions = AsyncMock(return_value=[])
        self.runtime.rest.order_history = AsyncMock(side_effect=AssertionError("full history forbidden"))
        self.runtime.rest.recent_order_history = AsyncMock()
        await self.runtime.drain()
        self.runtime.rest.order_history.assert_not_awaited()
        self.runtime.rest.recent_order_history.assert_not_awaited()
        status = json.loads((self.runtime.directory / "status.json").read_text())
        self.assertEqual(status["phase"], "stopped_flat")

    async def test_incomplete_realized_accounting_blocks_entries_immediately(self):
        with patch.object(self.runtime.journal, "realized_pnl_for_window", return_value=(D("0"), False)):
            with self.assertRaisesRegex(RuntimeError, "cannot be classified"):
                self.runtime._mark_live_risk()
        self.assertFalse(self.runtime.gate.may_submit)
        self.assertEqual(self.runtime._dynamic_pending_cap, 0)


class ManualCloseRiskFixTests(unittest.TestCase):
    def test_missing_manual_close_blocks_affected_window_until_fills_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                policy = RiskWindowPolicy(D("30"))
                start, end = policy.bounds(NOW)
                store.record_fill(
                    trade_id="entry", order_id="entry-order", client_order_id="entry-id",
                    symbol="X", side="BUY", outcome="YES", quantity=D("10"),
                    price=D(".80"), fee=D(".01"), event_at=NOW,
                )
                store.adopt_verified_flat_baseline(
                    confirmed_usd=D("93"), observed_at=NOW + timedelta(minutes=2),
                )
                self.assertEqual(store.realized_pnl_for_window(start, end), (D("0"), False))
                meter = PersistentRiskWindow(policy, store)
                meter.update(now=NOW, realized_pnl=D("-2"), classification_complete=False)
                # A genuine closing fill imported from authenticated evidence,
                # timestamped before the flat audit, resolves the missing basis.
                store.record_fill(
                    trade_id="actual-close", order_id="manual-order", client_order_id="external",
                    symbol="X", side="SELL", outcome="YES", quantity=D("10"),
                    price=D(".10"), fee=D(".02"), event_at=NOW + timedelta(minutes=1),
                )
                pnl, complete = store.realized_pnl_for_window(start, end)
                self.assertEqual((pnl, complete), (D("-7.03"), True))
                restored = meter.update(now=NOW, realized_pnl=pnl, classification_complete=complete, reconciled=True)
                self.assertTrue(restored.classification_complete)
                self.assertEqual(restored.loss_high_water, D("7.03"))
            finally:
                store.close()

    def test_reconciliation_never_clears_a_reached_hard_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                meter = PersistentRiskWindow(RiskWindowPolicy(D("30")), store)
                meter.update(now=NOW, realized_pnl=D("-30"), classification_complete=False)
                result = meter.update(now=NOW, realized_pnl=D("-7"), classification_complete=True, reconciled=True)
                self.assertTrue(result.classification_complete)
                self.assertEqual(result.pending_order_cap, 0)
                self.assertEqual(result.loss_high_water, D("30"))
            finally:
                store.close()

    def test_authenticated_manual_close_is_audited_and_closes_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                start, end = RiskWindowPolicy(D("30")).bounds(NOW)
                store.record_fill(
                    trade_id="entry", order_id="entry-order", client_order_id="entry-id",
                    symbol="X", side="BUY", outcome="NO", quantity=D("10"),
                    price=D(".77"), fee=D("0"), event_at=NOW,
                )
                store.adopt_verified_flat_baseline(
                    confirmed_usd=D("100"), observed_at=NOW + timedelta(minutes=2),
                )
                gap = store.manual_close_gaps(start, end)
                self.assertEqual(len(gap), 1)
                self.assertEqual(gap[0].signed_quantity, D("-10"))
                inserted = store.record_reconciled_manual_close(
                    order_id="manual-order", client_order_id="external-id", symbol="X",
                    side="SELL", outcome="NO", quantity=D("10"), price=D(".95"),
                    conservative_fee=D(".04"), event_at=NOW + timedelta(minutes=1),
                    audit_sequence=gap[0].audit_sequence,
                )
                self.assertTrue(inserted)
                self.assertFalse(store.manual_close_gaps(start, end))
                self.assertEqual(store.realized_pnl_for_window(start, end), (D("1.76"), True))
                audit = store.connection.execute(
                    "SELECT details_json FROM audit_event WHERE kind='authenticated_manual_close_imported'"
                ).fetchone()
                self.assertEqual(
                    json.loads(audit[0])["fee_basis"],
                    "published_standard_taker_upper_bound",
                )
            finally:
                store.close()


class ManualCloseHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = repair_fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.runtime = self.fixture.runtime

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    @staticmethod
    def filled_row(*, order_id: str = "manual-order") -> dict[str, object]:
        return {
            "orderId": order_id, "clientOrderId": "external-id", "status": "filled",
            "symbol": "X", "side": "sell", "outcome": "no", "filledQuantity": "10",
            "remainingQuantity": "0", "avgExecutionPrice": ".95",
            "updatedAt": (NOW + timedelta(minutes=1)).isoformat(),
        }

    async def test_import_matches_one_exact_authenticated_close(self):
        start, end = RiskWindowPolicy(D("30")).bounds(NOW)
        self.runtime.journal.record_fill(
            trade_id="entry", order_id="entry-order", client_order_id="entry-id",
            symbol="X", side="BUY", outcome="NO", quantity=D("10"),
            price=D(".77"), fee=D("0"), event_at=NOW,
        )
        self.runtime.journal.adopt_verified_flat_baseline(
            confirmed_usd=D("100"), observed_at=NOW + timedelta(minutes=2),
        )
        self.runtime.rest.filled_order_history = AsyncMock(return_value=[self.filled_row()])
        self.assertEqual(
            await self.runtime._import_authenticated_manual_closes(
                window_start=start, window_end=end,
            ),
            1,
        )
        self.assertEqual(
            self.runtime.journal.realized_pnl_for_window(start, end),
            (D("1.76"), True),
        )

    async def test_import_refuses_ambiguous_matching_orders(self):
        start, end = RiskWindowPolicy(D("30")).bounds(NOW)
        self.runtime.journal.record_fill(
            trade_id="entry", order_id="entry-order", client_order_id="entry-id",
            symbol="X", side="BUY", outcome="NO", quantity=D("10"),
            price=D(".77"), fee=D("0"), event_at=NOW,
        )
        self.runtime.journal.adopt_verified_flat_baseline(
            confirmed_usd=D("100"), observed_at=NOW + timedelta(minutes=2),
        )
        self.runtime.rest.filled_order_history = AsyncMock(return_value=[
            self.filled_row(order_id="one"), self.filled_row(order_id="two"),
        ])
        with self.assertRaisesRegex(RuntimeError, "matched uniquely"):
            await self.runtime._import_authenticated_manual_closes(
                window_start=start, window_end=end,
            )

    async def test_filled_history_uses_bounded_status_filter(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client.private_read = AsyncMock(return_value={"orders": [self.filled_row()]})
        rows = await client.filled_order_history(
            symbol="X", closed_from=NOW, closed_to=NOW + timedelta(minutes=2),
        )
        self.assertEqual(len(rows), 1)
        fields = client.private_read.await_args.args[1]
        self.assertEqual(fields["status"], "filled")
        self.assertEqual(fields["symbol"], "X")
        self.assertEqual(fields["from"], int(NOW.timestamp() * 1000))
        self.assertEqual(fields["to"], int((NOW + timedelta(minutes=2)).timestamp() * 1000))

    async def test_full_filled_history_window_fails_closed(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client.private_read = AsyncMock(return_value={"orders": [self.filled_row()]})
        with self.assertRaisesRegex(ApiError, "window_ceiling"):
            await client.filled_order_history(
                symbol="X", closed_from=NOW,
                closed_to=NOW + timedelta(minutes=2), limit=1,
            )

    async def test_settled_positions_uses_exact_bounded_event_filter(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        row = {
            "instrumentSymbol": "X", "position": "10", "positionQuantity": "10",
            "outcome": "yes", "payout": "10", "settledAt": NOW.isoformat(),
        }
        client.private_read = AsyncMock(return_value={"positions": [row], "total": 1})
        self.assertEqual(
            await client.settled_positions(event_ticker="EVENT"), [row],
        )
        client.private_read.assert_awaited_once_with(
            "/v1/prediction-markets/positions/settled",
            method="POST",
            query_params={"eventTicker": "EVENT", "limit": 100, "offset": 0},
        )

    async def test_settled_positions_refuses_an_incomplete_window(self):
        client = GeminiReadRestClient(AdmissionGovernor())
        client.private_read = AsyncMock(return_value={"positions": [{}], "total": 2})
        with self.assertRaisesRegex(ApiError, "window_ceiling"):
            await client.settled_positions(event_ticker="EVENT")
