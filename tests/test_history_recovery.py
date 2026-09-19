import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import test_repair_integration as fixtures
from gemini_bot.trade_recovery import import_terminal_trades
from gemini_bot.domain import QuoteLeg, Outcome, Side
from gemini_bot.execution import OrderPurpose, IntentLedger
from gemini_bot.account_runtime import AccountRuntime
from unittest.mock import AsyncMock, patch
from gemini_bot.rate_limits import AdmissionGovernor
from gemini_bot.rest_client import GeminiReadRestClient, ApiError
import test_fill_continuity as flow_fixtures

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)


class MakerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountRepairTests()
        self.fixture.setUp()
        self.account = self.fixture.account
        self.store = self.fixture.store
        intent = self.fixture.ledger.prepare(QuoteLeg('X', Outcome.YES, Side.BUY, D('.4'), D(10)), OrderPurpose.ENTRY)
        self.fixture.ledger.mark_sent(intent.client_order_id)
        self.fixture.ledger.acknowledge(intent.client_order_id, 'o')
        self.row = dict(orderId='o', clientOrderId=intent.client_order_id, symbol='X', side='buy', outcome='yes',
            quantity='10', filledQuantity='3', price='.4', avgExecutionPrice='.4', status='cancelled',
            timeInForce='maker-or-cancel', createdAt=(NOW-timedelta(hours=1)).isoformat(), updatedAt=NOW.isoformat())
        self.trade = dict(tid='actual-trade',order_id='o',client_order_id=intent.client_order_id,symbol='X',
                          type='Buy',amount='3',price='.4',fee_amount='0',fee_currency='USD',
                          timestampms=int((NOW-timedelta(minutes=10)).timestamp()*1000))

    def recover(self, rows=None, trades=None, account=None, **kwargs):
        return import_terminal_trades(account or self.account,[self.row] if rows is None else rows,
                                      [self.trade] if trades is None else trades,now=NOW,**kwargs)

    def tearDown(self):
        self.fixture.tearDown()

    def test_partial_terminal_import_is_exact_idempotent_and_conservatively_due(self):
        self.assertEqual(self.recover(), 1)
        self.assertEqual(self.account.expected_confirmed_usd, D('98.8'))
        self.assertEqual(self.account.expected_positions, {'X': D(3)})
        deadline = self.store.get_deadline('X','YES')
        self.assertLess(deadline, NOW)
        self.assertEqual(self.recover(),0)
        self.assertEqual(self.store.get_deadline('X','YES'), deadline)
        self.assertFalse(self.account.equity_classification_complete)
        self.fixture.cash('98.8'); self.fixture.position(3)
        self.assertTrue(self.account.equity_classification_complete)

    def test_earlier_deadline_and_risk_history_are_preserved(self):
        deadline=NOW-timedelta(hours=2)
        self.store.persist_deadline('X','YES',deadline-timedelta(seconds=300),deadline,D(1))
        self.recover()
        self.assertEqual(self.store.get_deadline('X','YES'),deadline)

    def test_late_detailed_event_does_not_double_count_after_restart(self):
        self.recover()
        restored=AccountRuntime(IntentLedger(self.store),self.store)
        self.recover(account=restored)
        restored.apply({'e':'orderUpdate','i':'o','c':self.row['clientOrderId'],'s':'x','O':'YES','S':'BUY',
                        'q':'10','Z':'3','z':'0','t':'actual-trade','X':'FILLED'})
        self.assertEqual(restored.expected_positions,{'X':D(3)})
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM execution_fill').fetchone()[0],1)

    def test_mismatch_in_late_event_is_not_ignored(self):
        self.recover()
        with self.assertRaises(RuntimeError):
            self.recover(trades=[dict(self.trade,symbol='WRONG')])

    def test_bad_identity_price_quantity_and_incomplete_history_block(self):
        for change in ({'outcome':'no'},{'quantity':'11'},{'price':'.5'},
                       {'filledQuantity':'11'},{'status':'open'},
                       {'createdAt':(NOW+timedelta(hours=1)).isoformat()}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.recover(rows=[dict(self.row,**change)])
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM execution_fill').fetchone()[0],0)

    def test_previously_recorded_partial_is_only_imported_for_missing_quantity(self):
        self.store.record_fill(trade_id='old',order_id='o',client_order_id=self.row['clientOrderId'],
            symbol='X',side='BUY',outcome='YES',quantity=D(1),price=D('.4'),fee=D(0),event_at=NOW-timedelta(minutes=30))
        self.account.expected_positions={'X':D(1)}; self.account.expected_confirmed_usd=D('99.6')
        old=dict(self.trade,tid='old',amount='1',timestampms=int((NOW-timedelta(minutes=30)).timestamp()*1000))
        self.recover(trades=[old,dict(self.trade,amount='2')])
        self.assertEqual(self.account.expected_positions,{'X':D(3)})
        self.assertEqual(self.account.expected_confirmed_usd,D('98.8'))

    def test_duplicate_rows_fail_without_partial_writes(self):
        with self.assertRaisesRegex(RuntimeError,'duplicate'):
            self.recover(rows=[self.row,self.row])
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM execution_fill').fetchone()[0],0)

    def test_actual_nonzero_fee_is_accounted_and_missing_trade_blocks(self):
        with self.assertRaisesRegex(RuntimeError,'incomplete'):
            self.recover(trades=[])
        self.recover(trades=[dict(self.trade,fee_amount='.03')])
        self.assertEqual(self.account.expected_confirmed_usd,D('98.77'))

    def test_fractional_fill_cash_uses_contract_quote_precision(self):
        row = dict(self.row, filledQuantity='1.01')
        trade = dict(self.trade, amount='1.01')
        self.recover(rows=[row], trades=[trade])
        self.assertEqual(self.account.expected_confirmed_usd, D('99.59'))
        self.fixture.position(D('1.01'))
        self.fixture.cash('99.596')
        self.assertFalse(self.account.equity_classification_complete)
        self.fixture.cash('99.59')
        self.assertTrue(self.account.equity_classification_complete)

    def test_external_reduction_requires_explicit_authority_and_exact_owned_quantity(self):
        close=dict(self.row,orderId='manual',clientOrderId='manual-client',side='sell',filledQuantity='3')
        trade=dict(self.trade,tid='manual-trade',order_id='manual',client_order_id='manual-client',type='Sell',
                   timestampms=int((NOW-timedelta(minutes=1)).timestamp()*1000))
        self.recover(rows=[self.row,close],trades=[self.trade,trade])
        self.assertEqual(self.account.expected_positions,{'X':D(3)})
        self.recover(rows=[self.row,close],trades=[self.trade,trade],allow_external_reductions=True)
        self.assertEqual(self.account.expected_positions,{})
        self.assertEqual(self.account.expected_confirmed_usd,D(100))

    def test_external_reversal_is_rejected_atomically(self):
        close=dict(self.row,orderId='manual',clientOrderId='manual-client',side='sell',filledQuantity='4')
        trade=dict(self.trade,tid='manual-trade',order_id='manual',client_order_id='manual-client',type='Sell',amount='4',
                   timestampms=int((NOW-timedelta(minutes=1)).timestamp()*1000))
        with self.assertRaisesRegex(RuntimeError,'exceeds'):
            self.recover(rows=[self.row,close],trades=[self.trade,trade],allow_external_reductions=True)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM execution_fill').fetchone()[0],0)

    def test_provisional_aggregate_is_replaced_without_double_counting(self):
        self.store.record_fill(trade_id='maker-history:o',order_id='o',client_order_id=self.row['clientOrderId'],
            symbol='X',side='BUY',outcome='YES',quantity=D(3),price=D('.4'),fee=D(0),event_at=NOW-timedelta(minutes=10))
        self.account.expected_positions={'X':D(3)}; self.account.expected_confirmed_usd=D('98.8')
        self.recover()
        self.assertEqual(self.account.expected_positions,{'X':D(3)})
        self.assertEqual(self.account.expected_confirmed_usd,D('98.8'))
        self.assertEqual(self.store.connection.execute('SELECT trade_id FROM execution_fill').fetchall(),[('actual-trade',)])

    def test_external_late_duplicate_is_safe_but_unknown_trade_is_not(self):
        close=dict(self.row,orderId='manual',clientOrderId='manual-client',side='sell')
        trade=dict(self.trade,tid='manual-trade',order_id='manual',client_order_id='manual-client',type='Sell',
                   timestampms=int((NOW-timedelta(minutes=1)).timestamp()*1000))
        self.recover(rows=[self.row,close],trades=[self.trade,trade],allow_external_reductions=True)
        self.account.apply({'e':'orderUpdate','i':'manual','X':'FILLED','t':'manual-trade','Z':'3'})
        with self.assertRaises(RuntimeError):
            self.account.apply({'e':'orderUpdate','i':'manual','X':'PARTIALLY_FILLED','t':'unknown','Z':'1'})
        self.assertFalse(self.account.expected_positions)


class RecoveryReadsTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_read_is_bounded_and_uses_paced_list_endpoint(self):
        client=GeminiReadRestClient(AdmissionGovernor(),api_key='fake',api_secret='fake')
        client._private_list_read=AsyncMock(return_value=[])
        self.assertEqual(await client.recent_account_trades(symbol='X',since_ms=1000),[])
        client._private_list_read.assert_awaited_once_with('/v1/mytrades',
            {'symbol':'X','timestamp':1000,'limit_trades':500},lane='private_rest')
        for rows in ([{}]*500,[None]):
            client._private_list_read.return_value=rows
            with self.assertRaises(ApiError):
                await client.recent_account_trades(symbol='X',since_ms=1000)

    async def test_explicit_partial_external_close_reconciles_remaining_inventory_without_baseline_reset(self):
        f=flow_fixtures.FillContinuityTests()
        await f.asyncSetUp()
        try:
            f.inventory()
            r=f.r
            r.read_only=True
            r.account.subscription_acknowledged=True
            order=next(iter(r.account.reducer.orders.values()))
            now=flow_fixtures.NOW
            entry=dict(orderId=order.order_id,clientOrderId=order.client_order_id,symbol='X',side='buy',outcome='yes',
                quantity='10',filledQuantity='10',price='.46',status='filled',createdAt=(now-timedelta(seconds=1)).isoformat(),updatedAt=now.isoformat())
            close=dict(entry,orderId='manual',clientOrderId='manual-client',side='sell',quantity='4',filledQuantity='4')
            trade=dict(tid='trade-1',order_id=order.order_id,client_order_id=order.client_order_id,symbol='X',type='Buy',
                amount='10',price='.46',fee_amount='0',fee_currency='USD',timestampms=int(now.timestamp()*1000))
            external=dict(trade,tid='manual-trade',order_id='manual',client_order_id='manual-client',type='Sell',amount='4')
            r.rest.recent_account_trades=AsyncMock(return_value=[trade,external])
            r.rest.recent_order_history=AsyncMock(return_value=[entry,close])
            r.account.reducer.positions={'X':D(6)}
            r.account.confirmed_usd += D('1.84')
            r._risk_mark=AsyncMock()
            deadline=r.journal.get_deadline('X','YES')
            with patch('gemini_bot.orchestrator._utc_now',return_value=now):
                self.assertEqual(await r.reconcile_observed_external_reductions(),1)
            self.assertTrue(r.account.equity_classification_complete)
            self.assertEqual(r.account.expected_positions,{'X':D(6)})
            self.assertEqual(r.journal.get_deadline('X','YES'),deadline)
            self.assertEqual(r.journal.connection.execute("SELECT COUNT(*) FROM audit_event WHERE kind='operator_manual_flat_reconciled'").fetchone()[0],0)
            r.private.exit_position.assert_not_awaited()
        finally:
            await f.asyncTearDown()
