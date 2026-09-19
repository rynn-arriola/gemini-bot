"""Regression coverage for fractional fills and collateral-return exits."""

from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.cash_accounting import fill_cash_delta
from gemini_bot.domain import Outcome, QuoteLeg, Side
from gemini_bot.execution import IntentLedger, OrderPurpose
from gemini_bot.storage import SQLiteJournal


NOW_MS = int(datetime(2026, 9, 19, 9, 47, tzinfo=timezone.utc).timestamp() * 1000)


class CashPrecisionTests(unittest.TestCase):
    def test_fractional_entry_debits_round_up_to_quote_precision(self) -> None:
        self.assertEqual(fill_cash_delta(
            side=Side.BUY, outcome=Outcome.NO, purpose=OrderPurpose.ENTRY,
            quantity=D("5.93"), price=D(".68"), fee=D("0"),
            quote_asset_precision=2,
        ), D("-4.04"))
        self.assertEqual(fill_cash_delta(
            side=Side.BUY, outcome=Outcome.NO, purpose=OrderPurpose.ENTRY,
            quantity=D("1.01"), price=D(".02"), fee=D("0"),
            quote_asset_precision=2,
        ), D("-.03"))

    def test_buy_yes_exit_credits_net_return_and_does_not_open_new_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                ledger = IntentLedger(journal)
                account = AccountRuntime(ledger, journal)
                account.quote_asset_precision = {"X": 2}
                account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "100", "f": "100"}]})
                account.apply({"e": "positionReport", "P": [], "u": 1})

                entry = ledger.prepare(
                    QuoteLeg("X", Outcome.NO, Side.BUY, D(".68"), D("5.93")),
                    OrderPurpose.ENTRY,
                )
                ledger.mark_sent(entry.client_order_id)
                account.apply({
                    "e": "orderUpdate", "i": "entry-order", "c": entry.client_order_id,
                    "s": "X", "S": "BUY", "O": "NO", "q": "5.93", "z": "0",
                    "Z": "5.93", "X": "FILLED", "t": "entry-trade", "L": ".68",
                    "n": "0", "T": NOW_MS,
                })
                self.assertEqual(account.expected_confirmed_usd, D("95.96"))
                self.assertEqual(account.expected_positions, {"X": D("-5.93")})
                self.assertIsNotNone(journal.get_deadline("X", "NO"))
                self.assertEqual(account.safety_symbols.get_nowait(), "X")

                account.apply({
                    "e": "positionReport", "P": [{
                        "t": "ec", "s": "X", "a": [{"t": "position", "v": "-5.93"}],
                    }], "u": 2,
                })
                account.apply({
                    "e": "balanceUpdate", "B": [{"a": "USD", "c": "95.96", "f": "95.96"}],
                })
                exit_intent = ledger.prepare(
                    QuoteLeg("X", Outcome.YES, Side.BUY, D(".32"), D("5.93")),
                    OrderPurpose.EXIT,
                )
                ledger.mark_sent(exit_intent.client_order_id)
                account.apply({
                    "e": "orderUpdate", "i": "exit-order", "c": exit_intent.client_order_id,
                    "s": "X", "S": "BUY", "O": "YES", "q": "5.93", "z": "0",
                    "Z": "5.93", "X": "FILLED", "t": "exit-trade", "L": ".32",
                    "n": "0", "T": NOW_MS + 1000,
                })
                self.assertEqual(account.expected_confirmed_usd, D("99.99"))
                self.assertEqual(account.expected_positions, {})
                self.assertTrue(account.safety_symbols.empty())
                self.assertIsNone(journal.get_deadline("X", "YES"))
                self.assertEqual(
                    journal.connection.execute(
                        "SELECT cash_delta FROM execution_fill ORDER BY event_at"
                    ).fetchall(),
                    [("-4.04",), ("4.03",)],
                )
            finally:
                journal.close()

    def test_legacy_fill_repair_requires_exact_fresh_balance_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(Path(directory) / "journal.sqlite")
            try:
                ledger = IntentLedger(journal)
                account = AccountRuntime(ledger, journal)
                account.quote_asset_precision = {"X": 2}
                account.apply({"e": "balanceUpdate", "B": [{"a": "USD", "c": "100", "f": "100"}]})
                account.apply({"e": "positionReport", "P": [], "u": 1})
                journal.set_runtime_state(
                    "cash_reconciliation_watermark", "verified",
                    updated_at=datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc),
                )
                intent = ledger.prepare(
                    QuoteLeg("X", Outcome.NO, Side.BUY, D(".68"), D("5.93")),
                    OrderPurpose.ENTRY,
                )
                ledger.mark_sent(intent.client_order_id)
                ledger.acknowledge(intent.client_order_id, "entry-order")
                ledger.mark_terminal(intent.client_order_id)
                journal.record_fill(
                    trade_id="entry-trade", order_id="entry-order",
                    client_order_id=intent.client_order_id, symbol="X", side="BUY",
                    outcome="NO", quantity=D("5.93"), price=D(".68"), fee=D("0"),
                    cash_delta=D("-4.0324"),
                    event_at=datetime(2026, 9, 19, 9, 47, tzinfo=timezone.utc),
                )
                journal.connection.execute(
                    "UPDATE execution_fill SET cash_delta='' WHERE trade_id='entry-trade'"
                )
                account.expected_confirmed_usd = D("95.9676")
                account.expected_positions = {"X": D("-5.93")}
                account.reducer.positions = {"X": D("-5.93")}
                account.confirmed_usd = D("95.96")
                self.assertEqual(account.reconcile_legacy_quote_precision_fills(
                    observed_at=datetime(2026, 9, 19, 10, tzinfo=timezone.utc),
                ), 1)
                self.assertEqual(account.expected_confirmed_usd, D("95.96"))
                self.assertEqual(journal.connection.execute(
                    "SELECT cash_delta FROM execution_fill WHERE trade_id='entry-trade'"
                ).fetchone(), ("-4.04",))
                self.assertEqual(account.reconcile_legacy_quote_precision_fills(
                    observed_at=datetime(2026, 9, 19, 10, 1, tzinfo=timezone.utc),
                ), 0)
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
