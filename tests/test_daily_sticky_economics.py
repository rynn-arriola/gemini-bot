"""Durable owner reservations under changing economics; synthetic accounts only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, patch

from gemini_bot.sticky_recovery import StickyRecovery, Failure, ECONOMIC_FAILURES
from gemini_bot.uptime import eastern_day_window
from gemini_bot.reconciliation import AccountSnapshot
import test_sticky_recovery as existing
import test_repair_integration as fixtures

NOW = fixtures.NOW
ECONOMIC = 'non-positive conservative net-profit estimate'


class DailyClockTests(unittest.TestCase):
    def test_economic_hold_spans_day_and_expiry_does_not_auto_renew(self):
        r = StickyRecovery()
        self.assertTrue(r.protect_economics('a', ECONOMIC, NOW))
        end = eastern_day_window(NOW)[1]
        self.assertTrue(r.protect_economics('a', ECONOMIC, end-timedelta(seconds=1)))
        self.assertFalse(r.protect_economics('a', ECONOMIC, end))
        self.assertFalse(r.protect_economics('a', ECONOMIC, end+timedelta(days=1)))
        self.assertEqual(r.economic_hold_until('a'), end)

    def test_midnight_requires_fresh_grace_before_release(self):
        r = StickyRecovery()
        r.protect_economics('a', ECONOMIC, NOW)
        end = eastern_day_window(NOW)[1]
        r.failures['a'] = Failure(ECONOMIC, 600, end-timedelta(seconds=1))
        for i in range(600):
            self.assertFalse(r.observe('a', ECONOMIC, end+timedelta(seconds=i)))
        self.assertTrue(r.observe('a', ECONOMIC, end+timedelta(seconds=600)))

    def test_dst_expiry_is_eastern_midnight(self):
        for day, duration in (('2026-03-08T00:00:00-05:00',23), ('2026-11-01T00:00:00-04:00',25)):
            r = StickyRecovery()
            start = datetime.fromisoformat(day)
            r.protect_economics('a', ECONOMIC, start)
            self.assertEqual((r.economic_hold_until('a')-start).total_seconds(), duration*3600)

    def test_protection_only_for_exact_economic_reasons(self):
        r = StickyRecovery()
        for reason in ECONOMIC_FAILURES:
            self.assertTrue(r.protect_economics('a', reason, NOW))
        for reason in ('entry_time_unavailable', 'stale market data', ECONOMIC+'; stale market data',
                       'offset produces an invalid quote price', 'unknown', 'rank_candidate_missing:no_book'):
            self.assertFalse(r.protect_economics('a', reason, NOW))

    def test_changed_failure_reason_cannot_inherit_mature_clock(self):
        r = StickyRecovery()
        r.failures['a'] = Failure(ECONOMIC, 600, NOW)
        self.assertFalse(r.observe('a', 'stale market data', NOW+timedelta(seconds=1)))
        self.assertEqual(r.failures['a'].observed_seconds, 0)

    def test_restart_preserves_expired_hold_and_never_credits_offline_grace(self):
        r = StickyRecovery()
        r.protect_economics('a', ECONOMIC, NOW)
        r.failures['a'] = Failure(ECONOMIC, 600, NOW)
        new = StickyRecovery()
        new.restore_state(r.dumps_state(), {'a'})
        end = r.economic_hold_until('a')
        self.assertFalse(new.protect_economics('a', ECONOMIC, end+timedelta(hours=1)))
        self.assertFalse(new.observe('a', ECONOMIC, end+timedelta(hours=1)))
        self.assertEqual(new.economic_hold_until('a'), end)

    def test_stream_lock_preserves_reservation_but_true_recovery_clears_it(self):
        r = StickyRecovery()
        r.protect_economics('a', ECONOMIC, NOW)
        r.observe('a', None, NOW, preserve_economic_hold=True)
        self.assertIsNotNone(r.economic_hold_until('a'))
        r.observe('a', None, NOW)
        self.assertIsNone(r.economic_hold_until('a'))

    def test_capacity_pruning_and_malformed_state(self):
        r = StickyRecovery()
        for i in range(20):
            r.protect_economics(str(i), ECONOMIC, NOW)
        with self.assertRaises(ValueError):
            r.protect_economics('21', ECONOMIC, NOW)
        restored = StickyRecovery()
        restored.restore_state(r.dumps_state(), {'0'})
        self.assertEqual(set(restored.economic_holds), {'0'})
        base = {'version':2,'failures':{},'economic_holds':{}}
        for raw in ({}, dict(base, version=True), dict(base, extra=1),
                    dict(base, economic_holds=[]), dict(base, economic_holds={'a':None}),
                    dict(base, economic_holds={'a':'2026-09-18'}),
                    dict(base, economic_holds={'a':'nonsense'}),
                    dict(base, economic_holds={str(i):NOW.isoformat() for i in range(21)})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                StickyRecovery().restore_state(json.dumps(raw), {'a'})


class DailyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = existing.RecoveryIntegrationTests()
        await self.fx.asyncSetUp()
        self.r, self.a, self.b = self.fx.r, self.fx.a, self.fx.b

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    async def test_long_profit_dip_keeps_slot_without_proof_or_new_selection(self):
        self.fx.bad(ECONOMIC)
        self.fx.mature()
        for seconds in (0, 601, 3600, 7200):
            with patch('gemini_bot.orchestrator._utc_now', return_value=NOW+timedelta(seconds=seconds)):
                desired, owners = await self.fx.choose()
                self.assertEqual(desired, [])
                self.assertEqual(owners, {self.a.owner_key})
        self.r._exit_account_snapshot.assert_not_awaited()
        self.assertFalse(self.r._candidate_actionable(self.r.ranked[0]))
        self.assertEqual(self.r.journal.connection.execute(
            "SELECT COUNT(*) FROM audit_event WHERE kind='sticky_owner_daily_reserved'").fetchone()[0], 1)

    async def test_orders_cancel_while_owner_reservation_survives(self):
        from gemini_bot.domain import Outcome
        from gemini_bot.execution import OrderPurpose
        from gemini_bot.domain import QuoteLeg, Side
        intent = self.r.ledger.prepare(QuoteLeg('A', Outcome.YES, Side.BUY, existing.D('.46'), existing.D(20)), OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        self.r.account.apply(dict(e='orderUpdate',i='owned-A',c=intent.client_order_id,s='A',S='BUY',
                                  O='YES',p='.46',q='20',z='20',Z='0',X='NEW'))
        self.fx.bad(ECONOMIC)
        self.fx.mature()
        desired, owners = await self.fx.choose()
        await self.r._cancel_ineligible_working_entries({c.symbol:c for c in desired})
        self.r._cancel_symbol_entries.assert_awaited_with('A')
        self.assertEqual(owners, {self.a.owner_key})

    async def test_recovery_returns_exact_incumbent_even_with_higher_ranked_challenger(self):
        self.fx.bad(ECONOMIC)
        await self.fx.choose()
        self.r.ranked = [self.b, self.a]
        desired, owners = await self.fx.choose()
        self.assertEqual([c.symbol for c in desired], ['A'])
        self.assertEqual(owners, {self.a.owner_key})
        self.assertFalse(self.r._sticky_recovery.economic_holds)

    async def test_shared_pool_marginal_failure_gets_same_daily_protection(self):
        self.r.ranked = [replace(self.a, expected_execution_cost_usd=self.a.expected_reward_usd), self.b]
        self.fx.mature()
        desired, owners = await self.fx.choose()
        self.assertEqual(desired, [])
        self.assertEqual(owners, {self.a.owner_key})
        self.assertIsNotNone(self.r._sticky_recovery.economic_hold_until(self.a.owner_key))
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_restored_same_day_reservation_survives_new_runtime_state(self):
        self.fx.bad(ECONOMIC)
        await self.fx.choose()
        before = self.r._sticky_recovery.economic_hold_until(self.a.owner_key)
        self.r._sticky_recovery = StickyRecovery()
        self.r._load_sticky_markets()
        self.assertEqual(self.r._sticky_recovery.economic_hold_until(self.a.owner_key), before)
        self.fx.mature()
        _, owners = await self.fx.choose()
        self.assertEqual(owners, {self.a.owner_key})
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_legacy_recovery_loads_without_resetting_owner_history(self):
        self.r.journal.set_runtime_state('sticky_markets_v2', json.dumps(self.r._sticky_markets), updated_at=NOW)
        old = StickyRecovery()
        old.observe(self.a.owner_key, ECONOMIC, NOW)
        self.r.journal.set_runtime_state('sticky_recovery_v1', old.dumps(), updated_at=NOW)
        self.r._load_sticky_markets()
        self.fx.bad(ECONOMIC)
        _, owners = await self.fx.choose()
        self.assertEqual(owners, {self.a.owner_key})
        self.assertIsNotNone(self.r.journal.get_runtime_state('sticky_recovery_v2'))

    async def test_expired_economic_hold_requires_fresh_grace_and_terminal_proof(self):
        self.fx.bad(ECONOMIC)
        await self.fx.choose()
        end = self.r._sticky_recovery.economic_hold_until(self.a.owner_key)
        with patch('gemini_bot.orchestrator._utc_now', return_value=end):
            _, owners = await self.fx.choose()
            self.assertEqual(owners, {self.a.owner_key})
        self.r._sticky_recovery.failures[self.a.owner_key] = Failure(ECONOMIC, 599, end+timedelta(seconds=599))
        with patch('gemini_bot.orchestrator._utc_now', return_value=end+timedelta(seconds=600)):
            desired, owners = await self.fx.choose()
        self.r._cancel_symbol_entries.assert_awaited_once_with('A')
        self.r._exit_account_snapshot.assert_awaited_once_with('A')
        self.assertEqual([c.symbol for c in desired], ['B'])

    async def test_structural_fault_can_release_despite_economic_reservation(self):
        self.fx.bad(ECONOMIC)
        await self.fx.choose()
        self.fx.bad('offset produces an invalid quote price')
        self.fx.mature()
        desired, _ = await self.fx.choose()
        self.assertEqual([c.symbol for c in desired], ['B'])
        self.r._exit_account_snapshot.assert_awaited_once()

    async def test_entry_cutoff_cannot_be_hidden_by_economic_rejection(self):
        self.fx.bad(ECONOMIC)
        await self.fx.choose()
        self.r._entry_time_valid.side_effect = lambda symbol: symbol != 'A'
        self.fx.mature()
        desired, _ = await self.fx.choose()
        self.assertEqual([c.symbol for c in desired], ['B'])

    async def test_failure_becomes_economic_during_proof_keeps_owner(self):
        self.fx.bad('quote exceeds maximum reward distance')
        self.fx.mature()
        self.r._rank_for_owner_check.side_effect = lambda: self.fx.bad(ECONOMIC)
        desired, owners = await self.fx.choose()
        self.assertEqual(desired, [])
        self.assertEqual(owners, {self.a.owner_key})
        self.assertIsNotNone(self.r._sticky_recovery.economic_hold_until(self.a.owner_key))

    async def test_twenty_economic_reservations_leave_no_room_for_challenger(self):
        rows = [existing.rank_candidate(existing.strategy.item(f'M{i}',f'p{i}'), now=NOW,
                                       quantity=existing.D(20)) for i in range(21)]
        self.r._sticky_markets = {c.owner_key:(c.event_ticker,c.symbol) for c in rows[:20]}
        self.r._sticky_pool_ids = set(self.r._sticky_markets)
        self.r._permitted_symbols = {c.symbol for c in rows}
        self.r.ranked = [replace(c,eligible_now=False,reasons=(ECONOMIC,)) for c in rows[:20]] + rows[20:]
        desired, owners = await self.fx.choose(maximum=20)
        self.assertEqual(desired, [])
        self.assertEqual(len(owners), 20)
        self.assertNotIn(rows[20].owner_key, owners)
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_reservation_persistence_failure_propagates(self):
        self.fx.bad(ECONOMIC)
        with patch.object(self.r.journal, 'set_runtime_state', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                await self.fx.choose()
