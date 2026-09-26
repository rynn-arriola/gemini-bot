"""Offline depth-shield placement; all books are synthetic."""
from datetime import datetime, timezone
from decimal import Decimal as D
import unittest

from gemini_bot.domain import ContractRules, Outcome
from gemini_bot.market_state import LocalBook
from gemini_bot.shield import (
    ShieldPolicy, entry_required, keep_required, outcome_levels, resting_shield,
    shielded_price, shielded_quote,
)

NOW = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
POLICY = ShieldPolicy(D(500), D(".60"), D(3), D(".10"), D(300))
RULES = ContractRules("X", D(".01"), D("1"), D("1"))


def book(bids, asks):
    b = LocalBook("X")
    b.apply_snapshot([(D(p), D(q)) for p, q in bids], [(D(p), D(q)) for p, q in asks], 1, 1, NOW)
    return b


class ShieldPolicyTests(unittest.TestCase):
    def test_policy_bounds(self):
        POLICY.validate()
        for bad in (
            ShieldPolicy(D(5), D(".6"), D(3), D(".1"), D(300)),
            ShieldPolicy(D(500), D("1"), D(3), D(".1"), D(300)),
            ShieldPolicy(D(500), D(".6"), D(-1), D(".1"), D(300)),
            ShieldPolicy(D(500), D(".6"), D(3), D(".11"), D(300)),
        ):
            with self.assertRaises(ValueError):
                bad.validate()

    def test_flow_floor_raises_requirements(self):
        self.assertEqual(entry_required(POLICY), D(500))
        self.assertEqual(entry_required(POLICY, D(250)), D(750))
        self.assertEqual(keep_required(POLICY), D(300))
        self.assertEqual(keep_required(POLICY, D(250)), D(450))
        self.assertEqual(entry_required(POLICY, uptime_first=True), D(300))


class ShieldedPriceTests(unittest.TestCase):
    def test_picks_first_level_with_enough_volume_ahead(self):
        b = book([(".50", "200"), (".49", "250"), (".48", "400")], [(".52", "900")])
        # 200 < 500, 450 < 500, 850 >= 500 -> join .48 behind 850 contracts.
        self.assertEqual(
            shielded_price(outcome_levels(b, Outcome.YES), midpoint=D(".51"),
                           tick=D(".01"), required=D(500), max_distance=D(".10")),
            D(".48"),
        )

    def test_no_leg_uses_complementary_asks(self):
        b = book([(".50", "900")], [(".52", "100"), (".53", "450")])
        levels = outcome_levels(b, Outcome.NO)
        self.assertEqual(levels, [(D(".48"), D("100")), (D(".47"), D("450"))])
        self.assertEqual(
            shielded_price(levels, midpoint=D(".49"), tick=D(".01"),
                           required=D(500), max_distance=D(".10")),
            D(".47"),
        )

    def test_refuses_when_required_depth_is_beyond_the_distance_limit(self):
        b = book([(".50", "100"), (".38", "900")], [(".52", "900")])
        self.assertIsNone(shielded_price(outcome_levels(b, Outcome.YES), midpoint=D(".51"),
                                         tick=D(".01"), required=D(500), max_distance=D(".10")))

    def test_refuses_thin_book(self):
        # The September 22 ZEC fill had about 208 contracts ahead.
        b = book([(".24", "208.33")], [(".26", "500")])
        self.assertIsNone(shielded_price(outcome_levels(b, Outcome.YES), midpoint=D(".25"),
                                         tick=D(".01"), required=D(500), max_distance=D(".10")))

    def test_quote_needs_both_legs_shielded(self):
        b = book([(".50", "600")], [(".52", "600")])
        quote = shielded_quote(b, RULES, D(50), {Outcome.YES: D(500), Outcome.NO: D(500)}, D(".10"))
        self.assertEqual((quote.yes.price, quote.no.price), (D(".50"), D(".48")))
        self.assertEqual((quote.yes.quantity, quote.no.quantity), (D(50), D(50)))
        with self.assertRaisesRegex(ValueError, "insufficient_shield"):
            shielded_quote(b, RULES, D(50), {Outcome.YES: D(500), Outcome.NO: D(700)}, D(".10"))


class RestingShieldTests(unittest.TestCase):
    def test_counts_better_levels_plus_estimated_queue_ahead(self):
        b = book([(".50", "200"), (".49", "700")], [(".52", "100")])
        self.assertEqual(resting_shield(b, Outcome.YES, D(".49"), D("150")), D("350"))
        self.assertEqual(resting_shield(b, Outcome.YES, D(".49"), None), D("200"))

    def test_draining_better_levels_trips_near_fill(self):
        b = book([(".50", "400"), (".49", "700")], [(".52", "100")])
        self.assertGreaterEqual(resting_shield(b, Outcome.YES, D(".49"), D(0)), keep_required(POLICY))
        b.apply_delta([(D(".50"), D("150"))], [], 2, 2, NOW)
        self.assertLess(resting_shield(b, Outcome.YES, D(".49"), D(0)), keep_required(POLICY))

    def test_no_leg_resting_shield(self):
        b = book([(".50", "100")], [(".52", "350"), (".53", "900")])
        # Our BUY NO at .47 rests behind the .52 YES ask (NO .48).
        self.assertEqual(resting_shield(b, Outcome.NO, D(".47"), D("20")), D("370"))


if __name__ == "__main__":
    unittest.main()
