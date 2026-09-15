from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.activation import (
    consume_activation_permit,
    load_activation_permit,
    policy_digest,
    build_digest,
)
from gemini_bot.catalog import RewardedContract
from gemini_bot.config import DEFAULT_POLICY_PATH, LiveConfig, load_policy
from gemini_bot.discovery import RewardUniverseLoader, select_pool_representatives
from gemini_bot.domain import ContractRules, Outcome, QuoteLeg, RewardPool, Side
from gemini_bot.execution import IntentLedger, IntentState, OrderPurpose
from gemini_bot.persistent_risk import PersistentRiskWindow
from gemini_bot.reconciliation import (
    assert_position_consistency,
    assert_stable_account_bracket,
    build_account_snapshot,
    exposure_from_snapshot,
)
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.runtime_files import atomic_write_json
from gemini_bot.storage import SQLiteJournal
from gemini_bot.uptime import locally_qualifying, sample_and_load_progress
from gemini_bot.account_state import AccountReducer
from gemini_bot.orchestrator import ProductionOrchestrator
from gemini_bot.market_state import LocalBook
from gemini_bot.domain import EngineState
from gemini_bot.domain import BookTop
from gemini_bot.reward_ranking import RankInput, rank_candidate


NOW = datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc)


class ReconciliationTests(unittest.TestCase):
    def test_signed_positions_and_pending_exposure_are_exact(self) -> None:
        snapshot = build_account_snapshot(
            [{
                "symbol": "test", "outcome": "no", "totalQuantity": "10",
                "avgPrice": ".20", "marketValue": "2.50",
            }],
            [{
                "orderId": "o1", "clientOrderId": "c1", "symbol": "test",
                "side": "buy", "outcome": "yes", "price": ".40",
                "quantity": "10", "remainingQuantity": "4", "status": "open",
            }],
        )
        self.assertEqual(snapshot.signed_positions, {"TEST": Decimal("-10")})
        exposure = exposure_from_snapshot(snapshot)
        self.assertEqual(exposure.gross_reservation_usd, Decimal("3.60"))
        self.assertEqual(exposure.pending_orders, 1)

    def test_rest_and_websocket_position_mismatch_fails_closed(self) -> None:
        snapshot = build_account_snapshot(
            [{
                "symbol": "X", "outcome": "YES", "totalQuantity": "10",
                "avgPrice": ".20", "marketValue": "2",
            }], []
        )
        with self.assertRaises(ValueError):
            assert_position_consistency(snapshot, {"X": Decimal("9")})

    def test_duplicate_position_symbol_requires_manual_reconciliation(self) -> None:
        row = {
            "symbol": "X", "outcome": "YES", "totalQuantity": "10",
            "avgPrice": ".20", "marketValue": "2",
        }
        with self.assertRaises(ValueError):
            build_account_snapshot([row, {**row, "outcome": "NO"}], [])

    def test_startup_bracket_allows_only_market_value_drift(self) -> None:
        first = build_account_snapshot([{
            "symbol": "X", "outcome": "YES", "totalQuantity": "10",
            "avgPrice": ".20", "marketValue": "2",
        }], [])
        marked = build_account_snapshot([{
            "symbol": "X", "outcome": "YES", "totalQuantity": "10",
            "avgPrice": ".20", "marketValue": "3",
        }], [])
        assert_stable_account_bracket(first, marked)
        changed = build_account_snapshot([{
            "symbol": "X", "outcome": "YES", "totalQuantity": "9",
            "avgPrice": ".20", "marketValue": "3",
        }], [])
        with self.assertRaises(ValueError):
            assert_stable_account_bracket(first, changed)


