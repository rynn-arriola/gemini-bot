"""Live-path regressions. All account/transport I/O is synthetic and offline."""
import asyncio
import json
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_repair_integration as fx
from gemini_bot.domain import Outcome, Side, EngineState, QuoteLeg, QuotePair
from gemini_bot.execution import OrderPurpose
from gemini_bot.guarded_bid import QueueCalibration
from gemini_bot.queue_pilot import PilotPolicy, QueuePilot, parse_pilot_policy
from gemini_bot.market_state import LocalBook
from gemini_bot.account_state import AccountReducer
from gemini_bot.rank_report import rank_live_universe
from gemini_bot.reconciliation import AccountSnapshot
from gemini_bot.config import load_policy, ConfigurationError, DEFAULT_POLICY_PATH

NOW = fx.NOW


def policy():
    c = QueueCalibration(D(50), D(20), D(30), D(100), D(5), NOW + timedelta(hours=1), "SYNTHETIC-TEST-ONLY")
    return PilotPolicy((("X", c, c), ("Y", c, c)))


def book(qty=100, seq=1, bid=".48", ask=".52"):
    b = LocalBook("X")
    b.apply_snapshot([(D(bid), D(qty))], [(D(ask), D(qty))], seq, seq, NOW)
    return b


def order(outcome=Outcome.YES, client_id="c", price=D(".48"), quantity=D(15)):
    r = AccountReducer()
    r.apply_order_update(dict(e="orderUpdate", i=outcome.value, c=client_id, s="X", S="BUY", O=outcome.value,
                             p=str(price), q=str(quantity), z=str(quantity), X="NEW"))
    return r.orders[outcome.value]


