"""Synthetic authenticated-summary shapes; no network or live mutations."""
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from unittest.mock import patch

import test_competition_strategy as fx
from gemini_bot.reward_focus import RewardFocus, build_evidence
from gemini_bot.reward_ranking import rank_candidate, select_sticky_markets
from gemini_bot.reward_uptime_report import parse_reward_results

NOW = datetime(2026,9,19,22,tzinfo=timezone.utc)

def day(date='2026-09-19', status='PAID', events=None, paid_at=None):
    return parse_reward_results([dict(payout_date=date, payout_status=status,
        total_reward_usd='3', paid_at=paid_at or date+'T21:45:00+00:00',
        events=events if events is not None else [
            event('1','2',normalized='.02'), event('2','.01',normalized='.0001')])])[0]

def event(identity, reward, snapshots=1000, total=1440, normalized='.01'):
    return dict(event_id=identity, event_reward_usd=reward,
        snapshot_count=snapshots,total_snapshots=total,normalized_score=normalized)

def candidate(symbol, identity, cost='0'):
    return replace(rank_candidate(fx.item(symbol, symbol, cost=cost), now=fx.NOW), event_id=identity)

def choose(rows, focus, sticky=(), locked=(), maximum=1, blocked=()):
    return select_sticky_markets(rows, sticky_pool_ids=set(sticky), locked_pool_ids=set(locked),
        permitted_symbols={r.symbol for r in rows}, maximum_active_pools=maximum,
        actionable=lambda c:c.eligible_now and c.symbol not in blocked,
        entry_time_valid=lambda s:True, reward_focus=focus)

