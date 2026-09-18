"""All-selected lifecycle/full-width execution; synthetic I/O, no live defaults."""
import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_adaptive_queue as adaptive
import test_queue_pilot as fx
from gemini_bot.adaptive_queue import AdaptivePolicy, AdaptiveQueuePilot, parse_adaptive_policy
from gemini_bot.config import DEFAULT_POLICY_PATH, ConfigurationError, load_policy
from gemini_bot.domain import Outcome, EngineState, QuotePair
from gemini_bot.reconciliation import AccountSnapshot
from gemini_bot.stage_queue_pilot import build_candidate

NOW = fx.NOW
SYMBOLS = frozenset(['X', 'Y'] + [f'M{i:02}' for i in range(18)])


def policy():
    return AdaptivePolicy((), adaptive.policy().limits, 'all_selected')


def raw_policy():
    return {'scope': 'all_selected', 'adaptive': adaptive.raw_policy()['adaptive']}


class SelectedStateTests(unittest.TestCase):
    def setUp(self):
        self.p = AdaptiveQueuePilot(policy())
        self.p.sync_selected(SYMBOLS)

    def test_twenty_warm_independently_with_shared_rule(self):
        for s in SYMBOLS:
            adaptive.warm(self.p, s)
        self.p.evidence['X'].peak_flow[Outcome.YES] = D(100)
        for s in SYMBOLS:
            self.assertEqual(self.p.desired_mode(s, now=NOW, generation=(0, 0), safe=True),
                             'fallback' if s == 'X' else 'best')
        self.assertEqual(len(self.p.report()['evidence']), 20)

    def test_unselected_observations_cannot_expand_scope(self):
        b = fx.book(); b.symbol = 'RESERVE'
        self.p.observe_book(b, generation=(0, 0), at=NOW)
        self.assertNotIn('RESERVE', self.p.markets)
        self.assertEqual(self.p.desired_mode('RESERVE', now=NOW, generation=(0, 0), safe=True), 'fallback')
        self.assertEqual(self.p.symbols, SYMBOLS)

    def test_replacement_and_readmission_have_no_inherited_state(self):
        adaptive.warm(self.p)
        self.p.modes['X'] = 'best'
        self.p.anchor_send('X', Outcome.YES, 'old', D('.48'), NOW)
        self.p.canceled('X', NOW)
        self.p.sync_selected((SYMBOLS - {'X'}) | {'NEW'})
        for mapping in (self.p.evidence, self.p.markets, self.p.cooldown_at,
                        self.p.cancel_sequence, self.p.reasons, self.p.modes):
            self.assertNotIn('X', mapping)
        self.assertNotIn('X', self.p.proof_required)
        self.assertNotIn('X', self.p.settling)
        self.assertEqual(self.p.modes['NEW'], 'fallback')
        self.p.sync_selected(SYMBOLS)
        self.assertNotIn('X', self.p.evidence)
        self.assertEqual(self.p.modes['X'], 'fallback')

    def test_many_replacements_keep_every_collection_bounded(self):
        for i in range(200):
            names = (SYMBOLS - {'X'}) | {f'NEW{i}'}
            self.p.sync_selected(names)
            s = f'NEW{i}'
            adaptive.warm(self.p, s)
            self.p.fail(s, ValueError())
            self.p.canceled(s, NOW)
        for mapping in (self.p.evidence, self.p.markets, self.p.cooldown_at, self.p.cancel_sequence,
                        self.p.disabled, self.p.reasons, self.p.modes, self.p.proof_required, self.p.settling):
            self.assertLessEqual(len(mapping), 20)
            self.assertTrue(set(mapping) <= names)

    def test_scope_rejects_excess_and_malformed_without_partial_changes(self):
        for names in (SYMBOLS | {'EXCESS'}, {' x'}, {1}):
            with self.assertRaises(ValueError): self.p.sync_selected(names)
            self.assertEqual(self.p.symbols, SYMBOLS)

    def test_empty_scope_allowed_but_cannot_quote(self):
        self.p.sync_selected(())
        self.assertFalse(self.p.symbols)
        self.assertFalse(self.p.markets)
        self.assertEqual(self.p.desired_mode('X', now=NOW, generation=(0, 0), safe=True), 'fallback')

    def test_shared_cancel_breach_survives_membership_change(self):
        self.p.record_cancel(6, succeeded=True)
        key = self.p.policy.breach_key
        self.p.sync_selected({'NEW'})
        adaptive.warm(self.p, 'NEW')
        self.assertEqual(self.p.policy.breach_key, key)
        self.assertEqual(self.p.desired_mode('NEW', now=NOW, generation=(0, 0), safe=True), 'fallback')
        self.assertNotEqual(key, adaptive.policy().breach_key)

    def test_single_shard_generation_does_not_reset_other_markets(self):
        for s in SYMBOLS: adaptive.warm(self.p, s)
        b = fx.book(seq=2)
        self.p.observe_book(b, generation=(1, 0), at=NOW)
        self.assertEqual(self.p.desired_mode('X', now=NOW, generation=(1, 0), safe=True), 'fallback')
        for s in SYMBOLS - {'X'}:
            self.assertEqual(self.p.desired_mode(s, now=NOW, generation=(0, 0), safe=True), 'best')

    def trade(self, tid, at, qty='1', maker=True):
        self.p.observe_trade(dict(s='X', t=tid, p='.48', q=qty, m=maker,
            E=int(at.timestamp()*10**9)), generation=(0,0), at=at)

    def test_cached_flow_matches_reference_at_window_edges_and_duplicates(self):
        adaptive.warm(self.p)
        rows = []
        for i in range(301):
            at = NOW + timedelta(seconds=i/2)
            self.trade(i, at, qty=str(i%7+1), maker=bool(i%2))
            self.trade(i, at, qty='9999')  # duplicate ID has no weight
            rows.append((at, Outcome.YES if i%2 else Outcome.NO, D(i%7+1)))
            got = self.p.markets['X'].sell_flow(at)
            for side in Outcome:
                expected = sum((q for stamp,o,q in rows if o is side and (at-stamp).total_seconds()<=60), D(0))
                self.assertEqual(got[side], expected)
        got = self.p.markets['X'].sell_flow(NOW+timedelta(seconds=211))
        self.assertEqual(got, {o:D(0) for o in Outcome})

    def test_overflow_is_uncertain_with_bounded_exact_retained_sum(self):
        adaptive.warm(self.p)
        for i in range(4100): self.trade(i, NOW, maker=bool(i%2))
        m = self.p.markets['X']
        self.assertEqual(len(m.trades), 4096)
        self.assertEqual(m.sell_flow(NOW), {o:D(2048) for o in Outcome})
        self.assertEqual(self.p.desired_mode('X',now=NOW,generation=(0,0),safe=True), 'fallback')
        self.assertEqual(m.sell_flow(NOW+timedelta(seconds=61)), {o:D(0) for o in Outcome})

    def test_flow_clock_reversal_cannot_reuse_best_bid_permission(self):
        adaptive.warm(self.p)
        self.trade(1,NOW)
        self.assertEqual(self.p.desired_mode('X',now=NOW-timedelta(seconds=1),generation=(0,0),safe=True), 'fallback')
        with self.assertRaises(ValueError): self.trade(2,NOW-timedelta(seconds=1))


