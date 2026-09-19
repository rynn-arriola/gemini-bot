"""Offline fill-to-flat continuity; all venue I/O is synthetic."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as fixtures
from test_repair_integration import NOW
from test_review_regressions import candidate
from gemini_bot.domain import EngineState, Outcome, QuoteLeg, Side
from gemini_bot.execution import OrderPurpose
from gemini_bot.exit_recovery import ExitDeferred
from gemini_bot.live_ws import EntryStateChangedBeforeSend
from gemini_bot.market_state import LocalBook
from gemini_bot.rest_client import ApiError
from gemini_bot.account_runtime import AccountRuntime


class FillContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continuous_service = True
        self.r.rest.active_orders = AsyncMock(return_value=[])
        self.r.rest.positions = AsyncMock(side_effect=self.positions)
        self.r.private.exit_position = AsyncMock(side_effect=AssertionError("unexpected exit"))
        self.r.account.wait_for_order_event = AsyncMock()
        self.counter = 0

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def positions(self):
        return [{"symbol": symbol, "outcome": "YES" if q > 0 else "NO",
                 "totalQuantity": str(abs(q)), "avgPrice": ".40", "marketValue": "4"}
                for symbol, q in self.r.account.reducer.positions.items()]

    def sync(self):
        a = self.r.account
        a.apply({"e": "positionReport", "u": a.reducer.position_update_ns + 1, "P": [
            {"t": "ec", "s": s, "a": [{"t": "position", "v": str(q)}]}
            for s, q in a.expected_positions.items()
        ]})
        a.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": str(a.expected_confirmed_usd), "f": "80"}]})

    def order(self, leg, *, status="FILLED", filled=None):
        self.counter += 1
        self.r.account.quote_asset_precision.setdefault(leg.symbol, 2)
        signed = self.r.account.reducer.positions.get(leg.symbol, D(0))
        purpose = (
            OrderPurpose.EXIT
            if leg.side is Side.SELL or (
                signed < 0 and leg.side is Side.BUY and leg.outcome is Outcome.YES
            )
            else OrderPurpose.ENTRY
        )
        intent = self.r.ledger.prepare(leg, purpose)
        self.r.ledger.mark_sent(intent.client_order_id)
        qty = leg.quantity if filled is None else D(filled)
        self.r.account.apply({
            "e": "orderUpdate", "i": f"audit-{self.counter}", "c": intent.client_order_id,
            "s": leg.symbol, "S": leg.side.value, "O": leg.outcome.value,
            "q": str(leg.quantity), "z": str(leg.quantity - qty), "Z": str(qty),
            "p": str(leg.price), "L": str(leg.price), "n": "0",
            "X": status, "t": f"trade-{self.counter}", "T": int(NOW.timestamp() * 1000),
        })
        self.sync()
        return intent.client_order_id

    def inventory(self, symbol="X", outcome=Outcome.YES, quantity=D(10)):
        self.order(QuoteLeg(symbol, outcome, Side.BUY, D(".46"), quantity))
        self.r.journal.persist_deadline(symbol, outcome.value, NOW, NOW, quantity)

    def book(self, symbol="X", depth="100"):
        book = LocalBook(symbol)
        book.apply_snapshot([(D(".48"), D(depth))], [(D(".52"), D(depth))], 1, 1, NOW)
        self.r.public.books[symbol] = book

    async def test_no_no_side_bids_waits_then_exits_without_restarting_or_extending_deadline(self):
        self.inventory(outcome=Outcome.NO, quantity=D('1.01'))
        r = self.r
        deadline = r.journal.get_deadline('X','NO')
        self.book()
        r.public.books['X'].asks.clear()
        await r._run_exit_worker('X')
        self.assertEqual(r._exit_waits['X'].reason,'exit_depth_unavailable')
        self.assertEqual(r.gate.state,EngineState.QUOTING)
        self.assertEqual(r.journal.get_deadline('X','NO'),deadline)
        r.private.exit_position.assert_not_awaited()
        self.book()
        async def exit_now(leg, **kwargs):
            self.assertEqual(leg.quantity,D('1.01'))
            self.assertEqual(leg.outcome,Outcome.YES)
            self.assertEqual(leg.side,Side.BUY)
            return self.order(leg)
        r.private.exit_position.side_effect=exit_now
        await r._run_exit_worker('X')
        self.assertFalse(r.account.reducer.positions)
        self.assertNotIn('X',r.account.blocked_entries)
        self.assertIsNone(r.journal.get_deadline('X','NO'))
        self.assertEqual(r.gate.state,EngineState.QUOTING)

    async def test_net_flat_two_fills_cleanup_both_deadlines_and_reentry_block(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        await self.r._run_exit_worker("X")
        self.assertTrue(self.r.gate.may_submit)
        self.assertNotIn("X", self.r.account.blocked_entries)
        self.assertEqual(self.r.journal.deadline_symbols(), set())
        self.r.private.exit_position.assert_not_awaited()

    async def test_flat_during_cancel_cleans_lifecycle_without_exit(self):
        self.inventory()
        async def cancel(symbol):
            self.order(replace(candidate().quote.no, quantity=D(10)))
        self.r._cancel_symbol_entries = AsyncMock(side_effect=cancel)
        await self.r._run_exit_worker("X")
        self.assertNotIn("X", self.r.account.blocked_entries)
        self.assertEqual(self.r.journal.deadline_symbols(), set())
        self.r.private.exit_position.assert_not_awaited()

    async def test_flat_cleanup_preserves_independent_lifecycle_hold(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        self.r.account.block_entries("X")
        await self.r._run_exit_worker("X")
        self.assertIn("X", self.r.account.blocked_entries)
        self.assertNotIn("X", self.r.account.fill_blocked_entries)
        self.assertEqual(self.r.journal.deadline_symbols(), set())

    async def test_flat_cleanup_refuses_unclassified_cash(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        self.r.account.confirmed_usd += D(1)
        await self.r._run_exit_worker("X")
        self.assertIn("X", self.r.account.blocked_entries)
        self.assertIn("X", self.r.journal.deadline_symbols())
        self.r.rest.positions.assert_not_awaited()

    async def test_flat_cleanup_refuses_unknown_intent(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        intent = self.r.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        with self.assertRaises(ExitDeferred):
            await self.r._run_exit_worker("X")
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_flat_cleanup_refuses_stale_account(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        self.r.account.last_position_at = 0
        await self.r._run_exit_worker("X")
        self.assertIn("X", self.r.journal.deadline_symbols())
        self.assertFalse(self.r._isolated_exit_proof_current("X"))

    async def test_stream_generation_change_invalidates_flat_proof(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        async def active():
            self.r._stream_generation["private"] += 1
            return []
        self.r.rest.active_orders.side_effect = active
        await self.r._run_exit_worker("X")
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_fill_during_flat_proof_invalidates_cleanup(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        async def active():
            self.inventory()
            return []
        self.r.rest.active_orders.side_effect = active
        await self.r._run_exit_worker("X")
        self.assertIn("X", self.r.account.blocked_entries)
        self.assertIn("X", self.r.journal.deadline_symbols())

    async def test_deadline_hold_is_reconstructed_after_reopen(self):
        self.inventory()
        account = AccountRuntime(self.r.ledger, self.r.journal)
        self.assertIn("X", account.fill_blocked_entries)
        self.assertIn("X", account.blocked_entries)

    async def test_thin_exit_book_waits_without_global_failure(self):
        self.inventory()
        self.book(depth="1")
        deadline = self.r.journal.get_deadline("X", "YES")
        await self.r._run_exit_worker("X")
        self.assertEqual(self.r._exit_waits["X"].reason, "exit_depth_unavailable")
        self.assertTrue(self.r.gate.may_submit)
        self.assertTrue(self.r._isolated_exit_proof_current("X"))
        self.assertEqual(self.r.journal.get_deadline("X", "YES"), deadline)
        self.r.private.exit_position.assert_not_awaited()

    async def test_stale_exit_book_waits_without_sending(self):
        self.inventory()
        self.r.public.books["X"].synchronized = False
        await self.r._run_exit_worker("X")
        self.assertEqual(self.r._exit_waits["X"].reason, "exit_book_unavailable")
        self.r.private.exit_position.assert_not_awaited()

    async def test_three_terminal_unfilled_then_later_round_flattens(self):
        self.inventory()
        async def exit(leg, **_):
            return self.order(leg, status="CANCELED", filled="0")
        self.r.private.exit_position = AsyncMock(side_effect=exit)
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            await self.r._run_exit_worker("X")
        self.assertEqual(self.r.private.exit_position.await_count, 3)
        self.assertEqual(self.r._exit_waits["X"].reason, "exit_round_exhausted")
        self.assertFalse(self.r.journal.pending_exit("X"))
        self.assertNotIn("X", self.r._exit_uncertain)
        self.assertTrue(self.r.gate.may_submit)
        await self.r._exit_due_positions()
        self.assertFalse(self.r._exit_workers)  # backoff honored
        self.r._exit_waits["X"].retry_at = 0
        self.r.private.exit_position.side_effect = lambda leg, **_: self.order(leg)
        await self.r._exit_due_positions()
        await asyncio.gather(*self.r._exit_workers.values())
        await self.r._exit_due_positions()
        self.assertEqual(self.r.private.exit_position.await_count, 4)
        self.assertNotIn("X", self.r.account.blocked_entries)
        self.assertFalse(self.r._exit_waits)

    async def test_partial_exit_retries_only_exact_residual(self):
        self.inventory()
        quantities = []
        async def exit(leg, **_):
            quantities.append(leg.quantity)
            return self.order(leg, status="CANCELED", filled="4" if len(quantities) == 1 else str(leg.quantity))
        self.r.private.exit_position.side_effect = exit
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            await self.r._run_exit_worker("X")
        self.assertEqual(quantities, [D(10), D(6)])
        self.assertFalse(self.r.account.reducer.positions)
        self.assertNotIn("X", self.r.account.blocked_entries)

    async def test_no_exit_uses_buy_yes_and_exact_quantity(self):
        self.inventory(outcome=Outcome.NO, quantity=D(3))
        self.r.private.exit_position.side_effect = lambda leg, **_: self.order(leg)
        await self.r._run_exit_worker("X")
        leg = self.r.private.exit_position.call_args.args[0]
        self.assertEqual((leg.side, leg.outcome, leg.quantity), (Side.BUY, Outcome.YES, D(3)))

    async def test_unknown_ack_is_never_retried_in_another_round_or_drain(self):
        self.inventory()
        async def exit(leg, **_):
            intent = self.r.ledger.prepare(leg, OrderPurpose.EXIT)
            self.r.ledger.mark_sent(intent.client_order_id)
            return intent.client_order_id
        self.r.private.exit_position.side_effect = exit
        self.r.account.wait_for_order_event.side_effect = TimeoutError("synthetic unknown")
        with self.assertRaises(TimeoutError):
            await self.r._run_exit_worker("X")
        with self.assertRaises(TimeoutError):
            await self.r._exit_due_positions(force=True)
        self.assertEqual(self.r.private.exit_position.await_count, 1)

    async def test_rejected_exit_not_retried_by_forced_drain(self):
        self.inventory()
        self.r.private.exit_position.side_effect = lambda leg, **_: self.order(leg, status="REJECTED", filled="0")
        for call in (self.r._run_exit_worker("X"), self.r._exit_due_positions(force=True)):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                await call
        self.assertEqual(self.r.private.exit_position.await_count, 1)

    async def test_residual_increase_or_reversal_between_rounds_refuses_send(self):
        self.inventory()
        self.book(depth="1")
        await self.r._run_exit_worker("X")
        self.book()
        for value in (D(11), D(-1)):
            self.r.journal.persist_deadline("X", "NO", NOW, NOW, D(1))
            self.r.account.expected_positions = {"X": value}
            self.sync()
            with self.assertRaisesRegex(RuntimeError, "increased or reversed"):
                await self.r._run_exit_worker("X")
        self.r.private.exit_position.assert_not_awaited()

    async def test_transient_read_wait_respects_retry_after(self):
        self.inventory()
        self.r.rest.positions.side_effect = ApiError(429, "synthetic", retry_after=120)
        before = time.monotonic()
        await self.r._run_exit_worker("X")
        self.assertGreaterEqual(self.r._exit_waits["X"].retry_at, before + 120)
        self.assertFalse(self.r._isolated_exit_proof_current("X"))
        self.r.private.exit_position.assert_not_awaited()

    async def test_malformed_read_is_not_a_transient_wait(self):
        self.inventory()
        self.r.rest.positions.side_effect = ValueError("malformed")
        with self.assertRaises(ValueError):
            await self.r._run_exit_worker("X")
        self.assertNotIn("X", self.r._exit_waits)

    async def test_two_exit_workers_independent_and_single_owner_per_symbol(self):
        self.inventory()
        self.inventory("Y")
        entered, release, y_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def worker(symbol):
            if symbol == "X":
                entered.set()
                await release.wait()
            else:
                y_seen.set()
        self.r._run_exit_worker = AsyncMock(side_effect=worker)
        await self.r._exit_due_positions()
        await asyncio.wait_for(entered.wait(), .5)
        await asyncio.wait_for(y_seen.wait(), .5)
        await self.r._exit_due_positions()
        self.assertEqual([c.args[0] for c in self.r._run_exit_worker.call_args_list].count("X"), 1)
        release.set()

    async def test_fill_cancels_bypass_lifecycle_lock_and_other_fill_wait(self):
        self.r.account.block_entries("X", fill=True)
        self.r.account.block_entries("Y", fill=True)
        entered, release, y_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def cancel(symbol):
            if symbol == "X":
                entered.set()
                await release.wait()
            else:
                y_seen.set()
        self.r._cancel_symbol_entries = AsyncMock(side_effect=cancel)
        async with self.r._lifecycle_lock:
            task = asyncio.create_task(self.r._safety_loop())
            try:
                self.r.account.safety_symbols.put_nowait("X")
                self.r.account.safety_symbols.put_nowait("Y")
                await asyncio.wait_for(entered.wait(), .5)
                await asyncio.wait_for(y_seen.wait(), .5)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_fill_cancel_failure_propagates(self):
        self.r.account.block_entries("X", fill=True)
        self.r._cancel_symbol_entries = AsyncMock(side_effect=RuntimeError("unproven cancel"))
        self.r.account.safety_symbols.put_nowait("X")
        with self.assertRaisesRegex(RuntimeError, "unproven cancel"):
            await asyncio.wait_for(self.r._safety_loop(), .5)

    async def test_private_recovery_owns_cancellation(self):
        self.r.account.block_entries("X", fill=True)
        self.r._stream_holds.add("private")
        self.r._cancel_symbol_entries = AsyncMock()
        self.r.account.safety_symbols.put_nowait("X")
        task = asyncio.create_task(self.r._safety_loop())
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_worker_fatal_error_surfaces_on_next_tick(self):
        self.inventory()
        self.r._run_exit_worker = AsyncMock(side_effect=ValueError("software fault"))
        await self.r._exit_due_positions()
        await asyncio.gather(*self.r._exit_workers.values(), return_exceptions=True)
        with self.assertRaisesRegex(ValueError, "software fault"):
            await self.r._exit_due_positions()

    async def test_no_worker_starts_after_operator_drain(self):
        self.inventory()
        self.r.gate.state = EngineState.DRAINING
        await self.r._exit_due_positions()
        self.assertFalse(self.r._exit_workers)

    async def test_no_worker_starts_before_original_deadline(self):
        self.order(candidate().quote.yes)
        await self.r._exit_due_positions()
        self.assertFalse(self.r._exit_workers)

    async def test_isolated_proof_cannot_survive_account_change_or_age(self):
        self.inventory()
        self.book(depth="1")
        await self.r._run_exit_worker("X")
        self.assertTrue(self.r._isolated_exit_proof_current("X"))
        self.r.account.fill_version += 1
        self.assertFalse(self.r._isolated_exit_proof_current("X"))
        self.r.account.fill_version -= 1
        self.r._exit_waits["X"].proved_at -= 61
        self.assertFalse(self.r._isolated_exit_proof_current("X"))

    async def test_legacy_permit_retains_three_attempt_limit(self):
        self.r._continuous_service = False
        self.inventory()
        self.r.private.exit_position.side_effect = lambda leg, **_: self.order(leg, status="CANCELED", filled="0")
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "bounded retry authority"):
                await self.r._exit_due_positions()
        self.assertEqual(self.r.private.exit_position.await_count, 3)

    async def test_proven_isolated_exit_allows_unrelated_guard_not_same_symbol(self):
        self.inventory()
        self.book(depth="1")
        await self.r._run_exit_worker("X")
        self.book("Y")
        self.r._expiry["Y"] = self.r._expiry["X"]
        self.r._permitted_symbols.add("Y")
        self.r.universe.append(replace(self.r.universe[0], rules=replace(self.r.universe[0].rules, symbol="Y")))
        payload = self.fixture.payload()
        # Resolve the synthetic fixture intent (no network) so it cannot
        # invalidate the X terminal proof under test.
        self.r.ledger.mark_sent(payload["params"]["clientOrderId"])
        self.r.ledger.mark_terminal(payload["params"]["clientOrderId"])
        payload["params"]["symbol"] = "Y"
        self.r._validate_send(payload)
        payload["params"]["symbol"] = "X"
        with self.assertRaises(EntryStateChangedBeforeSend):
            self.r._validate_send(payload)
        payload["params"]["symbol"] = "Y"
        self.r._stream_generation["private"] += 1
        with self.assertRaises(EntryStateChangedBeforeSend):
            self.r._validate_send(payload)

    async def test_paced_exit_cannot_send_after_round_deadline(self):
        self.inventory()
        self.r._exit_round_deadlines["X"] = time.monotonic() - 1
        intent = self.r.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.SELL, D(".48"), D("10")),
            OrderPurpose.EXIT,
        )
        payload = {"params": {
            "symbol": "X", "side": "SELL", "price": ".48", "quantity": "10",
            "eventOutcome": "YES", "clientOrderId": intent.client_order_id,
        }}
        with self.assertRaisesRegex(EntryStateChangedBeforeSend, "round expired"):
            self.r._validate_send(payload)

    async def test_duplicate_fills_dont_duplicate_exit_owner_or_extend_deadline(self):
        self.inventory()
        deadline = self.r.journal.get_deadline("X", "YES")
        entered, release = asyncio.Event(), asyncio.Event()
        async def worker(symbol):
            entered.set()
            await release.wait()
        self.r._run_exit_worker = AsyncMock(side_effect=worker)
        await self.r._exit_due_positions()
        await entered.wait()
        for _ in range(3):
            await self.r._exit_due_positions()
        self.assertEqual(self.r._run_exit_worker.await_count, 1)
        self.assertEqual(self.r.journal.get_deadline("X", "YES"), deadline)
        release.set()

    async def test_foreign_active_order_is_account_wide_failure(self):
        self.inventory()
        self.r.rest.active_orders.return_value = [{
            "orderId": "foreign", "clientOrderId": "not-owned", "symbol": "Y",
            "side": "buy", "outcome": "yes", "price": ".46", "quantity": "10",
            "remainingQuantity": "10", "status": "open",
        }]
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            await self.r._run_exit_worker("X")
        self.r.private.exit_position.assert_not_awaited()

    async def test_shutdown_waits_existing_exit_before_force_path(self):
        self.inventory()
        entered, release = asyncio.Event(), asyncio.Event()
        async def exit(leg, **_):
            entered.set()
            await release.wait()
            return self.order(leg)
        self.r.private.exit_position.side_effect = exit
        await self.r._exit_due_positions()
        await entered.wait()
        self.r._targeted_prior_intent_history = AsyncMock(return_value=[])
        drain = asyncio.create_task(self.r.drain())
        await asyncio.sleep(.01)
        self.assertFalse(drain.done())
        self.assertEqual(self.r.private.exit_position.await_count, 1)
        release.set()
        await asyncio.wait_for(drain, .5)
        self.assertFalse(self.r._exit_workers)
        self.assertEqual(self.r.private.exit_position.await_count, 1)
        self.assertFalse(self.r.account.reducer.positions)

    async def test_wait_reporting_excludes_position_and_cash_values(self):
        self.inventory()
        self.book(depth="1")
        await self.r._run_exit_worker("X")
        self.r._write_status("quoting", True, "synthetic test")
        import json
        status = json.loads((self.r.directory / "status.json").read_text())
        self.assertEqual(set(status["exit_recovery"]["X"]), {"reason", "rounds", "retry_in_seconds"})

    async def test_missing_deadline_still_fails_closed(self):
        self.inventory()
        self.r.journal.clear_deadline("X", "YES")
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            await self.r._exit_due_positions()

    async def test_account_wait_backoff_is_capped_without_extending_deadline(self):
        self.inventory()
        self.book(depth="1")
        deadline = self.r.journal.get_deadline("X", "YES")
        for _ in range(9):
            self.sync()
            before = time.monotonic()
            await self.r._run_exit_worker("X")
            self.assertLess(self.r._exit_waits["X"].retry_at - before, 60.5)
        self.assertEqual(self.r._exit_waits["X"].rounds, 9)
        self.assertEqual(self.r.journal.get_deadline("X", "YES"), deadline)

    async def test_full_width_exit_cancels_only_filled_market(self):
        self.inventory()
        self.order(candidate().quote.no, status="OPEN", filled="0")
        # Thirty total pending entries: one affected sibling plus 29 others.
        for index in range(29):
            self.order(replace(candidate().quote.yes, symbol=f"Y{index}"), status="OPEN", filled="0")
        canceled = []
        async def cancel(order_id):
            order = self.r.account.reducer.orders[order_id]
            canceled.append(order.symbol)
            self.r.account.apply({"e": "orderUpdate", "i": order_id,
                                  "X": "CANCELED", "z": "0", "Z": "0", "T": order.update_ns + 1})
        self.r.private.cancel = AsyncMock(side_effect=cancel)
        def active():
            return [{"orderId": o.order_id, "clientOrderId": o.client_order_id,
                     "symbol": o.symbol, "side": o.side.value, "outcome": o.outcome.value,
                     "price": str(o.price), "quantity": str(o.original_quantity),
                     "remainingQuantity": str(o.remaining_quantity), "status": o.status}
                    for o in self.r.account.reducer.orders.values() if o.working]
        self.r.rest.active_orders.side_effect = active
        async def exit(leg, **_):
            filled = "0" if self.r.private.exit_position.await_count < 3 else str(leg.quantity)
            return self.order(leg, status="CANCELED", filled=filled)
        self.r.private.exit_position.side_effect = exit
        with patch("gemini_bot.orchestrator.asyncio.sleep", new=AsyncMock()):
            await self.r._run_exit_worker("X")
        self.assertEqual(canceled, ["X"])
        self.assertEqual(self.r.account.reducer.pending_order_count(), 29)
        self.assertEqual(self.r.private.exit_position.await_count, 3)
        self.assertTrue(self.r.gate.may_submit)
        self.assertNotIn("X", self.r.account.blocked_entries)

    async def test_cleanup_write_failure_rolls_back_both_deadlines(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        with patch.object(self.r.journal, "audit", side_effect=RuntimeError("synthetic storage fault")):
            with self.assertRaisesRegex(RuntimeError, "storage fault"):
                await self.r._run_exit_worker("X")
        for outcome in ("YES", "NO"):
            self.assertIsNotNone(self.r.journal.get_deadline("X", outcome))
        self.assertIn("X", self.r.account.blocked_entries)

    async def test_stale_rest_working_row_does_not_clear_flat_lifecycle(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        order = next(iter(self.r.account.reducer.orders.values()))
        self.r.rest.active_orders.return_value = [{
            "orderId": order.order_id, "clientOrderId": order.client_order_id,
            "symbol": "X", "side": "buy", "outcome": "yes", "price": ".46",
            "quantity": "10", "remainingQuantity": "10", "status": "open",
        }]
        await self.r._run_exit_worker("X")
        self.assertEqual(self.r._exit_waits["X"].reason, "working_order_proof_pending")
        self.assertIn("X", self.r.account.blocked_entries)
        self.r.private.exit_position.assert_not_awaited()

    async def test_rest_position_lag_after_fill_waits_then_recovers(self):
        self.inventory()
        self.r.rest.positions.side_effect = None
        self.r.rest.positions.return_value = []
        await self.r._run_exit_worker("X")
        self.assertEqual(self.r._exit_waits["X"].reason, "position_proof_pending")
        self.assertFalse(self.r._isolated_exit_proof_current("X"))
        self.r.private.exit_position.assert_not_awaited()
        self.r.rest.positions.side_effect = self.positions
        self.r.private.exit_position.side_effect = lambda leg, **_: self.order(leg)
        await self.r._run_exit_worker("X")
        self.assertNotIn("X", self.r.account.blocked_entries)
        self.assertNotIn("X", self.r._position_proof_wait_started)

    async def test_rest_lag_after_flat_exit_does_not_stop_normal_flow(self):
        self.inventory()
        old_position = self.positions()
        self.order(QuoteLeg("X", Outcome.YES, Side.SELL, D(".48"), D(10)))
        self.r.rest.positions.side_effect = None
        self.r.rest.positions.return_value = old_position
        await self.r._run_exit_worker("X")
        self.assertEqual(self.r._exit_waits["X"].reason, "position_proof_pending")
        self.assertTrue(self.r.gate.may_submit)
        self.assertIn("X", self.r.account.blocked_entries)
        self.r.rest.positions.return_value = []
        await self.r._run_exit_worker("X")
        self.assertNotIn("X", self.r.account.blocked_entries)

    async def test_persistent_position_disagreement_is_not_forgiven(self):
        self.inventory()
        self.r.rest.positions.side_effect = None
        self.r.rest.positions.return_value = []
        await self.r._run_exit_worker("X")
        self.r._position_proof_wait_started["X"] -= 11
        with self.assertRaises(ValueError):
            await self.r._run_exit_worker("X")
        self.r.private.exit_position.assert_not_awaited()

    async def test_obsolete_fill_notification_cannot_cancel_reentry(self):
        self.inventory()
        self.inventory(outcome=Outcome.NO)
        await self.r._run_exit_worker("X")
        self.order(candidate().quote.yes, status="OPEN", filled="0")
        self.r._cancel_symbol_entries = AsyncMock()
        await self.r._cancel_fill_entries_if_blocked("X")
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_no_entry_or_retry_authority_from_unrelated_position_mismatch(self):
        self.inventory()
        self.r.account.expected_positions["Y"] = D(1)
        self.sync()
        self.r.rest.positions.side_effect = lambda: [p for p in self.positions() if p["symbol"] == "X"]
        with self.assertRaises(ValueError):
            await self.r._run_exit_worker("X")
        self.r.private.exit_position.assert_not_awaited()

    async def test_position_lag_cannot_mask_foreign_order_evidence(self):
        self.inventory()
        self.r.rest.positions.side_effect = None
        self.r.rest.positions.return_value = []
        self.r.rest.active_orders.return_value = [{
            "orderId": "foreign", "clientOrderId": "unknown", "symbol": "Y",
            "side": "buy", "outcome": "yes", "price": ".46", "quantity": "10",
            "remainingQuantity": "10", "status": "open",
        }]
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            await self.r._run_exit_worker("X")


if __name__ == "__main__":
    unittest.main()
