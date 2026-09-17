"""Current size policy through the real pair guard with fake venue transport."""
from dataclasses import replace
from decimal import Decimal as D
import unittest

import test_pair_send_guard as pair
import test_repair_integration as fixtures
from gemini_bot.config import load_policy
from gemini_bot.domain import EngineState
from gemini_bot.reward_ranking import rank_candidate
import test_competition_strategy as strategy_tests


class ContractSizeValidityTests(unittest.TestCase):
    def test_fifteen_obeys_contract_minimum_and_increment_without_rounding(self):
        quantity = load_policy()[0].quote_quantity
        original = strategy_tests.item()
        for minimum, increment, rejection in ((1,1,None),(10,5,None),
                (10,10,'quantity is off increment'),(20,1,'quantity below reward or contract minimum')):
            item = replace(original, rules=replace(original.rules,
                quantity_minimum=D(minimum), quantity_increment=D(increment)))
            result = rank_candidate(item, now=strategy_tests.NOW, quantity=quantity)
            if rejection:
                self.assertIn(rejection, result.reasons)
                self.assertFalse(result.eligible_now)
            else:
                self.assertTrue(result.eligible_now)
                self.assertEqual(result.quote.yes.quantity, D(15))
                self.assertEqual(result.quote.no.quantity, D(15))


class FifteenContractPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = pair.PairAdmissionTests()
        await self.fx.asyncSetUp()
        self.r = self.fx.r
        strategy, risk = load_policy()
        self.assertEqual(strategy.quote_quantity, D(15))
        self.r.config = replace(self.r.config, strategy=strategy, risk=risk)

    async def asyncTearDown(self):
        await self.fx.asyncTearDown()

    async def test_forty_size_fifteen_orders_fit_under_unchanged_cap(self):
        self.fx.add_working(38, price=D('.50'), quantity=D(15))
        await self.r._quote_pair_locked(fixtures.candidate(D(15)))
        self.assertEqual(len(self.fx.sent), 2)
        self.assertTrue(all(D(p['params']['quantity']) == 15 for p in self.fx.sent))
        state = self.r._websocket_exposure()
        self.assertEqual(state.pending_orders, 40)
        self.assertEqual(state.two_sided_quotes, 20)
        self.assertEqual(state.gross_reservation_usd, D('298.80'))
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_forty_first_order_still_blocked(self):
        self.fx.add_working(39, price=D('.1'), quantity=D(15))
        await self.r._quote_pair_locked(fixtures.candidate(D(15)))
        self.assertFalse(self.fx.sent)

    async def test_gross_cap_still_blocks_even_at_smaller_size(self):
        self.fx.add_working(38, price=D('.51'), quantity=D(15))
        await self.r._quote_pair_locked(fixtures.candidate(D(15)))
        self.assertFalse(self.fx.sent)

    async def test_fifteen_contract_fill_stops_second_leg_and_retains_deadline(self):
        def fill_first():
            self.r.account.apply({**self.fx.frames['YES'], 'X':'FILLED', 'z':'0',
                'Z':'15', 't':'fake-size15-fill', 'L':'.46', 'n':'.00',
                'T':int(fixtures.NOW.timestamp()*1000)})
        self.fx.at_second_admission = fill_first
        await self.r._quote_pair_locked(fixtures.candidate(D(15)))
        self.assertEqual(len(self.fx.sent), 1)
        self.assertIsNotNone(self.r.journal.get_deadline('X','YES'))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)

    async def test_hard_is_not_bypassed_by_smaller_size(self):
        self.r._dynamic_pending_cap = 0
        await self.r._quote_pair_locked(fixtures.candidate(D(15)))
        self.assertFalse(self.fx.sent)