class SelectedConfigTests(unittest.TestCase):
    def test_explicit_version_five_roundtrip_keeps_every_base_limit(self):
        review = dict(reviewed_at=NOW.isoformat(), incumbent_symbols=[], queue_pilot=raw_policy())
        base = json.loads(DEFAULT_POLICY_PATH.read_text())
        base.pop('queue_pilot', None)
        base['policy_version'] = 2
        with tempfile.TemporaryDirectory() as d:
            base_path = Path(d)/'base.json'; base_path.write_text(json.dumps(base))
            value = build_candidate(base_path, review, now=NOW)
            self.assertEqual(value['policy_version'], 5)
            self.assertEqual({k:v for k,v in value.items() if k not in {'policy_version','queue_pilot'}},
                             {k:v for k,v in base.items() if k != 'policy_version'})
            path = Path(d)/'policy.json'; path.write_text(json.dumps(value))
            strategy, risk = load_policy(path)
            self.assertEqual(strategy.queue_pilot.scope, 'all_selected')
            self.assertEqual(risk, load_policy(base_path)[1])
            value['policy_version'] = 4; path.write_text(json.dumps(value))
            with self.assertRaises(ConfigurationError): load_policy(path)

    def test_research_policy_cannot_silently_become_all_selected(self):
        for raw in ({**raw_policy(), 'symbols':['X','Y']},
                    {**raw_policy(), 'scope':'all'}, {'adaptive':raw_policy()['adaptive']}):
            with self.assertRaises(ValueError): parse_adaptive_policy(raw)
        with self.assertRaises(ValueError): AdaptivePolicy(('X','Y'), policy().limits, 'all_selected')
        self.assertEqual(parse_adaptive_policy(adaptive.raw_policy()).scope, 'research')