class PersistentRiskTests(unittest.TestCase):
    def test_loss_and_classification_only_tighten_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk.sqlite"
            store = SQLiteJournal(path)
            policy = RiskWindowPolicy(Decimal("25"))
            first = PersistentRiskWindow(policy, store).update(
                now=NOW, adjusted_equity=Decimal("300"), classification_complete=True
            )
            self.assertEqual(first.pending_order_cap, 30)
            loss = PersistentRiskWindow(policy, store).update(
                now=NOW + timedelta(minutes=1), adjusted_equity=Decimal("289.999999999999999999"),
                classification_complete=False,
            )
            self.assertEqual(loss.loss_high_water, Decimal("10.000000000000000001"))
            self.assertEqual(loss.pending_order_cap, 20)
            store.close()

            reopened = SQLiteJournal(path)
            recovered = PersistentRiskWindow(policy, reopened).update(
                now=NOW + timedelta(minutes=2), adjusted_equity=Decimal("310"),
                classification_complete=True,
            )
            self.assertEqual(recovered.loss_high_water, loss.loss_high_water)
            self.assertEqual(recovered.pending_order_cap, 20)
            self.assertFalse(recovered.classification_complete)
            reopened.close()

    def test_new_six_hour_window_gets_new_opening_equity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "risk.sqlite")
            policy = PersistentRiskWindow(RiskWindowPolicy(Decimal("25")), store)
            policy.update(now=NOW, adjusted_equity=Decimal("300"), classification_complete=True)
            later = policy.update(
                now=NOW + timedelta(hours=6), adjusted_equity=Decimal("280"),
                classification_complete=True,
            )
            self.assertEqual(later.opening_equity, Decimal("280"))
            self.assertEqual(later.loss_high_water, Decimal("0"))
            store.close()


class ActivationTests(unittest.TestCase):
    def _write(self, path: Path, **overrides: object) -> None:
        value = {
            "permit_version": 2,
            "permit_id": "approval-1",
            "approved_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "maximum_active_pools": 1,
            "policy_sha256": policy_digest(DEFAULT_POLICY_PATH),
            "account_fingerprint": "fake-account",
            "build_sha256": build_digest(),
            "symbols": ["X"],
            **overrides,
        }
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_permit_is_policy_bound_short_lived_and_one_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permit_path = root / "permit.json"
            self._write(permit_path)
            store = SQLiteJournal(root / "journal.sqlite")
            permit = load_activation_permit(
                permit_path, policy_path=DEFAULT_POLICY_PATH, store=store,
                now=NOW + timedelta(minutes=1), account_fingerprint="fake-account",
            )
            consume_activation_permit(permit, store, now=NOW + timedelta(minutes=1))
            with self.assertRaisesRegex(ValueError, "already been consumed"):
                load_activation_permit(
                    permit_path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(minutes=2), account_fingerprint="fake-account",
                )
            store.close()

    def test_orchestrator_cannot_activate_before_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            strategy, risk = load_policy()
            config = LiveConfig(
                "account-example", "secret", "I_UNDERSTAND_THIS_TRADES_REAL_MONEY",
                True, strategy, risk,
            )
            runtime = ProductionOrchestrator(config, Path(directory))
            try:
                with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                    asyncio.run(runtime.activate(Path(directory) / "missing-permit.json"))
            finally:
                asyncio.run(runtime.close())

    def test_expired_or_policy_mismatched_permit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteJournal(root / "journal.sqlite")
            path = root / "permit.json"
            self._write(path, expires_at=(NOW + timedelta(seconds=1)).isoformat())
            with self.assertRaises(ValueError):
                load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(seconds=2),
                )
            self._write(path, policy_sha256="0" * 64)
            with self.assertRaises(ValueError):
                load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(seconds=1),
                )
            store.close()


class AccountRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_fill_is_durable_deduplicated_and_requests_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            ledger = IntentLedger(store)
            intent = ledger.prepare(
                QuoteLeg("X", Outcome.YES, Side.BUY, Decimal(".40"), Decimal("10")),
                OrderPurpose.ENTRY,
            )
            ledger.mark_sent(intent.client_order_id)
            runtime = AccountRuntime(ledger, store)
            frame = {
                "e": "orderUpdate", "i": "order-1", "c": intent.client_order_id,
                "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "7",
                "X": "PARTIALLY_FILLED", "Z": "3", "t": "trade-1", "L": ".40",
                "n": ".01", "T": 1_789_492_800_000_000_000,
            }
            runtime.apply(frame)
            runtime.apply(frame)
            self.assertEqual(ledger.get(intent.client_order_id).state, IntentState.ACKNOWLEDGED)
            self.assertEqual(await runtime.safety_symbols.get(), "X")
            self.assertTrue(runtime.safety_symbols.empty())
            count = store.connection.execute("SELECT COUNT(*) FROM execution_fill").fetchone()[0]
            self.assertEqual(count, 1)
            store.close()

    async def test_unowned_order_halts_account_reducer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            runtime = AccountRuntime(IntentLedger(store), store)
            with self.assertRaisesRegex(RuntimeError, "unowned"):
                runtime.apply({
                    "e": "orderUpdate", "i": "o", "c": "someone-else", "s": "X",
                    "S": "BUY", "O": "YES", "q": "10", "z": "10", "X": "OPEN",
                })
            self.assertTrue(runtime.unowned_order_seen)
            store.close()


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_pool_is_discovered_once_and_best_open_contract_selected(self) -> None:
        class Rest:
            async def reward_events(self, *, ceiling: int = 3000):
                return [
                    {"event_ticker": "E1", "pool_id": 7, "daily_pool_usd": "100", "qualifying_maker_count": 2},
                    {"event_ticker": "E2", "pool_id": 7, "daily_pool_usd": "100", "qualifying_maker_count": 4},
                ]

            async def event(self, event_ticker: str):
                return {"event": {"status": "active", "contracts": [
                    {
                        "instrumentSymbol": "WIDE", "status": "active", "marketState": "open",
                        "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                        "expiryDate": "2026-09-16T00:00:00Z",
                        "prices": {"bestBid": ".2", "bestAsk": ".8"},
                    },
                    {
                        "instrumentSymbol": "TIGHT", "status": "active", "marketState": "open",
                        "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                        "expiryDate": "2026-09-16T00:00:00Z",
                        "prices": {"bestBid": ".4", "bestAsk": ".6"},
                    },
                ]}}

        result = await RewardUniverseLoader(Rest()).load(maximum_pools=32)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].rules.symbol, "TIGHT")

    async def test_discovery_cap_is_unique_pool_cap(self) -> None:
        rows = [
            {"event_ticker": f"E{n}", "pool_id": n, "daily_pool_usd": str(n + 1)}
            for n in range(40)
        ]
        self.assertEqual(len(select_pool_representatives(rows, maximum_pools=32)), 32)


