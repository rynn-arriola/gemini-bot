"""Six-socket isolation tests; all network and mutation endpoints are fake."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as fixtures
from gemini_bot.domain import EngineState
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.public_shards import PublicMarketShards, ShardEvent
from gemini_bot.rate_limits import AdmissionGovernor, Priority
from gemini_bot.ws_limits import observe_response
from gemini_bot.uptime import locally_qualifying
from gemini_bot.rank_report import rank_live_universe

NOW = fixtures.NOW


class FakeSocket(PublicMarketSocket):
    def __init__(self, governor):
        super().__init__(governor)
        self.queue = asyncio.Queue()
        self.connects = 0
        self.closes = 0
        self.fail_connect = False

    async def connect(self):
        self.connects += 1
        if self.fail_connect:
            raise ConnectionError('synthetic outage')

    async def close(self):
        self.closes += 1

    async def subscribe(self, symbols):
        self._subscribed_symbols = set(symbols)
        self.last_message_at = NOW
        for symbol in symbols:
            from gemini_bot.market_state import LocalBook
            b = self.books.setdefault(symbol, LocalBook(symbol))
            source = fixtures.book()
            b.apply_snapshot(list(source.bids.items()), list(source.asks.items()), 1, 1, NOW)

    async def unsubscribe(self, symbols):
        for symbol in symbols:
            self.books.pop(symbol, None)

    async def frames(self):
        while True:
            value = await self.queue.get()
            if isinstance(value, Exception):
                raise value
            yield value

    async def prove_liveness(self, *, timeout=2):
        self.last_message_at = NOW


class ShardAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.governor = AdmissionGovernor()
        self.public = PublicMarketShards(self.governor, socket_factory=FakeSocket)
        await self.public.connect()
        await self.public.subscribe(tuple(f'S{i:02}' for i in range(30)))

    async def asyncTearDown(self):
        await self.public.close()

    async def test_balanced_stable_and_persisted_membership(self):
        p = self.public
        self.assertEqual([len(p.symbols(i)) for i in range(5)], [6] * 5)
        old = dict(p.assignments)
        await p.subscribe(('Z',))
        self.assertTrue(all(p.assignments[s] == i for s, i in old.items()))
        restored = PublicMarketShards(self.governor, socket_factory=FakeSocket)
        restored.restore_assignments(json.dumps(p.assignments))
        self.assertEqual(restored.assignments, p.assignments)
        await restored.close()

    async def test_invalid_membership_and_capacity_fail_closed(self):
        for raw in ('[]', '{"X":5}', '{"X":true}', '{"x":0}'):
            with self.assertRaises(ValueError):
                self.public.restore_assignments(raw)
        with self.assertRaises(ValueError):
            await self.public.subscribe(('A', 'B', 'C'))

    async def test_outage_invalidates_only_its_books_and_freshness(self):
        p = self.public
        p.invalidate(0)
        for symbol, shard in p.assignments.items():
            self.assertEqual(p.books[symbol].synchronized, shard != 0)
            self.assertEqual(p.verified_at(symbol) is None, shard == 0)
        self.assertTrue(all(s._governor is self.governor for s in p.sockets))

    async def test_delayed_old_generation_cannot_revive_book(self):
        p = self.public
        symbol = p.symbols(0)[0]
        event = ShardEvent(0, 0, frame={'e':'depthUpdate','s':symbol,'U':1,'u':1,'b':[], 'a':[]})
        p.invalidate(0)
        self.assertIsNone(p.apply_frame(event))
        self.assertFalse(p.books[symbol].synchronized)

    async def test_wrong_shard_cannot_apply_trade_depth_or_lifecycle(self):
        symbol = self.public.symbols(0)[0]
        for frame in ({'e':'depthUpdate', 's':symbol}, {'e':'contractStatus','s':symbol}, {'s':symbol,'t':'trade'}):
            self.assertFalse(self.public.owns_frame(ShardEvent(1,0,frame=frame)))

    async def test_fan_in_preserves_healthy_reader_after_sibling_error(self):
        p = self.public
        frames = p.frames()
        first = asyncio.create_task(anext(frames))
        await p.sockets[0].queue.put(ConnectionError('synthetic'))
        event = await asyncio.wait_for(first, 1)
        self.assertEqual(event.shard, 0)
        self.assertIsInstance(event.error, ConnectionError)
        await p.sockets[1].queue.put({'e':'contractStatus','s':p.symbols(1)[0]})
        event = await asyncio.wait_for(anext(frames), 1)
        self.assertEqual(event.shard, 1)
        self.assertIsNone(event.error)
        await frames.aclose()
        self.assertFalse(p._readers)

    async def test_shared_429_blocks_every_socket_and_connection_attempt(self):
        observe_response(self.governor, 'public_ws', {'status':429})
        self.assertGreater(self.governor.admit_websocket_connection(), 0)
        for lane in ('public_ws','private_ws'):
            self.assertGreater(self.governor.admit_websocket(lane,Priority.EMERGENCY_CANCEL,'order.cancel'),0)

    async def test_lower_other_socket_count_cannot_forgive_shared_usage(self):
        row = {'rateLimitType':'REQUEST_WEIGHT','interval':'MINUTE','intervalNum':1,'limit':6000,'count':200}
        self.governor.observe_websocket_limits('public_ws',[row],now=1)
        self.governor.admit_websocket('public_ws',Priority.MARKET_RESYNC,'SUBSCRIBE',now=1)
        self.governor.observe_websocket_limits('public_ws',[dict(row,count=0)],now=2)
        self.assertEqual(self.governor.ws_buckets[('REQUEST_WEIGHT',60)].used,300)


class ShardOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = r = self.fixture.runtime
        r._continuous_service = True
        r.public = PublicMarketShards(r.governor, socket_factory=FakeSocket)
        await r.public.connect()
        await r.public.subscribe(('A','B','C','D','E','X'))
        # X shares shard zero with A; all other groups must remain independent.
        r._cancel_symbol_entries = AsyncMock()
        self.no_network = patch('websockets.connect', side_effect=AssertionError('network prohibited'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_one_shard_recovers_without_global_hold_or_sibling_reconnect(self):
        r = self.r
        r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError('synthetic')))
        self.assertFalse(r._stream_holds)
        self.assertFalse(r._book_fresh('X'))
        self.assertTrue(r._book_fresh('B'))
        await asyncio.wait_for(r._shard_restarts[0], 1)
        self.assertFalse(r.public.held)
        self.assertEqual(r.gate.state,EngineState.QUOTING)
        self.assertEqual([s.connects for s in r.public.sockets],[2,1,1,1,1])
        self.assertEqual({c.args[0] for c in r._cancel_symbol_entries.await_args_list},{'A','X'})

    async def test_second_leg_generation_changes_only_for_its_own_shard(self):
        r = self.r
        old_x,old_b = r._public_generation('X'),r._public_generation('B')
        r.public.invalidate(0)
        self.assertNotEqual(r._public_generation('X'),old_x)
        self.assertEqual(r._public_generation('B'),old_b)

    async def test_unsubscribed_inventory_has_stable_quarantine_generation(self):
        self.assertEqual(self.r._public_generation('CATALOG-MISSING'), (0, -1))

    async def test_healthy_sibling_admission_remains_open_but_held_symbol_cannot_send(self):
        from gemini_bot.execution import OrderPurpose, place_request
        r = self.r
        original = r.universe[0]
        r.universe.append(replace(original, rules=replace(original.rules,symbol='B'),
                                  pool=replace(original.pool,pool_id='other')))
        r._expiry['B'] = r._expiry['X']
        r._permitted_symbols.add('B')
        healthy = r._current_quote_target('B')
        self.assertIsNotNone(healthy)
        intent = r.ledger.prepare(healthy.yes,OrderPurpose.ENTRY)
        r.public.invalidate(0)
        r._validate_send(place_request(1,intent))
        with self.assertRaises(RuntimeError):
            r._validate_send(self.fixture.payload())

    async def test_no_assignment_change_or_new_subscription_when_shard_is_held(self):
        r = self.r
        before = dict(r.public.assignments)
        r.public.invalidate(0)
        r.rest.reward_events = AsyncMock()
        await r._rollover_flat_markets([])
        self.assertEqual(r.public.assignments,before)
        r.rest.reward_events.assert_not_awaited()

    async def test_unknown_send_never_becomes_scoped_retry(self):
        from gemini_bot.execution import OrderPurpose
        intent = self.r.ledger.prepare(fixtures.candidate().quote.yes,OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        with self.assertRaisesRegex(RuntimeError,'unknown mutation'):
            self.r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))
        self.assertFalse(self.r._shard_restarts)

    async def test_private_hold_defers_recovery_and_blocks_all_entry(self):
        r = self.r
        r._stream_holds.add('private')
        r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))
        await asyncio.sleep(.01)
        self.assertEqual(r.public.sockets[0].connects,1)
        r._cancel_symbol_entries.assert_not_awaited()
        r._stream_holds.clear()
        await asyncio.wait_for(r._shard_restarts[0],1)

    async def test_cash_mismatch_does_not_release_shard(self):
        r = self.r
        r.account.expected_confirmed_usd -= fixtures.D('.001')
        r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))
        await asyncio.sleep(.02)
        self.assertIn(0,r.public.held)
        self.assertFalse(r.account.equity_classification_complete)

    async def test_all_shards_can_recover_without_restarting_runner(self):
        r = self.r
        for i in range(5):
            r._begin_shard_recovery(ShardEvent(i,0,error=ConnectionError()))
        await asyncio.wait_for(asyncio.gather(*r._shard_restarts.values()),1)
        self.assertFalse(r.public.held)
        self.assertEqual(r.gate.state,EngineState.QUOTING)

    async def test_ranking_does_not_borrow_another_shards_freshness(self):
        r = self.r
        r.public.invalidate(0)
        result = rank_live_universe(r.universe,r.public.books,now=NOW,
            quantity=r.config.strategy.quote_quantity,offset_ticks=r.config.strategy.quote_offset_ticks,
            market_data_verified_at=r._market_data_verified_at())
        self.assertEqual(result,[])
        self.assertFalse(locally_qualifying(r.universe[0],r.account.reducer,r.public.books['X'],
                                          now=NOW,market_data_verified_at=r._market_data_verified_at()))

    async def test_stopped_runner_does_not_start_recovery(self):
        self.r.gate.state = EngineState.DRAINING
        with self.assertRaises(RuntimeError):
            self.r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))

    async def test_cancellation_ambiguity_remains_global_fatal(self):
        r = self.r
        r._cancel_symbol_entries.side_effect = TimeoutError('unknown cancellation')
        r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))
        with self.assertRaises(TimeoutError):
            await r._shard_restarts[0]
        self.assertEqual(r.gate.state,EngineState.HALTED)
        self.assertIn(0,r.public.held)

    async def test_real_fan_in_pump_recovers_one_failure_and_preserves_other_book(self):
        r = self.r
        sibling = r.public.books['B']
        pump = asyncio.create_task(r._public_pump())
        r._tasks.append(pump)
        await r.public.sockets[0].queue.put(ConnectionError('synthetic service restart'))
        async with asyncio.timeout(1):
            while r.public.sockets[0].connects < 2 or r.public.held:
                await asyncio.sleep(.01)
        self.assertIs(r.public.books['B'],sibling)
        self.assertFalse(pump.done())
        self.assertFalse(r._stream_holds)

    async def test_malformed_frame_is_not_hidden_as_local_transport_recovery(self):
        r = self.r
        pump = asyncio.create_task(r._public_pump())
        r._tasks.append(pump)
        await r.public.sockets[0].queue.put(ValueError('malformed packet'))
        with self.assertRaises(ValueError):
            await asyncio.wait_for(pump,1)
        self.assertEqual(r.gate.state,EngineState.HALTED)

    async def test_private_and_shard_recovery_do_not_wait_on_each_other(self):
        r = self.r
        r.private.close = AsyncMock()
        r.private.connect = AsyncMock()
        async def ready():
            r.account.apply({'status':200})
            r.account.apply({'e':'balanceUpdate','B':[{'a':'USD','c':'100','f':'100'}]})
            r.account.apply({'e':'positionReport','P':[],'u':1})
        r.private.subscribe_account = AsyncMock(side_effect=ready)
        r.rest.positions = AsyncMock(return_value=[])
        r.rest.active_orders = AsyncMock(return_value=[])
        r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))
        await r._recover_stream('private',ConnectionError())
        await asyncio.wait_for(asyncio.gather(r._stream_recovery_task,r._shard_restarts[0]),2)
        self.assertFalse(r._stream_holds)
        self.assertFalse(r.public.held)
        self.assertEqual(r.gate.state,EngineState.QUOTING)

    async def test_repeated_handshake_eof_uses_paced_same_shard_recovery(self):
        from test_maintenance_recovery import handshake_eof
        r = self.r
        r.public.sockets[0].connect = AsyncMock(side_effect=[handshake_eof() for _ in range(4)]+[None])
        sleep = asyncio.sleep
        async def fast_sleep(_):
            await sleep(0)
        with patch('gemini_bot.orchestrator.asyncio.sleep',side_effect=fast_sleep):
            r._begin_shard_recovery(ShardEvent(0,0,error=ConnectionError()))
            await asyncio.wait_for(r._shard_restarts[0],1)
        self.assertEqual(r.public.sockets[0].connect.await_count,5)
        self.assertTrue(all(s.connects == 1 for s in r.public.sockets[1:]))


class StartupInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_fill_continuity import FillContinuityTests
        self.fixture = FillContinuityTests()
        await self.fixture.asyncSetUp()
        self.fixture.inventory()
        self.r = r = self.fixture.r
        from gemini_bot.reconciliation import build_account_snapshot
        r._last_snapshot = build_account_snapshot(await r.rest.positions(),await r.rest.active_orders())
        r._exit_due_positions = AsyncMock()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_continuous_known_inventory_keeps_original_deadline_without_forced_startup_exit(self):
        r = self.r
        deadline = r.journal.get_deadline('X','YES')
        await r._prepare_activation_inventory()
        r._exit_due_positions.assert_not_awaited()
        self.assertEqual(r.journal.get_deadline('X','YES'),deadline)
        self.assertIn('X',r.account.fill_blocked_entries)

    async def test_catalog_quarantined_inventory_does_not_block_activation_inventory_proof(self):
        r = self.r
        r.universe = []
        r._inventory_catalog_holds = {'X'}
        r._permitted_symbols = {'X'}
        deadline = r.journal.get_deadline('X','YES')
        await r._prepare_activation_inventory()
        r._exit_due_positions.assert_not_awaited()
        self.assertEqual(r.journal.get_deadline('X','YES'),deadline)
        self.assertIn('X',r.account.fill_blocked_entries)

    async def test_unknown_cash_or_missing_deadline_blocks_startup(self):
        r = self.r
        r.account.expected_confirmed_usd -= fixtures.D('.001')
        with self.assertRaisesRegex(RuntimeError,'reconciled'):
            await r._prepare_activation_inventory()
        r.account.expected_confirmed_usd = r.account.confirmed_usd
        r.journal.clear_deadline('X','YES')
        with self.assertRaisesRegex(RuntimeError,'ownership/deadline'):
            await r._prepare_activation_inventory()

    async def test_legacy_startup_still_uses_bounded_forced_exit(self):
        self.r._continuous_service = False
        await self.r._prepare_activation_inventory()
        self.r._exit_due_positions.assert_awaited_once_with(force=True)

    async def test_startup_cancel_fill_uses_new_exact_snapshot_not_old_flat_snapshot(self):
        from gemini_bot.reconciliation import build_account_snapshot
        r = self.r
        r._last_snapshot = build_account_snapshot([],[])
        await r._prepare_activation_inventory()
        self.assertTrue(r._last_snapshot.positions)
        self.assertIn('X',r.account.fill_blocked_entries)


class BalanceDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_balance_read_is_bounded_and_read_only(self):
        from gemini_bot.rest_client import GeminiReadRestClient, ApiError
        client = GeminiReadRestClient(AdmissionGovernor(),api_key='fake',api_secret='fake')
        client._private_list_read = AsyncMock(return_value=[])
        self.assertEqual(await client.account_balances(),[])
        client._private_list_read.assert_awaited_once_with('/v1/balances',{'showPendingBalances':True},lane='private_rest')
        for value in ([{}] * 1001, [None]):
            client._private_list_read.return_value = value
            with self.assertRaises(ApiError):
                await client.account_balances()
