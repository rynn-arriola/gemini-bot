"""Synthetic envelopes only. No test value is a live calibrated recommendation."""
import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_queue_pilot as fx
from gemini_bot.adaptive_queue import AdaptiveLimits, AdaptivePolicy, AdaptiveQueuePilot, parse_adaptive_policy
from gemini_bot.config import DEFAULT_POLICY_PATH, ConfigurationError, load_policy
from gemini_bot.domain import Outcome, EngineState
from gemini_bot.reconciliation import AccountSnapshot
from gemini_bot.stage_queue_pilot import build_candidate

NOW = fx.NOW


def policy(**changes):
    values = dict(warmup_seconds=D(60), promotion_seconds=D(10), observation_gap_seconds=D(3),
        cancel_budget_seconds=D(5), buffer_contracts=D(15), burst_multiplier=D(1),
        depth_multiplier=D(2), minimum_requeue_gain=D(30), refresh_cooldown_seconds=D(5),
        expires_at=NOW+timedelta(hours=1), evidence_reference='SYNTHETIC-NOT-FOR-TRADING')
    return AdaptivePolicy(('X', 'Y'), AdaptiveLimits(**{**values, **changes}))


def raw_policy():
    return {'symbols': ['X', 'Y'], 'adaptive': {
        key: value.isoformat() if key == 'expires_at' else str(value)
        for key, value in asdict(policy().limits).items()}}


def warm(p, symbol='X', end=NOW, quantity=100):
    b = fx.book(quantity); b.symbol = symbol
    p.observe_book(b, generation=(0, 0), at=end-timedelta(seconds=70))
    for i in range(71):
        p.desired_mode(symbol, now=end-timedelta(seconds=70-i), generation=(0, 0), safe=True)


