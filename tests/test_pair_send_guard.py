"""Real pair/transport/guard/reducer flow with fake socket admission only."""
import json
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_repair_integration as fixtures
from gemini_bot.domain import EngineState, Outcome, QuoteLeg, Side
from gemini_bot.execution import IntentState, OrderPurpose


class PairAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continuous_service = True
        self.sent = []
        self.frames = {}
        self.admissions = 0
        self.at_second_admission = lambda: None
        self.fail_second_send = False

        def admit(*args):
            self.admissions += 1
            if self.admissions == 2:
                self.at_second_admission()
            return 0

        async def send(raw):
            payload = json.loads(raw)
            self.sent.append(payload)
            if self.fail_second_send and len(self.sent) == 2:
                raise ConnectionError('fake send outcome unknown')
            p = payload['params']
            frame = {'e': 'orderUpdate', 'i': p['eventOutcome'], 'c': p['clientOrderId'],
                's': 'X', 'S': 'BUY', 'O': p['eventOutcome'],
                'q': p['quantity'], 'z': p['quantity'],
                'p': p['price'], 'X': 'NEW'}
            self.frames[p['eventOutcome']] = frame
            self.r.account.apply(frame)

        self.r.private._connection = SimpleNamespace(send=send, close=AsyncMock())
        self.r.private._governor = SimpleNamespace(admit_websocket=admit)
        # Cleanup is observable without contacting any venue. The reducer and
        # the final transport guard are real, including unsent intent closure.
        self.r._cancel_symbol_entries = AsyncMock()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def cancel_first(self):
        self.r.account.apply({**self.frames['YES'], 'X': 'CANCELED'})

    async def assert_second_blocked(self):
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertEqual(len(self.sent), 1)
        rows = self.r.journal.connection.execute(
            "SELECT state FROM order_intent WHERE symbol='X' AND outcome='NO'").fetchall()
        self.assertEqual([row[0] for row in rows], ['terminal'])
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)
        self.assertFalse(self.r._maker_pair_guards)

    async def test_canceled_first_leg_during_second_admission_is_not_sent(self):
        self.at_second_admission = self.cancel_first
        await self.assert_second_blocked()

    async def test_missing_first_order_is_not_replaced_by_another_yes(self):
        def replace_first():
            self.cancel_first()
            leg = fixtures.candidate().quote.yes
            intent = self.r.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.r.ledger.mark_sent(intent.client_order_id)
            self.r.account.apply({**self.frames['YES'], 'i': 'different', 'c': intent.client_order_id})
        self.at_second_admission = replace_first
        await self.assert_second_blocked()

    async def test_first_order_identity_changed_is_blocked(self):
        self.at_second_admission = lambda: setattr(self.r.account.reducer.orders['YES'], 'client_order_id', 'different')
        await self.assert_second_blocked()

    async def test_first_quantity_changed_is_blocked(self):
        self.at_second_admission = lambda: setattr(self.r.account.reducer.orders['YES'], 'remaining_quantity', D(9))
        await self.assert_second_blocked()

    async def test_first_price_changed_is_blocked(self):
        self.at_second_admission = lambda: setattr(self.r.account.reducer.orders['YES'], 'price', D('.45'))
        await self.assert_second_blocked()

    async def test_partial_fill_blocks_second_and_preserves_deadline(self):
        def fill():
            self.r.account.apply({**self.frames['YES'], 'X': 'PARTIALLY_FILLED', 'z': '19',
                'Z': '1', 't': 'fake-fill', 'L': '.46', 'n': '.00',
                'T': int(fixtures.NOW.timestamp() * 1000)})
        self.at_second_admission = fill
        await self.assert_second_blocked()
        self.assertIsNotNone(self.r.journal.get_deadline('X', 'YES'))

    async def test_full_fill_blocks_second_and_preserves_deadline(self):
        def fill():
            self.r.account.apply({**self.frames['YES'], 'X': 'FILLED', 'z': '0',
                'Z': '20', 't': 'fake-fill', 'L': '.46', 'n': '.00',
                'T': int(fixtures.NOW.timestamp() * 1000)})
        self.at_second_admission = fill
        await self.assert_second_blocked()
        self.assertIsNotNone(self.r.journal.get_deadline('X', 'YES'))

    async def test_public_generation_change_invalidates_pair(self):
        self.at_second_admission = lambda: self.r._stream_generation.update(public=1)
        await self.assert_second_blocked()

    async def test_private_generation_change_invalidates_pair(self):
        self.at_second_admission = lambda: self.r._stream_generation.update(private=1)
        await self.assert_second_blocked()

    async def test_healthy_pair_sends_both_and_clears_guard(self):
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.r.account.reducer.pending_order_count(), 2)
        self.assertFalse(self.r._maker_pair_guards)
        self.r._cancel_symbol_entries.assert_not_awaited()

    def add_working(self, count, *, price=D('.46'), quantity=D(20)):
        for index in range(count):
            symbol = f'OTHER{index // 2}'
            outcome = Outcome.YES if index % 2 == 0 else Outcome.NO
            leg = QuoteLeg(symbol, outcome, Side.BUY, price, quantity)
            intent = self.r.ledger.prepare(leg, OrderPurpose.ENTRY)
            self.r.ledger.mark_sent(intent.client_order_id)
            self.r.account.apply({'e': 'orderUpdate', 'i': f'other-{index}',
                'c': intent.client_order_id, 's': symbol, 'S': 'BUY',
                'O': outcome.value, 'q': str(quantity), 'z': str(quantity),
                'p': str(price), 'X': 'NEW'})

    async def test_thirtieth_twenty_contract_order_fits_priced_reservation(self):
        self.add_working(28)
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertEqual(len(self.sent), 2)
        exposure = self.r._websocket_exposure()
        self.assertEqual(exposure.pending_orders, 30)
        self.assertEqual(exposure.two_sided_quotes, 15)
        self.assertEqual(exposure.gross_reservation_usd, D('276'))
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertTrue(all(o.working for k, o in self.r.account.reducer.orders.items()
                            if k.startswith('other-')))

    async def test_eighth_market_no_longer_repeats_first_leg_cancel(self):
        self.add_working(14)
        for _ in range(3):
            await self.r._quote_pair_locked(fixtures.candidate())
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.r.account.reducer.pending_order_count(), 16)
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertEqual(self.r.journal.connection.execute(
            "SELECT COUNT(*) FROM audit_event WHERE kind='maker_pair_presend_state_change'"
        ).fetchone()[0], 0)

    async def test_odd_pending_slot_cannot_start_a_pair(self):
        self.add_working(29, price=D('.1'))
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertFalse(self.sent)
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_pair_over_actual_gross_cap_sends_neither_leg(self):
        self.add_working(28, price=D('.51'))
        # Existing cost 285.60 plus the complete pair 18.40 exceeds 300.
        for _ in range(3):
            await self.r._quote_pair_locked(fixtures.candidate())
        self.assertFalse(self.sent)
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertEqual(self.r.account.reducer.pending_order_count(), 28)

    async def test_pair_at_exact_gross_cap_is_allowed(self):
        self.add_working(28, price=D('.50'))
        # Reserve exactly 281.60 before the 18.40 pair.
        self.r.account.reducer.orders['other-0'].price = D('.58')
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.r._websocket_exposure().gross_reservation_usd, D('300'))
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_available_funds_still_block_the_complete_pair(self):
        self.r.account.available_usd = D('18.39')
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertFalse(self.sent)
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_market_cap_still_blocks_the_complete_pair(self):
        self.r.config = replace(self.r.config, risk=replace(
            self.r.config.risk, max_market_exposure_usd=D('18.39')))
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertFalse(self.sent)
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_market_cap_rechecked_after_second_leg_admission(self):
        def lower_cap():
            self.r.config = replace(self.r.config, risk=replace(
                self.r.config.risk, max_market_exposure_usd=D('18.39')))
        self.at_second_admission = lower_cap
        await self.assert_second_blocked()

    async def test_hard_stop_pending_cap_zero_starts_no_pair(self):
        self.r._dynamic_pending_cap = 0
        await self.r._quote_pair_locked(fixtures.candidate())
        self.assertFalse(self.sent)
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_actual_exposure_change_still_blocks_second_send(self):
        self.add_working(28, price=D('.50'))
        def exposure_changes():
            self.r.account.reducer.orders['other-0'].price = D('.99')
        self.at_second_admission = exposure_changes
        await self.assert_second_blocked()

    def test_partial_order_uses_remaining_cost_and_inventory_is_not_freed_by_sell(self):
        self.add_working(2)
        first = self.r.account.reducer.orders['other-0']
        first.remaining_quantity = D(7)
        self.r.account.reducer.positions['HELD'] = D(-20)
        sell = self.r.account.reducer.orders['other-1']
        sell.side = Side.SELL
        sell.symbol = 'HELD'
        exposure = self.r._websocket_exposure()
        self.assertEqual(exposure.pending_orders, 2)
        self.assertEqual(exposure.gross_reservation_usd, D('23.22'))
        self.assertEqual(exposure.market_exposure_usd['HELD'], D(20))
        first.status = 'CANCELED'
        self.assertEqual(self.r._websocket_exposure().gross_reservation_usd, D(20))

    async def test_attempted_send_failure_is_unknown_and_not_retried(self):
        self.fail_second_send = True
        with self.assertRaises(ConnectionError):
            await self.r._quote_pair_locked(fixtures.candidate())
        second = self.r.ledger.get(self.sent[1]['params']['clientOrderId'])
        self.assertEqual(second.state, IntentState.SENT_UNKNOWN)
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(self.r._maker_pair_guards)
        self.r._cancel_symbol_entries.assert_not_awaited()

    async def test_same_symbol_cancel_does_not_touch_unrelated_market(self):
        leg = QuoteLeg('OTHER', Outcome.YES, Side.BUY, D('.4'), D(10))
        intent = self.r.ledger.prepare(leg, OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        self.r.account.apply({'e': 'orderUpdate', 'i': 'other', 'c': intent.client_order_id,
            's': 'OTHER', 'S': 'BUY', 'O': 'YES', 'q': '10', 'z': '10', 'p': '.4', 'X': 'NEW'})
        self.at_second_admission = self.cancel_first
        await self.assert_second_blocked()
        self.assertTrue(self.r.account.reducer.orders['other'].working)


class QuoteTargetDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def test_missing_contract(self):
        self.assertEqual(self.r._quote_target_state('MISSING')[1], 'missing_contract')

    def test_unsynchronized_book(self):
        self.r.public.books['X'].synchronized = False
        self.assertEqual(self.r._quote_target_state('X')[1], 'book_unsynchronized')

    def test_stale_stream(self):
        self.r.public.books['X'].observed_at = fixtures.NOW - fixtures.timedelta(seconds=10)
        self.assertEqual(self.r._quote_target_state('X')[1], 'market_stream_stale')

    def test_one_sided_book(self):
        self.r.public.books['X'].bids.clear()
        self.assertEqual(self.r._quote_target_state('X')[1], 'public_book_one_sided')

    def test_empty_after_own_depth_subtraction(self):
        leg = replace(fixtures.candidate().quote.yes, price=D('.48'), quantity=D(100))
        intent = self.r.ledger.prepare(leg, OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        self.r.account.apply({'e': 'orderUpdate', 'i': 'yes', 'c': intent.client_order_id,
            's': 'X', 'S': 'BUY', 'O': 'YES', 'q': '100', 'z': '100', 'p': '.48', 'X': 'NEW'})
        self.assertEqual(self.r._quote_target_state('X')[1], 'external_book_one_sided')

    def test_invalid_target_is_distinct_from_unavailable_book(self):
        self.r.public.books['X'].asks = {D('.99'): D(100)}
        self.assertEqual(self.r._quote_target_state('X')[1], 'invalid_quote_target')


if __name__ == '__main__':
    unittest.main()