class EvidenceTests(unittest.TestCase):
    def test_recent_qualified_paid_observations_and_weighted_mean(self):
        result = build_evidence([day('2026-09-18'),day()],now=NOW)
        self.assertEqual(result['1'].qualified_days,2)
        self.assertEqual(result['1'].mean_reward_per_qualified_day,D(2))
        self.assertEqual(result['1'].confidence,D('.75'))

    def test_older_regime_has_lower_influence(self):
        result = build_evidence([day('2026-09-17',events=[event('1','100')]),
            day(events=[event('1','1')])],now=NOW)['1']
        self.assertLess(result.mean_reward_per_qualified_day,D(10))

    def test_unqualified_evidence_lowers_reliability_without_becoming_a_gate(self):
        result = build_evidence([day('2026-09-18',events=[event('1','0',100)]),day()],now=NOW)
        self.assertEqual(result['1'].mean_reward_per_qualified_day,D(2))
        self.assertLess(result['1'].qualification_reliability,D(1))
        weak = build_evidence([day(events=[event('3','0',100,1440,'0')])],now=NOW)['3']
        self.assertEqual(weak.qualified_days,0)
        self.assertEqual(weak.preference,0)
        self.assertGreater(weak.confidence,0)

    def test_normalized_score_rate_retains_precision_lost_by_rounded_reward(self):
        result = build_evidence([day(events=[
            event('1','0.00',1000,1440,'.002'),
            event('2','0.00',1000,1440,'.0002'),
        ])],now=NOW)
        self.assertEqual(result['1'].mean_reward_per_qualified_day,0)
        self.assertGreater(result['1'].preference,result['2'].preference)

    def test_pending_unknown_future_payment_and_zero_denominator_are_unused(self):
        for row in [day(status='PENDING'),day(status='UNKNOWN'),
            day(paid_at='2026-09-20T00:00:00+00:00'),
            day(events=[event('1','1',0,0)])]:
            with self.subTest(row=row):
                self.assertEqual(build_evidence([row],now=NOW),{})
        row=day(); row['paid_at']=None
        self.assertEqual(build_evidence([row],now=NOW),{})

    def test_old_below_threshold_is_earned_history_not_paid_cash(self):
        row=day('2026-09-18',status='BELOW_THRESHOLD')
        self.assertIsNone(row['paid_reward_usd'])
        self.assertIn('1',build_evidence([row],now=NOW))
        self.assertEqual(build_evidence([day(status='BELOW_THRESHOLD')],now=NOW),{})

    def test_bad_history_falls_back_atomically(self):
        for rows in [[day(),day()], [day(events=[event('1','2'),event('1','3')])]]:
            focus=RewardFocus(); focus.update(rows,now=NOW)
            self.assertEqual(focus.evidence,{})
            self.assertEqual(focus.error,'history_unavailable')
        row=day(); row['events'][0]['event_reward_usd']='NaN'
        focus=RewardFocus(); focus.update([row],now=NOW)
        self.assertEqual(focus.evidence,{})

    def test_future_duplicate_date_and_oversized_history_rejected(self):
        for rows in [[day('2026-09-20')],[day()]*15]:
            with self.assertRaises(ValueError): build_evidence(rows,now=NOW)

    def test_stale_history_becomes_neutral(self):
        self.assertEqual(build_evidence([day('2026-09-10')],now=NOW),{})

    def test_caching_and_daily_expiry_do_not_scan_each_rank(self):
        focus=RewardFocus(); rows=[day()]
        with patch('gemini_bot.reward_focus.build_evidence',wraps=build_evidence) as build:
            focus.update(rows,now=NOW); focus.update(rows,now=NOW)
            self.assertEqual(build.call_count,1)
            focus.update(rows,now=NOW+timedelta(days=8))
            self.assertEqual(build.call_count,2)
            self.assertEqual(focus.evidence,{})

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.focus=RewardFocus()
        self.focus.update([day('2026-09-18'),day()],now=NOW)
        self.good=candidate('Z_GOOD','1'); self.weak=candidate('A_WEAK','2')

    def test_equal_model_favors_proven_reward(self):
        chosen,_=choose([self.weak,self.good],self.focus)
        self.assertEqual(chosen[0].symbol,'Z_GOOD')
        self.assertIsNotNone(chosen[0].selection_focus_score_proxy)

    def test_healthy_sticky_owner_never_displaced_by_history(self):
        chosen,_=choose([self.weak,self.good],self.focus,sticky=[self.weak.owner_key])
        self.assertEqual([c.symbol for c in chosen],['A_WEAK'])

    def test_locked_owner_reserves_slot(self):
        chosen,owners=choose([self.weak,self.good],self.focus,locked=['held'])
        self.assertFalse(chosen); self.assertEqual(owners,{'held'})

    def test_missing_history_keeps_model_order(self):
        rows=[replace(self.weak,event_id='90'),replace(self.good,event_id='91')]
        self.assertEqual([c.symbol for c in choose(rows,self.focus)[0]],
                         [c.symbol for c in choose(rows,None)[0]])

    def test_negative_economics_and_other_gates_cannot_be_rescued(self):
        bad=replace(self.good,expected_execution_cost_usd=D('100000'))
        self.assertEqual(choose([bad,self.weak],self.focus)[0][0].symbol,'A_WEAK')
        self.assertEqual(choose([self.good,self.weak],self.focus,blocked=['Z_GOOD'])[0][0].symbol,'A_WEAK')

    def test_shared_event_positive_uplift_is_not_repeated(self):
        score=self.focus.score(self.good,D('.1'),[])
        self.assertGreater(score,D('.1'))
        self.assertEqual(self.focus.score(self.good,D('.1'),[self.good]),D('.1'))

    def test_local_below_threshold_is_a_soft_discount_not_a_ban(self):
        below=replace(self.good,best_possible_uptime=D('.49'))
        normal=replace(self.good,best_possible_uptime=D('.75'))
        discounted=self.focus.score(below,D('.2'),[])
        self.assertEqual(discounted,self.focus.score(normal,D('.2'),[])*D('.5'))
        self.assertGreater(discounted,0)

    def test_realized_data_can_outrank_large_unqualified_live_proxy(self):
        history=[]
        for date in ('2026-09-18','2026-09-19'):
            history.append(day(date,events=[
                event('15','2.73',1415,1440,'.0109405946'),
                event('258610','0',607,1440,'0'),
            ]))
        focus=RewardFocus(); focus.update(history,now=NOW)
        senate=replace(candidate('SENATE','15'),best_possible_uptime=D('.98'))
        zec=replace(candidate('ZEC','258610'),best_possible_uptime=D('.42'))
        self.assertGreater(focus.score(senate,D('.08'),[]),focus.score(zec,D('1'),[]))

    def test_ambiguous_current_event_mapping_is_neutral(self):
        conflict=replace(self.weak,event_id='1')
        selected,_=choose([conflict,self.good],self.focus)
        plain,_=choose([conflict,self.good],None)
        self.assertEqual([c.symbol for c in selected],[c.symbol for c in plain])

    def test_history_cannot_change_pool_forecast_or_eligibility(self):
        selected,_=choose([self.good,self.weak],self.focus,maximum=2)
        for result in selected:
            original=next(c for c in [self.good,self.weak] if c.symbol==result.symbol)
            self.assertEqual(result.expected_reward_usd,original.expected_reward_usd)
            self.assertEqual(result.reasons,original.reasons)
            self.assertEqual(result.own_daily_score_proxy,original.own_daily_score_proxy)

    def test_blend_change_bounded_to_fifty_percent_without_local_discount(self):
        for model in [D(0),D('.2'),D(1)]:
            for c in [self.good,self.weak]:
                c=replace(c,best_possible_uptime=D('.75'))
                self.assertLessEqual(abs(self.focus.score(c,model,[])-model),D('.50'))

    def test_report_does_not_claim_forecast_or_contract_dollars(self):
        report=self.focus.report()
        self.assertEqual(report['version'],2)
        self.assertEqual(report['history_weight'],'.50')
        self.assertTrue(report['local_discount_is_not_an_entry_gate'])
        self.assertFalse(report['forecast_dollars_calibrated'])
        self.assertFalse(report['contract_attribution_available'])

class RuntimeIntegrationTests(fx.OwnershipTests):
    async def test_runtime_uses_restored_evidence_only_for_vacant_slots(self):
        self.r._continuous_service=True
        self.r.exchange_reward_uptime.reward_results=[day('2026-09-18'),day()]
        self.r.ranked=[candidate('A_WEAK','2'),candidate('Z_GOOD','1')]
        self.r._permitted_symbols={c.symbol for c in self.r.ranked}
        self.r._entry_time_valid=lambda s:True
        with patch('gemini_bot.orchestrator._utc_now',return_value=NOW):
            chosen,_=self.r._select_with_reserved_owners(set(),1)
            self.assertEqual(chosen[0].symbol,'Z_GOOD')
            weak=self.r.ranked[0]
            self.r._sticky_pool_ids={weak.owner_key}
            chosen,_=self.r._select_with_reserved_owners(set(),1)
            self.assertEqual(chosen[0].symbol,'A_WEAK')