class AdaptiveStateTests(unittest.TestCase):
    def setUp(self):
        self.p = AdaptiveQueuePilot(policy())

    def desired(self, at=NOW, safe=True, generation=(0, 0), symbol='X'):
        return self.p.desired_mode(symbol, now=at, generation=generation, safe=safe)

    def trade(self, qty, maker=True, tid=1, at=NOW):
        self.p.observe_trade(dict(s='X', t=tid, p='.48' if maker else '.52', q=str(qty), m=maker,
            E=int(at.timestamp()*10**9)), generation=(0, 0), at=at)

    def test_cold_market_uses_fallback(self):
        self.p.observe_book(fx.book(), generation=(0, 0), at=NOW)
        self.assertEqual(self.desired(), 'fallback')
        self.assertEqual(self.p.modes['X'], 'fallback')
        self.assertFalse(self.p.proof_required)

    def test_time_without_continuous_checks_is_not_warmup(self):
        self.p.observe_book(fx.book(), generation=(0, 0), at=NOW-timedelta(hours=1))
        self.assertEqual(self.desired(), 'fallback')
        self.assertEqual(self.desired(NOW+timedelta(seconds=71)), 'fallback')

    def test_warm_evidence_only_recommends_not_switches(self):
        warm(self.p)
        self.assertEqual(self.desired(), 'best')
        self.assertEqual(self.p.modes['X'], 'fallback')

    def test_clock_reversal_and_gap_reset_continuous_warmup(self):
        warm(self.p)
        for at in (NOW-timedelta(seconds=1), NOW+timedelta(seconds=10)):
            self.assertEqual(self.desired(at), 'fallback')

    def test_unsafe_account_resets_promotion_but_other_market_independent(self):
        warm(self.p); warm(self.p, 'Y')
        self.assertEqual(self.desired(safe=False), 'fallback')
        self.assertEqual(self.desired(symbol='Y'), 'best')
        self.assertEqual(self.desired(), 'fallback')

    def test_entire_burst_reserved_not_average_rate(self):
        warm(self.p, quantity=1000)
        self.trade(100)
        self.assertEqual(self.p.calibration('X', Outcome.YES).warning_ahead, D(115))
        self.assertEqual(self.p.calibration('X', Outcome.NO).warning_ahead, D(15))

    def test_flow_peak_never_decays_and_duplicates_do_not_inflate(self):
        warm(self.p, quantity=1000)
        self.trade(100); self.trade(100)
        self.assertEqual(self.p.evidence['X'].peak_flow[Outcome.YES], D(100))
        for i in range(1, 75):
            self.desired(NOW+timedelta(seconds=i))
        self.assertEqual(self.p.calibration('X', Outcome.YES).warning_ahead, D(115))

    def test_no_selling_maps_only_to_no_side(self):
        warm(self.p)
        self.trade(100, maker=False)
        self.assertEqual(self.p.calibration('X', Outcome.NO).minimum_depth, D(230))
        self.assertEqual(self.p.calibration('X', Outcome.YES).minimum_depth, D(30))
        self.assertEqual(self.desired(), 'fallback')

    def test_thin_one_side_demotes_whole_pair_immediately(self):
        warm(self.p)
        self.p.modes['X'] = 'best'
        self.p.markets['X'].depths[Outcome.NO] = D(1)
        self.assertEqual(self.desired(), 'fallback')

    def test_depth_loss_and_replenishment_never_restore_anchor(self):
        warm(self.p)
        self.p.anchor_send('X', Outcome.YES, 'c', D('.48'), NOW)
        self.p.observe_book(fx.book(80, 2), generation=(0, 0), at=NOW)
        self.p.observe_book(fx.book(600, 3), generation=(0, 0), at=NOW)
        self.assertEqual(self.p.calibration('X', Outcome.YES).warning_ahead, D(35))
        self.assertEqual(self.p.markets['X'].anchors[Outcome.YES].scenario.ahead_scenario, D(80))

    def test_generation_change_discards_old_market_evidence(self):
        warm(self.p); self.trade(5)
        self.p.observe_book(fx.book(seq=2), generation=(1, 0), at=NOW)
        self.assertEqual(self.desired(generation=(1, 0)), 'fallback')
        self.assertEqual(self.p.evidence['X'].peak_flow[Outcome.YES], 0)
        self.assertIn('X', self.p.proof_required)

    def test_replacement_symbol_cannot_inherit_ended_symbol_evidence(self):
        warm(self.p)
        self.assertEqual(self.desired(symbol='Y'), 'fallback')
        self.assertNotIn('Y', self.p.evidence)

    def test_cancel_tail_latched_not_erased_by_fast_samples(self):
        warm(self.p)
        self.p.record_cancel(4.9488708, succeeded=True)
        self.assertEqual(self.desired(), 'best')
        self.p.record_cancel(5.001, succeeded=True)
        for _ in range(300):
            self.p.record_cancel(.1, succeeded=True)
        self.assertEqual(self.desired(), 'fallback')
        self.assertEqual(self.p.cancel_maximum, D('5.001'))

    def test_failed_cancel_disables_best_bid(self):
        warm(self.p)
        self.p.record_cancel(.01, succeeded=False)
        self.assertEqual(self.desired(), 'fallback')

    def test_expiry_uses_fallback_not_uncalibrated_touch(self):
        warm(self.p)
        self.assertEqual(self.desired(NOW+timedelta(hours=1)), 'fallback')

    def test_partial_fill_still_withdraws(self):
        warm(self.p)
        self.p.modes['X'] = 'best'
        o = fx.order(); o.cumulative_filled, o.remaining_quantity = D(7), D(8)
        self.assertEqual(self.p.advice('X', now=NOW, generation=(0, 0), orders=[o], safe=True), 'withdraw')

    def test_bad_trade_clock_removes_permission(self):
        warm(self.p)
        self.p.observe_trade(dict(s='X', t=1, p='.48', q='1', m=True), generation=(0, 0), at=NOW)
        self.assertEqual(self.desired(), 'fallback')

    def test_report_explicitly_labels_scenario(self):
        warm(self.p)
        report = self.p.report()
        self.assertFalse(report['exact_queue_position_available'])
        self.assertTrue(report['thresholds_are_scenarios'])

    def test_shared_limits_are_strict(self):
        for key, value in [('warmup_seconds', D(59)), ('buffer_contracts', D(14)),
                           ('burst_multiplier', D('.99')), ('depth_multiplier', D(1)),
                           ('cancel_budget_seconds', D(61)), ('observation_gap_seconds', D(4)),
                           ('promotion_seconds', D(0)), ('buffer_contracts', D('NaN'))]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                policy(**{key: value})

    def test_scope_cannot_expand_or_duplicate(self):
        for symbols in [('X',), ('X', 'X'), ('X', 'Y', 'Z')]:
            with self.assertRaises(ValueError):
                AdaptivePolicy(symbols, policy().limits)

    def test_breach_key_canonical_for_equivalent_limits_and_scope_order(self):
        self.assertEqual(policy().breach_key, policy(buffer_contracts=D('15.00')).breach_key)
        self.assertEqual(policy().breach_key, AdaptivePolicy(('Y', 'X'), policy().limits).breach_key)
        self.assertNotEqual(policy().breach_key, policy(cancel_budget_seconds=D(6)).breach_key)