class FileAndConfigTests(unittest.TestCase):
    def test_atomic_status_never_leaves_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            atomic_write_json(path, {"phase": "ready_paused"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["phase"], "ready_paused")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_read_only_base_validation_does_not_require_live_ack(self) -> None:
        strategy, risk = load_policy()
        config = LiveConfig("account-example", "secret", "", True, strategy, risk)
        config.validate_base()
        with self.assertRaises(Exception):
            config.validate()


class UptimeTests(unittest.TestCase):
    def test_uptime_samples_once_per_minute_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uptime.sqlite"
            store = SQLiteJournal(path)
            contract = RewardedContract(
                "E", RewardPool("pool", Decimal("100")),
                ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1")),
                "2026-09-16T00:00:00Z",
                "2026-09-15T15:00:00Z", "2026-09-15T18:00:00Z",
            )
            reducer = AccountReducer()
            first = sample_and_load_progress(store, [contract], reducer, now=NOW)
            again = sample_and_load_progress(store, [contract], reducer, now=NOW + timedelta(seconds=30))
            self.assertEqual(first["pool"].eligible_snapshots, 61)
            self.assertEqual(again["pool"].eligible_snapshots, 61)
            store.close()
            reopened = SQLiteJournal(path)
            later = sample_and_load_progress(
                reopened, [contract], reducer, now=NOW + timedelta(minutes=1)
            )
            self.assertEqual(later["pool"].eligible_snapshots, 62)
            reopened.close()

    def test_two_working_ten_contract_outcomes_are_locally_qualifying(self) -> None:
        reducer = AccountReducer()
        for order_id, outcome in (("1", "YES"), ("2", "NO")):
            reducer.apply_order_update({
                "e": "orderUpdate", "i": order_id, "c": "owned-" + order_id,
                "s": "X", "S": "BUY", "O": outcome, "q": "10", "z": "10", "p": ".46", "X": "OPEN",
            })
        contract = RewardedContract(
            "E", RewardPool("pool", Decimal("100")),
            ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1")),
            "2026-09-16T00:00:00Z",
        )
        book = LocalBook("X")
        book.apply_snapshot([(Decimal(".48"), Decimal("100"))], [(Decimal(".52"), Decimal("100"))], 1, 1, NOW)
        self.assertTrue(locally_qualifying(contract, reducer, book, now=NOW))


class ReadOnlyPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_reconciles_and_ranks_without_mutation(self) -> None:
        class Rest:
            async def terms_status(self): return {"hasAcceptedLatest": True}
            async def reward_config(self): return {"enabled": True}
            async def positions(self): return []
            async def active_orders(self): return []
            async def order_history(self): return []
            async def reward_events(self, *, ceiling: int = 3000):
                return [{
                    "event_ticker": "E", "pool_id": 1, "daily_pool_usd": "100",
                    "ends_at": "2027-01-01T00:00:00Z",
                }]
            async def event(self, event_ticker: str):
                return {"event": {"status": "active", "contracts": [{
                    "instrumentSymbol": "X", "status": "active", "marketState": "open",
                    "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                    "expiryDate": "2027-01-01T00:00:00Z",
                    "prices": {"bestBid": ".48", "bestAsk": ".52"},
                }]}}

        class Public:
            def __init__(self): self.books = {}
            async def connect(self): return None
            async def subscribe(self, symbols):
                for symbol in symbols:
                    value = LocalBook(symbol)
                    value.apply_snapshot(
                        [(Decimal(".48"), Decimal("100"))],
                        [(Decimal(".52"), Decimal("100"))], 1, 1, NOW,
                    )
                    self.books[symbol] = value
            async def frames(self):
                while True:
                    await asyncio.sleep(3600)
                    yield {}
            def apply_frame(self, frame): return None
            async def resync(self, symbol): return None
            async def close(self): return None

        class Private:
            mutations = 0
            async def connect(self): return None
            async def subscribe_account(self): return None
            async def switch_to_account_deltas(self): return None
            async def messages(self):
                yield {"status": 200}
                yield {"e": "balanceUpdate", "B": [{"a": "USD", "f": "100", "c": "100"}]}
                yield {"e": "positionReport", "P": [], "u": 1}
                while True:
                    await asyncio.sleep(3600)
                    yield {}
            async def close(self): return None

        with tempfile.TemporaryDirectory() as directory:
            strategy, risk = load_policy()
            runtime = ProductionOrchestrator(
                LiveConfig("account-example", "secret", "", True, strategy, risk),
                Path(directory),
            )
            private = Private()
            runtime.rest = Rest()
            runtime.control_directory = Path(directory) / "isolated-control"
            runtime.public = Public()
            runtime.private = private
            try:
                result = await runtime.prepare(timeout=2)
                self.assertEqual(runtime.gate.state, EngineState.READY)
                self.assertEqual(result.ranked_count, 1)
                self.assertEqual(private.mutations, 0)
                status = json.loads((Path(directory) / "status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["phase"], "ready_paused")
                self.assertFalse(status["activated"])
            finally:
                await runtime.close()


class OrchestratorWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_pair_places_yes_then_ack_then_no(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            strategy, risk = load_policy()
            runtime = ProductionOrchestrator(
                LiveConfig("account-example", "secret", "", True, strategy, risk),
                Path(directory),
            )

            class Private:
                def __init__(self): self.outcomes = []
                async def place_maker(self, leg):
                    if leg.outcome is Outcome.NO:
                        self_test.assertEqual(self.outcomes, [Outcome.YES])
                    intent = runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
                    runtime.ledger.mark_sent(intent.client_order_id)
                    self.outcomes.append(leg.outcome)
                    runtime.account.apply({
                        "e": "orderUpdate", "i": "order-" + leg.outcome.value,
                        "c": intent.client_order_id, "s": leg.symbol, "S": "BUY",
                        "O": leg.outcome.value, "p": str(leg.price), "q": str(leg.quantity),
                        "z": str(leg.quantity), "X": "OPEN",
                    })
                    return intent.client_order_id
                async def close(self): return None

            self_test = self
            private = Private()
            runtime.private = private
            runtime.account.available_usd = Decimal("100")
            runtime._expiry["X"] = NOW + timedelta(hours=2)
            book = LocalBook("X")
            book.apply_snapshot([(Decimal(".48"), Decimal("100"))], [(Decimal(".52"), Decimal("100"))], 1, 1, NOW)
            runtime.public.books["X"] = book
            runtime.gate.state = EngineState.QUOTING
            for name in (
                "terms_accepted", "balances_known", "positions_known", "open_orders_known",
                "account_stream_live", "public_stream_live",
            ):
                setattr(runtime.gate.reconciliation, name, True)
            candidate = rank_candidate(
                RankInput(
                    RewardPool("pool", Decimal("100")),
                    ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1")),
                    BookTop(
                        "X", Decimal(".48"), Decimal("100"), Decimal(".52"),
                        Decimal("100"), 1, NOW,
                    ),
                ),
                now=NOW,
            )
            try:
                with patch("gemini_bot.orchestrator._utc_now", return_value=NOW):
                    await runtime._quote_pair(candidate)
                self.assertEqual(private.outcomes, [Outcome.YES, Outcome.NO])
                self.assertEqual(runtime.account.reducer.pending_order_count(), 2)
            finally:
                await runtime.close()


if __name__ == "__main__":
    unittest.main()
