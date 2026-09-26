"""Owner-approved one-tick policy through real guards and fake transport."""
import policy_v8  # noqa: F401  (pins the v8-era default policy)
from dataclasses import replace
from decimal import Decimal as D
import unittest
from gemini_bot.config import load_policy
from gemini_bot.domain import EngineState
import test_pair_send_guard as pair
import test_repair_integration as fixtures


class OneTickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = pair.PairAdmissionTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.r
        strategy, risk = load_policy()
        # This suite exercises the one-tick fallback, independently of live pilot scope.
        strategy = replace(strategy, queue_pilot=None, quote_offset_ticks=1)
        self.assertEqual(strategy.quote_offset_ticks, 1)
        self.assertEqual(strategy.quote_quantity, D(30))
        self.assertIsNone(strategy.queue_pilot)
        self.r.config = replace(self.r.config, strategy=strategy, risk=risk)
        target, _ = self.r._quote_target_state('X')
        self.c = replace(fixtures.candidate(D(30)), quote=target)

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    async def test_pair_places_one_tick_not_touch_and_is_maker_only(self):
        self.assertEqual(self.c.quote.yes.price, D('.47'))
        self.assertEqual(self.c.quote.no.price, D('.47'))
        await self.r._quote_pair_locked(self.c)
        self.assertEqual(len(self.fx.sent), 2)
        for message in self.fx.sent:
            self.assertEqual(D(message['params']['price']), D('.47'))
            self.assertEqual(D(message['params']['quantity']), D(30))
            self.assertEqual(message['params']['timeInForce'], 'MOC')

    async def test_exact_one_tick_pair_is_not_canceled_by_old_two_tick_boundary(self):
        await self.r._quote_pair_locked(self.c)
        self.assertFalse(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertTrue(self.r._retain_resting_pair(self.c, list(self.r.account.reducer.orders.values())))

    async def test_move_to_touch_cancels_only_that_symbol(self):
        await self.r._quote_pair_locked(self.c)
        self.r.public.books['X'].apply_snapshot([(D('.47'), D(100))], [(D('.52'), D(100))], 2, 2, fixtures.NOW)
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r._quote_cancel_reasons['inside_configured_tick_distance'], 1)

    async def test_paced_second_leg_price_change_blocks_without_global_halt(self):
        def move():
            self.r.public.books['X'].apply_snapshot([(D('.48'), D(100))], [(D('.53'), D(100))], 2, 2, fixtures.NOW)
        self.fx.at_second_admission = move
        await self.r._quote_pair_locked(self.c)
        self.assertEqual(len(self.fx.sent), 1)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_partial_seven_fill_blocks_second_leg_and_keeps_deadline(self):
        def fill():
            self.r.account.apply({**self.fx.frames['YES'], 'X':'PARTIALLY_FILLED', 'z':'23',
                'Z':'7', 't':'one-tick-fill', 'L':'.47', 'n':'.00',
                'T':int(fixtures.NOW.timestamp()*1000)})
        self.fx.at_second_admission = fill
        await self.r._quote_pair_locked(self.c)
        self.assertEqual(len(self.fx.sent), 1)
        self.assertIsNotNone(self.r.journal.get_deadline('X', 'YES'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_hard_still_blocks(self):
        self.r._dynamic_pending_cap = 0
        await self.r._quote_pair_locked(self.c)
        self.assertFalse(self.fx.sent)

    async def test_capacity_still_blocks(self):
        self.fx.add_working(39, price=D('.1'), quantity=D(30))
        await self.r._quote_pair_locked(self.c)
        self.assertFalse(self.fx.sent)

    async def test_twenty_size_thirty_orders_fit_without_raising_gross_cap(self):
        self.fx.add_working(18, price=D('.50'), quantity=D(30))
        await self.r._quote_pair_locked(self.c)
        self.assertEqual(len(self.fx.sent), 2)
        exposure = self.r._websocket_exposure()
        self.assertEqual(exposure.pending_orders, 20)
        self.assertEqual(exposure.gross_reservation_usd, D('298.20'))

    async def test_farther_pair_can_remain_sticky_without_refresh_churn(self):
        await self.r._quote_pair_locked(self.c)
        for order in self.r.account.reducer.orders.values():
            order.price -= D('.01')
        self.assertFalse(await self.r._cancel_too_aggressive_quote('X'))
        self.assertTrue(self.r._retain_resting_pair(self.c, list(self.r.account.reducer.orders.values())))
        self.r._cancel_symbol_entries.assert_not_awaited()