class PilotStateTests(unittest.TestCase):
    def setUp(self):
        self.p = QueuePilot(policy())
        self.p.observe_book(book(), generation=(0, 0), at=NOW - timedelta(seconds=61))
        self.p.proof_required.clear()

    def advice(self, orders=(), **kwargs):
        return self.p.advice("X", now=NOW, generation=(0, 0), orders=orders, safe=True, **kwargs)

    def anchor(self, outcome=Outcome.YES):
        self.p.anchor_send("X", outcome, "c", D(".48"), NOW - timedelta(seconds=10))
        return order(outcome)

    def trade(self, qty, maker=True, tid=1):
        self.p.observe_trade(dict(s="X", t=tid, p=".48" if maker else ".52", q=str(qty), m=maker,
                                  E=int(NOW.timestamp()) * 10**9), generation=(0, 0), at=NOW)

    def test_initial_join_requires_proof_and_warmup(self):
        self.assertEqual(self.advice(allow_entry=True), "allow")
        self.p.proof_required.add("X")
        self.assertEqual(self.advice(allow_entry=True), "wait")
        self.p.markets["X"].started_at = NOW
        self.assertEqual(self.advice(allow_entry=True), "withdraw")

    def test_working_order_without_actual_anchor_withdraws(self):
        self.assertEqual(self.advice([order()]), "withdraw")

    def test_healthy_actual_anchor_is_kept(self):
        self.assertEqual(self.advice([self.anchor()]), "keep")

    def test_configured_thirty_is_exact_for_queue_validation(self):
        pilot = QueuePilot(policy(), D(30))
        pilot.observe_book(book(), generation=(0, 0), at=NOW - timedelta(seconds=61))
        pilot.proof_required.clear()
        pilot.anchor_send("X", Outcome.YES, "c", D(".48"), NOW - timedelta(seconds=10))
        self.assertEqual(pilot.advice("X", now=NOW, generation=(0, 0),
            orders=[order(quantity=D(30))], safe=True), "keep")
        self.assertEqual(pilot.advice("X", now=NOW, generation=(0, 0),
            orders=[order(quantity=D(15))], safe=True), "withdraw")

    def test_new_depth_does_not_move_existing_anchor_back(self):
        o = self.anchor()
        self.p.observe_book(book(615, 2), generation=(0, 0), at=NOW, orders=[o])
        a = self.p.markets["X"].anchors[Outcome.YES].scenario
        self.assertEqual(a.ahead_scenario, D(100))
        self.assertEqual(a.external_depth, D(600))
        self.trade(85)
        self.assertEqual(self.advice([o]), "withdraw")
        self.assertEqual(self.p.reasons["X"], "estimated_queue_refresh_benefit")

    def test_no_outcome_uses_complement_and_correct_aggressor(self):
        o = self.anchor(Outcome.NO)
        self.trade(85, maker=False)
        self.assertEqual(self.advice([o]), "withdraw")
        self.assertEqual(self.p.markets["X"].anchors[Outcome.NO].scenario.ahead_scenario, D(15))

    def test_duplicate_trade_cannot_double_deplete(self):
        self.anchor()
        self.trade(10)
        self.trade(10)
        self.assertEqual(self.p.markets["X"].anchors[Outcome.YES].scenario.ahead_scenario, D(90))

    def test_best_price_move_never_transplants_anchor(self):
        o = self.anchor()
        self.p.observe_book(book(115, 2, bid=".49"), generation=(0, 0), at=NOW)
        self.assertEqual(self.p.markets["X"].anchors[Outcome.YES].scenario.price, D(".48"))
        self.assertEqual(self.advice([o]), "withdraw")

    def test_generation_change_drops_anchor_and_requires_new_proof(self):
        self.anchor()
        self.p.observe_book(book(seq=2), generation=(1, 0), at=NOW)
        self.assertFalse(self.p.markets["X"].anchors)
        self.assertIn("X", self.p.proof_required)
        self.assertEqual(self.advice([order()]), "withdraw")

    def test_public_private_skew_is_not_external_depth(self):
        self.p.observe_book(book(10, 2), generation=(0, 0), at=NOW, orders=[self.anchor()])
        self.assertNotIn("X", self.p.markets)

    def test_cancel_requires_proof_and_cooldown(self):
        self.anchor()
        self.p.canceled("X", NOW)
        self.assertFalse(self.p.markets["X"].anchors)
        self.assertEqual(self.advice(), "wait")
        self.p.proof_required.clear()
        self.assertEqual(self.advice(), "wait")

    def test_seven_of_fifteen_never_requeues(self):
        o = self.anchor()
        o.cumulative_filled, o.remaining_quantity = D(7), D(8)
        self.assertEqual(self.advice([o]), "withdraw")

    def test_expired_calibration_does_not_join(self):
        self.p.policy = PilotPolicy(tuple((s, replace(y, expires_at=NOW), n) for s, y, n in policy().markets))
        self.assertEqual(self.advice(), "wait")

    def test_missing_trade_time_withdraws_then_recovers_after_full_window(self):
        self.p.observe_trade(dict(s="X", t=1, p=".48", q="1", m=True), generation=(0, 0), at=NOW)
        self.assertEqual(self.advice(), "withdraw")
        self.assertEqual(self.p.advice("X", now=NOW + timedelta(seconds=61), generation=(0, 0),
                                      orders=[], safe=True, allow_entry=True), "allow")

    def test_failure_local_and_redacted(self):
        self.p.fail("X", ValueError("secret"))
        self.assertEqual(self.p.disabled, {"X": "ValueError"})
        self.assertNotIn("secret", str(self.p.report()))

    def test_policy_rejects_unbounded_duplicate_or_uncalibrated_scope(self):
        c = policy().markets[0][1]
        for rows in ((('X', c, c),), (('X', c, c), ('X', c, c)), (('X', None, c), ('Y', c, c))):
            with self.assertRaises(ValueError):
                PilotPolicy(rows)
        with self.assertRaises(ValueError):
            parse_pilot_policy({"enabled": True})


class PilotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = fx.RuntimeRepairTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.runtime
        self.r.rest._open_json = Mock(side_effect=AssertionError('unexpected network in offline queue test'))
        self.r.config = replace(self.r.config, strategy=replace(self.r.config.strategy, quote_quantity=D(15),
            quote_offset_ticks=getattr(self, 'nonpilot_offset', 2), queue_pilot=policy()))
        self.r.queue_pilot = QueuePilot(policy())
        self.r.queue_pilot.observe_book(book(), generation=(0, 0), at=NOW - timedelta(seconds=61))
        self.r.queue_pilot.proof_required.clear()
        self.r._pilot_proved['X'] = (time.monotonic(), (0, 0), self.r.account.fill_version)
        self.r._continuous_service = True
        self.sent = []
        self.on_second = lambda: None
        self.admissions = 0
        def admit(*args):
            self.admissions += 1
            if self.admissions == 2:
                self.on_second()
            return 0
        async def send(raw):
            payload = json.loads(raw)
            self.sent.append(payload)
            p = payload['params']
            server_id = p['eventOutcome'] if len(self.sent) <= 2 else p['eventOutcome'] + str(len(self.sent))
            self.r.account.apply(dict(e='orderUpdate', i=server_id, c=p['clientOrderId'], s=p['symbol'],
                                     S='BUY', O=p['eventOutcome'], p=p['price'], q=p['quantity'], z=p['quantity'], X='NEW'))
        self.r.private._connection = SimpleNamespace(send=send, close=AsyncMock())
        self.r.private._governor = SimpleNamespace(admit_websocket=admit)

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    def candidate(self):
        return replace(fx.candidate(D(15)), quote=QuotePair(
            QuoteLeg('X', Outcome.YES, Side.BUY, D('.48'), D(15)),
            QuoteLeg('X', Outcome.NO, Side.BUY, D('.48'), D(15))))

    async def test_actual_transport_sends_moc_at_best_bid_and_anchors_both(self):
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(p['params']['timeInForce'] == 'MOC' for p in self.sent))
        self.assertEqual(set(self.r.queue_pilot.markets['X'].anchors), {Outcome.YES, Outcome.NO})
        self.assertEqual(self.r._pilot_advice('X'), 'keep')

    async def test_final_admission_rechecks_depth_before_second_leg(self):
        self.r._cancel_symbol_entries = AsyncMock()
        self.on_second = lambda: self.r.queue_pilot.markets['X'].depths.update({Outcome.NO: D(1)})
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 1)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_seven_fill_during_second_send_blocks_and_keeps_deadline(self):
        def fill():
            o = self.r.account.reducer.orders['YES']
            self.r.account.apply(dict(e='orderUpdate', i='YES', c=o.client_order_id, s='X', S='BUY', O='YES',
                                     p='.48', q='15', z='8', Z='7', X='PARTIALLY_FILLED', t='f', L='.48', n='0',
                                     T=int(NOW.timestamp() * 1000)))
        self.r._cancel_symbol_entries = AsyncMock()
        self.on_second = fill
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 1)
        self.assertIsNotNone(self.r.journal.get_deadline('X', 'YES'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_expired_proof_during_pacing_prevents_second(self):
        self.r._cancel_symbol_entries = AsyncMock()
        self.on_second = lambda: self.r._pilot_proved.update(X=(time.monotonic()-4, (0, 0), 0))
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 1)

    async def test_spontaneous_terminal_cannot_reuse_old_flat_proof(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r.account.apply(dict(e='orderUpdate', i='YES', X='CANCELED', z='0', Z='0'))
        self.assertIn('X', self.r.queue_pilot.proof_required)
        self.assertNotIn('X', self.r._pilot_proved)
        self.assertEqual(self.r._pilot_advice('X'), 'withdraw')

    async def test_pilot_market_issue_cancels_only_it(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r._cancel_symbol_entries = AsyncMock()
        self.r.queue_pilot.fail('X', ValueError())
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_trade_event_wakes_fast_guard(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r.public.depth_changed.clear()
        self.r._observe_queue_pilot('X', frame=dict(s='X', t=1, p='.48', q='85', m=True, E=int(NOW.timestamp())*10**9))
        self.assertTrue(self.r.public.depth_changed.is_set())
        self.assertEqual(self.r.public.adverse_dirty.pop(), 'X')
        self.assertEqual(self.r._pilot_advice('X'), 'withdraw')

    async def test_cancel_terminal_proof_and_cooldown_not_same_cycle_replace(self):
        await self.r._quote_pair_locked(self.candidate())
        async def cancel(oid):
            o = self.r.account.reducer.orders[oid]
            self.r.account.apply(dict(e='orderUpdate', i=oid, c=o.client_order_id, s='X', S='BUY', O=o.outcome.value,
                                     p='.48', q='15', z='0', Z='0', X='CANCELED'))
        self.r.private.cancel = cancel
        await self.r._cancel_symbol_entries('X')
        self.assertIn('X', self.r.queue_pilot.proof_required)
        self.assertNotIn('X', self.r._pilot_proved)
        self.assertFalse(self.r.queue_pilot.markets['X'].anchors)
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        await self.r._pilot_flat_proof('X')
        self.assertNotIn('X', self.r.queue_pilot.proof_required)
        self.assertFalse(self.r._pilot_entry_ready('X'))  # Cooldown still wins.
        self.assertEqual(len(self.sent), 2)

    async def test_proof_generation_change_or_fill_does_not_release(self):
        async def proof(symbol):
            self.r.account.fill_version += 1
            return AccountSnapshot((), ())
        self.r.queue_pilot.proof_required.add('X')
        self.r._exit_account_snapshot = proof
        await self.r._pilot_flat_proof('X')
        self.assertIn('X', self.r.queue_pilot.proof_required)

    async def test_shadow_failure_cannot_stale_live_order_cache(self):
        self.r.queue_shadow.disable(ValueError())
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.r._queue_live_orders), 2)

    async def test_ranking_offset_and_reward_scoring_are_per_symbol(self):
        contracts = self.r.universe + [replace(self.r.universe[0], rules=replace(self.r.universe[0].rules, symbol='OTHER'))]
        other = book(); other.symbol = 'OTHER'
        offset = self.r.config.strategy.quote_offset_ticks
        ranked = self.r._rank_live_universe(contracts, {'X': book(), 'OTHER': other}, now=NOW, quantity=D(15), offset_ticks=offset)
        by_symbol = {c.symbol: c for c in ranked}
        self.assertEqual(by_symbol['X'].quote.yes.price, D('.48'))
        self.assertEqual(by_symbol['OTHER'].quote.yes.price, D('.48') - D('.01') * offset)
        self.assertEqual(by_symbol['X'].scoring_yes_price, D('.48'))

    async def test_halted_worker_failure_is_not_silently_closed(self):
        async def failed():
            self.r.gate.halt()
            raise TimeoutError('synthetic cancellation proof timeout')
        task = asyncio.create_task(failed())
        await asyncio.sleep(0)
        self.r._shard_restarts[0] = task
        with self.assertRaises(TimeoutError):
            self.r._check_background_failures()
        self.r._shard_restarts.clear()

    async def test_failed_cancel_stays_latched_and_never_replaced(self):
        await self.r._quote_pair_locked(self.candidate())
        self.r.private.cancel = AsyncMock(side_effect=TimeoutError('unknown cancel'))
        with self.assertRaises(TimeoutError):
            await self.r._cancel_symbol_entries('X')
        with self.assertRaises(TimeoutError):
            await self.r._cancel_symbol_entries('X')
        self.assertEqual(self.r.private.cancel.await_count, 2)  # Once per old order, never replayed.
        self.assertIn('X', self.r.queue_pilot.proof_required)
        self.assertEqual(len(self.sent), 2)

    async def test_terminal_cancel_then_fresh_depth_proof_rejoins_with_new_anchors(self):
        await self.r._quote_pair_locked(self.candidate())
        async def cancel(oid):
            self.r.account.apply(dict(e='orderUpdate', i=oid, X='CANCELED', z='0', Z='0'))
        self.r.private.cancel = cancel
        old_clients = {o.client_order_id for o in self.r.account.reducer.orders.values()}
        await self.r._cancel_symbol_entries('X')
        self.r._exit_account_snapshot = AsyncMock(return_value=AccountSnapshot((), ()))
        await self.r._pilot_flat_proof('X')
        self.r.queue_pilot.cooldown_at['X'] = NOW - timedelta(seconds=6)
        self.assertFalse(self.r._pilot_entry_ready('X'))  # Old depth still blocks.
        self.r.queue_pilot.observe_book(book(seq=2), generation=(0, 0), at=NOW)
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(all(a.client_id not in old_clients for a in self.r.queue_pilot.markets['X'].anchors.values()))

    async def test_real_activation_loop_surfaces_worker_halt_before_normal_close(self):
        await self.activation_worker_failure(hard=False)

    async def test_worker_timeout_at_hard_never_grants_restart(self):
        await self.activation_worker_failure(hard=True)

    async def activation_worker_failure(self, *, hard):
        # Exercise the actual activation loop, not only the helper. No network.
        self.r.queue_pilot = None
        self.r.config = replace(self.r.config, live_acknowledgement='I_UNDERSTAND_THIS_TRADES_REAL_MONEY',
                                strategy=replace(self.r.config.strategy, queue_pilot=None))
        self.r.gate.state = EngineState.READY
        self.r._risk_mark = AsyncMock()
        self.r._prepare_activation_inventory = AsyncMock()
        self.r.drain = AsyncMock()
        self.r._write_status = lambda *a, **kw: None
        self.r._advance_reward_epoch = lambda *a: False
        self.r._exit_due_positions = AsyncMock()
        self.r._maintain_candidate = AsyncMock()
        async def halted_worker():
            if hard:
                self.r._dynamic_pending_cap = 0
            self.r.gate.halt()
            raise TimeoutError('synthetic worker failure between iterations')
        self.r._live_safety_clock = halted_worker
        self.r._reward_reporting_loop = AsyncMock()
        from gemini_bot.activation import ActivationPermit
        permit = ActivationPermit('synthetic', NOW-timedelta(seconds=1), None, 20, '', '', '', (),
                                  universe_scope='reward_catalog', permit_version=7, supervised_restart_limit=2)
        with patch('gemini_bot.orchestrator.load_activation_permit', return_value=permit), \
             patch('gemini_bot.orchestrator.consume_activation_permit'), \
             patch('gemini_bot.orchestrator.finish_supervised_activation') as finish:
            with self.assertRaisesRegex(TimeoutError, 'synthetic worker failure'):
                await self.r.activate(self.r.directory / 'not-a-real-permit')
            self.assertEqual(finish.call_args.kwargs['outcome'], 'attention' if hard else 'recoverable_flat')
            self.assertEqual(self.r._recoverable_exit, not hard)


class OneTickPilotIntegrationTests(PilotIntegrationTests):
    """Repeat the complete real execution/guard suite with nonpilot offset one."""
    nonpilot_offset = 1

    async def test_nonpilot_pair_uses_one_tick_while_pilot_evidence_is_missing(self):
        other = replace(self.r.universe[0], rules=replace(self.r.universe[0].rules, symbol='OTHER'))
        self.r.universe.append(other)
        other_book = book(); other_book.symbol = 'OTHER'
        self.r.public.books['OTHER'] = other_book
        self.r._expiry['OTHER'] = self.r._expiry['X']
        self.r._permitted_symbols.add('OTHER')
        self.r.queue_pilot.invalidate('X')
        target, _ = self.r._quote_target_state('OTHER')
        candidate = replace(fx.candidate(D(15)), symbol='OTHER', quote=target)
        await self.r._quote_pair_locked(candidate)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(p['params']['price'] == '0.47' for p in self.sent))
        self.assertNotIn('OTHER', self.r.queue_pilot.markets)
        self.assertFalse(await self.r._cancel_too_aggressive_quote('OTHER'))
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)


class PilotConfigTests(unittest.TestCase):
    def test_version_three_calibration_is_strict_and_version_two_unchanged(self):
        import tempfile
        from pathlib import Path
        from dataclasses import asdict
        raw = json.loads(DEFAULT_POLICY_PATH.read_text())
        # This is the earlier two-tick nonpilot/v3 fixture, not the current
        # one-tick measurement policy (whose queue pilot remains disabled).
        raw['quote_offset_ticks'] = 2
        raw['policy_version'] = 3
        c = {k: v.isoformat() if hasattr(v, 'isoformat') else str(v)
             for k, v in asdict(policy().markets[0][1]).items()}
        raw['queue_pilot'] = {'markets': [dict(symbol=s, YES=c, NO=c) for s in ('X', 'Y')]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(raw))
            strategy, risk = load_policy(path)
            self.assertEqual(strategy.queue_pilot.symbols, {'X', 'Y'})
            self.assertEqual(risk.risk_window_loss_usd, D(30))
            raw['policy_version'] = 2
            path.write_text(json.dumps(raw))
            with self.assertRaises(ConfigurationError):
                load_policy(path)


if __name__ == '__main__':
    unittest.main()
