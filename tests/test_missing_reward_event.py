"""Public event 404 isolation; all account/mutation gates remain authoritative."""
from dataclasses import replace
from decimal import Decimal as D
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gemini_bot.discovery import RewardUniverseLoader, read_reward_event
from gemini_bot.rest_client import ApiError
import test_repair_integration as fixtures


def event(symbol):
    return {'status': 'active', 'contracts': [{
        'instrumentSymbol': symbol, 'status': 'active', 'marketState': 'open',
        'priceIncrement': '.01', 'quantityMinimum': '1', 'quantityIncrement': '1',
        'expiryDate': '2027-01-01T00:00:00Z',
        'prices': {'bestBid': '.48', 'bestAsk': '.52'},
    }]}


class MissingDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def rest(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [
            {'event_ticker': t, 'pool_id': 1, 'daily_pool_usd': '100'}
            for t in ('MISSING', 'HEALTHY')]
        async def get(ticker):
            if ticker == 'MISSING':
                raise ApiError(404, 'unavailable')
            return event('Y')
        rest.event.side_effect = get
        return rest

    async def test_missing_event_does_not_hide_healthy_same_pool(self):
        rest, logged = self.rest(), Mock()
        rows = await RewardUniverseLoader(rest, logged).load(now=fixtures.NOW, offset_ticks=1)
        self.assertEqual([c.rules.symbol for c in rows], ['Y'])
        logged.assert_called_once_with('MISSING')
        self.assertEqual(rest.event.await_count, 2)

    async def test_incumbent_reserves_room_and_recovers_on_later_load(self):
        rest = self.rest()
        loader = RewardUniverseLoader(rest)
        preferred = {'shared:1\x1eX': ('MISSING', 'X')}
        rows = await loader.load(maximum_pools=2, preferred=preferred, now=fixtures.NOW)
        self.assertEqual([c.rules.symbol for c in rows], ['Y'])
        self.assertEqual(preferred, {'shared:1\x1eX': ('MISSING', 'X')})
        rest.event.side_effect = lambda ticker: event('X' if ticker == 'MISSING' else 'Y')
        rows = await loader.load(maximum_pools=2, preferred=preferred, now=fixtures.NOW)
        self.assertEqual([c.rules.symbol for c in rows], ['X', 'Y'])
        self.assertEqual(loader.missing_events, set())

    async def test_all_missing_returns_no_fabricated_markets(self):
        rest = self.rest()
        rest.event.side_effect = ApiError(404, 'unavailable')
        self.assertEqual(await RewardUniverseLoader(rest).load(), [])

    async def test_missing_owner_room_cannot_be_used_by_alternative_contracts(self):
        rest = self.rest()
        async def get(ticker):
            if ticker == 'MISSING':
                raise ApiError(404, 'unavailable')
            response = event('Y')
            response['contracts'].extend([event(s)['contracts'][0] for s in ('Z','W')])
            return response
        rest.event.side_effect = get
        rows = await RewardUniverseLoader(rest).load(maximum_pools=2,
            preferred={'shared:1\x1eX': ('MISSING','X')}, now=fixtures.NOW)
        self.assertEqual(len(rows), 1)

    async def test_other_errors_and_cooldown_are_not_absence(self):
        for status, cooldown in [(400,None), (401,None), (403,None), (418,None),
                                 (429,60), (500,None), (503,None), (None,None), (404,60)]:
            with self.subTest(status=status, cooldown=cooldown):
                rest, logged = self.rest(), Mock()
                error = ApiError(status, 'test', cooldown)
                rest.event.side_effect = error
                with self.assertRaises(ApiError) as raised:
                    await read_reward_event(rest, 'MISSING', logged)
                self.assertIs(raised.exception, error)
                logged.assert_not_called()

    async def test_catalog_404_is_not_swallowed(self):
        rest = self.rest()
        rest.reward_events.side_effect = ApiError(404, 'unavailable')
        with self.assertRaises(ApiError):
            await RewardUniverseLoader(rest).load()
        rest.event.assert_not_awaited()

    async def test_unexpected_error_is_not_swallowed(self):
        rest = self.rest()
        rest.event.side_effect = ValueError('malformed')
        with self.assertRaises(ValueError):
            await RewardUniverseLoader(rest).load()


class MissingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = fixtures.RuntimeRepairTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.runtime
        self.r._continuous_service = True
        self.r.account.subscription_acknowledged = True
        self.r.public.note_message(fixtures.NOW)
        self.x = self.r.universe[0]
        self.y = replace(self.x, event_ticker='HEALTHY', rules=replace(self.x.rules, symbol='Y'))
        self.r.universe.append(self.y)
        self.r._expiry['Y'] = self.r._expiry['X']
        self.r._permitted_symbols.add('Y')
        self.r._sticky_markets = {c.owner_key: (c.event_ticker,c.rules.symbol) for c in self.r.universe}
        self.r._sticky_pool_ids = set(self.r._sticky_markets)
        self.owners = dict(self.r._sticky_markets)
        self.r.rest.reward_config = AsyncMock(return_value={'enabled':True})
        self.r.rest.reward_events = AsyncMock(return_value=[
            {'event_ticker':t,'daily_pool_usd':'100'} for t in ('E','HEALTHY')])
        async def get(ticker):
            if ticker == 'E':
                raise ApiError(404, 'unavailable')
            return event('Y')
        self.r.rest.event = AsyncMock(side_effect=get)
        self.r.rest.positions = AsyncMock(return_value=[])
        self.r.rest.active_orders = AsyncMock(return_value=[])
        self.r._cancel_symbol_entries = AsyncMock()
        self.join = patch('gemini_bot.orchestrator.join_reward_contracts',
            side_effect=lambda rewards, events: [self.x if rewards[0]['event_ticker']=='E' else self.y])
        self.join.start()

    async def asyncTearDown(self):
        self.join.stop()
        await self.fx.asyncTearDown()

    async def test_refresh_holds_and_cancels_only_affected_symbol(self):
        self.assertTrue(await self.r._reward_pools_still_active())
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertIn('X', self.r.account.blocked_entries)
        self.assertNotIn('Y', self.r.account.blocked_entries)
        self.assertFalse(self.r.universe[0].pool.active)
        self.assertTrue(self.r.universe[1].pool.active)
        self.assertTrue(self.r.gate.may_submit)
        self.assertEqual(self.r._sticky_markets, self.owners)

    async def test_multiple_siblings_block_before_cancel_and_one_event_read(self):
        sibling = replace(self.x, rules=replace(self.x.rules, symbol='Z'))
        self.r.universe.append(sibling)
        async def cancel(symbol):
            self.assertIn('X', self.r.account.blocked_entries)
            self.assertIn('Z', self.r.account.blocked_entries)
        self.r._cancel_symbol_entries.side_effect = cancel
        await self.r._reward_pools_still_active()
        self.assertEqual([c.args[0] for c in self.r.rest.event.await_args_list], ['E','HEALTHY'])
        self.assertEqual([c.args[0] for c in self.r._cancel_symbol_entries.await_args_list], ['X','Z'])

    async def test_known_inventory_and_original_deadline_are_preserved(self):
        self.r.account.reducer.positions['X'] = D(7)
        self.r.account.expected_positions = {'X':D(7)}
        self.r.journal.persist_deadline('X','YES',fixtures.NOW, fixtures.NOW+fixtures.timedelta(minutes=5),D(7))
        deadline = self.r.journal.get_deadline('X','YES')
        await self.r._reward_pools_still_active()
        self.assertEqual(self.r.account.reducer.positions['X'], D(7))
        self.assertEqual(self.r.journal.get_deadline('X','YES'), deadline)
        self.assertEqual(self.r._sticky_markets, self.owners)

    async def test_unknown_cancel_still_propagates(self):
        self.r._cancel_symbol_entries.side_effect = RuntimeError('unknown cancel')
        with self.assertRaisesRegex(RuntimeError, 'unknown cancel'):
            await self.r._reward_pools_still_active()
        self.assertIn('X',self.r.account.blocked_entries)

    async def test_successful_later_metadata_uses_terminal_flat_recovery(self):
        await self.r._reward_pools_still_active()
        self.r.rest.event.side_effect = lambda ticker: event('X' if ticker=='E' else 'Y')
        self.r.account.block_entries('X')  # Independent manual hold must survive.
        self.assertTrue(await self.r._reward_pools_still_active())
        self.r.rest.active_orders.assert_awaited_once()
        self.assertNotIn('reward_metadata', self.r.account.entry_hold_reasons.get('X',set()))
        self.assertIn('X',self.r.account.blocked_entries)
        self.assertTrue(self.r.universe[0].pool.active)

    async def test_rollover_does_not_retire_temporarily_missing_owner(self):
        await self.r._reward_pools_still_active()
        self.r.public.unsubscribe = AsyncMock()
        with patch('gemini_bot.orchestrator.RewardUniverseLoader.load', new=AsyncMock(return_value=[])):
            await self.r._rollover_flat_markets([])
        self.r.public.unsubscribe.assert_not_awaited()
        self.assertEqual(self.r._sticky_markets, self.owners)

    async def test_startup_omitted_owner_is_preferred_on_rollover(self):
        self.r.universe = [self.y]
        self.r.public.subscribe = AsyncMock()
        with patch('gemini_bot.orchestrator.RewardUniverseLoader.load', new=AsyncMock(return_value=[self.x])) as load:
            await self.r._rollover_flat_markets([])
        self.assertEqual(load.await_args.kwargs['preferred'][self.x.owner_key], ('E','X'))
        self.r.public.subscribe.assert_awaited_once_with(('X',))
        self.assertFalse(self.r.public.books['X'].synchronized)
        self.assertEqual(self.r._sticky_markets, self.owners)

    async def test_refresh_auth_failure_still_propagates(self):
        self.r.rest.event.side_effect = ApiError(401,'unavailable')
        with self.assertRaises(ApiError):
            await self.r._reward_pools_still_active()
