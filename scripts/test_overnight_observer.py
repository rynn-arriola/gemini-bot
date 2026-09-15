import asyncio
from decimal import Decimal as D
import unittest
from unittest.mock import patch, AsyncMock, Mock
from types import SimpleNamespace
from collections import Counter
import time
import urllib.error
import tempfile
from pathlib import Path
import sqlite3

from overnight_observer import group_pools, levels, liquidation_cash, pool_key, quote_models, PublicRest, Socket, diverse_selection, MAX_MARKETS, REST_SPACING, WS_SPACING, retry_delay, Observer, BUFFER_LIMIT, supervise


class PaperTests(unittest.TestCase):
    def test_diversity_before_siblings_and_no_duplicates(self):
        groups = [[{'symbol': 'A'}, {'symbol': 'B'}], [{'symbol': 'C'}, {'symbol': 'A'}, {'symbol': 'D'}]]
        self.assertEqual([r['symbol'] for r in diverse_selection(groups, 4)], ['A', 'C', 'B', 'D'])
        self.assertEqual(len(diverse_selection(groups, 2)), 2)

    def test_bounded_expansion_pacing(self):
        self.assertEqual(MAX_MARKETS, 50)
        self.assertGreater(REST_SPACING, 1)
        self.assertLess(MAX_MARKETS * WS_SPACING, 60)

    def test_rest_throttle_no_retry(self):
        rest = PublicRest()
        error = urllib.error.HTTPError('https://api.gemini.com', 429, 'limited', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as mocked:
            with self.assertRaises(urllib.error.HTTPError):
                rest.get('/v1/prediction-markets/events')
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(rest.throttles, 1)
        self.assertGreater(rest.blocked_until, time.monotonic() + 59)
        self.assertLess(rest.blocked_until, time.monotonic() + 61)

    def test_websocket_throttle_blocks_later_requests(self):
        async def check():
            observer = SimpleNamespace(metrics=Counter(), until=time.time() + 7200, rate_limits=[])
            socket = Socket(observer)
            async def frames():
                yield '{"id": 1, "status": 429}'
            await socket.reader(frames())
            self.assertEqual(observer.metrics['ws_throttles'], 1)
            self.assertLess(socket.blocked_until, time.monotonic() + 61)
            with self.assertRaises(ConnectionError):
                await socket.call('depth', {})
        asyncio.run(check())

    def test_retry_after_honors_longer_server_delay(self):
        self.assertEqual(retry_delay('120'), 120)
        self.assertEqual(retry_delay('5'), 60)
        self.assertEqual(retry_delay('bad'), 60)
        self.assertEqual(retry_delay('NaN'), 60)
        self.assertGreater(retry_delay((time.time() + 300) * 1000), 299)

    def test_websocket_ban_does_not_resume_early(self):
        async def check():
            observer = SimpleNamespace(metrics=Counter(), until=time.time()+7200, rate_limits=[])
            socket = Socket(observer)
            async def frames():
                yield '{"id": 1, "status": 418}'
            await socket.reader(frames())
            self.assertGreater(socket.blocked_until, time.monotonic()+7190)
        asyncio.run(check())

    def test_storage_failure_retains_rows_and_recovers_without_duplicate(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                obs = Observer(Path(folder), time.time()+60, [], {}, PublicRest())
                real_db = obs.db
                obs.store('test', time.time(), 'X', {'value': 1})
                fake = Mock()
                fake.__enter__ = Mock(side_effect=sqlite3.OperationalError('locked'))
                fake.__exit__ = Mock(return_value=False)
                obs.db = fake
                with patch('overnight_observer.asyncio.sleep', new=AsyncMock()):
                    self.assertFalse(await obs.flush())
                self.assertEqual(len(obs.buffer), 1)
                obs.db = real_db
                self.assertTrue(await obs.flush())
                self.assertTrue(await obs.flush())
                self.assertEqual(real_db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
                self.assertFalse(obs.buffer)
                real_db.close()
        asyncio.run(check())

    def test_status_write_failure_retries_without_killing_collection(self):
        async def check():
            obs = Observer.__new__(Observer)
            obs.error = Mock()
            obs.state = Mock(side_effect=[PermissionError('locked'), {'phase':'running'}])
            with patch('overnight_observer.asyncio.sleep', new=AsyncMock()):
                self.assertEqual(await obs.publish('running'), {'phase':'running'})
            self.assertEqual(obs.state.call_count, 2)
        asyncio.run(check())

    def test_sampling_budget_skips_and_rotates_without_stalling(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                obs = Observer(Path(folder), time.time()+60, [{'symbol':s} for s in ('A','B','C')], {}, PublicRest())
                obs.socket.ws = object()
                async def slow(*args):
                    await asyncio.sleep(1)
                obs.sample = slow
                start = time.monotonic()
                with patch('overnight_observer.CYCLE_BUDGET', 0.02):
                    await obs.sample_cycle()
                self.assertLess(time.monotonic()-start, 0.3)
                self.assertEqual(obs.metrics['missing_samples'], 3)
                self.assertEqual(obs.sample_cursor, 1)
                obs.db.close()
        asyncio.run(check())

    def test_storage_buffer_is_bounded_and_loss_counted(self):
        obs = Observer.__new__(Observer)
        obs.buffer = [None] * BUFFER_LIMIT
        obs.metrics = Counter()
        obs.store('test', 0, '', {})
        self.assertEqual(len(obs.buffer), BUFFER_LIMIT)
        self.assertEqual(obs.metrics['records_dropped_storage_overflow'], 1)

    def test_metadata_retries_at_60_seconds_after_failure(self):
        async def check():
            obs = Observer.__new__(Observer)
            obs.rest = SimpleNamespace(blocked_until=0)
            obs.error = Mock()
            obs.refresh = Mock(side_effect=OSError('temporary'))
            sleeper = AsyncMock(side_effect=[None, asyncio.CancelledError()])
            with patch('overnight_observer.asyncio.sleep', sleeper):
                with self.assertRaises(asyncio.CancelledError):
                    await obs.metadata_loop()
            self.assertEqual(sleeper.await_args_list[0].args[0], 3600)
            self.assertEqual(sleeper.await_args_list[1].args[0], 60)
        asyncio.run(check())

    def test_supervisor_preserves_cooldown_and_buffer(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                cooldown = time.monotonic()+600
                first = SimpleNamespace(metrics=Counter(), generation=3, buffer=[('pending',)],
                    socket=SimpleNamespace(blocked_until=cooldown, throttle_count=2),
                    selected={}, config={}, db=SimpleNamespace(close=Mock()), store=Mock(),
                    run=AsyncMock(side_effect=OSError('temporary')))
                second = SimpleNamespace(metrics=Counter(), socket=SimpleNamespace(), store=Mock(), run=AsyncMock())
                with patch('overnight_observer.Observer', side_effect=[first, second]), patch('overnight_observer.SUPERVISOR_BACKOFF', 0):
                    await supervise(Path(folder), time.time()+60, [], {}, PublicRest())
                self.assertEqual(second.socket.blocked_until, cooldown)
                self.assertEqual(second.socket.throttle_count, 2)
                self.assertEqual(second.buffer, [('pending',)])
                self.assertEqual(second.generation, 4)
        asyncio.run(check())

    def test_websocket_weight_headroom_cooldown(self):
        async def check():
            observer = SimpleNamespace(metrics=Counter(), until=time.time() + 7200, rate_limits=[])
            socket = Socket(observer)
            async def frames():
                yield '{"id": 1, "status": 200, "rateLimits": [{"rateLimitType":"REQUEST_WEIGHT","count": 70,"limit":100}]}'
            await socket.reader(frames())
            self.assertEqual(observer.metrics['ws_weight_cooldowns'], 1)
            self.assertGreater(socket.blocked_until, time.monotonic() + 59)
        asyncio.run(check())

    def test_shared_pools_not_multiplied(self):
        rows = [{"pool_source": "category_default", "pool_id": 53, "daily_pool_usd": "500", "event_ticker": x} for x in ("a", "b")]
        self.assertEqual(len(group_pools(rows)), 1)

    def test_missing_override_ids_are_separate(self):
        rows = [{"pool_source": "event_override", "daily_pool_usd": "100", "event_ticker": x} for x in ("a", "b")]
        self.assertEqual(len(group_pools(rows)), 2)

    def test_conflicting_shared_budget_rejected(self):
        rows = [{"pool_source": "category_default", "pool_id": 1, "daily_pool_usd": str(n)} for n in (100, 200)]
        with self.assertRaises(ValueError):
            group_pools(rows)

    def test_unknown_pool_rejected(self):
        with self.assertRaises(ValueError):
            pool_key({"pool_source": "category_default"})

    def test_yes_no_depth_uses_opposite_price_frames(self):
        self.assertEqual(liquidation_cash([(D('.48'), D('6')), (D('.47'), D('10'))], 'yes'), D('4.76'))
        self.assertEqual(liquidation_cash([(D('.52'), D('6')), (D('.53'), D('10'))], 'no'), D('4.76'))

    def test_insufficient_depth_is_unknown(self):
        self.assertIsNone(liquidation_cash([(D('.48'), D('9'))], 'yes'))

    def test_two_ticks_behind_is_primary_and_non_crossing(self):
        models = quote_models([(D('.48'), D(100))], [(D('.52'), D(100))], D('.01'), D('.10'))
        self.assertEqual(models[2]['yes_bid'], .46)
        self.assertEqual(models[2]['yes_ask'], .54)
        self.assertEqual(models[2]['role'], 'primary_cautious_candidate')
        self.assertEqual(models[2]['reserved_quote_value'], 9.2)

    def test_boundary_not_assumed_to_score(self):
        models = quote_models([(D('.40'), D(100))], [(D('.60'), D(100))], D('.01'), D('.10'))
        self.assertFalse(any(m['eligible_geometry'] for m in models))

    def test_empty_or_crossed_book_has_no_quotes(self):
        self.assertEqual(quote_models([], [], D('.01'), D('.1')), [])
        self.assertEqual(quote_models([(D('.6'), D(10))], [(D('.5'), D(10))], D('.01'), D('.1')), [])

    def test_malformed_depth_rejected(self):
        for raw in ([['NaN', '10']], [['.5', '-1']], [['.5', '10'], ['.5', '20']]):
            with self.assertRaises(ValueError):
                levels(raw, True)

    def test_trade_method_blocked_before_network(self):
        with self.assertRaises(ValueError):
            asyncio.run(Socket(None).call('order.place', {}))

    def test_rest_mutation_route_blocked_before_network(self):
        with self.assertRaises(ValueError):
            PublicRest().get('/v1/prediction-markets/order')


if __name__ == '__main__':
    unittest.main()
