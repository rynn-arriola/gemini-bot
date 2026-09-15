"""Regression evidence for the September 15 follow-up review; offline only."""
from __future__ import annotations

import asyncio
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
from gemini_bot.domain import EngineState
from gemini_bot.execution import IntentState
from gemini_bot.live_ws import QuoteChangedBeforeSend
from gemini_bot.persistent_risk import PersistentRiskWindow
from gemini_bot.rate_limits import AdmissionGovernor
from gemini_bot.rest_client import ApiError, GeminiReadRestClient
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.storage import SQLiteJournal


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
                "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "10",
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
