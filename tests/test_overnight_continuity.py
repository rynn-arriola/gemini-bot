"""Offline midnight, persistence and ownership tests; no venue connections."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as fixtures
import test_production_components as components
import test_core as core
from gemini_bot.activation import load_activation_permit, consume_activation_permit, finish_supervised_activation
from gemini_bot.config import DEFAULT_POLICY_PATH
from gemini_bot.discovery import RewardUniverseLoader
from gemini_bot.domain import EngineState
from gemini_bot.live_ws import EntryStateChangedBeforeSend
from gemini_bot.market_state import LocalBook
from gemini_bot.profitability import ProfitabilityEvidence
from gemini_bot.reward_ranking import rank_unique_pools, RankInput
from gemini_bot.storage import SQLiteJournal

MIDNIGHT = datetime(2026, 9, 16, 4, tzinfo=timezone.utc)


class OvernightPermitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "permit.json"
        self.store = SQLiteJournal(Path(self.tmp.name) / "journal.sqlite")
        components.ActivationTests()._write(self.path)
        self.value = json.loads(self.path.read_text())
        self.value.pop("symbols")
        self.value.update(permit_version=6, universe_scope="reward_catalog", supervised_restart_limit=2,
                          approved_at=(MIDNIGHT - timedelta(hours=3)).isoformat(),
                          expires_at=(MIDNIGHT + timedelta(hours=20)).isoformat(),
                          uptime_override_until=MIDNIGHT.isoformat())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def load(self, now):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        return load_activation_permit(self.path, policy_path=DEFAULT_POLICY_PATH, store=self.store,
                                      now=now, account_fingerprint="fake-account")

    def test_v6_can_resume_after_override_ends_but_before_session_expiry(self):
        permit = self.load(MIDNIGHT - timedelta(minutes=1))
        consume_activation_permit(permit, self.store, now=MIDNIGHT - timedelta(minutes=1), supervisor_session_id="s")
        finish_supervised_activation(permit, self.store, supervisor_session_id="s", outcome="recoverable_flat", now=MIDNIGHT)
        permit = self.load(MIDNIGHT + timedelta(minutes=1))
        consume_activation_permit(permit, self.store, now=MIDNIGHT + timedelta(minutes=1), supervisor_session_id="s")
        self.assertEqual(json.loads(self.store.get_runtime_state("consumed_permit:approval-1"))["activations"], 2)

    def test_v6_without_override_is_valid_normal_mode(self):
        self.value["uptime_override_until"] = None
        self.assertIsNone(self.load(MIDNIGHT).uptime_override_until)

    def test_legacy_expired_override_still_refused(self):
        self.value["permit_version"] = 5
        with self.assertRaises(ValueError):
            self.load(MIDNIGHT)

    def test_session_expiry_and_twenty_four_hour_limit_still_enforced(self):
        with self.assertRaises(ValueError):
            self.load(MIDNIGHT + timedelta(hours=20))
        self.value["expires_at"] = (MIDNIGHT + timedelta(hours=22)).isoformat()
        with self.assertRaisesRegex(ValueError, "24 hours"):
            self.load(MIDNIGHT)

    def test_override_cannot_extend_beyond_permit(self):
        self.value["uptime_override_until"] = (MIDNIGHT + timedelta(hours=21)).isoformat()
        with self.assertRaises(ValueError):
            self.load(MIDNIGHT)

    def test_v6_keeps_cross_session_and_nonflat_refusal(self):
        permit = self.load(MIDNIGHT)
        consume_activation_permit(permit, self.store, now=MIDNIGHT, supervisor_session_id="s")
        with self.assertRaises(ValueError):
            self.load(MIDNIGHT + timedelta(seconds=1))
        finish_supervised_activation(permit, self.store, supervisor_session_id="s", outcome="recoverable_flat", now=MIDNIGHT)
        permit = self.load(MIDNIGHT + timedelta(seconds=1))
        with self.assertRaises(ValueError):
            consume_activation_permit(permit, self.store, supervisor_session_id="other")


class MidnightRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continue_after_override = True
        self.r._commissioning_ignore_uptime_proxy = True
        self.r._uptime_override_until = MIDNIGHT
        self.r._permit_expires_at = MIDNIGHT + timedelta(hours=20)
        self.r.universe = [replace(self.r.universe[0], expiry=(MIDNIGHT + timedelta(days=2)).isoformat())]
        self.r._expiry["X"] = MIDNIGHT + timedelta(days=2)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def freshness(self, now):
        self.r.public.note_message(now)

    async def test_override_ends_exactly_at_midnight_without_draining(self):
        c = replace(fixtures.candidate(), eligible_now=False, uptime_feasible=False,
                    reasons=("50% uptime is no longer reachable in the estimated window",))
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT - timedelta(microseconds=1)):
            self.assertTrue(self.r._candidate_actionable(c))
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT):
            self.assertTrue(self.r._candidate_actionable(c))
            self.assertFalse(self.r._recovery_stop_requested())
            self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_new_day_recomputes_eligibility_and_preserves_previous_day_rows(self):
        before = MIDNIGHT - timedelta(seconds=1)
        with patch("gemini_bot.orchestrator._utc_now", return_value=before):
            self.freshness(before)
            self.r._advance_reward_epoch(before)
            self.assertFalse(self.r.ranked[0].uptime_feasible)
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT):
            self.freshness(MIDNIGHT)
            self.assertTrue(self.r._advance_reward_epoch(MIDNIGHT))
            self.assertTrue(self.r.ranked[0].uptime_feasible)
            self.assertFalse(self.r._override_active())
            self.assertFalse(self.r._advance_reward_epoch(MIDNIGHT))
        rows = self.r.journal.connection.execute("SELECT window_start FROM reward_uptime_v2").fetchall()
        self.assertEqual(len(rows), 2)

    async def test_presend_blocks_previous_day_candidate_until_rerank(self):
        before = MIDNIGHT - timedelta(seconds=1)
        self.r._reward_epoch = self.r._current_reward_epoch(before)
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT):
            self.freshness(MIDNIGHT)
            with self.assertRaisesRegex(EntryStateChangedBeforeSend, "fresh ranking"):
                self.r._validate_send(self.fixture.payload())

    async def test_presend_accepts_only_uptime_proxy_after_override(self):
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT):
            self.freshness(MIDNIGHT)
            self.r._reward_epoch = self.r._current_reward_epoch(MIDNIGHT)
            self.r.ranked = [replace(fixtures.candidate(), eligible_now=False,
                                     reasons=("50% uptime is no longer reachable in the estimated window",))]
            self.r._validate_send(self.fixture.payload())

    async def test_midnight_safety_clock_keeps_session_running_but_expiry_drains(self):
        async def end_iteration(_):
            self.assertEqual(self.r.gate.state, EngineState.QUOTING)
            self.r.gate.state = EngineState.READY
        self.r._exit_due_positions = AsyncMock()
        self.r._mark_live_risk = lambda: None
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT), patch("gemini_bot.orchestrator.asyncio.sleep", side_effect=end_iteration):
            self.freshness(MIDNIGHT)
            await self.r._live_safety_clock()
        self.r.gate.state = EngineState.QUOTING
        with patch("gemini_bot.orchestrator._utc_now", return_value=self.r._permit_expires_at):
            await self.r._live_safety_clock()
        self.assertEqual(self.r.gate.state, EngineState.DRAINING)

    async def test_sticky_identity_survives_reload_without_touching_uptime(self):
        c = replace(fixtures.candidate(), event_ticker="E")
        self.r._save_sticky_markets([c], set())
        self.r._sticky_markets = {}
        self.r._sticky_pool_ids.clear()
        self.r._load_sticky_markets()
        self.assertEqual(self.r._sticky_markets, {c.pool_id: (c.event_ticker, c.symbol)})

    async def test_malformed_cohort_fails_closed(self):
        self.r.journal.set_runtime_state("sticky_markets_v1", '{"p": [1, 2]}', updated_at=MIDNIGHT)
        with self.assertRaises(RuntimeError):
            self.r._load_sticky_markets()

    async def test_healthy_orders_stay_resting_across_midnight(self):
        for leg in (fixtures.candidate().quote.yes, fixtures.candidate().quote.no):
            intent = self.r.ledger.prepare(leg, fixtures.OrderPurpose.ENTRY)
            self.r.ledger.mark_sent(intent.client_order_id)
            self.r.account.apply({"e": "orderUpdate", "i": leg.outcome.value, "c": intent.client_order_id,
                                  "s": "X", "S": "BUY", "O": leg.outcome.value,
                                  "q": "10", "z": "10", "p": str(leg.price), "X": "NEW"})
        self.r._cancel_symbol_entries = AsyncMock()
        self.r._quote_pair_locked = AsyncMock()
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT):
            self.freshness(MIDNIGHT)
            self.r._advance_reward_epoch(MIDNIGHT)
            await self.r._maintain_candidate(self.r.ranked[0])
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.r._quote_pair_locked.assert_not_awaited()
        self.assertEqual(self.r.account.reducer.pending_order_count(), 2)

    async def test_legacy_override_expiry_still_drains(self):
        self.r._continue_after_override = False
        with patch("gemini_bot.orchestrator._utc_now", return_value=MIDNIGHT):
            await self.r._live_safety_clock()
        self.assertEqual(self.r.gate.state, EngineState.DRAINING)

    async def test_midnight_rebuild_does_not_erase_risk_or_extend_inventory_deadline(self):
        deadline = MIDNIGHT + timedelta(seconds=30)
        self.r.journal.persist_deadline("X", "YES", MIDNIGHT - timedelta(minutes=4), deadline, D("10"))
        policy = fixtures.RiskWindowPolicy(D("30"))
        start, end = policy.bounds(MIDNIGHT - timedelta(seconds=1))
        self.r.journal.upsert_realized_risk_window(window_start=start, window_end=end,
            realized_pnl=D("-5"), loss_high_water=D("5"), pending_order_cap=30,
            classification_complete=True, updated_at=MIDNIGHT - timedelta(seconds=1))
        before = self.r.journal.load_realized_risk_window(start)
        self.freshness(MIDNIGHT)
        self.r._advance_reward_epoch(MIDNIGHT)
        self.assertEqual(self.r.journal.load_realized_risk_window(start), before)
        self.assertEqual(self.r.journal.get_deadline("X", "YES"), deadline)


class DiscoveryContinuityTests(unittest.IsolatedAsyncioTestCase):
    def event(self, symbol, expiry=None):
        return {"status": "active", "contracts": [{
            "instrumentSymbol": symbol, "status": "active", "marketState": "open",
            "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
            "expiryDate": (expiry or MIDNIGHT + timedelta(hours=8)).isoformat(),
            "prices": {"bestBid": ".48", "bestAsk": ".52"},
        }]}

    async def test_restart_prefers_existing_event_and_contract_over_better_competitor(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [
            {"event_ticker": "OLD", "pool_id": 1, "daily_pool_usd": "100", "qualifying_maker_count": 10},
            {"event_ticker": "NEW", "pool_id": 1, "daily_pool_usd": "100", "qualifying_maker_count": 1},
        ]
        old = self.event("X")
        old["contracts"].append({**self.event("Y")["contracts"][0], "prices": {"bestBid": ".49", "bestAsk": ".50"}})
        rest.event.side_effect = lambda ticker: old if ticker == "OLD" else self.event("Z")
        items = await RewardUniverseLoader(rest).load(preferred={"shared:1": ("OLD", "X")}, now=MIDNIGHT)
        self.assertEqual([(c.event_ticker, c.rules.symbol) for c in items], [("OLD", "X")])

    async def test_expired_incumbent_replaced_by_current_recurring_event(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [
            {"event_ticker": "OLD", "pool_id": 1, "daily_pool_usd": "100"},
            {"event_ticker": "NEW", "pool_id": 1, "daily_pool_usd": "100"},
        ]
        rest.event.side_effect = lambda ticker: self.event("X", MIDNIGHT) if ticker == "OLD" else self.event("Y")
        items = await RewardUniverseLoader(rest).load(preferred={"shared:1": ("OLD", "X")}, now=MIDNIGHT)
        self.assertEqual(items[0].rules.symbol, "Y")

    async def test_sticky_symbol_is_not_replaced_by_higher_profit_sibling(self):
        pool = core.RewardPool("p", D("100"))
        a = RankInput(pool, core.rules("A"), core.book("A"), profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("2")))
        b = RankInput(pool, core.rules("B"), core.book("B"))
        self.assertEqual(rank_unique_pools([a, b], now=core.NOW)[0].symbol, "B")
        self.assertEqual(rank_unique_pools([a, b], now=core.NOW, preferred_symbols={"p": "A"})[0].symbol, "A")
        unsafe = replace(a, profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("1000")))
        self.assertEqual(rank_unique_pools([unsafe, b], now=core.NOW, preferred_symbols={"p": "A"})[0].symbol, "B")


class RolloverOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continue_after_override = True
        self.r._catalog_scope = True
        self.old = replace(self.r.universe[0], pool=replace(self.r.universe[0].pool, active=False))
        self.fresh = replace(self.r.universe[0], event_ticker="NEW", rules=replace(self.r.universe[0].rules, symbol="Y"))
        self.r.universe = [self.old]
        self.r.account.blocked_entries.add("X")
        self.r.rest.positions = AsyncMock(return_value=[])
        self.r.rest.active_orders = AsyncMock(return_value=[])
        self.r.public.unsubscribe = AsyncMock()
        self.r.public.subscribe = AsyncMock()
        self.r._cancel_symbol_entries = AsyncMock()
        self.loader = patch("gemini_bot.orchestrator.RewardUniverseLoader.load", new=AsyncMock(return_value=[self.fresh]))
        self.loader.start()

    async def asyncTearDown(self):
        self.loader.stop()
        await self.fixture.asyncTearDown()

    async def test_flat_terminal_owner_replaced_with_unsynchronized_new_book(self):
        await self.r._rollover_flat_markets([])
        self.assertEqual([c.rules.symbol for c in self.r.universe], ["Y"])
        self.r.public.unsubscribe.assert_awaited_once_with(("X",))
        self.r.public.subscribe.assert_awaited_once_with(("Y",))
        self.assertFalse(self.r.public.books["Y"].synchronized)
        self.assertEqual(self.r._permitted_symbols, {"Y"})

    async def test_position_owner_and_deadline_cannot_be_replaced(self):
        self.r.account.reducer.positions["X"] = D("10")
        self.r.journal.persist_deadline("X", "YES", fixtures.NOW, fixtures.NOW + timedelta(minutes=2), D("10"))
        await self.r._rollover_flat_markets([])
        self.assertEqual([c.rules.symbol for c in self.r.universe], ["X"])
        self.r.public.unsubscribe.assert_not_awaited()
        self.assertIsNotNone(self.r.journal.get_deadline("X", "YES"))

    async def test_unknown_intent_blocks_retirement(self):
        intent = self.r.ledger.prepare(fixtures.candidate().quote.yes, fixtures.OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        await self.r._rollover_flat_markets([])
        self.r.public.unsubscribe.assert_not_awaited()
        self.assertEqual([c.rules.symbol for c in self.r.universe], ["X"])

    async def test_fill_during_cancel_keeps_original_owner(self):
        async def fill(_):
            self.r.account.reducer.positions["X"] = D("10")
        self.r._cancel_symbol_entries = AsyncMock(side_effect=fill)
        await self.r._rollover_flat_markets([])
        self.r.public.unsubscribe.assert_not_awaited()
        self.assertEqual([c.rules.symbol for c in self.r.universe], ["X"])

    async def test_recovery_hold_prevents_catalog_changes(self):
        self.r._stream_holds.add("public")
        await self.r._rollover_flat_markets([])
        self.r.public.subscribe.assert_not_awaited()
        self.r.public.unsubscribe.assert_not_awaited()

    async def test_catalog_omitted_inventory_recovers_without_rotating_healthy_markets(self):
        pool_id = self.fresh.pool.pool_id
        self.r.universe = []
        self.r._inventory_catalog_holds = {"Y"}
        self.r._sticky_markets = {pool_id: ("NEW", "Y")}
        self.r._sticky_pool_ids = {pool_id}
        self.r.account.reducer.positions["Y"] = D("10")
        self.r.account.block_entries("Y", fill=True)

        await self.r._rollover_flat_markets([])

        self.assertEqual([c.rules.symbol for c in self.r.universe], ["Y"])
        self.r.public.unsubscribe.assert_not_awaited()
        self.r.public.subscribe.assert_awaited_once_with(("Y",))
        self.assertEqual(self.r._inventory_catalog_holds, set())
        self.assertIn("Y", self.r.account.blocked_entries)
        self.assertIn("Y", self.r._permitted_symbols)
        self.assertIn("Y", self.r._expiry)

    async def test_unsubscribed_late_depth_cannot_resurrect_book(self):
        socket = fixtures.PublicMarketSocket(fixtures.AdmissionGovernor())
        socket._send = AsyncMock()
        await socket.subscribe(("X",))
        await socket.unsubscribe(("X",))
        socket.apply_frame({"e": "depthUpdate", "s": "X", "U": 1, "u": 1, "b": [[".4", "10"]], "a": [[".6", "10"]]})
        self.assertNotIn("X", socket.books)
