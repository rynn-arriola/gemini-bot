"""Offline failure-boundary tests; no credentials or exchange connections."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal as D
import json
import hashlib
from pathlib import Path
import tempfile
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
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.rate_limits import AdmissionGovernor, Priority
from gemini_bot.rank_report import rank_live_universe
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.storage import SQLiteJournal
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

    def test_restart_cannot_accept_new_cash_as_fresh_baseline(self):
        restored = AccountRuntime(IntentLedger(self.store), self.store)
        restored.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "200", "f": "200"}]})
        restored.apply({"e": "positionReport", "P": [], "u": 1})
        self.assertFalse(restored.equity_classification_complete)

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
        self.runtime = ProductionOrchestrator(LiveConfig("account-fake", "fake-secret", "", True, strategy, risk), Path(self.tmp.name))
        self.runtime.public.books["X"] = book()
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
        intent = self.runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
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

    async def test_exit_send_guard_requires_same_outcome_and_exact_quantity(self):
        self.runtime.account.reducer.positions["X"] = D("-3")
        payload = {"params": {"symbol": "X", "side": "SELL", "quantity": "3", "eventOutcome": "NO"}}
        self.runtime._validate_send(payload)
        payload["params"]["eventOutcome"] = "YES"
        with self.assertRaises(RuntimeError):
            self.runtime._validate_send(payload)

    async def test_live_marks_preserve_transient_loss_between_rest_polls(self):
        account = self.runtime.account
        account.reducer.positions = {"X": D("100")}
        account.expected_positions = {"X": D("100")}
        self.runtime._mark_live_risk()
        self.runtime.public.books["X"].apply_snapshot([(D(".28"), D("100"))], [(D(".32"), D("100"))], 2, 2, NOW)
        self.runtime._mark_live_risk()
        self.assertEqual(self.runtime._dynamic_pending_cap, 10)
        self.runtime.public.books["X"] = book()
        self.runtime._mark_live_risk()
        stored = self.runtime.journal.load_risk_window(RiskWindowPolicy(D("25")).bounds(NOW)[0])
        self.assertEqual(stored["loss_high_water"], D("20"))
        self.assertEqual(self.runtime._dynamic_pending_cap, 10)

    async def test_sequence_gap_halts_without_reusing_buffered_delta(self):
        async def frames():
            yield {"e": "depthUpdate", "s": "X", "U": 5, "u": 5, "b": [], "a": []}
        self.runtime.public.frames = frames
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

    async def test_contract_closure_invalidates_permission(self):
        async def frames():
            yield {"e": "contractStatus", "s": "x", "n": "Closed"}
        self.runtime.public.frames = frames
        with self.assertRaises(RuntimeError):
            await self.runtime._public_pump()
        self.assertIn("X", self.runtime.account.blocked_entries)
        self.assertFalse(self.runtime.gate.may_submit)

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
