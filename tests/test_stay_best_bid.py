"""Owner accepts thin-queue fills; proof, fill and rate guards remain real.

All wire/account adapters are synthetic. No live orders or calibration.
"""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import test_adaptive_queue as adaptive
import test_adaptive_all_selected as selected
import test_queue_pilot as fx
from gemini_bot.adaptive_queue import AdaptivePolicy, AdaptiveQueuePilot
from gemini_bot.config import DEFAULT_POLICY_PATH, ConfigurationError, load_policy
from gemini_bot.domain import Outcome
from gemini_bot.stage_queue_pilot import build_candidate

NOW = fx.NOW


def policy():
    return AdaptivePolicy((),adaptive.policy().limits,'all_selected','stay_best',D(300),D(500))


class StayBestStateTests(unittest.TestCase):
    def setUp(self):
        self.p = AdaptiveQueuePilot(policy())
        self.p.sync_selected({'X', 'Y'})
        self.p.observe_book(fx.book(1), generation=(0, 0), at=NOW)
        self.p.proof_required.clear()

    def advice(self, orders=(), at=NOW, **kwargs):
        return self.p.advice('X', now=at, generation=(0, 0), orders=orders,
                             safe=True, **kwargs)

    def pair(self, depth=1, age=0):
        self.p.markets['X'].started_at = NOW-timedelta(seconds=age)
        self.p.markets['X'].depths = {o: D(depth) for o in Outcome}
        orders = []
        for o in Outcome:
            self.p.anchor_send('X', o, 'c'+o.value, D('.48'), NOW)
            orders.append(fx.order(o, 'c'+o.value))
        return orders

    def test_cold_thin_book_joins_best_without_promotion(self):
        self.assertEqual(self.p.modes['X'], 'best')
        self.assertEqual(self.advice(allow_entry=True), 'allow')
        self.assertEqual(self.p.desired_mode('X',now=NOW,generation=(0,0),safe=True),'best')

    def test_depleted_with_no_new_depth_keeps_pair(self):
        orders = self.pair(age=70)
        for a in self.p.markets['X'].anchors.values(): a.scenario.trade(D(1))
        self.assertEqual(self.advice(orders), 'keep')

    def test_new_depth_can_trigger_useful_reposition_without_mode_change(self):
        orders = self.pair(age=70)
        self.p.markets['X'].depths[Outcome.YES] = D(600)
        self.assertEqual(self.advice(orders), 'withdraw')
        self.assertEqual(self.p.reasons['X'], 'estimated_queue_refresh_benefit')
        self.assertEqual(self.p.modes['X'], 'best')

    def test_less_than_five_hundred_contract_gain_keeps_pair_resting(self):
        orders=self.pair(age=70)
        self.p.markets['X'].depths[Outcome.YES]=D(500)  # 499 new behind one estimated ahead.
        self.assertEqual(self.advice(orders),'keep')
        self.p.markets['X'].depths[Outcome.YES]=D(501)
        self.assertEqual(self.advice(orders),'withdraw')

    def test_cooldown_skips_optional_reposition_and_keeps_orders(self):
        orders = self.pair(age=70)
        self.p.markets['X'].depths[Outcome.YES] = D(600)
        self.p.cooldown_at['X'] = NOW
        self.assertEqual(self.advice(orders), 'keep')

    def test_same_price_refresh_waits_five_minutes_without_delaying_price_tracking(self):
        orders=self.pair(age=70)
        self.p.markets['X'].depths[Outcome.YES]=D(600)
        self.assertEqual(self.advice(orders),'withdraw')
        self.assertEqual(self.advice(orders,at=NOW+timedelta(seconds=299)),'keep')
        self.p.markets['X'].prices[Outcome.YES]=D('.49')
        self.assertEqual(self.advice(orders,at=NOW+timedelta(seconds=299)),'withdraw')
        self.assertEqual(self.p.reasons['X'],'best_price_changed')

    def test_same_price_refresh_can_repeat_after_five_minutes(self):
        orders=self.pair(age=70)
        self.p.markets['X'].depths[Outcome.YES]=D(600)
        self.assertEqual(self.advice(orders),'withdraw')
        self.assertEqual(self.advice(orders,at=NOW+timedelta(seconds=300)),'withdraw')
        self.assertEqual(self.p.reasons['X'],'estimated_queue_refresh_benefit')

    def test_warmup_uncertain_or_expired_optimization_does_not_withdraw(self):
        for variant in ('cold', 'uncertain', 'expired', 'breach', 'missing_anchor'):
            with self.subTest(variant=variant):
                self.setUp()
                orders = self.pair(age=70)
                self.p.markets['X'].depths[Outcome.YES] = D(600)
                if variant == 'cold': self.p.markets['X'].started_at = NOW
                if variant == 'uncertain': self.p.markets['X'].uncertain_until = NOW+timedelta(seconds=60)
                if variant == 'expired': self.p.policy=replace(policy(),limits=replace(policy().limits,expires_at=NOW))
                if variant == 'breach': self.p.record_cancel(6,succeeded=True)
                if variant == 'missing_anchor': self.p.markets['X'].anchors.clear()
                self.assertEqual(self.advice(orders),'keep')
                self.assertEqual(self.p.desired_mode('X',now=NOW,generation=(0,0),safe=True),'best')

    def test_price_tracking_still_repositions_when_optimization_expired(self):
        orders = self.pair()
        self.p.policy=replace(policy(),limits=replace(policy().limits,expires_at=NOW))
        self.p.markets['X'].prices[Outcome.YES]=D('.49')
        self.assertEqual(self.advice(orders),'withdraw')
        self.assertEqual(self.p.reasons['X'],'best_price_changed')

    def test_unknown_cancel_blocks_entry_and_surviving_sibling(self):
        orders = self.pair()
        self.p.canceled('X',NOW)
        self.assertEqual(self.advice(allow_entry=True),'wait')
        self.assertEqual(self.advice(orders[1:]),'withdraw')

    def test_partial_fill_never_requeues_residual(self):
        orders=self.pair()
        orders[0].cumulative_filled,orders[0].remaining_quantity=D(7),D(8)
        self.assertEqual(self.advice(orders),'withdraw')

    def test_unsafe_account_and_wrong_generation_still_block(self):
        for safe,generation in ((False,(0,0)),(True,(1,0))):
            self.assertEqual(self.p.advice('X',now=NOW,generation=generation,orders=(),
                safe=safe,allow_entry=True),'withdraw')

    def test_replacement_starts_best_with_proof_and_no_inherited_anchor(self):
        self.pair()
        self.p.last_optional_refresh_at['X']=NOW
        self.p.sync_selected({'NEW','Y'})
        self.assertEqual(self.p.modes['NEW'],'best')
        self.assertIn('NEW',self.p.proof_required)
        self.assertNotIn('X',self.p.markets)
        self.assertNotIn('X',self.p.last_optional_refresh_at)

    def test_same_envelope_preserves_previous_breach_identity(self):
        self.assertEqual(policy().breach_key,selected.policy().breach_key)

    def test_expired_optimization_does_not_expire_best_bid_policy(self):
        self.assertFalse(policy().expired(NOW+timedelta(days=1)))
        self.assertTrue(selected.policy().expired(NOW+timedelta(days=1)))