class AdaptiveConfigTests(unittest.TestCase):
    def test_version_four_draft_preserves_base_and_can_load(self):
        review = dict(reviewed_at=NOW.isoformat(), incumbent_symbols=['X', 'Y'], queue_pilot=raw_policy())
        draft = build_candidate(DEFAULT_POLICY_PATH, review, now=NOW)
        base = json.loads(DEFAULT_POLICY_PATH.read_text())
        self.assertEqual(draft['policy_version'], 4)
        self.assertEqual({k: v for k, v in draft.items() if k not in {'queue_pilot', 'policy_version'}},
                         {k: v for k, v in base.items() if k != 'policy_version'})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'synthetic.json'; path.write_text(json.dumps(draft))
            strategy, _ = load_policy(path)
            self.assertIsInstance(strategy.queue_pilot, AdaptivePolicy)
            draft['quote_offset_ticks'] = 2; path.write_text(json.dumps(draft))
            with self.assertRaises(ConfigurationError):
                load_policy(path)

    def test_template_remains_invalid_and_disabled_policy_unchanged(self):
        template = json.loads((DEFAULT_POLICY_PATH.parent/'adaptive_queue_review.template.json').read_text())
        with self.assertRaises((ValueError, TypeError)):
            build_candidate(DEFAULT_POLICY_PATH, template, now=NOW)
        self.assertIsNone(load_policy()[0].queue_pilot)

    def test_strict_adaptive_parser_rejects_unknown_boolean_and_missing(self):
        for mutation in ('unknown', 'boolean', 'missing'):
            raw = raw_policy()
            if mutation == 'unknown': raw['extra'] = 1
            elif mutation == 'boolean': raw['adaptive']['burst_multiplier'] = True
            else: del raw['adaptive']['buffer_contracts']
            with self.assertRaises(ValueError): parse_adaptive_policy(raw)