class SelectedExecutionTests(adaptive.AdaptiveExecutionTests):
    """Repeat existing race/exit/unknown/HARD guards in all-selected mode too."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.r.config = replace(self.r.config, strategy=replace(self.r.config.strategy, queue_pilot=policy()))
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        self.r.queue_pilot.sync_selected(SYMBOLS)
        adaptive.warm(self.r.queue_pilot)
        self.r.queue_pilot.modes['X'] = 'best'

    async def test_cancel_breach_survives_restart_with_same_envelope(self):
        task = asyncio.create_task(asyncio.sleep(0)); await task
        self.r._queue_cancel_done(time.monotonic()-6, task)
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        self.r.queue_pilot.sync_selected({'NEW'})
        self.r._adaptive_breach_saved = False
        self.r._restore_adaptive_breach()
        self.assertTrue(self.r.queue_pilot.cancel_envelope_breached)
        self.assertGreater(self.r.queue_pilot.cancel_maximum, 5)

    async def test_corrupted_durable_breach_never_reenables(self):
        self.r.journal.set_runtime_state(policy().breach_key, '{"breached": false}', updated_at=NOW)
        with self.assertRaises(RuntimeError): self.r._restore_adaptive_breach()

    def install_width(self):
        original = self.r.universe[0]
        self.r.universe = []
        for s in sorted(SYMBOLS):
            self.r.universe.append(replace(original, event_ticker=f'E{s}',
                pool=replace(original.pool, pool_id=f'P{s}'), rules=replace(original.rules, symbol=s)))
            b = fx.book(); b.symbol = s
            self.r.public.books[s] = b
            self.r._expiry[s] = NOW + timedelta(hours=2)
            if s not in self.r.queue_pilot.evidence:
                adaptive.warm(self.r.queue_pilot, s)
            self.r.queue_pilot.modes[s] = 'best'
        self.r._permitted_symbols = set(SYMBOLS)
        self.r._sticky_markets = {c.owner_key:(c.event_ticker,c.rules.symbol) for c in self.r.universe}
        self.r.account.available_usd = D(1000)

    async def place_width(self):
        self.install_width()
        for s in sorted(SYMBOLS):
            self.r._pilot_proved[s] = (time.monotonic(), (0,0), self.r.account.fill_version)
            target, _ = self.r._quote_target_state(s)
            c = replace(self.candidate(), symbol=s, quote=target)
            await self.r._quote_pair_locked(c)
        self.assertEqual(len(self.sent), 40)

    async def test_twenty_pairs_real_guards_and_transport_below_existing_caps(self):
        await self.place_width()
        exposure = self.r._websocket_exposure()
        self.assertEqual(exposure.pending_orders, 40)
        self.assertEqual(exposure.gross_reservation_usd, D(288))
        self.assertTrue(all(p['params']['timeInForce']=='MOC' and p['params']['quantity']=='15'
                            and p['params']['price']=='0.48' for p in self.sent))
        for s in SYMBOLS:
            self.assertEqual(set(self.r.queue_pilot.markets[s].anchors), set(Outcome))
            self.assertFalse(await self.r._cancel_too_aggressive_quote(s))

    async def test_full_width_orders_use_one_real_shared_rate_governor(self):
        from test_ws_rate_limits import limits
        self.r.private._governor = self.r.governor
        self.r.governor.observe_websocket_limits('private_ws',limits(),now=time.monotonic())
        self.assertIs(self.r.private._governor, self.r.public._governor)
        async def account_stream():
            while True:
                self.r.account.apply(dict(e='positionReport',P=[],u=self.r.account.reducer.position_update_ns+1))
                self.r.account.apply(dict(e='balanceUpdate',B=[{'a':'USD','c':'100','f':'1000'}]))
                await asyncio.sleep(.25)
        stream = asyncio.create_task(account_stream())
        try:
            await self.place_width()
        finally:
            stream.cancel()
            await asyncio.gather(stream,return_exceptions=True)
        self.assertEqual(self.r.governor.ws_buckets[('ORDERS',86400.0)].used,40)
        self.assertEqual(self.r.governor.ws_buckets[('REQUEST_WEIGHT',10.0)].used,4000)

    async def test_one_market_evidence_failure_preserves_other_thirty_eight_orders(self):
        await self.place_width()
        async def cancel(oid):
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = AsyncMock(side_effect=cancel)
        self.r.queue_pilot.fail('X', ValueError())
        await self.r._cancel_too_aggressive_quote('X')
        self.assertEqual(self.r.private.cancel.await_count, 2)
        self.assertEqual(len([o for o in self.r.account.reducer.orders.values() if o.working]), 38)
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_scope_change_waits_for_orders_then_clears_old_proofs(self):
        await self.place_width()
        owner = next(k for k,v in self.r._sticky_markets.items() if v[1]=='X')
        self.r._sticky_markets.pop(owner)
        self.r._sticky_markets['newowner'] = ('ENEW','NEW')
        self.r._permitted_symbols.add('NEW')
        self.r._sync_adaptive_scope()
        self.assertIn('X', self.r.queue_pilot.symbols)
        self.assertNotIn('NEW', self.r.queue_pilot.symbols)
        async def cancel(oid):
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = cancel
        await self.r._cancel_symbol_entries('X')
        self.r._sync_adaptive_scope()
        self.assertNotIn('X', self.r.queue_pilot.symbols)
        self.assertNotIn('X', self.r._pilot_proved)
        self.assertIn('NEW', self.r.queue_pilot.symbols)
        self.assertEqual(self.r.queue_pilot.modes['NEW'], 'fallback')
        self.assertNotIn('NEW', self.r.queue_pilot.evidence)

    async def test_only_selected_not_all_watched_are_members(self):
        self.r.queue_pilot.sync_selected(())
        self.r._sticky_markets = {'owner':('E','X')}
        self.r._permitted_symbols = set(SYMBOLS) | {'RESERVE'}
        self.r._sync_adaptive_scope()
        self.assertEqual(self.r.queue_pilot.symbols, {'X'})
        self.assertFalse(self.r._pilot_at_touch('X'))

    async def test_full_width_proof_requests_are_serialized(self):
        self.install_width()
        entered, release = asyncio.Event(), asyncio.Event()
        async def snapshot(symbol):
            entered.set()
            await release.wait()
            return AccountSnapshot((), ())
        self.r._exit_account_snapshot = AsyncMock(side_effect=snapshot)
        self.r._pilot_proved.clear()
        for s in sorted(SYMBOLS): self.assertFalse(self.r._pilot_entry_ready(s))
        await entered.wait()
        self.assertEqual(self.r._exit_account_snapshot.await_count, 1)
        self.assertEqual(len(self.r._pilot_proof_tasks), 1)
        release.set()
        await asyncio.gather(*self.r._pilot_proof_tasks.values())
        for s in sorted(SYMBOLS): self.r._pilot_entry_ready(s)
        await asyncio.sleep(0)
        self.assertEqual(self.r._exit_account_snapshot.await_count, 2)
        await asyncio.gather(*self.r._pilot_proof_tasks.values())

    async def test_failed_retiring_proof_is_not_swallowed(self):
        async def fail(): raise RuntimeError('synthetic account ambiguity')
        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        self.r._pilot_proof_tasks['X'] = task
        with self.assertRaisesRegex(RuntimeError, 'account ambiguity'): self.r._sync_adaptive_scope()
        self.r._pilot_proof_tasks.clear()

    async def test_inventory_or_deadline_prevents_scope_eviction(self):
        self.r._sticky_markets = {}
        self.r.account.reducer.positions['X'] = D(7)
        self.r._sync_adaptive_scope()
        self.assertIn('X', self.r.queue_pilot.symbols)
        self.r.account.reducer.positions.clear()
        self.r.journal.persist_deadline('X','YES',NOW,NOW+timedelta(minutes=5),D(7))
        self.r._sync_adaptive_scope()
        self.assertIn('X', self.r.queue_pilot.symbols)

    async def test_activation_requires_catalog_v7_and_current_envelope(self):
        from gemini_bot.activation import ActivationPermit
        self.r.config = replace(self.r.config, live_acknowledgement='I_UNDERSTAND_THIS_TRADES_REAL_MONEY')
        self.r.gate.state = EngineState.READY
        base = ActivationPermit('synthetic',NOW,None,20,'','','',('X',),
                                universe_scope='reward_catalog',permit_version=7)
        for permit in (replace(base,permit_version=6),replace(base,universe_scope=None)):
            with patch('gemini_bot.orchestrator.load_activation_permit',return_value=permit), \
                 patch('gemini_bot.orchestrator.consume_activation_permit') as consume:
                with self.assertRaisesRegex(RuntimeError,'scope'):
                    await self.r.activate(self.r.directory/'synthetic')
                consume.assert_not_called()
        expired = replace(policy(), limits=replace(policy().limits,expires_at=NOW))
        self.r.queue_pilot = AdaptiveQueuePilot(expired)
        with patch('gemini_bot.orchestrator.load_activation_permit',return_value=base), \
             patch('gemini_bot.orchestrator.consume_activation_permit') as consume:
            with self.assertRaisesRegex(RuntimeError,'expired'):
                await self.r.activate(self.r.directory/'synthetic')
            consume.assert_not_called()

    async def test_valid_activation_binds_owners_before_authority_consumption(self):
        from gemini_bot.activation import ActivationPermit
        self.install_width()
        self.r.queue_pilot = AdaptiveQueuePilot(policy())
        self.r.config = replace(self.r.config, live_acknowledgement='I_UNDERSTAND_THIS_TRADES_REAL_MONEY')
        self.r.gate.state = EngineState.READY
        permit = ActivationPermit('synthetic',NOW,None,20,'','','',(),
                                  universe_scope='reward_catalog',permit_version=7)
        with patch('gemini_bot.orchestrator.load_activation_permit',return_value=permit), \
             patch('gemini_bot.orchestrator.consume_activation_permit',side_effect=RuntimeError('synthetic boundary')) as consume:
            with self.assertRaisesRegex(RuntimeError,'synthetic boundary'):
                await self.r.activate(self.r.directory/'synthetic')
            consume.assert_called_once()
        self.assertEqual(self.r.queue_pilot.symbols,SYMBOLS)
        self.assertTrue(all(v=='fallback' for v in self.r.queue_pilot.modes.values()))
        self.assertFalse(self.sent)

    async def test_seven_of_fifteen_no_buyer_then_exact_exit_preserves_other_orders(self):
        await self.place_width()
        r = self.r
        order = next(o for o in r.account.reducer.orders.values() if o.symbol=='X' and o.outcome is Outcome.YES)
        r.account.apply(dict(e='orderUpdate', i=order.order_id, c=order.client_order_id, s='X',
            S='BUY', O='YES', p='.48', q='15', z='8', Z='7', X='PARTIALLY_FILLED',
            t='synthetic-partial', L='.48', n='0', T=int((NOW-timedelta(seconds=301)).timestamp()*1000)))
        def sync():
            a=r.account
            a.apply(dict(e='positionReport',u=a.reducer.position_update_ns+1,P=[
                {'t':'ec','s':s,'a':[{'t':'position','v':str(q)}]} for s,q in a.expected_positions.items()]))
            a.apply(dict(e='balanceUpdate',B=[{'a':'USD','c':str(a.expected_confirmed_usd),'f':'100'}]))
        sync()
        deadline = r.journal.get_deadline('X','YES')
        self.assertIsNotNone(deadline)
        async def cancel(oid):
            o=r.account.reducer.orders[oid]
            r.account.apply(dict(e='orderUpdate',i=oid,X='CANCELED',z='0',Z=str(o.cumulative_filled),
                                 T=int(NOW.timestamp()*1000)))
        r.private.cancel=AsyncMock(side_effect=cancel)
        await r._cancel_symbol_entries('X')
        r.rest.positions=AsyncMock(side_effect=lambda:[dict(symbol=s,outcome='YES' if q>0 else 'NO',
            totalQuantity=str(abs(q)),avgPrice='.48',marketValue=str(abs(q)*D('.48')))
            for s,q in r.account.reducer.positions.items() if q])
        r.rest.active_orders=AsyncMock(side_effect=lambda:[dict(orderId=o.order_id,clientOrderId=o.client_order_id,
            symbol=o.symbol,side=o.side.value,outcome=o.outcome.value,quantity=str(o.original_quantity),
            remainingQuantity=str(o.remaining_quantity),price=str(o.price),status='NEW')
            for o in r.account.reducer.orders.values() if o.working])
        r.public.books['X'].bids.clear()
        await r._run_exit_worker('X')
        self.assertEqual(r._exit_waits['X'].reason,'exit_depth_unavailable')
        self.assertEqual(r.journal.get_deadline('X','YES'),deadline)
        self.assertTrue(r._isolated_exit_proof_current('X'))
        self.assertEqual(len([o for o in r.account.reducer.orders.values() if o.working]),38)
        self.assertEqual(r._websocket_exposure().pending_orders,38)
        r.public.books['X']=fx.book(seq=2)
        async def sell(raw):
            p=json.loads(raw)['params']
            self.assertEqual((p['side'],p['eventOutcome'],D(p['quantity'])),('SELL','YES',D(7)))
            r.account.apply(dict(e='orderUpdate',i='synthetic-exit',c=p['clientOrderId'],s='X',
                S='SELL',O='YES',p=p['price'],q='7',z='0',Z='7',X='FILLED',t='synthetic-exit-fill',
                L=p['price'],n='0',T=int(NOW.timestamp()*1000)))
            sync()
        r.private._connection.send=sell
        await r._run_exit_worker('X')
        self.assertFalse(r.account.reducer.positions)
        self.assertIsNone(r.journal.get_deadline('X','YES'))
        self.assertNotIn('X',r.account.blocked_entries)
        self.assertFalse(r.journal.pending_exit('X'))
        self.assertEqual(len([o for o in r.account.reducer.orders.values() if o.working]),38)
        self.assertEqual(r.gate.state,EngineState.QUOTING)
