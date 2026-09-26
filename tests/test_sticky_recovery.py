"""Offline owner continuity and capacity tests; no real account or venue I/O."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gemini_bot.sticky_recovery import StickyRecovery, Failure
from gemini_bot.reward_ranking import rank_candidate
from gemini_bot.reconciliation import AccountSnapshot
from gemini_bot.exit_recovery import ExitDeferred
from gemini_bot.persistent_risk import PersistentRiskWindow
from gemini_bot.risk_windows import RiskWindowPolicy
from gemini_bot.inventory import pending_capacity
import test_competition_strategy as strategy
import test_repair_integration as fixtures

NOW = strategy.NOW


class RecoveryClockTests(unittest.TestCase):
    def test_one_bad_cycle_cannot_release(self):
        r = StickyRecovery()
        self.assertFalse(r.observe('p', 'one_sided', NOW))
        self.assertFalse(r.observe('p', 'one_sided', NOW+timedelta(seconds=1)))

    def test_continuous_ten_minutes_then_eligible_for_proof(self):
        r = StickyRecovery()
        for second in range(600):
            self.assertFalse(r.observe('p', 'unquotable', NOW+timedelta(seconds=second)))
        self.assertTrue(r.observe('p', 'unquotable', NOW+timedelta(seconds=600)))

    def test_recovery_resets_failure_history(self):
        r = StickyRecovery()
        r.failures['p'] = Failure('bad', 599, NOW)
        r.observe('p', None, NOW)
        self.assertFalse(r.observe('p', 'bad', NOW+timedelta(seconds=1)))
        self.assertEqual(r.failures['p'].observed_seconds, 0)

    def test_restart_gap_not_failure_credit(self):
        r = StickyRecovery()
        r.failures['p'] = Failure('bad', 599, NOW)
        restored = StickyRecovery()
        restored.restore(r.dumps(), {'p'})
        self.assertFalse(restored.observe('p', 'bad', NOW+timedelta(hours=1)))
        self.assertEqual(restored.failures['p'].observed_seconds, 0)
        restored.restore(r.dumps(), {'p'})
        self.assertFalse(restored.observe('p', 'bad', NOW+timedelta(seconds=2)))
        self.assertEqual(restored.failures['p'].observed_seconds, 0)

    def test_unavailable_account_and_clock_reversal_do_not_credit_failure(self):
        for delta, available in ((1, False), (-1, True)):
            r = StickyRecovery()
            r.failures['p'] = Failure('bad', 599, NOW)
            self.assertFalse(r.observe('p', 'bad', NOW+timedelta(seconds=delta), evidence_available=available))
            self.assertEqual(r.failures['p'].observed_seconds, 0)

    def test_malformed_persistence_fails_closed(self):
        valid = {'reason':'bad', 'observed_seconds':1, 'last_observed':NOW.isoformat()}
        for value in ([], {'p':{}}, {'p':dict(valid, observed_seconds=float('nan'))},
                      {'p':dict(valid, observed_seconds=True)}, {'p':dict(valid, observed_seconds=-1)},
                      {'p':dict(valid, observed_seconds=601)}, {'p':dict(valid, last_observed='2026-09-16')}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                StickyRecovery().restore(json.dumps(value), {'p'})

    def test_twenty_owners_bound_and_stale_owner_cleanup(self):
        r = StickyRecovery()
        from gemini_bot.capacity import MAX_ACTIVE_MARKETS
        for i in range(MAX_ACTIVE_MARKETS):
            r.observe(str(i), 'bad', NOW)
        with self.assertRaises(ValueError):
            r.observe('extra', 'bad', NOW)
        restored = StickyRecovery()
        restored.restore(r.dumps(), {'0'})
        self.assertEqual(set(restored.failures), {'0'})

    def test_forty_order_boundary_and_hard_unchanged(self):
        self.assertTrue(pending_capacity(38, 2, 40))
        self.assertFalse(pending_capacity(39, 2, 40))
        with self.assertRaises(ValueError):
            pending_capacity(0, 1, 61)
        policy = RiskWindowPolicy(D(30), 40)
        self.assertEqual(policy.pending_order_cap(D('29.99')), 40)
        self.assertEqual(policy.pending_order_cap(D(30)), 0)


class RecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = fixtures.RuntimeRepairTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.runtime
        self.r._continuous_service = True
        self.a = rank_candidate(strategy.item('A', 'a'), now=NOW, quantity=D(20))
        self.b = rank_candidate(strategy.item('B', 'b', budget='1000'), now=NOW, quantity=D(20))
        self.r.ranked = [self.a, self.b]
        self.r._sticky_pool_ids = {self.a.owner_key}
        self.r._sticky_markets = {self.a.owner_key: (self.a.event_ticker, self.a.symbol)}
        self.r._permitted_symbols = {'A','B'}
        self.r._entry_time_valid = Mock(return_value=True)
        self.r._book_fresh = Mock(return_value=True)
        self.r._quote_target_state = Mock(return_value=(None, 'external_book_one_sided'))
        self.r._cancel_symbol_entries = AsyncMock()
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        self.r._rank_for_owner_check = Mock()
        self.r.account.subscription_acknowledged = True
        self.r.account.last_balance_at = self.r.account.last_position_at = time.monotonic()

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    async def choose(self, locked=(), maximum=1):
        selected, owners, reserved = await self.r._select_recovery_cohort(set(locked), maximum)
        self.r._sticky_pool_ids = owners
        self.r._save_sticky_markets(selected, reserved)
        return selected, owners

    def bad(self, reason='quote exceeds maximum reward distance'):
        self.r.ranked = [replace(self.a, eligible_now=False, reasons=(reason,)), self.b]

    def mature(self):
        self.r._sticky_recovery.failures[self.a.owner_key] = Failure(
            self.r._owner_failure_reason(self.a.symbol), 600, fixtures.NOW)

    async def test_missing_book_keeps_owner_then_resumes_same_market(self):
        self.r.ranked = [self.b]
        selected, owners = await self.choose()
        self.assertEqual(selected, [])
        self.assertEqual(owners, {self.a.owner_key})
        self.assertIn(self.a.owner_key, self.r._sticky_markets)
        self.r.ranked = [self.a, self.b]
        selected, owners = await self.choose()
        self.assertEqual([c.symbol for c in selected], ['A'])
        self.assertFalse(self.r._sticky_recovery.failures)
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_bad_quote_and_bad_economics_both_hold_without_unsafe_selection(self):
        for reason in ('offset produces an invalid quote price', 'quote exceeds maximum reward distance',
                       'non-positive conservative net-profit estimate', 'stale market data'):
            self.bad(reason)
            selected, owners = await self.choose()
            self.assertEqual(selected, [])
            self.assertEqual(owners, {self.a.owner_key})

    async def test_same_pool_marginal_failure_reserves_owner(self):
        b = replace(self.b, pool_id=self.a.pool_id, daily_pool_usd=self.a.daily_pool_usd,
                    pool_competition_score_proxy=self.a.pool_competition_score_proxy)
        self.r.ranked = [replace(self.a, expected_execution_cost_usd=self.a.expected_reward_usd), b]
        selected, owners = await self.choose()
        self.assertEqual(selected, [])
        self.assertEqual(owners, {self.a.owner_key})

    async def test_sustained_failure_requires_cancel_then_proof_before_release(self):
        self.bad()
        self.mature()
        order = []
        async def cancel(symbol): order.append('cancel')
        async def proof(symbol):
            order.append('proof')
            return AccountSnapshot((),())
        self.r._cancel_symbol_entries.side_effect = cancel
        self.r._exit_account_snapshot.side_effect = proof
        selected, owners = await self.choose()
        self.assertEqual(order, ['cancel','proof'])
        self.assertEqual([c.symbol for c in selected], ['B'])
        events = self.r.journal.connection.execute("SELECT kind,details_json FROM audit_event WHERE kind LIKE 'sticky_owner_%'").fetchall()
        self.assertTrue(any(k=='sticky_owner_released' and 'terminal_flat' in v for k,v in events))
        self.assertTrue(any(k=='sticky_owner_admitted' for k,v in events))

    async def test_recovery_during_account_proof_preserves_original(self):
        self.bad()
        self.mature()
        self.r._rank_for_owner_check.side_effect = lambda: setattr(self.r,'ranked',[self.a,self.b])
        selected, owners = await self.choose()
        self.assertEqual([c.symbol for c in selected], ['A'])

    async def test_failed_proof_never_releases_and_retries_are_paced(self):
        self.bad()
        self.mature()
        self.r._exit_account_snapshot.side_effect = ExitDeferred('working_order_proof_pending')
        for _ in range(3):
            selected, owners = await self.choose()
            self.assertEqual(owners, {self.a.owner_key})
        self.r._exit_account_snapshot.assert_awaited_once()

    async def test_unknown_proof_failure_is_not_swallowed(self):
        self.bad()
        self.mature()
        self.r._exit_account_snapshot.side_effect = RuntimeError('unknown execution')
        with self.assertRaises(RuntimeError):
            await self.choose()
        self.assertIn(self.a.owner_key, self.r._sticky_markets)

    async def test_inventory_and_shard_locked_slots_never_released(self):
        self.bad()
        self.mature()
        selected, owners = await self.choose(locked={self.a.owner_key})
        self.assertEqual(owners, {self.a.owner_key})
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_original_deadline_and_pending_exit_prevent_release(self):
        self.bad()
        self.mature()
        self.r.journal.persist_deadline('A', 'YES', NOW, NOW+timedelta(minutes=5), D(20))
        _, owners = await self.choose()
        self.assertEqual(owners, {self.a.owner_key})
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_pause_does_not_accumulate_expiry(self):
        self.bad()
        self.mature()
        self.r._read_recovery_attempts['read'] = 1
        _, owners = await self.choose()
        self.assertEqual(owners, {self.a.owner_key})
        self.assertEqual(self.r._sticky_recovery.failures[self.a.owner_key].observed_seconds, 0)
        self.r._exit_account_snapshot.assert_not_awaited()

    async def test_recovery_persistence_and_legacy_cohort_preserved(self):
        self.bad()
        await self.choose()
        self.r._sticky_markets.clear()
        self.r._sticky_pool_ids.clear()
        self.r._load_sticky_markets()
        self.assertEqual(self.r._sticky_pool_ids, {self.a.owner_key})
        self.assertIn(self.a.owner_key, self.r._sticky_recovery.failures)

    async def test_twenty_owner_reservations_block_twenty_first_even_if_one_missing(self):
        rows = [rank_candidate(strategy.item(f'M{i}',f'p{i}'),now=NOW,quantity=D(20)) for i in range(21)]
        self.r._sticky_markets = {c.owner_key:(c.event_ticker,c.symbol) for c in rows[:20]}
        self.r._sticky_pool_ids = set(self.r._sticky_markets)
        self.r._permitted_symbols = {c.symbol for c in rows}
        self.r.ranked = rows[1:]
        selected, owners = await self.choose(maximum=20)
        self.assertEqual(len(selected),19)
        self.assertEqual(len(owners),20)
        self.assertNotIn(rows[-1].owner_key,owners)

    async def test_new_width_does_not_automatically_raise_persisted_cap_or_forgive_hard(self):
        p = PersistentRiskWindow(RiskWindowPolicy(D(30),30),self.r.journal)
        p.update(now=NOW,realized_pnl=D(-5),classification_complete=True)
        new = PersistentRiskWindow(RiskWindowPolicy(D(30),40),self.r.journal)
        self.assertEqual(new.update(now=NOW,realized_pnl=D(-5),classification_complete=True,reconciled=True).pending_order_cap,30)
        new.update(now=NOW,realized_pnl=D(-30),classification_complete=True)
        self.assertEqual(new.update(now=NOW,realized_pnl=D(0),classification_complete=True,reconciled=True).pending_order_cap,0)

    async def test_explicit_capacity_migration_preserves_every_economic_field_and_is_one_use(self):
        from gemini_bot.capacity_migration import apply_approved_migration
        p = PersistentRiskWindow(RiskWindowPolicy(D(30),30),self.r.journal)
        mark = p.update(now=NOW,realized_pnl=D(-5),classification_complete=True)
        before = self.r.journal.load_realized_risk_window(mark.window_start)
        self.assertTrue(apply_approved_migration(self.r.journal, now=NOW))
        after = self.r.journal.load_realized_risk_window(mark.window_start)
        self.assertEqual(after, dict(before, pending_order_cap=40))
        self.assertFalse(apply_approved_migration(self.r.journal, now=NOW))

    async def test_explicit_capacity_migration_cannot_clear_hard_or_incomplete(self):
        from gemini_bot.capacity_migration import apply_approved_migration
        for loss, complete in ((D(-5),False),(D(-30),True),(D(0),True)):
            p = PersistentRiskWindow(RiskWindowPolicy(D(30),30),self.r.journal)
            p.update(now=NOW,realized_pnl=loss,classification_complete=complete)
            with self.assertRaises(RuntimeError):
                apply_approved_migration(self.r.journal, now=NOW)