class AdaptiveExecutionTests(fx.OneTickPilotIntegrationTests):
    """Repeat the real fake-wire race, fill, unknown-cancel and HARD suite."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.r.config = replace(self.r.config, strategy=replace(self.r.config.strategy, queue_pilot=policy()))
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        warm(self.r.queue_pilot)
        self.r.queue_pilot.modes['X'] = 'best'
        self.r.queue_pilot.proof_required.clear()

    async def test_warmup_places_normal_one_tick_not_zero_orders(self):
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.candidate(), quote=target))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(p['params']['price'] == '0.47' for p in self.sent))
        self.assertFalse(await self.r._cancel_too_aggressive_quote('X'))

    async def test_promotion_must_cancel_baseline_pair_before_switching(self):
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.candidate(), quote=target))
        warm(self.r.queue_pilot)
        self.r._cancel_symbol_entries = AsyncMock()
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')
        self.assertFalse(self.r._pilot_entry_ready('X'))

    async def test_terminal_cancel_then_fresh_depth_proof_rejoins_with_new_anchors(self):
        # The adaptive path checks the canonical public sequence as well as the
        # pilot observer; update both, just as the real public pump does.
        await self.r._quote_pair_locked(self.candidate())
        old_clients = {o.client_order_id for o in self.r.account.reducer.orders.values()}
        async def cancel(oid):
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = cancel
        await self.r._cancel_symbol_entries('X')
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.r._exit_account_snapshot.assert_not_awaited()  # No wasted proof during cooldown.
        self.r.queue_pilot.cooldown_at['X'] = NOW-timedelta(seconds=6)
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.r.public.books['X'] = fx.book(seq=2)
        self.r.queue_pilot.observe_book(self.r.public.books['X'], generation=(0, 0), at=NOW)
        await self.r._pilot_flat_proof('X')
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(all(a.client_id not in old_clients for a in self.r.queue_pilot.markets['X'].anchors.values()))

    async def test_normal_pair_promotes_after_real_cancel_and_proof(self):
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.candidate(), quote=target))
        warm(self.r.queue_pilot)
        async def cancel(oid):
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = cancel
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.r.queue_pilot.cooldown_at['X'] = NOW-timedelta(seconds=6)
        self.r.public.books['X'] = fx.book(seq=2)
        self.r.queue_pilot.observe_book(self.r.public.books['X'], generation=(0, 0), at=NOW)
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        await self.r._pilot_flat_proof('X')
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.assertEqual(self.r.queue_pilot.modes['X'], 'best')
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.candidate(), quote=target))
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(all(p['params']['price'] == '0.48' for p in self.sent[-2:]))

    async def test_demotion_cancel_proof_cooldown_new_depth_then_fallback(self):
        await self.r._quote_pair_locked(self.candidate())
        async def cancel(oid):
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = cancel
        self.r.queue_pilot.record_cancel(6, succeeded=True)
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.assertEqual(self.r.queue_pilot.modes['X'], 'best')
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        await self.r._pilot_flat_proof('X')
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.r.public.books['X'] = fx.book(seq=2)
        self.assertFalse(self.r._pilot_entry_ready('X'))  # Cooldown still wins after proof.
        self.r.queue_pilot.cooldown_at['X'] = NOW-timedelta(seconds=6)
        self.assertFalse(self.r._pilot_entry_ready('X'))  # Changes mode, needs rerank.
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.candidate(), quote=target))
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(all(p['params']['price'] == '0.47' for p in self.sent[-2:]))
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_disabled_observer_can_fall_back_after_proofs(self):
        self.r.queue_pilot.fail('X', ValueError('private detail'))
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        await self.r._pilot_flat_proof('X')
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.assertEqual(self.r.queue_pilot.modes['X'], 'fallback')
        # Re-observation must not perpetually re-add the initial barrier.
        self.assertTrue(self.r._pilot_entry_ready('X'))

    async def test_latency_breach_between_legs_cancels_first_no_second(self):
        self.r._cancel_symbol_entries = AsyncMock()
        self.on_second = lambda: self.r.queue_pilot.record_cancel(6, succeeded=True)
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 1)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_fallback_cannot_ignore_unknown_cancel(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r.private.cancel = AsyncMock(side_effect=TimeoutError('unknown'))
        self.r.queue_pilot.record_cancel(6, succeeded=True)
        with self.assertRaises(TimeoutError):
            await self.r._cancel_too_aggressive_quote('X')
        self.assertFalse(self.r._pilot_entry_ready('X'))
        self.assertEqual(self.r.queue_pilot.modes['X'], 'best')
        self.assertEqual(len(self.sent), 2)

    async def test_hard_blocks_fallback_too(self):
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        self.r._dynamic_pending_cap = 0
        target, _ = self.r._quote_target_state('X')
        await self.r._quote_pair_locked(replace(self.candidate(), quote=target))
        self.assertEqual(self.sent, [])

    async def test_invalid_adaptive_evidence_cannot_affect_nonpilot(self):
        self.r.queue_pilot.record_cancel(99, succeeded=True)
        await self.test_nonpilot_pair_uses_one_tick_while_pilot_evidence_is_missing()

    async def test_cancel_measurement_connects_to_runtime_high_water(self):
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        self.r._queue_cancel_done(time.monotonic()-6, task)
        self.assertTrue(self.r.queue_pilot.cancel_envelope_breached)

    async def test_cancel_breach_survives_restart_with_same_envelope(self):
        task = asyncio.create_task(asyncio.sleep(0)); await task
        self.r._queue_cancel_done(time.monotonic()-6, task)
        self.assertIsNotNone(self.r.journal.get_runtime_state(policy().breach_key))
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        self.r._adaptive_breach_saved = False
        self.r._restore_adaptive_breach()
        self.assertTrue(self.r.queue_pilot.cancel_envelope_breached)
        self.assertGreater(self.r.queue_pilot.cancel_maximum, 5)

    async def test_breach_persistence_failure_is_global_not_silent_success(self):
        task = asyncio.create_task(asyncio.sleep(0)); await task
        with patch.object(self.r.journal, 'set_runtime_state', side_effect=RuntimeError('storage failure')):
            self.r._queue_cancel_done(time.monotonic()-6, task)
        self.assertEqual(self.r.gate.state, EngineState.HALTED)
        with self.assertRaisesRegex(RuntimeError, 'storage failure'):
            self.r._check_background_failures()

    async def test_corrupted_durable_breach_never_reenables(self):
        self.r.journal.set_runtime_state(policy().breach_key, '{"breached": false}', updated_at=NOW)
        with self.assertRaises(RuntimeError):
            self.r._restore_adaptive_breach()
