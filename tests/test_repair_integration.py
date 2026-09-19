"""Offline failure-boundary tests; no credentials or exchange connections."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from test_review_regressions import NOW, candidate
from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.activation import build_digest, load_activation_permit, policy_digest
from gemini_bot.config import DEFAULT_POLICY_PATH, LiveConfig, load_policy
from gemini_bot.domain import EngineState, Outcome, QuoteLeg, Side
from gemini_bot.execution import IntentLedger, OrderPurpose, place_request
from gemini_bot.market_state import LocalBook
from gemini_bot.orchestrator import ProductionOrchestrator
from gemini_bot.live_ws import MutationNotSentError
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.rate_limits import AdmissionGovernor, Priority
from gemini_bot.rank_report import rank_live_universe
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.storage import SQLiteJournal, WindowsOwnerLock
from gemini_bot.uptime import locally_qualifying
from gemini_bot.catalog import RewardedContract


def book():
    value = LocalBook("X")
    value.apply_snapshot([(D(".48"), D("100"))], [(D(".52"), D("100"))], 1, 1, NOW)
    return value


class AccountRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal.sqlite"
        self.store = SQLiteJournal(self.path)
        self.ledger = IntentLedger(self.store)
        self.account = AccountRuntime(self.ledger, self.store)
        self.account.quote_asset_precision = {"X": 2}
        self.account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "100", "f": "100"}]})
        self.account.apply({"e": "positionReport", "P": [], "u": 1})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fill(self, outcome=Outcome.YES):
        intent = self.ledger.prepare(QuoteLeg("X", outcome, Side.BUY, D(".4"), D("10")), OrderPurpose.ENTRY)
        self.ledger.mark_sent(intent.client_order_id)
        frame = {"e": "orderUpdate", "i": "o1", "c": intent.client_order_id, "s": "X",
                 "S": "BUY", "O": outcome.value, "q": "10", "z": "7", "Z": "3",
                 "X": "PARTIALLY_FILLED", "t": "trade1", "L": ".4", "n": ".01",
                 "T": int(NOW.timestamp() * 1000)}
        self.account.apply(frame)
        return frame

    def position(self, signed):
        self.account.apply({"e": "positionReport", "u": 2,
                            "P": [{"t": "ec", "s": "X", "a": [{"t": "position", "v": str(signed)}]}]})

    def cash(self, cash):
        self.account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": str(cash), "f": str(cash)}]})

    def test_cash_and_position_must_both_match_fill_amount(self):
        self.fill()
        self.cash("98.79")
        self.assertFalse(self.account.equity_classification_complete)
        self.position(3)
        self.assertTrue(self.account.equity_classification_complete)
        self.cash("198.79")
        self.assertFalse(self.account.equity_classification_complete)

    def test_balance_before_fill_event_is_matched_by_amount_not_time(self):
        self.cash("98.79")
        self.assertFalse(self.account.equity_classification_complete)
        self.fill(Outcome.NO)
        self.position(-3)
        self.assertTrue(self.account.equity_classification_complete)

    def test_deadline_exists_before_delayed_position_and_duplicate_does_not_extend(self):
        frame = self.fill()
        deadline = self.store.get_deadline("X", "YES")
        self.assertIsNotNone(deadline)
        self.assertFalse(self.account.reducer.positions)
        self.account.apply(frame)
        self.position(3)
        self.assertEqual(self.store.get_deadline("X", "YES"), deadline)
        self.assertIn("X", self.account.blocked_entries)

    def test_terminal_settlement_closes_owned_position_without_releasing_lifecycle_hold(self):
        self.fill()
        self.position(3)
        self.account.block_entries("X", reason="lifecycle")
        self.account.apply({
            "e": "positionReport", "u": 3,
            "E": int((NOW + timedelta(minutes=1)).timestamp() * 1000),
            "P": [{"t": "ec", "s": "X", "a": [
                {"t": "position", "v": "0"},
                {"t": "settlement_payout", "v": "3"},
            ]}],
        })
        self.assertEqual(self.account.expected_positions, {})
        self.assertEqual(self.account.expected_confirmed_usd, D("101.79"))
        self.assertNotIn("X", self.account.fill_blocked_entries)
        self.assertIn("X", self.account.blocked_entries)
        self.assertIsNone(self.store.get_deadline("X", "YES"))
        start, end = RiskWindowPolicy(D("30")).bounds(NOW)
        self.assertEqual(self.store.realized_pnl_for_window(start, end), (D("1.79"), True))

        # Exact stream repeats are idempotent and cannot double-credit cash.
        self.account.apply({
            "e": "positionReport", "u": 3,
            "E": int((NOW + timedelta(minutes=1)).timestamp() * 1000),
            "P": [{"t": "ec", "s": "X", "a": [
                {"t": "position", "v": "0"},
                {"t": "settlement_payout", "v": "3"},
            ]}],
        })
        self.assertEqual(self.account.expected_confirmed_usd, D("101.79"))

    def test_terminal_settlement_must_match_owned_quantity_and_payout_bounds(self):
        self.fill()
        self.position(3)
        with self.assertRaisesRegex(RuntimeError, "economics are invalid"):
            self.account.apply({
                "e": "positionReport", "u": 3,
                "E": int((NOW + timedelta(minutes=1)).timestamp() * 1000),
                "P": [{"t": "ec", "s": "X", "a": [
                    {"t": "position", "v": "0"},
                    {"t": "settlement_payout", "v": "4"},
                ]}],
            })

    def test_fill_deadline_honors_earlier_sports_flat_cutoff(self):
        self.account.hard_exit_cutoff["X"] = NOW + timedelta(seconds=60)
        self.fill()
        self.assertEqual(self.store.get_deadline("X", "YES"), NOW + timedelta(seconds=60))

    def test_restart_cannot_accept_new_cash_as_fresh_baseline(self):
        restored = AccountRuntime(IntentLedger(self.store), self.store)
        restored.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "200", "f": "200"}]})
        restored.apply({"e": "positionReport", "P": [], "u": 1})
        self.assertFalse(restored.equity_classification_complete)

    def test_verified_manual_flat_adopts_baseline_and_preserves_risk_window(self):
        self.store.persist_deadline("X", "YES", NOW, NOW + timedelta(minutes=5), D("10"))
        window_start, window_end = RiskWindowPolicy(D("90")).bounds(NOW)
        self.store.upsert_realized_risk_window(
            window_start=window_start, window_end=window_end,
            realized_pnl=D("-7.25"), loss_high_water=D("7.25"),
            pending_order_cap=30, classification_complete=True, updated_at=NOW,
        )
        self.account.expected_positions = {"X": D("10")}
        self.account.expected_confirmed_usd = D("96")
        self.account.confirmed_usd = D("99")
        self.account.subscription_acknowledged = True
        self.account.last_balance_at = self.account.last_position_at = time.monotonic()

        self.account.adopt_verified_manual_flat(observed_at=NOW)

        self.assertEqual(self.account.expected_positions, {})
        self.assertEqual(self.account.expected_confirmed_usd, D("99"))
        self.assertIsNone(self.store.get_deadline("X", "YES"))
        risk = self.store.load_realized_risk_window(window_start)
        self.assertEqual(risk["realized_pnl"], D("-7.25"))
        self.assertEqual(risk["loss_high_water"], D("7.25"))
        restored = AccountRuntime(IntentLedger(self.store), self.store)
        self.assertEqual(restored.expected_positions, {})
        self.assertEqual(restored.expected_confirmed_usd, D("99"))

    def test_manual_flat_refuses_nonterminal_bot_intent(self):
        intent = self.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.SELL, D(".4"), D("10")),
            OrderPurpose.EXIT,
        )
        self.ledger.mark_sent(intent.client_order_id)
        self.account.subscription_acknowledged = True
        self.account.last_balance_at = self.account.last_position_at = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "terminal bot intents"):
            self.account.adopt_verified_manual_flat(observed_at=NOW)

    def test_manual_flat_refuses_nonzero_or_stale_account_state(self):
        self.account.subscription_acknowledged = True
        self.account.last_balance_at = 0
        with self.assertRaisesRegex(RuntimeError, "fresh account snapshots"):
            self.account.adopt_verified_manual_flat(observed_at=NOW)
        self.account.last_balance_at = self.account.last_position_at = time.monotonic()
        self.account.reducer.positions["X"] = D("10")
        with self.assertRaisesRegex(RuntimeError, "zero positions"):
            self.account.adopt_verified_manual_flat(observed_at=NOW)

    def test_manual_flat_refuses_a_working_account_order(self):
        intent = self.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.BUY, D(".4"), D("10")),
            OrderPurpose.ENTRY,
        )
        self.ledger.mark_sent(intent.client_order_id)
        self.account.apply({
            "e": "orderUpdate", "i": "working", "c": intent.client_order_id,
            "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "10",
            "p": ".4", "X": "OPEN",
        })
        self.account.subscription_acknowledged = True
        self.account.last_balance_at = self.account.last_position_at = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "zero active orders"):
            self.account.adopt_verified_manual_flat(observed_at=NOW)

    def test_verified_presend_not_sent_repair_is_exact_and_audited(self):
        intent = self.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.SELL, D(".40"), D("10")),
            OrderPurpose.EXIT,
        )
        self.ledger.mark_sent(intent.client_order_id)
        self.store.resolve_verified_presend_not_sent(
            (intent.client_order_id,), observed_at=NOW,
        )
        self.assertFalse(self.store.pending_exit("X"))
        audit = self.store.connection.execute(
            "SELECT kind,details_json FROM audit_event ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit[0], "verified_presend_exit_not_sent_reconciled")
        self.assertEqual(json.loads(audit[1]), {"intent_count": 1})

    def test_presend_not_sent_repair_refuses_nonexit_or_server_order(self):
        entry = self.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.BUY, D(".40"), D("10")),
            OrderPurpose.ENTRY,
        )
        self.ledger.mark_sent(entry.client_order_id)
        with self.assertRaisesRegex(RuntimeError, "serverless exit"):
            self.store.resolve_verified_presend_not_sent(
                (entry.client_order_id,), observed_at=NOW,
            )

    def test_snapshot_sql_error_rolls_back_both_tables_on_reopen(self):
        row = {"symbol": "X", "outcome": "YES", "signed_quantity": "10", "average_price": ".4", "market_value": "4"}
        self.store.replace_account_snapshot([row], [], observed_at=NOW)
        with self.assertRaises(Exception):
            self.store.replace_account_snapshot([row, row], [], observed_at=NOW)
        reopened = SQLiteJournal(self.path)
        try:
            self.assertEqual(reopened.load_position_quantities(), {"X": D("10")})
        finally:
            reopened.close()


class RuntimeRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        strategy, risk = load_policy()
        # Historical fill/cancel fixtures use explicit 20-contract wire frames.
        # Current size policy has separate end-to-end coverage.
        strategy = replace(strategy, quote_quantity=D(20), quote_offset_ticks=2, queue_pilot=None)
        self.runtime = ProductionOrchestrator(LiveConfig("account-fake", "fake-secret", "", True, strategy, risk), Path(self.tmp.name), public_shard_count=1)
        self.runtime.control_directory = Path(self.tmp.name) / "isolated-control"
        self.runtime.lock = WindowsOwnerLock(self.runtime.control_directory / "mutation-owner.lock")
        self.runtime.public.books["X"] = book()
        self.runtime.universe = [RewardedContract(
            "E", candidate_pool(), candidate_rules(),
            (NOW + timedelta(hours=2)).isoformat(),
        )]
        self.runtime.account.quote_asset_precision = {"X": 2}
        self.runtime._expiry["X"] = NOW + timedelta(hours=2)
        self.runtime._permitted_symbols = {"X"}
        self.runtime.gate.state = EngineState.QUOTING
        for name in ("terms_accepted", "balances_known", "positions_known", "open_orders_known", "account_stream_live", "public_stream_live"):
            setattr(self.runtime.gate.reconciliation, name, True)
        self.runtime.account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "100", "f": "100"}]})
        self.runtime.account.apply({"e": "positionReport", "P": [], "u": 1})
        self.clock = patch("gemini_bot.orchestrator._utc_now", return_value=NOW)
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()
        await self.runtime.close()
        self.tmp.cleanup()

    def payload(self):
        intent = self.runtime.ledger.prepare(
            candidate(self.runtime.config.strategy.quote_quantity).quote.yes,
            OrderPurpose.ENTRY,
        )
        return place_request(1, intent)

    async def test_send_guard_rejects_state_change_during_pacing(self):
        payload = self.payload()
        self.runtime._validate_send(payload)
        self.runtime.account.blocked_entries.add("X")
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(payload)

    async def test_send_guard_rejects_expired_permit(self):
        self.runtime._permit_expires_at = NOW
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(self.payload())

    async def test_sports_entry_stops_twenty_minutes_before_start(self):
        self.runtime._sports_start["X"] = NOW + timedelta(minutes=20)
        self.assertFalse(self.runtime._entry_time_valid("X"))

    async def test_send_guard_rejects_unapproved_symbol(self):
        self.runtime._permitted_symbols.clear()
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(self.payload())

    async def test_send_guard_rejects_stale_account_even_with_live_socket(self):
        self.runtime.account.last_position_at = 0
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(self.payload())

    async def test_known_active_order_resolves_prior_unknown_without_resubmission(self):
        intent = self.runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime._resolve_prior_intents([], [{"clientOrderId": intent.client_order_id, "orderId": "known-active"}])
        self.assertEqual(self.runtime.journal.unresolved_ids(), ())

    async def test_acknowledged_exit_resolves_terminal_after_restart(self):
        leg = QuoteLeg("X", Outcome.NO, Side.SELL, D(".48"), D("10"))
        intent = self.runtime.ledger.prepare(leg, OrderPurpose.EXIT)
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.ledger.acknowledge(intent.client_order_id, "exit1")
        self.assertTrue(self.runtime.journal.pending_exit("X"))
        self.runtime._resolve_prior_intents([{"clientOrderId": intent.client_order_id, "orderId": "exit1", "status": "cancelled"}])
        self.assertFalse(self.runtime.journal.pending_exit("X"))

    async def test_startup_history_is_targeted_only_to_unresolved_symbols(self):
        intent = self.runtime.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.SELL, D(".40"), D("10")),
            OrderPurpose.EXIT,
        )
        self.runtime.ledger.mark_sent(intent.client_order_id)
        terminal = {
            "clientOrderId": intent.client_order_id,
            "orderId": "exit1", "status": "cancelled",
        }
        self.runtime.rest.recent_order_history = AsyncMock(return_value=[terminal])
        self.runtime.rest.order_history = AsyncMock(
            side_effect=AssertionError("global history must not be read"),
        )
        history = await self.runtime._targeted_prior_intent_history([])
        self.runtime._resolve_prior_intents(history)
        self.runtime.rest.recent_order_history.assert_awaited_once_with(symbol="X")
        self.runtime.rest.order_history.assert_not_awaited()
        self.assertFalse(self.runtime.journal.pending_exit("X"))

    async def test_full_targeted_history_without_intent_remains_ambiguous(self):
        intent = self.runtime.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.SELL, D(".40"), D("10")),
            OrderPurpose.EXIT,
        )
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.rest.recent_order_history = AsyncMock(
            return_value=[{"clientOrderId": f"other-{value}"} for value in range(1000)],
        )
        with self.assertRaisesRegex(RuntimeError, "history is full"):
            await self.runtime._targeted_prior_intent_history([])

    async def test_exit_send_guard_requires_same_outcome_and_exact_quantity(self):
        self.runtime.account.reducer.positions["X"] = D("-3")
        intent = self.runtime.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.BUY, D(".52"), D("3")),
            OrderPurpose.EXIT,
        )
        payload = {"params": {
            "symbol": "X", "side": "BUY", "price": ".52", "quantity": "3",
            "eventOutcome": "YES", "clientOrderId": intent.client_order_id,
        }}
        self.runtime._validate_send(payload)
        payload["params"]["eventOutcome"] = "NO"
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(payload)

    async def test_guard_rejection_is_proven_not_sent_and_terminal(self):
        send = AsyncMock()
        self.runtime.private._connection = SimpleNamespace(send=send)
        leg = QuoteLeg("X", Outcome.YES, Side.SELL, D(".40"), D("10"))
        with self.assertRaises(MutationNotSentError):
            await self.runtime.private.exit_position(leg, signed_position=D("10"))
        send.assert_not_awaited()
        self.assertFalse(self.runtime.journal.pending_exit("X"))

    async def test_presend_exit_race_reconciles_flat_without_duplicate_order(self):
        self.runtime.account.reducer.positions["X"] = D("10")
        self.runtime.account.expected_positions = {"X": D("10")}
        self.runtime.journal.persist_deadline("X", "YES", NOW, NOW, D("10"))
        position = {
            "symbol": "X", "outcome": "YES", "totalQuantity": "10",
            "avgPrice": ".40", "marketValue": "4",
        }
        self.runtime.rest.positions = AsyncMock(side_effect=[[position], []])

        async def filled_before_retry(*args, **kwargs):
            self.runtime.account.reducer.positions.clear()
            self.runtime.account.expected_positions = {}
            raise MutationNotSentError("position changed while waiting")

        self.runtime.private.exit_position = AsyncMock(side_effect=filled_before_retry)
        await self.runtime._exit_due_positions()
        self.assertFalse(self.runtime.journal.pending_exit("X"))
        self.assertIsNone(self.runtime.journal.get_deadline("X", "YES"))
        self.runtime.private.exit_position.assert_awaited_once()

    async def test_websocket_adverse_guard_cancels_quote_that_moves_inside_two_ticks(self):
        self.runtime.universe = [
            RewardedContract(
                "E", candidate_pool(), candidate_rules(),
                (NOW + timedelta(hours=2)).isoformat(),
            )
        ]
        intent = self.runtime.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.BUY, D(".46"), D("10")),
            OrderPurpose.ENTRY,
        )
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.account.apply({
            "e": "orderUpdate", "i": "working", "c": intent.client_order_id,
            "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "10",
            "p": ".46", "X": "OPEN",
        })
        self.runtime.public.books["X"].apply_snapshot(
            [(D(".40"), D("100"))], [(D(".60"), D("100"))], 2, 2, NOW,
        )
        self.runtime._cancel_symbol_entries = AsyncMock()
        self.assertTrue(await self.runtime._cancel_too_aggressive_quote("X"))
        self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")

    async def test_websocket_adverse_guard_does_not_churn_quote_farther_from_touch(self):
        self.runtime.universe = [
            RewardedContract(
                "E", candidate_pool(), candidate_rules(),
                (NOW + timedelta(hours=2)).isoformat(),
            )
        ]
        intent = self.runtime.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.BUY, D(".40"), D("10")),
            OrderPurpose.ENTRY,
        )
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.account.apply({
            "e": "orderUpdate", "i": "working", "c": intent.client_order_id,
            "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "10",
            "p": ".40", "X": "OPEN",
        })
        self.runtime._cancel_symbol_entries = AsyncMock()
        self.assertFalse(await self.runtime._cancel_too_aggressive_quote("X"))
        self.runtime._cancel_symbol_entries.assert_not_awaited()

    async def test_sports_entry_stops_at_published_game_start(self):
        self.runtime._sports_start["X"] = NOW
        self.assertFalse(self.runtime._entry_time_valid("X"))
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(self.payload())

    async def test_entry_waits_for_contract_effective_time(self):
        self.runtime._trade_start["X"] = NOW + timedelta(seconds=1)
        self.assertFalse(self.runtime._entry_time_valid("X"))
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(self.payload())

    async def test_open_position_marks_do_not_reduce_pending_capacity(self):
        account = self.runtime.account
        account.reducer.positions = {"X": D("100")}
        account.expected_positions = {"X": D("100")}
        self.runtime._mark_live_risk()
        self.runtime.public.books["X"].apply_snapshot([(D(".28"), D("100"))], [(D(".32"), D("100"))], 2, 2, NOW)
        self.runtime._mark_live_risk()
        self.assertEqual(self.runtime._dynamic_pending_cap, 40)
        self.runtime.public.books["X"] = book()
        self.runtime._mark_live_risk()
        stored = self.runtime.journal.load_realized_risk_window(RiskWindowPolicy(D("90")).bounds(NOW)[0])
        self.assertEqual(stored["loss_high_water"], D("0"))
        self.assertEqual(self.runtime._dynamic_pending_cap, 40)

    async def test_one_sided_position_does_not_enter_realized_loss_meter(self):
        account = self.runtime.account
        account.reducer.positions = {"X": D("10")}
        account.expected_positions = {"X": D("10")}
        self.runtime.public.books["X"].apply_snapshot([], [(D(".52"), D("100"))], 2, 2, NOW)
        self.runtime.public.last_message_at = NOW
        self.runtime._mark_live_risk()
        stored = self.runtime.journal.load_realized_risk_window(RiskWindowPolicy(D("90")).bounds(NOW)[0])
        self.assertEqual(stored["realized_pnl"], D("0"))
        self.runtime.public.books["X"].apply_snapshot([(D(".40"), D("100"))], [], 3, 3, NOW)
        self.runtime._mark_live_risk()
        stored = self.runtime.journal.load_realized_risk_window(RiskWindowPolicy(D("90")).bounds(NOW)[0])
        self.assertEqual(stored["loss_high_water"], D("0"))

    async def test_all_one_sided_markets_still_block_startup(self):
        one_sided = LocalBook("X")
        one_sided.apply_snapshot([(D(".48"), D("100"))], [], 1, 1, NOW)
        self.runtime.public.books["X"] = one_sided
        self.runtime.public.last_message_at = NOW
        self.runtime.universe = [RewardedContract("E", candidate_pool(), candidate_rules(), (NOW + timedelta(hours=2)).isoformat())]
        self.runtime.account.subscription_acknowledged = True
        with self.assertRaises(TimeoutError):
            await self.runtime._wait_ready(.01)

    async def test_read_only_orchestrator_cannot_activate(self):
        strategy, risk = load_policy()
        read_only = ProductionOrchestrator(
            LiveConfig("account-read-only", "fake-secret", "", True, strategy, risk),
            Path(self.tmp.name) / "read-only", read_only=True,
        )
        try:
            self.assertIn("cancelOnDisconnect=false", read_only.private._uri)
            read_only.gate.state = EngineState.READY
            with self.assertRaisesRegex(RuntimeError, "read-only"):
                await read_only.activate(Path(self.tmp.name) / "missing-permit.json")
        finally:
            await read_only.close()

    async def test_sequence_gap_halts_without_reusing_buffered_delta(self):
        async def frames():
            yield {"e": "depthUpdate", "s": "X", "U": 5, "u": 5, "b": [], "a": []}
        self.runtime.public.frames = frames
        self.runtime._recover_stream = AsyncMock(side_effect=RuntimeError("recovery unavailable"))
        with self.assertRaises(RuntimeError):
            await self.runtime._public_pump()
        self.assertFalse(self.runtime.gate.may_submit)
        self.runtime.public.apply_frame({"e": "depthUpdate", "s": "X", "U": 6, "u": 6, "b": [[".49", "1"]], "a": []})
        self.assertFalse(self.runtime.public.books["X"].synchronized)

    async def test_close_failure_still_releases_native_lock_and_journal(self):
        self.runtime.lock.acquire()
        self.runtime.private.close = AsyncMock(side_effect=RuntimeError("fake close failure"))
        await self.runtime.close()
        self.assertIsNone(self.runtime.lock._file)

    async def test_flat_contract_closure_is_isolated_without_global_halt(self):
        self.runtime._handle_contract_status({"e": "contractStatus", "s": "x", "n": "Closed"})
        self.assertIn("X", self.runtime.account.blocked_entries)
        self.assertTrue(self.runtime._reward_refresh_requested)
        self.assertTrue(self.runtime.gate.may_submit)

    async def test_contract_closure_with_proven_inventory_is_market_local(self):
        self.runtime._continuous_service = True
        self.runtime.account.last_balance_at = time.monotonic()
        self.runtime.account.last_position_at = time.monotonic()
        self.runtime.account.reducer.positions["X"] = D("10")
        self.runtime.account.expected_positions = {"X": D("10")}
        self.runtime.journal.persist_deadline(
            "X", "YES", NOW, NOW + timedelta(minutes=5), D("10")
        )
        self.runtime._handle_contract_status({"e": "contractStatus", "s": "x", "n": "Closed"})
        self.assertTrue(self.runtime.gate.may_submit)
        self.assertIn("X", self.runtime.account.fill_blocked_entries)
        self.assertIn("X", self.runtime.account.blocked_entries)

    async def test_contract_closure_with_unproven_inventory_halts(self):
        self.runtime._continuous_service = True
        self.runtime.account.reducer.positions["X"] = D("10")
        with self.assertRaisesRegex(RuntimeError, "scoped inventory proof"):
            self.runtime._handle_contract_status({"e": "contractStatus", "s": "x", "n": "Closed"})

    async def test_missed_terminal_position_event_recovers_from_exact_settled_history(self):
        intent = self.runtime.ledger.prepare(
            QuoteLeg("X", Outcome.YES, Side.BUY, D(".4"), D("3")),
            OrderPurpose.ENTRY,
        )
        self.runtime.ledger.mark_sent(intent.client_order_id)
        self.runtime.account.apply({
            "e": "orderUpdate", "i": "entry", "c": intent.client_order_id,
            "s": "X", "S": "BUY", "O": "YES", "q": "3", "z": "0",
            "X": "FILLED", "Z": "3", "t": "trade", "L": ".4", "n": "0",
            "T": int(NOW.timestamp() * 1000),
        })
        self.runtime.account.apply({
            "e": "positionReport", "u": 2,
            "P": [{"t": "ec", "s": "X", "a": [{"t": "position", "v": "3"}]}],
        })
        self.runtime.account.reducer.positions.clear()  # complete stream snapshot missed terminal detail
        self.runtime.account.block_entries("X", reason="lifecycle")
        self.runtime.rest.positions = AsyncMock(side_effect=[[], []])
        self.runtime.rest.active_orders = AsyncMock(side_effect=[[], []])
        self.runtime.rest.settled_positions = AsyncMock(return_value=[{
            "instrumentSymbol": "X", "outcome": "YES", "positionQuantity": "3",
            "position": "3", "payout": "3",
            "settledAt": (NOW + timedelta(minutes=1)).isoformat(),
        }])

        count = await self.runtime._reconcile_missed_settlements()

        self.assertEqual(count, 1)
        self.assertEqual(self.runtime.account.expected_positions, {})
        self.assertEqual(self.runtime.account.expected_confirmed_usd, D("101.8"))
        self.assertTrue(self.runtime.gate.may_submit)
        self.assertIn("X", self.runtime.account.blocked_entries)
        self.runtime.rest.settled_positions.assert_awaited_once_with(event_ticker="E")

    async def test_one_sided_market_does_not_block_initialized_cohort(self):
        one_sided = LocalBook("Y")
        one_sided.apply_snapshot([(D(".20"), D("25"))], [], 1, 1, NOW)
        self.runtime.public.books["Y"] = one_sided
        self.runtime.public.last_message_at = NOW
        self.runtime.universe = [
            RewardedContract("E1", candidate_pool(), candidate_rules(), (NOW + timedelta(hours=2)).isoformat()),
            RewardedContract(
                "E2", type(candidate_pool())("pool-y", D("50")),
                type(candidate_rules())("Y", D(".01"), D("1"), D("1")),
                (NOW + timedelta(hours=2)).isoformat(),
            ),
        ]
        self.runtime._expiry["Y"] = NOW + timedelta(hours=2)
        self.runtime.account.subscription_acknowledged = True
        await self.runtime._wait_ready(.1)

    async def test_quiet_book_uses_fresh_shared_stream_continuity(self):
        quiet = book()
        quiet.observed_at = NOW - timedelta(minutes=10)
        self.runtime.public.books["X"] = quiet
        self.runtime.public.last_message_at = NOW
        self.assertTrue(self.runtime._book_fresh("X"))
        rows = rank_live_universe(
            [RewardedContract("E", candidate_pool(), candidate_rules(), (NOW + timedelta(hours=2)).isoformat())],
            {"X": quiet}, now=NOW, quantity=D("10"), offset_ticks=2,
            account=self.runtime.account.reducer, market_data_verified_at=NOW,
        )
        self.assertTrue(rows[0].eligible_now)

    async def test_stale_shared_stream_blocks_every_entry_even_if_book_changed_recently(self):
        self.runtime.public.last_message_at = NOW - timedelta(seconds=4)
        self.assertFalse(self.runtime._book_fresh("X"))
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(self.payload())

    async def test_one_sided_book_is_initialized_but_not_ranked_for_pair_strategy(self):
        one_sided = LocalBook("X")
        one_sided.apply_snapshot([(D(".48"), D("100"))], [], 1, 1, NOW)
        rows = rank_live_universe(
            [RewardedContract("E", candidate_pool(), candidate_rules(), (NOW + timedelta(hours=2)).isoformat())],
            {"X": one_sided}, now=NOW, quantity=D("10"), offset_ticks=2,
            market_data_verified_at=NOW,
        )
        self.assertEqual(rows, [])

    async def test_bounded_canary_uses_activation_safety_and_terminal_drain(self):
        runtime = self.runtime
        strategy, risk = load_policy()
        runtime.config = LiveConfig("account-fake", "fake-secret", "I_UNDERSTAND_THIS_TRADES_REAL_MONEY", True, strategy, risk)
        runtime.gate.state = EngineState.READY
        runtime.universe = [RewardedContract("E", candidate_pool(), candidate_rules(), "2027-01-01T00:00:00Z", NOW.isoformat(), (NOW + timedelta(hours=2)).isoformat())]
        runtime.ranked = [candidate()]
        async def place(leg):
            intent = runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
            runtime.ledger.mark_sent(intent.client_order_id)
            runtime.account.apply({"e": "orderUpdate", "i": leg.outcome.value, "c": intent.client_order_id,
                                   "s": "X", "S": "BUY", "O": leg.outcome.value, "q": "10", "z": "10", "p": str(leg.price), "X": "OPEN"})
            return intent.client_order_id
        async def cancel(order_id):
            runtime.account.apply({"e": "orderUpdate", "i": order_id, "X": "CANCELED", "z": "0", "Z": "0"})
        async def active():
            return [{"orderId": order.order_id, "clientOrderId": order.client_order_id, "symbol": order.symbol,
                     "side": order.side.value, "outcome": order.outcome.value, "price": str(order.price),
                     "quantity": str(order.original_quantity), "remainingQuantity": str(order.remaining_quantity), "status": "open"}
                    for order in runtime.account.reducer.orders.values() if order.working]
        async def history():
            return [{"orderId": order.order_id, "clientOrderId": order.client_order_id, "status": "cancelled"}
                    for order in runtime.account.reducer.orders.values() if not order.working]
        runtime.private = SimpleNamespace(place_maker=AsyncMock(side_effect=place), cancel=AsyncMock(side_effect=cancel), close=AsyncMock())
        runtime.rest = SimpleNamespace(positions=AsyncMock(return_value=[]), active_orders=active, order_history=history)
        permit = {"permit_version": 2, "permit_id": "offline-only", "approved_at": (NOW - timedelta(seconds=1)).isoformat(),
                  "expires_at": (NOW + timedelta(hours=1)).isoformat(), "maximum_active_pools": 1,
                  "policy_sha256": policy_digest(DEFAULT_POLICY_PATH), "build_sha256": build_digest(),
                  "account_fingerprint": hashlib.sha256(b"account-fake").hexdigest(), "symbols": ["X"]}
        path = Path(self.tmp.name) / "fake-permit.json"
        path.write_text(json.dumps(permit), encoding="utf-8")
        # Fix only the permit wall clock; exchange methods above are all fake.
        with patch("gemini_bot.activation.datetime") as date:
            date.now.return_value = NOW
            from datetime import datetime
            date.fromisoformat.side_effect = datetime.fromisoformat
            await runtime.activate(path, commissioning_seconds=2, single_leg=True)
        self.assertEqual(runtime.private.place_maker.await_count, 1)
        self.assertEqual(runtime.account.reducer.pending_order_count(), 0)
        self.assertFalse(runtime.gate.may_submit)
        status = json.loads((Path(self.tmp.name) / "status.json").read_text())
        self.assertEqual(status["phase"], "stopped_flat")

    async def test_mechanics_canary_can_ignore_only_uptime_proxy(self):
        runtime = self.runtime
        strategy, risk = load_policy()
        runtime.config = LiveConfig("account-fake", "fake-secret", "I_UNDERSTAND_THIS_TRADES_REAL_MONEY", True, strategy, risk)
        runtime.gate.state = EngineState.READY
        runtime.universe = [RewardedContract("E", candidate_pool(), candidate_rules(), "2027-01-01T00:00:00Z", NOW.isoformat(), (NOW + timedelta(hours=2)).isoformat())]
        from gemini_bot.domain import UptimeProgress
        from gemini_bot.reward_ranking import RankInput, rank_candidate
        blocked = rank_candidate(
            RankInput(candidate_pool(), candidate_rules(), book().top(), UptimeProgress(80, 10, 20)),
            now=NOW, offset_ticks=strategy.quote_offset_ticks,
        )
        self.assertEqual(blocked.reasons, ("50% uptime is no longer reachable in the estimated window",))
        runtime.ranked = [blocked]
        async def place(leg):
            intent = runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
            runtime.ledger.mark_sent(intent.client_order_id)
            runtime.account.apply({"e": "orderUpdate", "i": leg.outcome.value, "c": intent.client_order_id,
                                   "s": "X", "S": "BUY", "O": leg.outcome.value,
                                   "q": "10", "z": "10", "p": str(leg.price), "X": "OPEN"})
            return intent.client_order_id
        async def cancel(order_id):
            runtime.account.apply({"e": "orderUpdate", "i": order_id, "X": "CANCELED", "z": "0", "Z": "0"})
        async def active():
            return [{"orderId": order.order_id, "clientOrderId": order.client_order_id, "symbol": order.symbol,
                     "side": order.side.value, "outcome": order.outcome.value, "price": str(order.price),
                     "quantity": str(order.original_quantity), "remainingQuantity": str(order.remaining_quantity), "status": "open"}
                    for order in runtime.account.reducer.orders.values() if order.working]
        async def history():
            return [{"orderId": order.order_id, "clientOrderId": order.client_order_id, "status": "cancelled"}
                    for order in runtime.account.reducer.orders.values() if not order.working]
        runtime.private = SimpleNamespace(place_maker=AsyncMock(side_effect=place), cancel=AsyncMock(side_effect=cancel), close=AsyncMock())
        runtime.rest = SimpleNamespace(positions=AsyncMock(return_value=[]), active_orders=active, order_history=history)
        permit = {"permit_version": 2, "permit_id": "offline-uptime-only", "approved_at": (NOW - timedelta(seconds=1)).isoformat(),
                  "expires_at": (NOW + timedelta(hours=1)).isoformat(), "maximum_active_pools": 1,
                  "policy_sha256": policy_digest(DEFAULT_POLICY_PATH), "build_sha256": build_digest(),
                  "account_fingerprint": hashlib.sha256(b"account-fake").hexdigest(), "symbols": ["X"]}
        path = Path(self.tmp.name) / "uptime-permit.json"
        path.write_text(json.dumps(permit), encoding="utf-8")
        with patch("gemini_bot.activation.datetime") as date:
            date.now.return_value = NOW
            from datetime import datetime
            date.fromisoformat.side_effect = datetime.fromisoformat
            await runtime.activate(
                path, commissioning_seconds=2, single_leg=True, ignore_uptime_proxy=True,
                expected_yes_price=blocked.quote.yes.price,
            )
        self.assertEqual(runtime.private.place_maker.await_count, 1)
        self.assertEqual(runtime.account.reducer.pending_order_count(), 0)

    async def test_uptime_proxy_is_reporting_only_and_other_failures_still_block(self):
        runtime = self.runtime
        from dataclasses import replace
        uptime_only = replace(candidate(), eligible_now=False, uptime_feasible=False,
                              reasons=("50% uptime is no longer reachable in the estimated window",))
        self.assertTrue(runtime._candidate_actionable(uptime_only))
        stale_too = replace(uptime_only, reasons=("stale market data", "50% uptime is no longer reachable in the estimated window"))
        self.assertFalse(runtime._candidate_actionable(stale_too))
        runtime._commissioning_expected_yes_price = uptime_only.quote.yes.price + D(".01")
        self.assertFalse(runtime._candidate_actionable(uptime_only))

    async def test_cleanup_does_not_cancel_actionable_mechanics_canary(self):
        runtime = self.runtime
        from dataclasses import replace
        uptime_only = replace(candidate(), eligible_now=False, uptime_feasible=False,
                              reasons=("50% uptime is no longer reachable in the estimated window",))
        runtime._commissioning_ignore_uptime_proxy = True
        runtime._commissioning_expected_yes_price = uptime_only.quote.yes.price
        runtime.account.reducer.apply_order_update(
            {"e": "orderUpdate", "i": "working-1", "c": "owned-1", "s": "X",
             "S": "BUY", "O": "YES", "q": "10", "z": "10",
             "p": str(uptime_only.quote.yes.price), "X": "OPEN"}
        )
        runtime._cancel_symbol_entries = AsyncMock()
        await runtime._cancel_ineligible_working_entries({"X": uptime_only})
        runtime._cancel_symbol_entries.assert_not_awaited()
        stale_too = replace(uptime_only, reasons=("stale market data", uptime_only.reasons[0]))
        await runtime._cancel_ineligible_working_entries({"X": stale_too})
        runtime._cancel_symbol_entries.assert_awaited_once_with("X")


class RewardAndRateRepairTests(unittest.TestCase):
    def test_public_throttle_sets_cooldown_and_rejects_frame(self):
        governor = AdmissionGovernor()
        socket = PublicMarketSocket(governor)
        with self.assertRaises(RuntimeError):
            socket.apply_frame({"status": 429})
        self.assertGreater(governor.lanes["public_ws"].delay(), 59)

    def test_invalid_depth_numbers_never_become_a_snapshot(self):
        for price, quantity in ((D("NaN"), D("1")), (D(".5"), D("-1")), (D("1.5"), D("1"))):
            with self.assertRaises(ValueError):
                LocalBook("X").apply_snapshot([(price, quantity)], [], 1, 1, NOW)
    def test_one_sided_qualified_order_counts_but_stale_or_far_does_not(self):
        from gemini_bot.account_state import AccountReducer
        account = AccountReducer()
        account.apply_order_update({"e": "orderUpdate", "i": "o1", "c": "c1", "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "10", "p": ".46", "X": "OPEN"})
        item = RewardedContract("E", candidate_pool(), candidate_rules(), "2027-01-01T00:00:00Z")
        self.assertTrue(locally_qualifying(item, account, book(), now=NOW))
        self.assertFalse(locally_qualifying(item, account, book(), now=NOW + timedelta(seconds=4)))
        account.orders["o1"].price = D(".1")
        self.assertFalse(locally_qualifying(item, account, book(), now=NOW))

    def test_own_depth_is_subtracted_without_mutating_exchange_book(self):
        from gemini_bot.account_state import AccountReducer
        account = AccountReducer()
        account.apply_order_update({"e": "orderUpdate", "i": "o1", "c": "c1", "s": "X", "S": "BUY", "O": "YES", "q": "100", "z": "100", "p": ".48", "X": "OPEN"})
        local = book()
        local.bids[D(".45")] = D("100")
        item = RewardedContract("E", candidate_pool(), candidate_rules(), "2027-01-01T00:00:00Z")
        rows = rank_live_universe([item], {"X": local}, now=NOW, quantity=D("10"), offset_ticks=2, account=account)
        self.assertEqual(rows[0].quote.yes.price, D(".43"))
        self.assertIn(D(".48"), local.bids)

    def test_emergency_cancel_respects_local_and_server_pacing(self):
        governor = AdmissionGovernor()
        self.assertEqual(governor.admit("private_ws", Priority.EMERGENCY_CANCEL, now=100), 0)
        self.assertGreater(governor.admit("private_ws", Priority.EMERGENCY_CANCEL, now=100), 0)
        governor.apply_server_cooldown("private_ws", 120, now=100)
        self.assertEqual(governor.admit("private_ws", Priority.EMERGENCY_CANCEL, now=100), 120)

    def test_permit_cannot_move_to_other_account_or_build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = SQLiteJournal(path / "journal.sqlite")
            try:
                raw = {"permit_version": 2, "permit_id": "fake", "approved_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=1)).isoformat(), "maximum_active_pools": 1,
                       "policy_sha256": policy_digest(DEFAULT_POLICY_PATH), "build_sha256": build_digest(), "account_fingerprint": "fake-a", "symbols": ["X"]}
                (path / "permit.json").write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "account"):
                    load_activation_permit(path / "permit.json", policy_path=DEFAULT_POLICY_PATH, store=store, now=NOW, account_fingerprint="fake-b")
                raw["build_sha256"] = "wrong"
                (path / "permit.json").write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "build"):
                    load_activation_permit(path / "permit.json", policy_path=DEFAULT_POLICY_PATH, store=store, now=NOW, account_fingerprint="fake-a")
            finally:
                store.close()


def candidate_pool():
    from gemini_bot.domain import RewardPool
    return RewardPool("pool", D("100"))


def candidate_rules():
    from gemini_bot.domain import ContractRules
    return ContractRules("X", D(".01"), D("1"), D("1"))
