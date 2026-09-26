from __future__ import annotations

import policy_v8  # noqa: F401  (pins the v8-era default policy)
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.activation import (
    consume_activation_permit,
    finish_supervised_activation,
    load_activation_permit,
    policy_digest,
    build_digest,
)
from gemini_bot.catalog import RewardedContract
from gemini_bot.config import DEFAULT_POLICY_PATH, LiveConfig, load_policy
from gemini_bot.discovery import RewardUniverseLoader, select_pool_representatives
from gemini_bot.rest_client import ApiError
from gemini_bot.domain import ContractRules, Outcome, QuoteLeg, RewardPool, Side
from gemini_bot.execution import IntentLedger, IntentState, OrderPurpose
from gemini_bot.persistent_risk import PersistentRiskWindow
from gemini_bot.reconciliation import (
    assert_position_consistency,
    assert_stable_account_bracket,
    build_account_snapshot,
    exposure_from_snapshot,
)
from gemini_bot.reconciliation_errors import InventoryMarketUnavailable
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.runtime_files import atomic_write_json
from gemini_bot.storage import SQLiteJournal, WindowsOwnerLock
from gemini_bot.uptime import (
    eastern_day_window,
    estimated_reward_window,
    locally_qualifying,
    sample_and_load_progress,
)
from gemini_bot.domain import uptime_progress_key
from gemini_bot.account_state import AccountReducer
from gemini_bot.orchestrator import ProductionOrchestrator
from gemini_bot.market_state import LocalBook
from gemini_bot.domain import EngineState
from gemini_bot.domain import BookTop
from gemini_bot.reward_ranking import RankInput, rank_candidate
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.rate_limits import AdmissionGovernor


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
    def test_only_a_closed_fill_lifecycle_enters_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "risk.sqlite")
            start, end = RiskWindowPolicy(Decimal("30")).bounds(NOW)
            store.record_fill(
                trade_id="entry", order_id="o1", client_order_id="c1",
                symbol="X", side="BUY", outcome="YES", quantity=Decimal("10"),
                price=Decimal(".40"), fee=Decimal(".01"), event_at=NOW,
            )
            self.assertEqual(store.realized_pnl_for_window(start, end), (Decimal("0"), True))
            store.record_fill(
                trade_id="exit", order_id="o2", client_order_id="c2",
                symbol="X", side="SELL", outcome="YES", quantity=Decimal("10"),
                price=Decimal(".30"), fee=Decimal(".02"),
                event_at=NOW + timedelta(minutes=1),
            )
            self.assertEqual(
                store.realized_pnl_for_window(start, end),
                (Decimal("-1.03"), True),
            )
            store.close()

    def test_manual_flat_marks_missing_closing_pnl_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "risk.sqlite")
            start, end = RiskWindowPolicy(Decimal("30")).bounds(NOW)
            store.record_fill(
                trade_id="old-entry", order_id="o1", client_order_id="c1",
                symbol="X", side="BUY", outcome="NO", quantity=Decimal("10"),
                price=Decimal(".70"), fee=Decimal("0"), event_at=NOW,
            )
            reset_at = NOW + timedelta(minutes=1)
            store.connection.execute(
                "INSERT INTO audit_event(occurred_at,kind,details_json) VALUES(?,?,?)",
                (reset_at.isoformat(), "operator_manual_flat_reconciled", "{}"),
            )
            store.record_fill(
                trade_id="new-entry", order_id="o2", client_order_id="c2",
                symbol="X", side="BUY", outcome="YES", quantity=Decimal("10"),
                price=Decimal(".40"), fee=Decimal("0"),
                event_at=NOW + timedelta(minutes=2),
            )
            store.record_fill(
                trade_id="new-exit", order_id="o3", client_order_id="c3",
                symbol="X", side="SELL", outcome="YES", quantity=Decimal("10"),
                price=Decimal(".35"), fee=Decimal("0"),
                event_at=NOW + timedelta(minutes=3),
            )
            pnl, complete = store.realized_pnl_for_window(start, end)
            self.assertEqual(pnl, Decimal("-.50"))
            self.assertFalse(complete)
            store.close()

    def test_realized_loss_keeps_fixed_cap_until_hard_and_tightens_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk.sqlite"
            store = SQLiteJournal(path)
            policy = RiskWindowPolicy(Decimal("30"))
            first = PersistentRiskWindow(policy, store).update(
                now=NOW, realized_pnl=Decimal("0"), classification_complete=True
            )
            self.assertEqual(first.pending_order_cap, 30)
            loss = PersistentRiskWindow(policy, store).update(
                now=NOW + timedelta(minutes=1), realized_pnl=Decimal("-29.999999999999999999"),
                classification_complete=False,
            )
            self.assertEqual(loss.loss_high_water, Decimal("29.999999999999999999"))
            self.assertEqual(loss.pending_order_cap, 30)
            hard = PersistentRiskWindow(policy, store).update(
                now=NOW + timedelta(minutes=2), realized_pnl=Decimal("-30"),
                classification_complete=True,
            )
            self.assertEqual(hard.pending_order_cap, 0)
            store.close()
            reopened = SQLiteJournal(path)
            recovered = PersistentRiskWindow(policy, reopened).update(
                now=NOW + timedelta(minutes=3), realized_pnl=Decimal("10"),
                classification_complete=True,
            )
            self.assertEqual(recovered.loss_high_water, hard.loss_high_water)
            self.assertEqual(recovered.pending_order_cap, 0)
            self.assertFalse(recovered.classification_complete)
            reopened.close()

    def test_new_six_hour_window_gets_new_realized_loss_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "risk.sqlite")
            policy = PersistentRiskWindow(RiskWindowPolicy(Decimal("30")), store)
            policy.update(now=NOW, realized_pnl=Decimal("-20"), classification_complete=True)
            later = policy.update(
                now=NOW + timedelta(hours=6), realized_pnl=Decimal("0"),
                classification_complete=True,
            )
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

    def test_version_three_permit_binds_uptime_override_to_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteJournal(root / "journal.sqlite")
            path = root / "permit.json"
            self._write(
                path,
                permit_version=3,
                uptime_override_until=(NOW + timedelta(minutes=30)).isoformat(),
            )
            permit = load_activation_permit(
                path, policy_path=DEFAULT_POLICY_PATH, store=store,
                now=NOW + timedelta(minutes=1), account_fingerprint="fake-account",
            )
            self.assertEqual(permit.uptime_override_until, NOW + timedelta(minutes=30))
            self._write(
                path,
                permit_version=3,
                uptime_override_until=(NOW + timedelta(hours=2)).isoformat(),
            )
            with self.assertRaisesRegex(ValueError, "within permit validity"):
                load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(minutes=1), account_fingerprint="fake-account",
                )
            store.close()

    def test_version_four_permit_authorizes_only_reward_catalog_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteJournal(root / "journal.sqlite")
            path = root / "permit.json"
            self._write(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["permit_version"] = 4
            value.pop("symbols")
            value["universe_scope"] = "reward_catalog"
            value["uptime_override_until"] = (NOW + timedelta(minutes=30)).isoformat()
            path.write_text(json.dumps(value), encoding="utf-8")
            permit = load_activation_permit(
                path, policy_path=DEFAULT_POLICY_PATH, store=store,
                now=NOW + timedelta(minutes=1), account_fingerprint="fake-account",
            )
            self.assertEqual(permit.universe_scope, "reward_catalog")
            self.assertEqual(permit.symbols, ())
            value["universe_scope"] = "all_markets"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "scope is not approved"):
                load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(minutes=1), account_fingerprint="fake-account",
                )
            store.close()

    def test_version_five_session_allows_only_two_proven_flat_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteJournal(root / "journal.sqlite")
            path = root / "permit.json"
            self._write(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value.update({
                "permit_version": 5,
                "universe_scope": "reward_catalog",
                "uptime_override_until": (NOW + timedelta(minutes=30)).isoformat(),
                "supervised_restart_limit": 2,
            })
            value.pop("symbols")
            path.write_text(json.dumps(value), encoding="utf-8")

            for activation_number in range(1, 4):
                permit = load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(minutes=activation_number),
                    account_fingerprint="fake-account",
                )
                consume_activation_permit(
                    permit, store, now=NOW + timedelta(minutes=activation_number),
                    supervisor_session_id="session-a",
                )
                finish_supervised_activation(
                    permit, store, supervisor_session_id="session-a",
                    outcome="recoverable_flat",
                    now=NOW + timedelta(minutes=activation_number),
                )
            with self.assertRaisesRegex(ValueError, "not recoverable"):
                load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(minutes=4), account_fingerprint="fake-account",
                )
            store.close()

    def test_version_five_session_refuses_cross_session_or_nonflat_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteJournal(root / "journal.sqlite")
            path = root / "permit.json"
            self._write(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value.update({
                "permit_version": 5,
                "universe_scope": "reward_catalog",
                "uptime_override_until": (NOW + timedelta(minutes=30)).isoformat(),
                "supervised_restart_limit": 2,
            })
            value.pop("symbols")
            path.write_text(json.dumps(value), encoding="utf-8")
            permit = load_activation_permit(
                path, policy_path=DEFAULT_POLICY_PATH, store=store,
                now=NOW + timedelta(minutes=1), account_fingerprint="fake-account",
            )
            with self.assertRaisesRegex(ValueError, "session identity"):
                consume_activation_permit(permit, store)
            consume_activation_permit(
                permit, store, supervisor_session_id="session-a",
                now=NOW + timedelta(minutes=1),
            )
            finish_supervised_activation(
                permit, store, supervisor_session_id="session-a",
                outcome="recoverable_flat", now=NOW + timedelta(minutes=1),
            )
            resumable = load_activation_permit(
                path, policy_path=DEFAULT_POLICY_PATH, store=store,
                now=NOW + timedelta(minutes=2), account_fingerprint="fake-account",
            )
            with self.assertRaisesRegex(ValueError, "cannot be resumed"):
                consume_activation_permit(
                    resumable, store, supervisor_session_id="session-b",
                    now=NOW + timedelta(minutes=2),
                )
            consume_activation_permit(
                resumable, store, supervisor_session_id="session-a",
                now=NOW + timedelta(minutes=2),
            )
            finish_supervised_activation(
                resumable, store, supervisor_session_id="session-a",
                outcome="attention", now=NOW + timedelta(minutes=2),
            )
            with self.assertRaisesRegex(ValueError, "not recoverable"):
                load_activation_permit(
                    path, policy_path=DEFAULT_POLICY_PATH, store=store,
                    now=NOW + timedelta(minutes=3), account_fingerprint="fake-account",
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
            runtime.quote_asset_precision = {"X": 2}
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
    async def test_shared_pool_budget_kept_once_with_multiple_quotable_markets(self) -> None:
        class Rest:
            async def reward_events(self, *, ceiling: int = 3000):
                return [
                    {"event_ticker": "E1", "pool_id": 7, "daily_pool_usd": "100", "qualifying_maker_count": 2},
                    {"event_ticker": "E2", "pool_id": 7, "daily_pool_usd": "100", "qualifying_maker_count": 4},
                ]

            async def event(self, event_ticker: str):
                return {"event": {"status": "active", "contracts": [
                    {
                        "instrumentSymbol": event_ticker + "-WIDE", "status": "active", "marketState": "open",
                        "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                        "quoteAssetPrecision": 2,
                        "expiryDate": "2026-09-16T00:00:00Z",
                        "prices": {"bestBid": ".2", "bestAsk": ".8"},
                    },
                    {
                        "instrumentSymbol": event_ticker + "-TIGHT", "status": "active", "marketState": "open",
                        "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                        "quoteAssetPrecision": 2,
                        "expiryDate": "2026-09-16T00:00:00Z",
                        "prices": {"bestBid": ".48", "bestAsk": ".52"},
                    },
                ]}}

        result = await RewardUniverseLoader(Rest()).load(maximum_pools=32)
        self.assertEqual([c.rules.symbol for c in result], ["E1-TIGHT", "E2-TIGHT"])
        self.assertEqual({c.pool.pool_id for c in result}, {"shared:7"})

    async def test_live_sports_event_is_not_discovered(self) -> None:
        class Rest:
            async def reward_events(self, *, ceiling: int = 3000):
                return [{"event_ticker": "GAME", "daily_pool_usd": "100", "category": "Sports"}]

            async def event(self, event_ticker: str):
                return {"event": {
                    "status": "active", "category": "Sports", "isLive": True,
                    "startTime": "2026-09-15T16:00:00Z", "contracts": [{
                        "instrumentSymbol": "GAME-X", "status": "active", "marketState": "open",
                        "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                        "quoteAssetPrecision": 2,
                        "expiryDate": "2026-09-15T19:00:00Z",
                    }],
                }}

        self.assertEqual(await RewardUniverseLoader(Rest()).load(maximum_pools=32), [])

    async def test_discovery_cap_is_unique_pool_cap(self) -> None:
        rows = [
            {"event_ticker": f"E{n}", "pool_id": n, "daily_pool_usd": str(n + 1)}
            for n in range(40)
        ]
        self.assertEqual(len(select_pool_representatives(rows, maximum_pools=32)), 32)


class FileAndConfigTests(unittest.TestCase):
    def test_approved_inventory_exit_is_five_minutes(self) -> None:
        _, risk = load_policy()
        self.assertEqual(risk.max_inventory_age_seconds, 300)
        self.assertEqual(risk.sports_exit_before_start_seconds, 1200)

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


class PublicLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_pong_refreshes_quiet_shared_stream(self) -> None:
        class Connection:
            async def ping(self):
                future = asyncio.get_running_loop().create_future()
                future.set_result(Decimal(".01"))
                return future

        socket = PublicMarketSocket(AdmissionGovernor())
        socket._connection = Connection()
        latency = await socket.prove_liveness(timeout=1)
        self.assertEqual(latency, .01)
        self.assertIsNotNone(socket.last_message_at)

    async def test_market_subscriptions_batch_depth_and_trades_separately(self) -> None:
        socket = PublicMarketSocket(AdmissionGovernor())
        socket._send = AsyncMock()
        await socket.subscribe(tuple(f"S{index}" for index in range(21)))
        requests = [item.args[1] for item in socket._send.await_args_list]
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[0], ["contractStatus"])
        self.assertEqual(len(requests[1]), 20)
        self.assertTrue(all(value.endswith("@depth@100ms") for value in requests[1]))
        self.assertEqual(len(requests[2]), 20)
        self.assertTrue(all(value.endswith("@trade") for value in requests[2]))
        self.assertEqual(requests[3], ["S20@depth@100ms"])
        self.assertEqual(requests[4], ["S20@trade"])


class UptimeTests(unittest.TestCase):
    def test_eastern_reward_day_uses_eastern_midnight(self) -> None:
        start, end = eastern_day_window(datetime(2026, 9, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 9, 15, 4, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 16, 4, tzinfo=timezone.utc))

    def test_eastern_reward_day_preserves_dst_length(self) -> None:
        spring_start, spring_end = eastern_day_window(datetime(2026, 3, 8, 16, tzinfo=timezone.utc))
        fall_start, fall_end = eastern_day_window(datetime(2026, 11, 1, 16, tzinfo=timezone.utc))
        self.assertEqual(spring_end - spring_start, timedelta(hours=23))
        self.assertEqual(fall_end - fall_start, timedelta(hours=25))

    def test_missing_venue_start_uses_eastern_day_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "uptime.sqlite")
            contract = RewardedContract(
                "E", RewardPool("pool", Decimal("100")),
                ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1")),
                "2026-09-17T00:00:00Z",
            )
            progress = sample_and_load_progress(
                store, [contract], AccountReducer(),
                now=datetime(2026, 9, 15, 15, tzinfo=timezone.utc),
            )["pool"]
            # 11:00 Eastern: 661 minutes elapsed including the current bucket,
            # with 779 remaining buckets (never double count the current bucket).
            self.assertEqual(progress.eligible_snapshots, 661)
            self.assertEqual(progress.remaining_snapshots_estimate, 779)
            self.assertGreater(progress.best_possible_ratio, Decimal(".50"))
            store.close()

    def test_venue_window_does_not_subtract_local_preexpiry_guard(self) -> None:
        contract = RewardedContract(
            "E", RewardPool("pool", Decimal("100")),
            ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1")),
            "2026-09-15T18:00:00Z",
            reward_ends_at="2026-09-15T19:00:00Z",
            trade_starts_at="2026-09-15T15:30:00Z",
        )
        start, end, source = estimated_reward_window(contract, NOW)
        self.assertEqual(start, datetime(2026, 9, 15, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(source, "contract_effective_date_conservative_reward_start")

    def test_shared_pool_keeps_event_uptime_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "uptime.sqlite")
            common = RewardPool("shared:7", Decimal("100"))
            rules = ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1"))
            first = RewardedContract(
                "EARLY", common, rules, "2026-09-15T18:00:00Z",
                "2026-09-15T15:00:00Z", "2026-09-15T18:00:00Z",
            )
            second = RewardedContract(
                "LATE", common, rules, "2026-09-15T19:00:00Z",
                "2026-09-15T16:00:00Z", "2026-09-15T19:00:00Z",
            )
            progress = sample_and_load_progress(
                store, [first, second], AccountReducer(), now=NOW,
            )
            early = progress[uptime_progress_key("EARLY", "shared:7")]
            late = progress[uptime_progress_key("LATE", "shared:7")]
            self.assertEqual(early.eligible_snapshots, 61)
            self.assertEqual(late.eligible_snapshots, 1)
            self.assertEqual(early.observed_window_eligible_snapshots, 1)
            self.assertEqual(late.observed_window_eligible_snapshots, 1)
            rows = store.connection.execute(
                "SELECT event_ticker FROM reward_uptime_v2 ORDER BY event_ticker"
            ).fetchall()
            self.assertEqual(rows, [("EARLY",), ("LATE",)])
            store.close()

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
    async def test_prepare_skips_missing_event_without_mutation(self) -> None:
        await self._prepare_fixture(manual_flat_repair=False, missing_event=True)

    async def test_missing_held_event_keeps_scoped_inventory_proof(self) -> None:
        def verify(runtime):
            self.assertEqual(runtime._inventory_catalog_holds, {"Y"})
            self.assertIn("Y", runtime.account.blocked_entries)
            self.assertEqual(runtime.account.reducer.positions, {"Y": Decimal("10")})
            self.assertIsNotNone(runtime.journal.get_deadline("Y", "YES"))
        await self._prepare_fixture(manual_flat_repair=False, missing_event=True,
            outside_inventory=True, scoped_outside_inventory=True, after_prepare=verify)

    async def test_all_event_404s_remain_no_markets_not_ready(self) -> None:
        from gemini_bot.recovery_policy import NoMarketsAvailable
        with self.assertRaises(NoMarketsAvailable):
            await self._prepare_fixture(manual_flat_repair=False, all_events_missing=True)

    async def test_prepare_reconciles_and_ranks_without_mutation(self) -> None:
        await self._prepare_fixture(manual_flat_repair=False)

    async def test_manual_flat_preparation_reaches_repair_with_old_expected_state(self) -> None:
        await self._prepare_fixture(manual_flat_repair=True)

    async def test_unknown_held_market_missing_from_catalog_stays_global_standby(self) -> None:
        with self.assertRaises(InventoryMarketUnavailable):
            await self._prepare_fixture(manual_flat_repair=False, outside_inventory=True)

    async def test_owned_held_market_missing_from_catalog_is_scoped(self) -> None:
        def verify(runtime):
            self.assertEqual(runtime._inventory_catalog_holds, {"Y"})
            self.assertEqual(runtime.account.reducer.positions, {"Y": Decimal("10")})
            self.assertIn("Y", runtime.account.blocked_entries)
            self.assertNotIn("Y", runtime.public.books)
            exposure = runtime._websocket_exposure()
            self.assertEqual(exposure.pending_orders, 0)
            self.assertEqual(exposure.gross_reservation_usd, Decimal("10"))
            self.assertEqual(runtime._inventory_locked_pool_ids(), {"pool-held\x1eY"})
            status = json.loads((runtime.directory / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["inventory_catalog_holds"], ["Y"])

        await self._prepare_fixture(
            manual_flat_repair=False,
            outside_inventory=True,
            scoped_outside_inventory=True,
            after_prepare=verify,
        )

    async def test_prepared_account_events_record_subclock_gaps_and_counters(self) -> None:
        def exercise(runtime):
            # Exercise the replacement account created by real prepare(), not
            # the constructor callback or a direct call to _account_event.
            runtime._sticky_pool_ids = {runtime.universe[0].owner_key}
            clock = time.monotonic()
            frames = {}

            def opened(order_id, outcome):
                intent = runtime.ledger.prepare(
                    QuoteLeg("X", outcome, Side.BUY, Decimal(".46"), Decimal("10")),
                    OrderPurpose.ENTRY,
                )
                runtime.ledger.mark_sent(intent.client_order_id)
                frame = {"e": "orderUpdate", "i": order_id, "c": intent.client_order_id,
                         "s": "X", "S": "BUY", "O": outcome.value, "p": ".46",
                         "q": "10", "z": "10", "X": "NEW"}
                frames[order_id] = frame
                runtime.account.apply(frame)

            with patch.object(runtime.log, "event", wraps=runtime.log.event) as logged:
                with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock):
                    opened("yes-1", Outcome.YES)
                    opened("no-1", Outcome.NO)
                with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock + .05):
                    runtime.account.apply({**frames["yes-1"], "X": "CANCELED"})
                with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock + .07):
                    runtime.account.apply({**frames["no-1"], "X": "CANCELED"})
                with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock + .10):
                    opened("yes-2", Outcome.YES)
                with patch("gemini_bot.orchestrator.time.monotonic", return_value=clock + .12):
                    opened("no-2", Outcome.NO)
                self.assertEqual(sum(c.args[1] == "order_transition" for c in logged.call_args_list), 6)

            view = runtime._continuity_view()
            self.assertIn("X", view["symbols"])
            events = view["symbols"]["X"]
            self.assertEqual(events["order_new_events"], 4)
            self.assertEqual(events["order_cancel_events"], 2)
            for group in ("symbols", "two_sided_symbols"):
                observation = view[group]["X"]
                self.assertEqual(observation["gap_count"], 1)
                self.assertGreater(observation["last_gap_seconds"], 0)
                self.assertLess(observation["last_gap_seconds"], .25)
                self.assertTrue(observation["qualifying_at_last_observation"])
            self.assertEqual(runtime.account.reducer.pending_order_count(), 2)
            self.assertEqual(runtime.account.fill_version, 0)
            self.assertEqual(runtime.gate.state, EngineState.READY)
            self.assertFalse(runtime._activation_consumed)

        await self._prepare_fixture(manual_flat_repair=False, after_prepare=exercise)

    async def _prepare_fixture(
        self, *, manual_flat_repair: bool, after_prepare=None,
        outside_inventory: bool = False,
        scoped_outside_inventory: bool = False,
        missing_event: bool = False,
        all_events_missing: bool = False,
    ) -> None:
        class Rest:
            async def terms_status(self): return {"hasAcceptedLatest": True}
            async def reward_config(self): return {"enabled": True}
            async def positions(self):
                if not outside_inventory:
                    return []
                return [{
                    "symbol": "Y", "outcome": "YES", "totalQuantity": "10",
                    "avgPrice": ".40", "marketValue": "4",
                }]
            async def active_orders(self): return []
            async def order_history(self): return []
            async def reward_events(self, *, ceiling: int = 3000):
                return ([{"event_ticker": "HELD", "daily_pool_usd": "100"}] if missing_event else []) + [{
                    "event_ticker": "E", "pool_id": 1, "daily_pool_usd": "100",
                    "ends_at": "2027-01-01T00:00:00Z",
                }]
            async def event(self, event_ticker: str):
                if event_ticker == "HELD" or all_events_missing:
                    raise ApiError(404, "unavailable")
                return {"event": {"status": "active", "contracts": [{
                    "instrumentSymbol": "X", "status": "active", "marketState": "open",
                    "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                    "quoteAssetPrecision": 2,
                    "expiryDate": "2027-01-01T00:00:00Z",
                    "prices": {"bestBid": ".48", "bestAsk": ".52"},
                }]}}

        class Public:
            def __init__(self):
                self.books = {}
                self.last_message_at = None
            async def connect(self): return None
            async def subscribe(self, symbols):
                self.last_message_at = datetime.now(timezone.utc)
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
                yield {
                    "e": "positionReport",
                    "P": ([{"t": "ec", "s": "Y", "a": [{"t": "position", "v": "10"}]}]
                          if scoped_outside_inventory else []),
                    "u": 1,
                }
                while True:
                    await asyncio.sleep(3600)
                    yield {}
            async def close(self): return None

        with tempfile.TemporaryDirectory() as directory:
            strategy, risk = load_policy()
            # Synthetic preparation fixture targets ordinary one-tick startup.
            strategy = replace(strategy, queue_pilot=None)
            runtime = ProductionOrchestrator(
                LiveConfig("account-example", "secret", "", True, strategy, risk),
                Path(directory), read_only=manual_flat_repair,
            )
            private = Private()
            runtime.rest = Rest()
            runtime.control_directory = Path(directory) / "isolated-control"
            runtime.lock = WindowsOwnerLock(runtime.control_directory / "mutation-owner.lock")
            runtime.public = Public()
            runtime.private = private
            try:
                if manual_flat_repair:
                    prior = SQLiteJournal(runtime.control_directory / "production.sqlite")
                    try:
                        prior.set_runtime_state("expected_confirmed_usd", "96", updated_at=NOW)
                        prior.set_runtime_state("expected_positions", '{"X":"10"}', updated_at=NOW)
                    finally:
                        prior.close()
                elif scoped_outside_inventory:
                    prior = SQLiteJournal(runtime.control_directory / "production.sqlite")
                    try:
                        prior.set_runtime_state("expected_confirmed_usd", "100", updated_at=NOW)
                        prior.set_runtime_state("expected_positions", '{"Y":"10"}', updated_at=NOW)
                        prior.set_runtime_state(
                            "sticky_markets_v1", '{"pool-held":["HELD","Y"]}', updated_at=NOW,
                        )
                        prior.persist_deadline(
                            "Y", "YES", NOW - timedelta(minutes=10),
                            NOW - timedelta(minutes=5), Decimal("10"),
                        )
                    finally:
                        prior.close()
                result = await runtime.prepare(timeout=2, manual_flat_repair=manual_flat_repair)
                if manual_flat_repair:
                    self.assertFalse(runtime.account.equity_classification_complete)
                    self.assertFalse(runtime._last_snapshot.positions)
                    self.assertFalse(runtime._last_snapshot.active_orders)
                self.assertEqual(runtime.gate.state, EngineState.READY)
                self.assertEqual(result.ranked_count, 1)
                self.assertEqual(private.mutations, 0)
                status = json.loads((Path(directory) / "status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["phase"], "ready_paused")
                self.assertFalse(status["activated"])
                if after_prepare is not None:
                    after_prepare(runtime)
                    self.assertEqual(private.mutations, 0)
            finally:
                await runtime.close()


class OrchestratorWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_pair_places_yes_then_ack_then_no(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            strategy, risk = load_policy()
            # Exercise basic legacy leg ordering independently of queue policy.
            strategy = replace(strategy, queue_pilot=None, quote_offset_ticks=1)
            runtime = ProductionOrchestrator(
                LiveConfig("account-example", "secret", "", True, strategy, risk),
                Path(directory), public_shard_count=1,
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
            runtime.account.confirmed_usd = Decimal("100")
            runtime.account.expected_confirmed_usd = Decimal("100")
            runtime.account.expected_positions = {}
            runtime.account.last_balance_at = time.monotonic()
            runtime.account.last_position_at = time.monotonic()
            runtime._expiry["X"] = NOW + timedelta(hours=2)
            book = LocalBook("X")
            book.apply_snapshot([(Decimal(".48"), Decimal("100"))], [(Decimal(".52"), Decimal("100"))], 1, 1, NOW)
            runtime.public.books["X"] = book
            runtime.universe = [RewardedContract(
                "E", RewardPool("pool", Decimal("100")),
                ContractRules("X", Decimal(".01"), Decimal("1"), Decimal("1")),
                (NOW + timedelta(hours=2)).isoformat(),
            )]
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
                now=NOW, quantity=strategy.quote_quantity, offset_ticks=strategy.quote_offset_ticks,
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
