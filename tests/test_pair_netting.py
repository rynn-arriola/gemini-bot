"""Offline pair-netting accounting: both legs of a maker pair can fill."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from gemini_bot.account_runtime import AccountRuntime
from gemini_bot.cash_accounting import fill_cash_delta, netted_quantity
from gemini_bot.domain import Outcome, QuoteLeg, Side
from gemini_bot.execution import IntentLedger, OrderPurpose
from gemini_bot.storage import SQLiteJournal


NOW = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)


class CashDeltaTests(unittest.TestCase):
    def cash(self, side, outcome, qty, price, prior, purpose=OrderPurpose.ENTRY, fee="0"):
        return fill_cash_delta(
            side=side, outcome=outcome, purpose=purpose, quantity=D(qty), price=D(price),
            fee=D(fee), quote_asset_precision=2, prior_signed_position=prior,
        )

    def test_buy_yes_against_no_inventory_returns_collateral(self):
        self.assertEqual(self.cash(Side.BUY, Outcome.YES, "30", ".49", D("-30")), D("15.30"))

    def test_buy_no_against_yes_inventory_returns_collateral(self):
        self.assertEqual(self.cash(Side.BUY, Outcome.NO, "30", ".49", D("30")), D("15.30"))

    def test_partial_netting_splits_credit_and_debit(self):
        # 10 contracts close NO inventory, 20 open YES exposure.
        self.assertEqual(self.cash(Side.BUY, Outcome.YES, "30", ".49", D("-10")), D("-4.70"))
        self.assertEqual(
            netted_quantity(side=Side.BUY, outcome=Outcome.YES, quantity=D(30),
                            prior_signed_position=D(-10)),
            D(10),
        )

    def test_same_side_and_flat_buys_are_plain_debits(self):
        self.assertEqual(self.cash(Side.BUY, Outcome.YES, "30", ".49", D("0")), D("-14.70"))
        self.assertEqual(self.cash(Side.BUY, Outcome.YES, "30", ".49", D("30")), D("-14.70"))
        self.assertEqual(self.cash(Side.BUY, Outcome.NO, "30", ".49", D("-30")), D("-14.70"))

    def test_legacy_callers_keep_exit_semantics(self):
        self.assertEqual(
            self.cash(Side.BUY, Outcome.YES, "5.93", ".32", None, OrderPurpose.EXIT, ".07"),
            D("3.97"),
        )
        self.assertEqual(self.cash(Side.BUY, Outcome.YES, "30", ".49", None), D("-14.70"))

    def test_exact_exit_with_prior_matches_legacy(self):
        self.assertEqual(
            self.cash(Side.BUY, Outcome.YES, "5.93", ".32", D("-5.93"), OrderPurpose.EXIT, ".07"),
            D("3.97"),
        )


class PairNettingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLiteJournal(Path(self.directory.name) / "journal.sqlite")
        self.ledger = IntentLedger(self.store)
        self.runtime = AccountRuntime(self.ledger, self.store)
        self.runtime.quote_asset_precision = {"X": 2}
        self.runtime.expected_positions = {}
        self.runtime.expected_confirmed_usd = D("100")
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def fill(self, outcome, qty, price, at):
        self.counter += 1
        intent = self.ledger.prepare(
            QuoteLeg("X", outcome, Side.BUY, D(price), D(qty)), OrderPurpose.ENTRY,
        )
        self.ledger.mark_sent(intent.client_order_id)
        self.runtime.apply({
            "e": "orderUpdate", "i": f"o-{self.counter}", "c": intent.client_order_id,
            "s": "X", "S": "BUY", "O": outcome.value, "q": qty, "z": "0", "Z": qty,
            "p": price, "L": price, "n": "0", "X": "FILLED", "t": f"t-{self.counter}",
            "T": int(at.timestamp() * 1000),
        })

    def test_both_legs_filled_nets_flat_with_exact_cash_and_realized_spread(self):
        self.fill(Outcome.NO, "30", ".49", NOW)
        self.assertIsNotNone(self.store.get_deadline("X", "NO"))
        self.fill(Outcome.YES, "30", ".49", NOW + timedelta(seconds=1))
        self.assertEqual(self.runtime.expected_positions, {})
        # -14.70 for NO, then +15.30 collateral return for the netted YES.
        self.assertEqual(self.runtime.expected_confirmed_usd, D("100.60"))
        # The netting fill opens nothing, so it writes no YES deadline.
        self.assertIsNone(self.store.get_deadline("X", "YES"))
        realized, complete = self.store.realized_pnl_for_window(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1),
        )
        self.assertTrue(complete)
        self.assertEqual(realized, D("0.60"))

    def test_partial_opposite_fill_keeps_original_deadline_only(self):
        self.fill(Outcome.NO, "30", ".49", NOW)
        self.fill(Outcome.YES, "10", ".49", NOW + timedelta(seconds=1))
        self.assertEqual(self.runtime.expected_positions, {"X": D("-20")})
        self.assertEqual(self.runtime.expected_confirmed_usd, D("100") - D("14.70") + D("5.10"))
        self.assertIsNotNone(self.store.get_deadline("X", "NO"))
        self.assertIsNone(self.store.get_deadline("X", "YES"))

    def test_sign_flip_closes_old_lifecycle_and_opens_new_one(self):
        self.fill(Outcome.NO, "20", ".49", NOW)
        self.fill(Outcome.YES, "50", ".49", NOW + timedelta(seconds=1))
        self.assertEqual(self.runtime.expected_positions, {"X": D("30")})
        self.assertIsNotNone(self.store.get_deadline("X", "YES"))
        realized, complete = self.store.realized_pnl_for_window(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1),
        )
        self.assertTrue(complete)
        # Closed 20: -9.80 entry + 20 * 0.51 collateral credit = +0.40.
        self.assertEqual(realized, D("0.40"))


if __name__ == "__main__":
    unittest.main()
