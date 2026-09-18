"""Real transition guards with interleaved cancellation and advancing clocks.

Synthetic account/transport and temporary journals only; never venue I/O.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

import test_adaptive_queue as adaptive
import test_adaptive_all_selected as selected
import test_queue_pilot as fx
from gemini_bot.adaptive_queue import AdaptivePolicy, AdaptiveQueuePilot
from gemini_bot.domain import EngineState, Outcome
from gemini_bot.reconciliation import AccountSnapshot

NOW = fx.NOW


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = adaptive.AdaptiveExecutionTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.r
        self.sent = self.fixture.sent
        # Exercise the deployed timings without sleeping or changing policy.
        limits = replace(adaptive.policy().limits, warmup_seconds=D(600),
                         promotion_seconds=D(60), refresh_cooldown_seconds=D(10))
        self.policy = AdaptivePolicy((), limits, 'all_selected')
        self.r.queue_pilot = AdaptiveQueuePilot(self.policy)
        self.r.queue_pilot.sync_selected({'X', 'Y'})
        self.r.config = replace(self.r.config, strategy=replace(
            self.r.config.strategy, queue_pilot=self.policy))
        self.now = NOW
        self.clock = patch('gemini_bot.orchestrator._utc_now', side_effect=lambda: self.now)
        self.clock.start()
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))

    async def asyncTearDown(self):
        self.clock.stop()
        await self.fixture.asyncTearDown()

    def tick(self, seconds):
        self.now = NOW + timedelta(seconds=seconds)
        # Quiet synchronized book, but fresh shard traffic/pong proof.
        self.r.public.last_message_at = self.now
        self.r._refresh_adaptive_evidence()

    async def baseline(self):
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.fixture.candidate(), quote=target))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(p['params']['price'] == '0.47' for p in self.sent))
        for i in range(661):
            self.tick(i)
        self.assertEqual(self.r._adaptive_desired('X'), 'best')
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')

    def clear_dirty(self):
        while self.r.public.dirty.pop() is not None:
            pass

    async def cancel_with_checks(self, failure=None):
        async def cancel(oid):
            self.assertIn('X', self.r._entry_cancel_symbols)
            self.assertFalse(self.r._pilot_safe('X'))
            if failure:
                failure()
            self.r._refresh_adaptive_evidence()
            self.assertFalse(self.r._pilot_entry_ready('X'))
            self.assertEqual(self.r._pilot_advice('X', allow_entry=True), 'withdraw')
            await asyncio.sleep(0)  # Other checks run before terminal events.
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = cancel
        await self.r._cancel_too_aggressive_quote('X')

    def later_book(self):
        self.r.public.books['X'] = fx.book(seq=2)
        self.r.queue_pilot.observe_book(self.r.public.books['X'], generation=(0, 0), at=self.now)

    async def test_interleaved_cancel_cooldown_proof_promotes_and_places_at_touch(self):
        await self.baseline()
        since = self.r.queue_pilot.evidence['X'].since
        await self.cancel_with_checks()
        self.assertEqual(self.r.queue_pilot.evidence['X'].since, since)
        self.assertEqual(self.r._adaptive_desired('X'), 'best')
        for i in range(661, 671):
            self.tick(i)  # No working orders and no public depth change.
            self.assertFalse(self.r._pilot_entry_ready('X'))
        self.r._exit_account_snapshot.assert_not_awaited()
        self.later_book()
        self.clear_dirty()
        await self.r._pilot_flat_proof('X')
        self.assertEqual(self.r.public.dirty.pop(), 'X')
        self.assertFalse(self.r._pilot_entry_ready('X'))  # Rerank new mode.
        self.assertEqual(self.r.queue_pilot.modes['X'], 'best')
        self.assertEqual(self.r.public.dirty.pop(), 'X')
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.fixture.candidate(), quote=target))
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(all(p['params']['price'] == '0.48' for p in self.sent[-2:]))
        self.assertEqual(set(self.r.queue_pilot.markets['X'].anchors), set(Outcome))
        self.assertFalse(await self.r._cancel_too_aggressive_quote('X'))

    async def test_unknown_cancel_cannot_grant_entry_or_switch(self):
        await self.baseline()
        self.r.private.cancel = AsyncMock(side_effect=TimeoutError('synthetic unknown cancel'))
        with self.assertRaises(TimeoutError):
            await self.r._cancel_too_aggressive_quote('X')
        self.tick(661)
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')
        self.assertIn('X', self.r.queue_pilot.proof_required)
        self.assertTrue(self.r.journal.has_nonterminal_intents('X'))
        self.assertEqual(len(self.sent), 2)

    async def test_actual_fill_during_cancel_resets_warmup_and_blocks_promotion(self):
        await self.baseline()
        since = self.r.queue_pilot.evidence['X'].since
        first = True
        filled_outcome = None
        original_deadline = None
        async def cancel(oid):
            nonlocal first, filled_outcome, original_deadline
            if first:
                first = False
                o = self.r.account.reducer.orders[oid]
                self.r.account.apply(dict(e='orderUpdate', i=oid, c=o.client_order_id,
                    s='X', S='BUY', O=o.outcome.value, p=str(o.price), q='15', z='8', Z='7',
                    X='PARTIALLY_FILLED', t='transition-fill', L=str(o.price), n='0',
                    T=int(self.now.timestamp()*1000)))
                filled_outcome = o.outcome.value
                original_deadline = self.r.journal.get_deadline('X', filled_outcome)
            self.r._refresh_adaptive_evidence()
            self.assertEqual(self.r._adaptive_desired('X'), 'fallback')
            o = self.r.account.reducer.orders[oid]
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0',
                Z=str(o.cumulative_filled), T=int(self.now.timestamp()*1000)))
        self.r.private.cancel = cancel
        await self.r._cancel_too_aggressive_quote('X')
        self.assertNotEqual(self.r.queue_pilot.evidence['X'].since, since)
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.assertEqual(abs(self.r.account.expected_positions['X']), 7)
        self.assertIsNotNone(original_deadline)
        self.assertEqual(self.r.journal.get_deadline('X', filled_outcome), original_deadline)
        self.assertEqual(len(self.sent), 2)

    async def test_real_unsafe_conditions_still_reset_during_owned_cancel(self):
        await self.baseline()
        for kind in ('private', 'public', 'read', 'blocked', 'exit', 'stale', 'account'):
            with self.subTest(kind=kind):
                # Restore healthy, fully warm synthetic evidence for each case.
                e = self.r.queue_pilot.evidence['X']
                e.since = self.now-timedelta(seconds=700)
                e.last_check = self.now
                e.good_since = self.now-timedelta(seconds=70)
                self.r._entry_cancel_symbols['X'] = 1
                with patch.object(self.r, '_public_shard_held', return_value=kind=='public'), \
                     patch.object(self.r, '_book_fresh', return_value=kind!='stale'), \
                     patch.object(type(self.r.account), 'equity_classification_complete',
                                  new_callable=PropertyMock,
                                  return_value=kind!='account'):
                    if kind=='private': self.r._stream_holds.add('private')
                    if kind=='read': self.r._read_recovery_attempts['synthetic'] = 1
                    if kind=='blocked': self.r.account.blocked_entries.add('X')
                    if kind=='exit': self.r._exit_uncertain.add('X')
                    try:
                        self.assertEqual(self.r._adaptive_desired('X'), 'fallback')
                        self.assertEqual(e.since, self.now)
                        self.assertFalse(self.r._pilot_entry_ready('X'))
                    finally:
                        self.r._stream_holds.discard('private')
                        self.r._read_recovery_attempts.pop('synthetic', None)
                        self.r.account.blocked_entries.discard('X')
                        self.r._exit_uncertain.discard('X')
                        self.r._entry_cancel_symbols.pop('X')

    async def test_missing_continuity_still_resets_after_cancellation(self):
        await self.baseline()
        await self.cancel_with_checks()
        self.tick(664)  # Four seconds with no observation exceeds approved gap.
        self.assertEqual(self.r._adaptive_desired('X'), 'fallback')
        self.assertEqual(self.r.queue_pilot.evidence['X'].since, self.now)

    async def test_expired_envelope_cannot_promote_after_terminal_proof(self):
        await self.baseline()
        await self.cancel_with_checks()
        self.now = self.policy.limits.expires_at
        self.r.public.last_message_at = self.now
        self.r._refresh_adaptive_evidence()
        self.later_book()
        await self.r._pilot_flat_proof('X')
        self.assertEqual(self.r._adaptive_desired('X'), 'fallback')
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')

    async def test_depth_loss_during_cooldown_prevents_promotion(self):
        await self.baseline()
        await self.cancel_with_checks()
        for i in range(661, 671): self.tick(i)
        self.later_book()
        self.r.queue_pilot.markets['X'].depths[Outcome.YES] = D(1)
        await self.r._pilot_flat_proof('X')
        self.assertTrue(self.r._pilot_entry_ready('X'))
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')
        self.assertEqual(self.r.queue_pilot.reasons['X'], 'adaptive_depth_below_burst_scenario')

    async def test_independent_safety_clock_checks_evidence_without_working_orders(self):
        self.r.queue_pilot.observe_book(fx.book(), generation=(0, 0), at=NOW)
        calls = []
        original = self.r._refresh_adaptive_evidence
        def observe():
            calls.append(True)
            original()
            self.r.gate.state = EngineState.DRAINING
        with patch.object(self.r, '_refresh_adaptive_evidence', side_effect=observe), \
             patch.object(self.r, '_mark_live_risk', return_value=40), \
             patch.object(self.r, '_exit_due_positions', new=AsyncMock()):
            await self.r._live_safety_clock()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.r.queue_pilot.evidence['X'].last_check, NOW)
        self.assertEqual(len(self.sent), 0)

    async def test_twenty_members_keep_independent_evidence_with_one_canceling(self):
        self.r.queue_pilot.sync_selected(selected.SYMBOLS)
        for symbol in selected.SYMBOLS:
            b = fx.book(); b.symbol = symbol
            self.r.public.books[symbol] = b
            self.r.queue_pilot.observe_book(b, generation=(0, 0), at=NOW)
        self.r._entry_cancel_symbols['X'] = 1
        for i in range(661): self.tick(i)
        self.assertTrue(all(self.r._adaptive_desired(s)=='best' for s in selected.SYMBOLS))
        self.assertFalse(self.r._pilot_safe('X'))
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.assertTrue(all(self.r._pilot_safe(s) for s in selected.SYMBOLS-{'X'}))
        self.assertEqual(len(self.sent), 0)
