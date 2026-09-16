"""Offline regression coverage. No exchange requests or process launches."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as fixtures
from test_review_regressions import candidate
from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.account_state import AccountReducer
from gemini_bot.catalog import RewardedContract
from gemini_bot.domain import ContractRules, EngineState, RewardPool
from gemini_bot.execution import IntentLedger, OrderPurpose
from gemini_bot.operational_logging import ErrorClass, OperationalLogger, configure_logging
from gemini_bot.rest_client import ApiError
from gemini_bot.live_ws import EntryStateChangedBeforeSend
from gemini_bot.reward_ranking import RankInput, rank_candidate
from gemini_bot.storage import SQLiteJournal
from gemini_bot.uptime import estimated_reward_window, sample_and_load_progress

NOW = datetime(2026, 9, 15, 16, tzinfo=timezone.utc)


class WindowHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteJournal(Path(self.tmp.name) / "test.sqlite")
        self.contract = RewardedContract(
            "E", RewardPool("p", D("100")), ContractRules("X", D(".01"), D("1"), D("1")),
            (NOW + timedelta(minutes=60)).isoformat(), trade_starts_at=NOW.isoformat(),
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def progress(self, contract=None, now=NOW):
        return sample_and_load_progress(self.store, [contract or self.contract], AccountReducer(), now=now)["p"]

    def test_exit_reduces_numerator_not_venue_denominator(self):
        p = self.progress(now=NOW + timedelta(minutes=25))
        self.assertEqual(p.window_end, NOW + timedelta(minutes=60))
        self.assertEqual(p.quote_cutoff, NOW + timedelta(minutes=55))
        self.assertEqual(p.eligible_snapshots + p.remaining_venue_snapshots_estimate, 60)
        self.assertEqual(p.remaining_snapshots_estimate, 30)
        self.assertEqual(p.best_possible_ratio, D(".5"))
        self.assertLess(self.progress(now=NOW + timedelta(minutes=26)).best_possible_ratio, D(".5"))

    def test_sports_cutoff_never_shortens_venue_window(self):
        c = replace(self.contract, event_category="sports", event_starts_at=(NOW + timedelta(minutes=40)).isoformat())
        p = self.progress(c)
        self.assertEqual(p.window_end, NOW + timedelta(minutes=60))
        self.assertEqual(p.remaining_snapshots_estimate, 20)
        self.assertEqual(p.best_possible_ratio, D(20) / 60)

    def test_unknown_sports_start_has_no_safe_quote_opportunities(self):
        self.assertEqual(self.progress(replace(self.contract, event_category="sports")).best_possible_ratio, 0)

    def test_inverted_window_cannot_become_unknown_and_admissible(self):
        p = self.progress(replace(self.contract, expiry=(NOW - timedelta(minutes=1)).isoformat()))
        self.assertFalse(p.window_valid)
        ranked = rank_candidate(RankInput(self.contract.pool, self.contract.rules, fixtures.book().top(), p))
        self.assertFalse(ranked.eligible_now)
        self.assertIn("invalid estimated reward window", ranked.reasons)

    def test_restart_does_not_forgive_missed_minutes(self):
        self.progress()
        p = self.progress(now=NOW + timedelta(minutes=35))
        self.assertEqual(p.eligible_snapshots, 36)
        self.assertEqual(p.qualifying_snapshots, 0)
        self.assertEqual(p.best_possible_ratio, D(20) / 60)

    def test_inactive_event_still_keeps_elapsed_denominator(self):
        c = replace(self.contract, pool=replace(self.contract.pool, active=False))
        p = self.progress(c, NOW + timedelta(minutes=35))
        self.assertEqual(p.eligible_snapshots, 36)
        self.assertLess(p.best_possible_ratio, D(".5"))

    def test_future_start_has_no_elapsed_samples(self):
        p = self.progress(now=NOW - timedelta(minutes=10))
        self.assertEqual(p.eligible_snapshots, 0)
        self.assertEqual(p.remaining_venue_snapshots_estimate, 60)
        self.assertEqual(p.remaining_snapshots_estimate, 55)

    def test_dst_days_preserve_calendar_duration(self):
        for month, day, hours in ((3, 8, 23), (11, 1, 25)):
            now = datetime(2026, month, day, 12, tzinfo=timezone.utc)
            c = replace(self.contract, expiry="", trade_starts_at="")
            start, end, _ = estimated_reward_window(c, now)
            self.assertEqual((end - start).total_seconds(), hours * 3600)


class RuntimeHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.runtime = self.fixture.runtime

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_second_leg_full_or_partial_fill_cancels_only_its_symbol(self):
        for filled in ("3", "10"):
            with self.subTest(filled=filled):
                self.runtime.account.reducer.orders.clear()
                self.runtime.account.blocked_entries.clear()
                self.runtime.account.expected_confirmed_usd = self.runtime.account.confirmed_usd
                self.runtime.account.expected_positions = {}
                async def place(leg):
                    intent = self.runtime.ledger.prepare(leg, OrderPurpose.ENTRY)
                    self.runtime.ledger.mark_sent(intent.client_order_id)
                    frame = {
                        "e": "orderUpdate", "i": leg.outcome.value + filled, "c": intent.client_order_id,
                        "s": "X", "S": "BUY", "O": leg.outcome.value, "q": "10",
                        "p": str(leg.price), "z": "10", "X": "OPEN",
                    }
                    if leg.outcome.value == "NO":
                        frame.update(z=str(10 - D(filled)), Z=filled, L=str(leg.price), n="0",
                                     t="fill-" + filled, X="FILLED" if filled == "10" else "PARTIALLY_FILLED")
                    self.runtime.account.apply(frame)
                    return intent.client_order_id
                self.runtime.private.place_maker = AsyncMock(side_effect=place)
                self.runtime._cancel_symbol_entries = AsyncMock()
                await self.runtime._quote_pair(candidate())
                self.assertEqual(self.runtime.private.place_maker.await_count, 2)
                self.runtime._cancel_symbol_entries.assert_awaited_once_with("X")
                self.assertTrue(self.runtime.gate.may_submit)
                self.assertIsNotNone(self.runtime.journal.get_deadline("X", "NO"))

    async def test_fill_write_fault_rolls_back_all_durable_and_expected_state(self):
        account = self.runtime.account
        intent = self.runtime.ledger.prepare(candidate().quote.yes, OrderPurpose.ENTRY)
        self.runtime.ledger.mark_sent(intent.client_order_id)
        frame = {"e": "orderUpdate", "i": "fault-order", "c": intent.client_order_id,
                 "s": "X", "S": "BUY", "O": "YES", "q": "10", "z": "0", "Z": "10",
                 "p": ".46", "L": ".46", "n": "0", "t": "atomic-fill", "X": "FILLED"}
        cash_before = account.expected_confirmed_usd
        with patch.object(self.runtime.journal, "persist_deadline", side_effect=OSError("injected storage failure")):
            with self.assertRaises(OSError):
                account.apply(frame)
        self.assertEqual(self.runtime.journal.connection.execute("SELECT COUNT(*) FROM execution_fill").fetchone()[0], 0)
        self.assertEqual(account.expected_confirmed_usd, cash_before)
        self.assertEqual(account.expected_positions, {})
        restored = AccountRuntime(IntentLedger(self.runtime.journal), self.runtime.journal)
        self.assertEqual(restored.expected_confirmed_usd, cash_before)
        self.assertNotIn("fault-order", account.reducer.orders)
        account.apply(frame)
        account.apply(frame)
        self.assertEqual(account.expected_positions, {"X": D("10")})
        self.assertEqual(account.expected_confirmed_usd, cash_before - D("4.6"))
        self.assertEqual(account.fill_version, 1)
        self.assertIsNotNone(self.runtime.journal.get_deadline("X", "YES"))

    async def test_startup_cash_mismatch_still_fails_closed(self):
        self.runtime.gate.state = EngineState.READY
        self.runtime.account.expected_confirmed_usd = D("99")
        self.runtime.rest.positions = AsyncMock(return_value=[])
        self.runtime.rest.active_orders = AsyncMock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, "cannot be classified"):
            await self.runtime._risk_mark()

    async def test_fill_during_rest_mark_defers_to_ws_safety_clock(self):
        async def positions():
            self.runtime.account.fill_version += 1
            return []
        self.runtime.rest.positions = AsyncMock(side_effect=positions)
        self.runtime.rest.active_orders = AsyncMock(return_value=[])
        self.assertEqual(await self.runtime._risk_mark(), 30)
        self.assertIsNone(self.runtime._last_live_mark)

    async def test_manual_flat_flag_never_allowed_on_live_runtime(self):
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            await self.runtime.prepare(manual_flat_repair=True)

    async def test_transient_read_recovers_in_same_process_and_preserves_policy(self):
        for expected in (5, 10, 20, 40, 60, 60):
            self.assertEqual(self.runtime._defer_read_failure("account_audit", ApiError(503, "unavailable")), expected)
        self.assertTrue(self.runtime.gate.may_submit)
        with self.assertRaises(EntryStateChangedBeforeSend):
            self.runtime._validate_send(self.fixture.payload())
        self.runtime.private.place_maker = AsyncMock()
        await self.runtime._quote_pair(candidate())
        self.runtime.private.place_maker.assert_not_awaited()
        self.runtime.account.reducer.positions = {"X": D("10")}
        self.runtime.account.expected_positions = {"X": D("10")}
        self.runtime._validate_send({"params": {
            "symbol": "X", "side": "SELL", "quantity": "10", "eventOutcome": "YES",
        }})
        self.runtime.account.reducer.positions = {}
        self.runtime.account.expected_positions = {}
        self.runtime._complete_read_recovery("account_audit")
        self.runtime._validate_send(self.fixture.payload())
        self.assertEqual(self.runtime._dynamic_pending_cap, 30)

    async def test_recovery_domains_do_not_clear_each_others_entry_hold(self):
        for component in ("account_audit", "reward_metadata"):
            self.runtime._defer_read_failure(component, ApiError(429, "rate_limit"))
        self.runtime._complete_read_recovery("reward_metadata")
        self.assertEqual(set(self.runtime._read_recovery_attempts), {"account_audit"})

    async def test_response_shape_auth_and_stale_ws_cannot_enter_read_recovery(self):
        for status in (200, 400, 401, 403, 404):
            with self.subTest(status=status), self.assertRaises(ApiError):
                self.runtime._defer_read_failure("account_audit", ApiError(status, "invalid"))
        self.runtime.account.last_position_at = 0
        with self.assertRaises(ApiError):
            self.runtime._defer_read_failure("account_audit", ApiError(503, "unavailable"))
        self.assertFalse(self.runtime._read_recovery_attempts)

    async def test_pending_cap_uses_fresh_ws_without_redundant_rest(self):
        self.runtime.rest.active_orders = AsyncMock(side_effect=AssertionError("unnecessary REST"))
        await self.runtime._enforce_pending_cap(30)
        self.runtime.rest.active_orders.assert_not_awaited()

    async def test_reward_metadata_refresh_changes_budget_without_rotating_symbol(self):
        old = self.runtime.universe[0]
        fresh = replace(old, pool=replace(old.pool, daily_budget_usd=D("250"), qualifying_maker_count=7),
                        reward_ends_at=(fixtures.NOW + timedelta(minutes=30)).isoformat())
        self.runtime.rest.reward_config = AsyncMock(return_value={"enabled": True})
        self.runtime.rest.reward_events = AsyncMock(return_value=[{"event_ticker": "E", "daily_pool_usd": "250"}])
        self.runtime.rest.event = AsyncMock(return_value={})
        with patch("gemini_bot.orchestrator.join_reward_contracts", return_value=[fresh]):
            self.assertTrue(await self.runtime._reward_pools_still_active())
        self.assertEqual(self.runtime.universe, [fresh])
        self.assertEqual(self.runtime._permitted_symbols, {"X"})
        self.assertEqual(self.runtime._uptime[fresh.pool.pool_id].window_end, fixtures.NOW + timedelta(minutes=30))

    async def test_status_lifecycle_and_heartbeat_are_logged_without_per_tick_spam(self):
        # Monotonic time patch is synchronous; never alters the event-loop clock.
        with patch("gemini_bot.orchestrator.time.monotonic", return_value=100):
            self.runtime._write_status("quoting", True, "heartbeat")
            self.runtime._write_status("quoting", True, "heartbeat")
        with patch("gemini_bot.orchestrator.time.monotonic", return_value=161):
            self.runtime._write_status("quoting", True, "heartbeat")
        await self.runtime.close()
        rows = [json.loads(line) for line in (self.runtime.directory / "logs/gemini-bot.jsonl").read_text().splitlines()]
        events = [row["event"] for row in rows]
        self.assertEqual(events.count("runtime_status"), 1)
        self.assertEqual(events.count("runtime_heartbeat"), 1)
        self.assertIn("runner_created", events)
        self.assertIn("runner_closing", events)
        self.assertTrue(all("timestamp" in row and "process_id" in row for row in rows))


class LoggingHardeningTests(unittest.TestCase):
    def test_exception_redaction_and_bounded_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = configure_logging(Path(directory), secrets=("SECRET-EXAMPLE-123",), max_bytes=700, backups=2)
            log = OperationalLogger(runtime)
            for _ in range(12):
                log.event(logging.INFO, "heartbeat", api_secret="SECRET-EXAMPLE-123")
            try:
                raise OSError("SECRET-EXAMPLE-123 X-GEMINI-SIGNATURE=abcdef account-123456789")
            except OSError as exc:
                log.exception("fault", ErrorClass.STORAGE, exc)
            runtime.close()
            files = list(Path(directory).glob("gemini-bot.jsonl*"))
            self.assertGreater(len(files), 1)
            self.assertLessEqual(len(files), 3)
            saw_exception = False
            for file in files:
                text = file.read_text()
                self.assertNotIn("SECRET-EXAMPLE-123", text)
                self.assertNotIn("abcdef", text)
                for line in text.splitlines():
                    saw_exception |= "exception" in json.loads(line)
            self.assertTrue(saw_exception)

    def test_windows_health_reader_rejects_stale_running_file(self):
        root = Path(__file__).resolve().parents[1]
        (root / "data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="test-health-", dir=root / "data") as directory:
            path = Path(directory)
            (path / "supervisor-status.json").write_text(json.dumps({"state": "RUNNING", "updated_at": "2020-01-01T00:00:00Z"}))
            result = subprocess.run(["powershell", "-NoProfile", "-File", str(root / "scripts/check_runtime.ps1"),
                                     "-RuntimeDirectory", str(path.relative_to(root))], capture_output=True, text=True, check=True)
            row = json.loads(result.stdout)
            self.assertEqual(row["observation"], "ATTENTION_REQUIRED_OR_STOPPED")
            self.assertFalse(row["venue_state_verified"])
            self.assertFalse(row["supervisor_fresh"])

    def test_live_launcher_uses_unlimited_on_demand_scheduled_task(self):
        root = Path(__file__).resolve().parents[1]
        (root / "data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="test-launcher-", dir=root / "data") as directory:
            path = Path(directory)
            environment = path / "offline.env"
            permit = path / "offline-permit.json"
            runtime_argument = str(path.relative_to(root))
            environment_argument = str(environment.relative_to(root))
            permit_argument = str(permit.relative_to(root))
            environment.write_text("OFFLINE_ONLY=1\n", encoding="utf-8")
            permit.write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-File",
                    str(root / "tests/persistent_launcher_harness.ps1"),
                    "-RuntimeDirectory", runtime_argument,
                    "-EnvironmentFile", environment_argument,
                    "-PermitFile", permit_argument,
                ],
                capture_output=True, text=True, check=True,
            )
            row = json.loads(result.stdout)
            self.assertTrue(row["registered"])
            self.assertTrue(row["started"])
            self.assertRegex(row["task_name"], r"^GeminiBotProduction-[0-9A-F]{12}$")
            self.assertTrue(row["action"]["Execute"].lower().endswith("windows\\system32\\windowspowershell\\v1.0\\powershell.exe"))
            self.assertIn("supervise_production.ps1", row["action"]["Argument"])
            self.assertIn(str(path), row["action"]["Argument"])
            self.assertEqual(row["settings"]["ExecutionTimeLimit"], "00:00:00")
            self.assertEqual(row["settings"]["MultipleInstances"], "IgnoreNew")
            self.assertTrue(row["settings"]["AllowStartIfOnBatteries"])
            self.assertTrue(row["settings"]["DontStopIfGoingOnBatteries"])
            self.assertEqual(row["principal"]["LogonType"], "Interactive")
            self.assertEqual(row["principal"]["RunLevel"], "Limited")
            self.assertEqual(row["launcher_state"]["launch_kind"], "on_demand_scheduled_task")
            self.assertFalse(row["launcher_state"]["boot_trigger"])
            self.assertEqual(row["process_id"], row["harness_pid"])