class StayBestExecutionTests(fx.PilotIntegrationTests):
    """Repeat real send/fill/cancel/proof/HARD tests for the new policy."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.r.config=replace(self.r.config,strategy=replace(self.r.config.strategy,
            queue_pilot=policy(),quote_offset_ticks=0))
        self.r.queue_pilot=AdaptiveQueuePilot(policy())
        self.r.queue_pilot.sync_selected(selected.SYMBOLS)
        self.r.queue_pilot.observe_book(fx.book(1),generation=(0,0),at=NOW)
        self.r.queue_pilot.proof_required.clear()

    async def test_final_admission_rechecks_depth_before_second_leg(self):
        # Depth alone is explicitly no longer a send veto.
        self.on_second=lambda:self.r.queue_pilot.markets['X'].depths.update({Outcome.NO:D(1)})
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent),2)

    async def test_trade_event_wakes_fast_guard(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r.public.depth_changed.clear()
        self.r._observe_queue_pilot('X',frame=dict(s='X',t=1,p='.48',q='85',m=True,E=int(NOW.timestamp())*10**9))
        self.assertTrue(self.r.public.depth_changed.is_set())
        self.assertEqual(self.r._pilot_advice('X'),'keep')

    test_terminal_cancel_then_fresh_depth_proof_rejoins_with_new_anchors = adaptive.AdaptiveExecutionTests.test_terminal_cancel_then_fresh_depth_proof_rejoins_with_new_anchors

    install_width = selected.SelectedExecutionTests.install_width

    async def place_width(self):
        self.install_width()
        self.r.queue_pilot.proof_required.clear()
        for s in sorted(selected.SYMBOLS):
            self.r._pilot_proved[s]=(time.monotonic(),(0,0),self.r.account.fill_version)
            target,_=self.r._quote_target_state(s)
            await self.r._quote_pair_locked(replace(self.candidate(),symbol=s,quote=target))
        self.assertEqual(len(self.sent),40)

    test_twenty_pairs_real_guards_and_transport_below_existing_caps = selected.SelectedExecutionTests.test_twenty_pairs_real_guards_and_transport_below_existing_caps
    test_seven_of_fifteen_no_buyer_then_exact_exit_preserves_other_orders = selected.SelectedExecutionTests.test_seven_of_fifteen_no_buyer_then_exact_exit_preserves_other_orders
    test_full_width_orders_use_one_real_shared_rate_governor = selected.SelectedExecutionTests.test_full_width_orders_use_one_real_shared_rate_governor
    test_full_width_proof_requests_are_serialized = selected.SelectedExecutionTests.test_full_width_proof_requests_are_serialized

    async def test_unselected_reserve_cannot_skip_best_bid_proof(self):
        self.assertFalse(self.r._pilot_entry_ready('RESERVE'))

    async def test_depth_depletion_does_not_cancel_valid_working_pair(self):
        await self.r._quote_pair_locked(self.candidate())
        for a in self.r.queue_pilot.markets['X'].anchors.values(): a.scenario.trade(D(1))
        self.r._cancel_symbol_entries=AsyncMock()
        self.assertFalse(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_expired_queue_settings_still_place_best_bid_pair(self):
        self.r.queue_pilot.policy=replace(policy(),limits=replace(policy().limits,expires_at=NOW))
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent),2)
        self.assertTrue(all(p['params']['price']=='0.48' for p in self.sent))
        self.assertEqual(self.r._pilot_advice('X'),'keep')

    async def test_useful_optional_requeue_cancels_without_demotion(self):
        await self.r._quote_pair_locked(self.candidate())
        m=self.r.queue_pilot.markets['X']
        m.started_at=NOW-timedelta(seconds=70)
        m.depths[Outcome.YES]=D(600)
        self.r._cancel_symbol_entries=AsyncMock()
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.queue_pilot.modes['X'],'best')

    async def test_price_change_cancels_only_affected_pair(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r.queue_pilot.markets['X'].prices[Outcome.YES]=D('.49')
        self.r._cancel_symbol_entries=AsyncMock()
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')


class StayBestConfigTests(unittest.TestCase):
    def test_staging_requires_explicit_zero_offset_and_roundtrips_v7(self):
        active=json.loads(DEFAULT_POLICY_PATH.read_text())
        base={k:v for k,v in active.items() if k!='queue_pilot'}
        base['policy_version']=2
        review={'reviewed_at':NOW.isoformat(),'incumbent_symbols':['X'],
                'queue_pilot':active['queue_pilot']}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'base.json'
            path.write_text(json.dumps(base))
            candidate=build_candidate(path,review,now=NOW)
            self.assertEqual(candidate['policy_version'],7)
            output=Path(d)/'candidate.json'
            output.write_text(json.dumps(candidate))
            self.assertEqual(load_policy(output)[0].queue_pilot.placement_mode,'stay_best')
            self.assertEqual(load_policy(output)[1],load_policy(path)[1])
            base['quote_offset_ticks']=1
            path.write_text(json.dumps(base))
            with self.assertRaises(ConfigurationError): build_candidate(path,review,now=NOW)

    def test_candidate_default_is_explicit_zero_offset_size_fifteen(self):
        s,r=load_policy()
        self.assertEqual(s.quote_offset_ticks,0)
        self.assertEqual(s.queue_pilot.placement_mode,'stay_best')
        self.assertEqual(s.queue_pilot.same_price_refresh_minimum_seconds,D(300))
        self.assertEqual(s.queue_pilot.same_price_refresh_minimum_gain,D(500))
        self.assertEqual(s.queue_pilot.limits.minimum_requeue_gain,D(250))
        self.assertEqual(s.queue_pilot.limits.refresh_cooldown_seconds,D(10))
        self.assertEqual(s.quote_quantity,D(15))
        self.assertEqual(r.max_pending_orders,40)
        self.assertEqual(r.risk_window_loss_usd,D(30))

    def test_old_versions_cannot_gain_new_placement_and_new_cannot_fallback(self):
        base=json.loads(DEFAULT_POLICY_PATH.read_text())
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'policy.json'
            for update in ({'policy_version':5},{'policy_version':6},
                           {'quote_offset_ticks':1},{'quote_quantity':'20'}):
                path.write_text(json.dumps({**base,**update}))
                with self.assertRaises(ConfigurationError): load_policy(path)
            legacy=json.loads(DEFAULT_POLICY_PATH.read_text())
            legacy['policy_version']=6
            legacy['queue_pilot'].pop('same_price_refresh_minimum_seconds')
            legacy['queue_pilot'].pop('same_price_refresh_minimum_gain')
            path.write_text(json.dumps(legacy))
            self.assertEqual(load_policy(path)[0].queue_pilot.same_price_refresh_minimum_seconds,D(0))
            base=json.loads(DEFAULT_POLICY_PATH.read_text())
            base['queue_pilot'].pop('placement_mode')
            path.write_text(json.dumps(base))
            with self.assertRaises(ConfigurationError): load_policy(path)
