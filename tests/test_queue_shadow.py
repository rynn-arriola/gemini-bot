"""Synthetic queue evidence only: no credentials, sockets or live mutations."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from gemini_bot.account_state import AccountReducer, OwnedOrder
from gemini_bot.domain import Outcome, Side
from gemini_bot.guarded_bid import (
    BidObservation, QueueCalibration, ReplacementProof, recommend_bid, replacement_proven,
)
from gemini_bot.market_state import LocalBook
from gemini_bot.queue_shadow import QueueShadow, ShadowArchive
import test_repair_integration as fixtures

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def book(size="1000", ask_size="800", sequence=1, symbol="X", bid=".40", ask=".42"):
    b = LocalBook(symbol)
    b.apply_snapshot([(D(bid), D(size))], [(D(ask), D(ask_size))], sequence, sequence, NOW)
    return b


def trade(identity=1, size="100", price=".40", maker=True, at=NOW, symbol="X"):
    return dict(s=symbol, t=identity, p=price, q=size, m=maker, E=int(at.timestamp() * 1e9))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.q = QueueShadow()
        self.q.observe_book(book(), generation=1, at=NOW)

    def scenario(self, side=Outcome.YES):
        return self.q.markets["X"].scenarios[side]

    def update(self, size, sequence=2):
        self.q.observe_book(book(size=size, sequence=sequence), generation=1, at=NOW + timedelta(seconds=sequence))

    def test_new_depth_goes_behind_not_ahead(self):
        self.update("1500")
        self.assertEqual(self.scenario().ahead_scenario, D(1000))
        self.assertEqual(self.scenario().report()["behind_scenario"], "500")

    def test_100_ahead_500_behind_is_only_a_scenario(self):
        self.update("1500")
        self.update("600", 3)
        row = self.scenario().report()
        self.assertEqual((row["ahead_scenario"], row["behind_scenario"]), ("100", "500"))
        self.assertEqual(row["guaranteed_quantity_ahead"], "0")

    def test_hidden_churn_never_proves_protection(self):
        self.update("1000")
        self.assertEqual(self.scenario().report()["guaranteed_quantity_ahead"], "0")

    def test_trade_then_depth_not_debited_twice(self):
        self.q.observe_trade(trade(size="100"), generation=1, at=NOW)
        self.update("900")
        self.assertEqual(self.scenario().ahead_scenario, D(900))

    def test_late_trade_is_pessimistic_not_exact(self):
        self.update("900")
        self.q.observe_trade(trade(size="100"), generation=1, at=NOW + timedelta(seconds=3))
        self.assertEqual(self.scenario().ahead_scenario, D(800))
        self.assertEqual(self.scenario().report()["guaranteed_quantity_ahead"], "0")

    def test_duplicates_and_nonmonotonic_trade_ids_ignored(self):
        for identity in (2, 2, 1):
            self.q.observe_trade(trade(identity), generation=1, at=NOW)
        self.assertEqual(self.scenario().ahead_scenario, D(900))

    def test_no_side_uses_complement_and_correct_aggressor(self):
        self.q.observe_trade(trade(price=".42", maker=False), generation=1, at=NOW)
        self.assertEqual(self.scenario(Outcome.NO).price, D(".58"))
        self.assertEqual(self.scenario(Outcome.NO).ahead_scenario, D(700))
        self.assertEqual(self.scenario().ahead_scenario, D(1000))

    def test_outside_price_trade_does_not_deplete_this_queue(self):
        self.q.observe_trade(trade(price=".41"), generation=1, at=NOW)
        self.assertEqual(self.scenario().ahead_scenario, D(1000))

    def test_large_trade_marks_possible_touch_not_a_fill(self):
        self.q.observe_trade(trade(size="2000"), generation=1, at=NOW)
        self.assertTrue(self.scenario().possible_touch)
        self.assertEqual(self.scenario().ahead_scenario, D(0))
        self.update("3000")
        self.assertTrue(self.scenario().possible_touch)
        self.assertEqual(self.scenario().ahead_scenario, D(0))

    def test_generation_change_resets_and_old_trade_ignored(self):
        self.update("600")
        self.q.observe_book(book(size="200", sequence=1), generation=2, at=NOW + timedelta(seconds=4))
        self.q.observe_trade(trade(size="2000"), generation=1, at=NOW + timedelta(seconds=4))
        self.assertEqual(self.scenario().ahead_scenario, D(200))

    def test_changed_price_starts_explicit_new_virtual_join(self):
        self.q.observe_book(book(bid=".41", sequence=2), generation=1, at=NOW + timedelta(seconds=1))
        self.assertEqual(self.scenario().price, D(".41"))
        self.assertEqual(self.q.markets["X"].resets, 3)

    def test_pre_join_and_future_and_missing_timestamp_trades_ignored(self):
        for identity, event_at in enumerate((NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)), 1):
            self.q.observe_trade(trade(identity, at=event_at), generation=1, at=NOW)
        frame = trade(3)
        del frame["E"]
        self.q.observe_trade(frame, generation=1, at=NOW)
        self.assertEqual(self.scenario().ahead_scenario, D(1000))

    def test_stale_report_does_not_publish_stale_queue_numbers(self):
        row = self.q.report(at=NOW, fresh_symbols=set())["markets"][0]
        self.assertEqual(row["availability"], "unavailable")
        self.assertEqual(row["outcomes"], {})
        self.assertIsNone(row["yes_sell_quantity_60s"])

    def test_flow_warmup_and_unknown_trade_time_are_explicit(self):
        row = self.q.report(at=NOW, fresh_symbols={"X"})["markets"][0]
        self.assertFalse(row["full_flow_window_observed"])
        row = self.q.report(at=NOW + timedelta(seconds=60), fresh_symbols={"X"})["markets"][0]
        self.assertTrue(row["full_flow_window_observed"])
        frame = trade(); del frame["E"]
        self.q.observe_trade(frame, generation=1, at=NOW + timedelta(seconds=60))
        row = self.q.report(at=NOW + timedelta(seconds=61), fresh_symbols={"X"})["markets"][0]
        self.assertTrue(row["trade_time_uncertain"])
        self.assertFalse(row["full_flow_window_observed"])

    def test_own_yes_and_no_orders_are_removed_without_mutating_public_book(self):
        b = book()
        orders = tuple(OwnedOrder(str(i), str(i), "X", Side.BUY, side, price, D(15), D(15), D(0), "OPEN", 0)
                       for i, side, price in ((1, Outcome.YES, D(".40")), (2, Outcome.NO, D(".58"))))
        self.q = QueueShadow()
        self.q.observe_book(b, generation=1, at=NOW, orders=orders)
        self.assertEqual(self.scenario().external_depth, D(985))
        self.assertEqual(self.scenario(Outcome.NO).external_depth, D(785))
        self.assertEqual(b.bids[D(".40")], D(1000))

    def test_own_public_skew_invalidates_instead_of_assuming_external_depth(self):
        own = OwnedOrder("o", "c", "X", Side.BUY, Outcome.YES, D(".39"), D(15), D(15), D(0), "OPEN", 0)
        self.q.observe_book(book(sequence=2), generation=1, at=NOW, orders=(own,))
        self.assertNotIn("X", self.q.markets)

    def test_gap_or_one_sided_book_discards_history(self):
        b = book(); b.synchronized = False
        self.q.observe_book(b, generation=1, at=NOW)
        self.assertFalse(self.q.markets)
        b = book(); b.asks.clear()
        self.q.observe_book(b, generation=1, at=NOW)
        self.assertFalse(self.q.markets)

    def test_clock_reversal_invalidates(self):
        self.q.observe_book(book(sequence=2), generation=1, at=NOW - timedelta(seconds=1))
        self.assertFalse(self.q.markets)

    def test_market_and_trade_memory_are_bounded(self):
        for i in range(50):
            self.q.observe_book(book(symbol=f"S{i}"), generation=1, at=NOW)
        self.assertEqual(len(self.q.markets), 32)
        for i in range(300):
            self.q.observe_trade(trade(i), generation=1, at=NOW)
        self.assertEqual(len(self.q.markets["X"].trades), 256)
        self.assertTrue(self.q.markets["X"].trade_overflow)
        self.q.retain({"X"})
        self.assertEqual(set(self.q.markets), {"X"})

    def test_expired_trades_not_reported_as_current_flow(self):
        self.q.observe_trade(trade(), generation=1, at=NOW)
        row = self.q.report(at=NOW + timedelta(seconds=61), fresh_symbols={"X"})["markets"][0]
        self.assertEqual(row["yes_sell_quantity_60s"], "0")

    def test_bad_trade_fields_rejected(self):
        for change in ({"t": True}, {"q": "NaN"}, {"q": "-1"}, {"m": "true"}, {"p": "Infinity"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.q.observe_trade({**trade(), **change}, generation=1, at=NOW)

    def test_cancel_metrics_are_bounded_and_distinguish_failed_calls(self):
        for i in range(300):
            self.q.record_cancel(i / 1000, succeeded=True)
        self.q.record_cancel(45, succeeded=False)
        result = self.q.report(at=NOW, fresh_symbols={"X"})["cancel_measurement"]
        self.assertEqual(result["successful_samples"], 256)
        self.assertEqual(result["failed_or_interrupted_calls"], 1)
        self.assertEqual(result["maximum_seconds"], "0.299")


class PlannerTests(unittest.TestCase):
    def setUp(self):
        # Synthetic TEST parameters, not approved live thresholds.
        self.c = QueueCalibration(D(300), D(100), D(300), D(200), D(10), NOW + timedelta(hours=1), "synthetic-test-only")
        self.o = BidObservation("X", NOW, D(600), D(100), D(0), working=True,
                                observation_complete=True, safety_checks_pass=True,
                                since_last_refresh_seconds=D(20))

    def advice(self, **changes):
        return recommend_bid(replace(self.o, **changes), pilot_symbols=frozenset({"X", "Y"}), calibration=self.c)

    def test_no_calibration_means_no_join(self):
        result = recommend_bid(self.o, pilot_symbols=frozenset({"X", "Y"}), calibration=None)
        self.assertEqual(result.reason, "missing_or_expired_calibration")
        self.assertFalse(result.used_for_trading)

    def test_rejoin_only_when_scenario_shows_material_gain(self):
        self.assertEqual(self.advice().reason, "estimated_queue_refresh_benefit")
        self.assertTrue(self.advice().requires_cancel_then_fresh_proof)

    def test_no_rejoin_protection_when_only_100_remain(self):
        self.assertEqual(self.advice(external_depth=D(100)).reason, "thin_depth_or_fast_selling")

    def test_cooldown_prevents_refresh_but_not_withdrawal(self):
        self.assertEqual(self.advice(since_last_refresh_seconds=D(1)).action, "withdraw_if_working_else_wait")
        self.assertEqual(self.advice(working=False, since_last_refresh_seconds=D(1)).action, "wait")
        self.assertEqual(self.advice(best_price_changed=True, since_last_refresh_seconds=D(1)).action, "cancel_then_reassess")

    def test_healthy_queue_is_retained(self):
        self.assertEqual(self.advice(ahead_scenario=D(400)).action, "keep")

    def test_join_requires_evidence_and_all_existing_safety_gates(self):
        self.assertEqual(self.advice(working=False).action, "join_best_bid_moc")
        for change in ({"safety_checks_pass": False}, {"observation_complete": False}, {"quote_quantity": D(20)}):
            self.assertEqual(self.advice(working=False, **change).action, "withdraw_if_working_else_wait")

    def test_fast_selling_or_expired_calibration_withdraws(self):
        self.assertEqual(self.advice(sell_flow_60s=D(201)).action, "withdraw_if_working_else_wait")
        self.assertEqual(self.advice(at=self.c.expires_at).reason, "missing_or_expired_calibration")

    def test_partial_fill_and_pending_exit_win_over_requeue(self):
        self.assertEqual(self.advice(any_fill=True).action, "existing_fill_exit_flow")
        self.assertEqual(self.advice(inventory_or_exit_pending=True).action, "existing_fill_exit_flow")

    def test_unknown_cancel_never_replays_or_replaces(self):
        self.assertEqual(self.advice(cancellation_pending_or_unknown=True).action, "wait_for_reconciliation")

    def test_nonpilot_market_unchanged_and_breadth_bounded(self):
        self.assertEqual(self.advice(symbol="OTHER").action, "unchanged_configured_offset_policy")
        for symbols in (frozenset(), frozenset({"X"}), frozenset({"X", "Y", "Z"})):
            with self.assertRaises(ValueError):
                recommend_bid(self.o, pilot_symbols=symbols, calibration=self.c)

    def test_calibration_rejects_missing_evidence_or_nonfinite_values(self):
        for change in ({"evidence_reference": ""}, {"minimum_depth": D("NaN")}, {"refresh_cooldown_seconds": D(0)}):
            with self.assertRaises(ValueError):
                replace(self.c, **change)


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.p = ReplacementProof(frozenset({"YES", "NO"}), frozenset({"YES", "NO"}), NOW, NOW,
                                  (1, 2), (1, 2), 3, 3, True, True, True)

    def test_exact_proof_required(self):
        self.assertTrue(replacement_proven(self.p, now=NOW, max_age_seconds=D(1)))
        for change in (
            {"zero_fill_cancelled_order_ids": frozenset({"YES"})},
            {"zero_fill_cancelled_order_ids": frozenset({"OTHER", "NO"})},
            {"actual_public_generation": (1, 3)}, {"actual_private_generation": 4},
            {"fresh_classified_flat": False}, {"no_working_or_unresolved_orders": False},
            {"no_fill_since_request": False}, {"proven_at": NOW - timedelta(seconds=1)},
        ):
            with self.subTest(change=change):
                self.assertFalse(replacement_proven(replace(self.p, **change), now=NOW, max_age_seconds=D(1)))
        self.assertFalse(replacement_proven(self.p, now=NOW + timedelta(seconds=2), max_age_seconds=D(1)))

    def test_real_reducer_7_of_15_fill_then_cancel_is_not_zero_fill_proof(self):
        account = AccountReducer()
        account.apply_order_update(dict(e="orderUpdate", i="YES", c="c", s="X", S="BUY", O="YES",
                                        p=".40", q="15", z="15", X="NEW", T=1))
        account.apply_order_update(dict(e="orderUpdate", i="YES", X="PARTIALLY_FILLED", z="8", Z="7", t="t", T=2))
        account.apply_order_update(dict(e="orderUpdate", i="YES", X="CANCELED", z="0", Z="7", T=3))
        order = account.orders["YES"]
        self.assertEqual(order.cumulative_filled, D(7))
        proof = replace(self.p, no_fill_since_request=order.cumulative_filled == 0)
        self.assertFalse(replacement_proven(proof, now=NOW, max_age_seconds=D(1)))


class ArchiveTests(unittest.TestCase):
    def test_archive_cannot_be_configured_unbounded(self):
        with tempfile.TemporaryDirectory() as directory:
            for limits in ({"backups": 0}, {"backups": 10}, {"max_bytes": 0}):
                with self.assertRaises(ValueError):
                    ShadowArchive(Path(directory), **limits)

    def test_minute_dedupe_rotation_and_forecast_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = ShadowArchive(Path(directory), max_bytes=2000, backups=2)
            try:
                for i in range(10):
                    at = NOW + timedelta(minutes=i)
                    for repeat in range(3):
                        archive.write(at=at, report={"used_for_trading": False}, forecasts=[{"symbol": "X", "api_key": "SECRET"}])
                files = list(Path(directory).glob("queue-shadow.jsonl*"))
                self.assertLessEqual(len(files), 3)
                lines = [line for p in files for line in p.read_text().splitlines()]
                self.assertEqual(len({json.loads(line)["observed_at"] for line in lines}), len(lines))
                self.assertNotIn("SECRET", "".join(lines))
            finally:
                archive.close()

    def test_disk_failure_is_visible_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = ShadowArchive(Path(directory))
            try:
                with patch.object(archive.handler, "_open", side_effect=PermissionError("SECRET")):
                    archive.write(at=NOW, report={}, forecasts=[])
                self.assertEqual(archive.error, "PermissionError")
                self.assertNotIn("SECRET", str(archive.error))
            finally:
                archive.close()


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = fixtures.RuntimeRepairTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.runtime
        self.r._public_stream_fresh = lambda symbol: True
        self.r._queue_shadow_report(fixtures.NOW, [])

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    async def test_observer_adds_no_intents_and_cannot_change_policy(self):
        before = self.r.config.strategy
        self.r._observe_queue_shadow("X")
        result = self.r._queue_shadow_report(fixtures.NOW, [])
        self.assertEqual(result["mode"], "shadow_only")
        self.assertFalse(result["used_for_trading"])
        self.assertFalse(result["live_pilot_ready"])
        self.assertEqual(len(result["markets"]), 1)
        self.assertEqual(self.r.config.strategy, before)
        self.assertEqual(self.r.journal.unresolved_ids(), ())

    async def test_observer_failure_does_not_halt_or_touch_orders(self):
        state = self.r.gate.state
        with patch.object(self.r.queue_shadow, "observe_book", side_effect=ValueError("SECRET")):
            self.r._observe_queue_shadow("X")
        self.assertEqual(self.r.queue_shadow.error, "ValueError")
        self.assertEqual(self.r.gate.state, state)
        self.assertEqual(self.r.account.reducer.pending_order_count(), 0)

    async def test_stream_hold_invalidates_queue_only(self):
        self.r._observe_queue_shadow("X")
        self.r._stream_holds.add("private")
        self.r._observe_queue_shadow("X")
        self.assertFalse(self.r.queue_shadow.markets)

    async def test_private_generation_change_invalidates_report_before_next_depth(self):
        self.r._observe_queue_shadow("X")
        self.r._stream_generation["private"] += 1
        report = self.r._queue_shadow_report(fixtures.NOW, [])
        self.assertEqual(report["markets"][0]["availability"], "unavailable")
        self.assertFalse(self.r.queue_shadow.markets)

    async def test_real_public_pump_updates_observer_without_mutation(self):
        self.r.private.place_maker = AsyncMock()
        self.r.private.cancel = AsyncMock()
        self.r.public.connect = AsyncMock()
        async def frames():
            yield {"e": "depthUpdate", "s": "X", "U": 2, "u": 2,
                   "b": [[".48", "100"]], "a": [[".52", "100"]]}
            yield {"e": "trade", **trade(size="10", price=".48", at=fixtures.NOW)}
            raise asyncio.CancelledError  # Stop the fake stream without reconnecting.
        self.r.public.frames = frames
        with self.assertRaises(asyncio.CancelledError):
            await self.r._public_pump()
        report = self.r._queue_shadow_report(fixtures.NOW, [])
        self.assertEqual(report["markets"][0]["outcomes"]["YES"]["ahead_scenario"], "90")
        self.r.private.place_maker.assert_not_awaited()
        self.r.private.cancel.assert_not_awaited()
        self.r.public.connect.assert_not_awaited()

    async def test_order_transition_invalidates_virtual_queue_and_tracks_remaining(self):
        self.r._observe_queue_shadow("X")
        order = OwnedOrder("o", "c", "X", Side.BUY, Outcome.YES, D(".48"), D(15), D(8), D(7), "PARTIALLY_FILLED", 1)
        self.r.account.reducer.orders["o"] = order
        self.r._account_event("order_transition", dict(symbol="X", order_id="o", side="BUY", status="PARTIALLY_FILLED"))
        self.assertFalse(self.r.queue_shadow.markets)
        self.assertEqual(self.r._queue_live_orders["o"].remaining_quantity, D(8))
        order.status = "CANCELED"
        self.r._account_event("order_transition", dict(symbol="X", order_id="o", side="BUY", status="CANCELED"))
        self.assertFalse(self.r._queue_live_orders)

    async def test_shared_cancel_records_one_sample_and_preserves_unknown_latch(self):
        self.r._cancel_order_and_confirm_once = AsyncMock()
        await asyncio.gather(self.r._cancel_order_and_confirm("o"), self.r._cancel_order_and_confirm("o"))
        await asyncio.sleep(0)
        self.r._cancel_order_and_confirm_once.assert_awaited_once()
        self.assertEqual(len(self.r.queue_shadow.cancel_seconds), 1)
        self.r._cancel_order_and_confirm_once = AsyncMock(side_effect=TimeoutError("unknown"))
        with self.assertRaises(TimeoutError):
            await self.r._cancel_order_and_confirm("u")
        await asyncio.sleep(0)
        self.assertIn("u", self.r._cancel_tasks)
        self.assertEqual(self.r.queue_shadow.cancel_failures, 1)
