from __future__ import annotations

import policy_v8  # noqa: F401  (pins the v8-era default policy)
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import asyncio
import unittest
from pathlib import Path
import tempfile
import json
import logging
import base64

from gemini_bot.auth import NonceFactory, websocket_auth_headers
from gemini_bot.config import ConfigurationError, RiskConfig, StrategyConfig, load_policy
from gemini_bot.domain import BookTop, ContractRules, Outcome, QuoteLeg, RewardPool, Side, UptimeProgress
from gemini_bot.engine import LiveEngineGate, SafetyViolation
from gemini_bot.execution import (
    IntentLedger,
    IntentState,
    OrderPurpose,
    expected_signed_position_delta,
    exit_request,
    immediate_entry_request,
    place_request,
)
from gemini_bot.inventory import ExitAction, Position, TimedExitPolicy, pending_capacity, plan_timed_exit
from gemini_bot.market_state import LocalBook, SequenceGap
from gemini_bot.rate_limits import AdmissionGovernor, Priority
from gemini_bot.reward_ranking import (
    RankInput,
    rank_candidate,
    rank_unique_pools,
    select_sticky_candidates,
)
from gemini_bot.scheduler import DirtySymbolScheduler
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.storage import SQLiteJournal, WindowsOwnerLock
from gemini_bot.risk_windows import RiskWindowPolicy, evaluate_window
from gemini_bot.account_state import AccountReducer
from gemini_bot.exit_pricing import exact_crossing_exit
from gemini_bot.operational_logging import ErrorClass, OperationalLogger, configure_logging
from gemini_bot.error_policy import FailureContext, RecoveryAction, decide_recovery
from gemini_bot.risk_gate import ExposureState, admit_quote_pair
from gemini_bot.rest_client import GeminiReadRestClient, private_headers
from gemini_bot.catalog import join_reward_contracts
from gemini_bot.profitability import (
    ProfitabilityEvidence,
    ProfitabilityTracker,
    active_maker_rebate_fractions,
    estimated_round_trip_fees,
)

D = Decimal


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def book(symbol: str = "GEMI-TEST") -> BookTop:
    return BookTop(symbol, Decimal("0.48"), Decimal("100"), Decimal("0.52"), Decimal("100"), 10, NOW)


def rules(symbol: str = "GEMI-TEST") -> ContractRules:
    return ContractRules(symbol, Decimal("0.01"), Decimal("1"), Decimal("1"))


class ConfigTests(unittest.TestCase):
    def test_strategy_uses_ten_contract_base(self) -> None:
        value = StrategyConfig()
        value.validate()
        self.assertEqual(value.quote_quantity, Decimal("10"))

    def test_pending_cap_cannot_exceed_forty(self) -> None:
        risk = RiskConfig(Decimal("100"), Decimal("20"), Decimal("10"), Decimal("5"), max_pending_orders=41)
        with self.assertRaises(ConfigurationError):
            risk.validate()

    def test_approved_policy_has_one_stop_and_expected_caps(self) -> None:
        strategy, risk = load_policy()
        self.assertEqual(strategy.quote_quantity, Decimal("30"))
        self.assertEqual(risk.max_gross_reservation_usd, Decimal("300"))
        self.assertEqual(risk.max_market_exposure_usd, Decimal("90"))
        self.assertEqual(risk.max_stressed_exit_loss_usd, Decimal("40"))
        self.assertEqual(risk.risk_window_loss_usd, Decimal("30"))
        self.assertEqual(risk.max_two_sided_quotes, 20)
        self.assertEqual(risk.max_pending_orders, 40)
        self.assertEqual(risk.max_tracked_markets, 32)

    def test_approved_exposure_gate_accepts_base_pair(self) -> None:
        strategy, risk = load_policy()
        candidate = rank_candidate(
            RankInput(RewardPool("p", Decimal("50")), rules(), book()), now=NOW,
            quantity=strategy.quote_quantity, offset_ticks=strategy.quote_offset_ticks,
        )
        result = admit_quote_pair(
            risk, ExposureState(), candidate.quote,
            stressed_exit_loss_usd=Decimal("5"), opens_new_owner=True,
        )
        self.assertTrue(result.allowed)

    def test_approved_thirty_contract_size_respects_unchanged_gross_cap(self) -> None:
        strategy, risk = load_policy()
        candidate = rank_candidate(
            RankInput(RewardPool("p", Decimal("50")), rules(), book()), now=NOW,
            quantity=strategy.quote_quantity, offset_ticks=strategy.quote_offset_ticks,
        )
        self.assertEqual(candidate.quote.yes.quantity, Decimal("30"))
        self.assertEqual(candidate.quote.no.quantity, Decimal("30"))
        state = ExposureState(gross_reservation_usd=Decimal("290"))
        result = admit_quote_pair(
            risk, state, candidate.quote,
            stressed_exit_loss_usd=Decimal("5"), opens_new_owner=True,
        )
        self.assertIn("gross exposure cap", result.reasons)

    def test_approved_exposure_gate_blocks_gross_and_stressed_loss(self) -> None:
        strategy, risk = load_policy()
        candidate = rank_candidate(
            RankInput(RewardPool("p", Decimal("50")), rules(), book()), now=NOW,
            quantity=strategy.quote_quantity, offset_ticks=strategy.quote_offset_ticks,
        )
        state = ExposureState(gross_reservation_usd=Decimal("295"))
        result = admit_quote_pair(
            risk, state, candidate.quote,
            stressed_exit_loss_usd=Decimal("41"), opens_new_owner=True,
        )
        self.assertIn("gross exposure cap", result.reasons)
        self.assertIn("stressed-exit entry gate", result.reasons)


class RankingTests(unittest.TestCase):
    def test_base_quote_is_two_sided_ten_and_eligible(self) -> None:
        candidate = rank_candidate(RankInput(RewardPool("p1", Decimal("100")), rules(), book()), now=NOW)
        self.assertTrue(candidate.eligible_now)
        self.assertEqual(candidate.quote.yes.quantity, Decimal("10"))
        self.assertEqual(candidate.quote.no.quantity, Decimal("10"))
        self.assertEqual(candidate.quote.yes.price, Decimal("0.46"))
        self.assertEqual(candidate.quote.no.price, Decimal("0.46"))

    def test_shared_pool_is_counted_once(self) -> None:
        items = [
            RankInput(RewardPool("shared:1", Decimal("100")), rules("A"), book("A")),
            RankInput(RewardPool("shared:1", Decimal("100")), rules("B"), book("B")),
        ]
        self.assertEqual(len(rank_unique_pools(items, now=NOW)), 1)

    def test_shared_pool_uses_v2_potential_when_only_uptime_is_overridden(self) -> None:
        progress = UptimeProgress(100, 0, 0)
        items = [
            RankInput(
                RewardPool("shared:1", D("100")), rules("A"), book("A"), progress,
                profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("5")),
            ),
            RankInput(
                RewardPool("shared:1", D("100")), rules("B"), book("B"), progress,
                profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("1")),
            ),
        ]
        selected = rank_unique_pools(items, now=NOW)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].symbol, "B")
        self.assertEqual(selected[0].reasons, (
            "50% uptime is no longer reachable in the estimated window",
        ))

    def test_impossible_uptime_is_ineligible(self) -> None:
        progress = UptimeProgress(80, 10, 20)
        result = rank_candidate(RankInput(RewardPool("p", Decimal("50")), rules(), book(), progress), now=NOW)
        self.assertFalse(result.uptime_feasible)
        self.assertFalse(result.eligible_now)

    def test_stale_book_cannot_rank(self) -> None:
        result = rank_candidate(RankInput(RewardPool("p", Decimal("50")), rules(), book()), now=NOW + timedelta(seconds=4))
        self.assertFalse(result.eligible_now)
        self.assertIn("stale market data", result.reasons)

    def test_v2_prefers_better_midpoint_quality_with_same_pool_economics(self) -> None:
        close = BookTop("A", D(".49"), D("100"), D(".51"), D("100"), 1, NOW)
        wide = BookTop("B", D(".45"), D("100"), D(".55"), D("100"), 1, NOW)
        ranked = rank_unique_pools([
            RankInput(RewardPool("close", D("100"), qualifying_maker_count=5), rules("A"), close),
            RankInput(RewardPool("wide", D("100"), qualifying_maker_count=5), rules("B"), wide),
        ], now=NOW)
        self.assertEqual(ranked[0].symbol, "A")
        self.assertGreater(ranked[0].profitability_per_capital_hour, ranked[1].profitability_per_capital_hour)

    def test_v2_subtracts_execution_cost_instead_of_only_ranking_rewards(self) -> None:
        safe = RankInput(
            RewardPool("safe", D("100"), qualifying_maker_count=5), rules("A"), book("A"),
            profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("1")),
        )
        costly = RankInput(
            RewardPool("costly", D("100"), qualifying_maker_count=5), rules("B"), book("B"),
            profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("10")),
        )
        ranked = rank_unique_pools([costly, safe], now=NOW)
        self.assertEqual(ranked[0].symbol, "A")
        self.assertEqual(
            ranked[0].expected_net_profit_usd,
            ranked[0].expected_reward_usd - D("1"),
        )

    def test_v2_blocks_non_positive_conservative_net(self) -> None:
        result = rank_candidate(RankInput(
            RewardPool("loss", D("10"), qualifying_maker_count=100),
            rules(), book(),
            profitability=ProfitabilityEvidence(expected_execution_cost_usd=D("10")),
        ), now=NOW)
        self.assertFalse(result.eligible_now)
        self.assertIn("non-positive conservative net-profit estimate", result.reasons)

    def test_v2_sticky_cohort_resists_rank_churn_until_incumbent_is_unsafe(self) -> None:
        challenger = rank_candidate(RankInput(
            RewardPool("challenger", D("200")), rules("A"), book("A"),
        ), now=NOW)
        incumbent = rank_candidate(RankInput(
            RewardPool("incumbent", D("100")), rules("B"), book("B"),
        ), now=NOW)
        selected, pool_ids = select_sticky_candidates(
            [challenger, incumbent], sticky_pool_ids={"incumbent"},
            locked_pool_ids=set(), permitted_symbols={"A", "B"},
            maximum_active_pools=1, actionable=lambda _: True,
            entry_time_valid=lambda _: True,
        )
        self.assertEqual([item.pool_id for item in selected], ["incumbent"])
        self.assertEqual(pool_ids, {"incumbent"})
        replaced, pool_ids = select_sticky_candidates(
            [challenger, incumbent], sticky_pool_ids={"incumbent"},
            locked_pool_ids=set(), permitted_symbols={"A", "B"},
            maximum_active_pools=1,
            actionable=lambda item: item.pool_id != "incumbent",
            entry_time_valid=lambda _: True,
        )
        self.assertEqual([item.pool_id for item in replaced], ["challenger"])
        self.assertEqual(pool_ids, {"challenger"})

    def test_open_position_pool_reserves_active_capacity(self) -> None:
        challenger = rank_candidate(RankInput(
            RewardPool("challenger", D("200")), rules("A"), book("A"),
        ), now=NOW)
        selected, pool_ids = select_sticky_candidates(
            [challenger], sticky_pool_ids=set(),
            locked_pool_ids={"position-pool"}, permitted_symbols={"A"},
            maximum_active_pools=1, actionable=lambda _: True,
            entry_time_valid=lambda _: True,
        )
        self.assertEqual(selected, [])
        self.assertEqual(pool_ids, {"position-pool"})


class ProfitabilityTests(unittest.TestCase):
    def test_fee_estimate_includes_maker_rebate_and_taker_exit(self) -> None:
        self.assertEqual(
            estimated_round_trip_fees(
                quantity=D("10"), entry_price=D(".50"), exit_price=D(".50"),
                maker_rebate_fraction=D(".30"),
            ),
            D(".18"),
        )

    def test_current_rebate_rule_resolution_prefers_category_rule(self) -> None:
        rows = [
            {"effective_from": "2026-01-01T00:00:00Z", "rebate_multiplier_bps": 3000},
            {"effective_from": "2026-02-01T00:00:00Z", "rebate_multiplier_bps": 5000,
             "effective_to": "2026-03-01T00:00:00Z", "category": "Sports"},
            {"effective_from": "2026-03-01T00:00:00Z", "rebate_multiplier_bps": 2500,
             "category": "Sports"},
        ]
        active = active_maker_rebate_fractions(rows, now=NOW)
        self.assertEqual(active["*"], D(".30"))
        self.assertEqual(active["sports"], D(".25"))

    def test_trade_telemetry_projects_touch_cost_and_survives_restart(self) -> None:
        tracker = ProfitabilityTracker(window_minutes=30)
        local = LocalBook("X")
        local.apply_snapshot([(D(".49"), D("100"))], [(D(".51"), D("100"))], 1, 1, NOW)
        for minute in range(30):
            tracker.observe_book(local, observed_at=NOW - timedelta(minutes=29 - minute))
        tracker.observe_trade(
            {"s": "X", "t": 1, "p": ".47", "q": "10", "m": True},
            observed_at=NOW,
        )
        quote = rank_candidate(
            RankInput(RewardPool("p", D("100")), rules("X"), local.top()), now=NOW,
        ).quote
        evidence = tracker.estimate(
            symbol="X", quote=quote, book=local, tick_size=D(".01"),
            planned_minutes=D("1080"), now=NOW, maker_rebate_fraction=D(".30"),
        )
        self.assertEqual(evidence.data_confidence, D("1"))
        self.assertGreater(evidence.estimated_daily_fills, D("0"))
        self.assertGreater(evidence.expected_execution_cost_usd, D("0"))
        restored = ProfitabilityTracker(window_minutes=30)
        restored.loads(tracker.dumps(now=NOW), now=NOW)
        again = restored.estimate(
            symbol="X", quote=quote, book=local, tick_size=D(".01"),
            planned_minutes=D("1080"), now=NOW, maker_rebate_fraction=D(".30"),
        )
        self.assertEqual(again, evidence)

    def test_duplicate_trade_id_is_not_double_counted_across_restart(self) -> None:
        tracker = ProfitabilityTracker()
        frame = {"s": "X", "t": 7, "p": ".47", "q": "10", "m": True}
        tracker.observe_trade(frame, observed_at=NOW)
        tracker.observe_trade(frame, observed_at=NOW)
        saved = tracker.dumps(now=NOW)
        self.assertEqual(len(json.loads(saved)["trades"]["X"]), 1)
        restored = ProfitabilityTracker()
        restored.loads(saved, now=NOW)
        restored.observe_trade(frame, observed_at=NOW + timedelta(seconds=1))
        self.assertEqual(len(json.loads(restored.dumps(now=NOW))["trades"]["X"]), 1)


class BookTests(unittest.TestCase):
    def test_sequence_gap_invalidates_book(self) -> None:
        local = LocalBook("X")
        local.apply_snapshot([(Decimal(".48"), Decimal("10"))], [(Decimal(".52"), Decimal("10"))], 1, 5, NOW)
        with self.assertRaises(SequenceGap):
            local.apply_delta([], [], 7, 7, NOW)
        self.assertFalse(local.synchronized)

    def test_valid_delta_updates_top(self) -> None:
        local = LocalBook("X")
        local.apply_snapshot([(Decimal(".48"), Decimal("10"))], [(Decimal(".52"), Decimal("10"))], 1, 5, NOW)
        local.apply_delta([(Decimal(".49"), Decimal("4"))], [], 6, 6, NOW)
        self.assertEqual(local.top().bid, Decimal(".49"))


class ExecutionTests(unittest.TestCase):
    def test_moc_request_and_unknown_retry_block(self) -> None:
        ranked = rank_candidate(RankInput(RewardPool("p", Decimal("50")), rules(), book()), now=NOW)
        ledger = IntentLedger()
        intent = ledger.prepare(ranked.quote.yes, OrderPurpose.ENTRY)
        request = place_request(1, intent)
        self.assertEqual(request["params"]["timeInForce"], "MOC")
        ledger.mark_sent(intent.client_order_id)
        self.assertEqual(intent.state, IntentState.SENT_UNKNOWN)
        self.assertFalse(ledger.may_retry(intent.client_order_id))

    def test_commissioning_entry_is_limit_ioc(self) -> None:
        leg = QuoteLeg("X", Outcome.YES, Side.BUY, Decimal(".40"), Decimal("10"))
        intent = IntentLedger().prepare(leg, OrderPurpose.ENTRY)
        request = immediate_entry_request(2, intent)
        self.assertEqual(request["params"]["type"], "LIMIT")
        self.assertEqual(request["params"]["timeInForce"], "IOC")
        self.assertLessEqual(len(intent.client_order_id), 36)
        self.assertFalse(IntentLedger().owns("missing"))

    def test_engine_requires_complete_reconciliation(self) -> None:
        gate = LiveEngineGate()
        gate.begin_reconciliation()
        with self.assertRaises(SafetyViolation):
            gate.mark_ready()
        for name in gate.reconciliation.__dataclass_fields__:
            setattr(gate.reconciliation, name, True)
        gate.mark_ready()
        gate.activate(True)
        self.assertTrue(gate.may_submit)

    def test_partial_fill_deduplicates_and_requests_entry_cancellation(self) -> None:
        reducer = AccountReducer()
        base = {
            "e": "orderUpdate", "E": 10, "T": 10, "s": "gemi-test", "i": 7,
            "c": "client-7", "S": "BUY", "O": "YES", "q": "10", "z": "10", "X": "NEW",
        }
        reducer.apply_order_update(base)
        fill = {**base, "E": 11, "T": 11, "X": "PARTIALLY_FILLED", "z": "7", "Z": "3", "t": 91}
        first = reducer.apply_order_update(fill)
        duplicate = reducer.apply_order_update(fill)
        self.assertEqual(first.newly_filled, Decimal("3"))
        self.assertTrue(first.cancel_other_entry_orders)
        self.assertEqual(duplicate.newly_filled, Decimal("0"))
        self.assertEqual(reducer.orders["7"].cumulative_filled, Decimal("3"))

    def test_exact_position_controls_exit_side_and_quantity(self) -> None:
        reducer = AccountReducer()
        reducer.apply_position_report({
            "e": "positionReport", "u": 20,
            "P": [{"t": "ec", "s": "gemi-test", "a": [{"t": "position", "v": "-3.5"}]}],
        })
        leg = reducer.exact_exit_leg("GEMI-TEST", Decimal(".40"))
        self.assertEqual(leg.side.value, "BUY")
        self.assertEqual(leg.outcome.value, "YES")
        self.assertEqual(leg.quantity, Decimal("3.5"))
        intent = IntentLedger().prepare(leg, OrderPurpose.EXIT)
        request = exit_request(3, intent, signed_position=Decimal("-3.5"))
        self.assertEqual(request["params"]["timeInForce"], "IOC")
        self.assertEqual(request["params"]["quantity"], "3.5")

    def test_complete_empty_position_snapshot_clears_stale_position(self) -> None:
        reducer = AccountReducer()
        reducer.apply_position_report({
            "e": "positionReport", "u": 20,
            "P": [{"t": "ec", "s": "X", "a": [{"t": "position", "v": "10"}]}],
        }, complete_snapshot=True)
        changed = reducer.apply_position_report(
            {"e": "positionReport", "u": 21, "P": []},
            complete_snapshot=True,
        )
        self.assertEqual(changed, {"X"})
        self.assertEqual(reducer.positions, {})

    def test_all_four_gemini_side_outcome_deltas_are_explicit(self) -> None:
        cases = (
            (Side.BUY, Outcome.YES, Decimal("10")),
            (Side.SELL, Outcome.YES, Decimal("-10")),
            (Side.BUY, Outcome.NO, Decimal("-10")),
            (Side.SELL, Outcome.NO, Decimal("10")),
        )
        for side, outcome, expected in cases:
            with self.subTest(side=side, outcome=outcome):
                leg = QuoteLeg("X", outcome, side, Decimal(".40"), Decimal("10"))
                self.assertEqual(expected_signed_position_delta(leg), expected)

    def test_exit_rejects_wrong_outcome_or_non_exact_quantity(self) -> None:
        wrong_outcome = QuoteLeg("X", Outcome.NO, Side.SELL, Decimal(".40"), Decimal("10"))
        wrong_quantity = QuoteLeg("X", Outcome.YES, Side.SELL, Decimal(".40"), Decimal("9"))
        for leg in (wrong_outcome, wrong_quantity):
            intent = IntentLedger().prepare(leg, OrderPurpose.EXIT)
            with self.assertRaises(ValueError):
                exit_request(4, intent, signed_position=Decimal("10"))

    def test_entry_and_exit_purposes_cannot_cross(self) -> None:
        buy = QuoteLeg("X", Outcome.YES, Side.BUY, Decimal(".40"), Decimal("10"))
        sell = QuoteLeg("X", Outcome.YES, Side.SELL, Decimal(".40"), Decimal("10"))
        with self.assertRaises(ValueError):
            place_request(5, IntentLedger().prepare(buy, OrderPurpose.EXIT))
        with self.assertRaises(ValueError):
            exit_request(
                6,
                IntentLedger().prepare(sell, OrderPurpose.ENTRY),
                signed_position=Decimal("10"),
            )

    def test_cancelled_order_does_not_hide_prior_fill(self) -> None:
        reducer = AccountReducer()
        reducer.apply_order_update({
            "e": "orderUpdate", "T": 1, "s": "X", "i": "1", "c": "c1",
            "S": "BUY", "O": "YES", "q": "10", "z": "10", "X": "NEW",
        })
        transition = reducer.apply_order_update({"e": "orderUpdate", "T": 2, "i": "1", "X": "CANCELED", "Z": "2"})
        self.assertEqual(transition.newly_filled, Decimal("2"))
        self.assertEqual(reducer.orders["1"].remaining_quantity, Decimal("0"))
        self.assertEqual(reducer.pending_order_count(), 0)

    def test_exact_yes_exit_walks_haircut_depth(self) -> None:
        local = LocalBook("X")
        local.apply_snapshot(
            [(Decimal(".48"), Decimal("10")), (Decimal(".47"), Decimal("20"))],
            [(Decimal(".52"), Decimal("10"))], 1, 1, NOW,
        )
        leg = exact_crossing_exit(local, Decimal("10"), depth_haircut=Decimal(".5"))
        self.assertEqual(leg.outcome, Outcome.YES)
        self.assertEqual(leg.side.value, "SELL")
        self.assertEqual(leg.price, Decimal(".47"))
        self.assertEqual(leg.quantity, Decimal("10"))

    def test_exact_no_exit_buys_yes_from_asks(self) -> None:
        local = LocalBook("X")
        local.apply_snapshot(
            [(Decimal(".48"), Decimal("10"))],
            [(Decimal(".52"), Decimal("10")), (Decimal(".53"), Decimal("20"))], 1, 1, NOW,
        )
        leg = exact_crossing_exit(local, Decimal("-10"), depth_haircut=Decimal(".5"))
        self.assertEqual(leg.outcome, Outcome.YES)
        self.assertEqual(leg.side, Side.BUY)
        self.assertEqual(leg.price, Decimal(".53"))
        self.assertEqual(leg.quantity, Decimal("10"))

    def test_thin_exit_depth_prices_exact_position_ioc(self) -> None:
        local = LocalBook("X")
        local.apply_snapshot(
            [(Decimal(".48"), Decimal("5")), (Decimal(".20"), Decimal("5"))],
            [(Decimal(".52"), Decimal("5")), (Decimal(".80"), Decimal("5"))],
            1, 1, NOW,
        )
        yes = exact_crossing_exit(local, Decimal("10"))
        no = exact_crossing_exit(local, Decimal("-10"))
        self.assertEqual((yes.side, yes.price, yes.quantity),
                         (Side.SELL, Decimal(".48"), Decimal("10")))
        self.assertEqual((no.side, no.outcome, no.price, no.quantity),
                         (Side.BUY, Outcome.YES, Decimal(".52"), Decimal("10")))

    def test_exit_without_executable_depth_still_waits(self) -> None:
        local = LocalBook("X")
        local.apply_snapshot([], [], 1, 1, NOW)
        self.assertIsNone(exact_crossing_exit(local, Decimal("10")))
        self.assertIsNone(exact_crossing_exit(local, Decimal("-10")))


class InventoryTests(unittest.TestCase):
    def position(self) -> Position:
        return Position("X", Outcome.YES, Decimal("10"), Decimal(".4"), NOW, NOW + timedelta(hours=1))

    def test_pending_cap_excludes_positions(self) -> None:
        self.assertTrue(pending_capacity(28, 2))
        self.assertFalse(pending_capacity(29, 2))

    def test_cancel_before_timed_exit(self) -> None:
        result = plan_timed_exit(
            self.position(), TimedExitPolicy(), now=NOW + timedelta(seconds=901),
            building_orders_open=1, all_order_state_known=True, account_state_fresh=True,
            book_fresh=True,
        )
        self.assertEqual(result.action, ExitAction.CANCEL_BUILDING_ORDERS)

    def test_pre_expiry_deadline_triggers_exact_exit(self) -> None:
        position = self.position()
        result = plan_timed_exit(
            position, TimedExitPolicy(), now=position.expiry_at - timedelta(seconds=299),
            building_orders_open=0, all_order_state_known=True, account_state_fresh=True,
            book_fresh=True,
        )
        self.assertEqual(result.action, ExitAction.PLACE_EXPIRY_EXIT)

    def test_unknown_state_halts_at_deadline(self) -> None:
        result = plan_timed_exit(
            self.position(), TimedExitPolicy(), now=NOW + timedelta(seconds=901),
            building_orders_open=0, all_order_state_known=False, account_state_fresh=True,
            book_fresh=True,
        )
        self.assertEqual(result.action, ExitAction.HALT_UNKNOWN_STATE)


class RiskWindowTests(unittest.TestCase):
    def test_six_hour_eastern_boundaries(self) -> None:
        policy = RiskWindowPolicy(Decimal("30"))
        start, end = policy.bounds(datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(start.hour, 10)  # 06:00 EDT
        self.assertEqual(end.hour, 16)    # 12:00 EDT

    def test_cap_stays_fixed_until_realized_hard_stop(self) -> None:
        policy = RiskWindowPolicy(Decimal("30"))
        self.assertEqual(policy.pending_order_cap(Decimal("29.99")), 30)
        self.assertEqual(policy.pending_order_cap(Decimal("30")), 0)

    def test_open_position_marks_do_not_reduce_pending_capacity(self) -> None:
        mark = evaluate_window(
            RiskWindowPolicy(Decimal("30")), now=NOW,
            realized_pnl=Decimal("-10"),
        )
        self.assertEqual(mark.pending_order_cap, 30)


class InfrastructureTests(unittest.TestCase):
    def test_emergency_cancel_does_not_bypass_server_cooldown(self) -> None:
        governor = AdmissionGovernor()
        governor.apply_server_cooldown("private_ws", 10, now=100)
        self.assertEqual(governor.admit("private_ws", Priority.EMERGENCY_CANCEL, now=101), 9)

    def test_nonce_is_monotonic(self) -> None:
        factory = NonceFactory()
        self.assertGreater(factory.next(), 0)
        self.assertGreater(factory.next(), 0)

    def test_auth_headers_do_not_include_raw_secret(self) -> None:
        headers = websocket_auth_headers("account-test", "secret-value", 123)
        self.assertEqual(headers["X-GEMINI-APIKEY"], "account-test")
        self.assertNotIn("secret-value", "".join(headers.values()))

    def test_dirty_queue_coalesces_hot_symbol(self) -> None:
        scheduler = DirtySymbolScheduler(2)
        self.assertTrue(scheduler.mark("A"))
        self.assertFalse(scheduler.mark("A"))
        self.assertEqual(scheduler.coalesced, 1)
        self.assertEqual(scheduler.pop(), "A")

    def test_public_depth_first_frame_is_snapshot_then_delta(self) -> None:
        socket = PublicMarketSocket(AdmissionGovernor())
        socket.apply_frame({
            "e": "depthUpdate", "s": "X", "U": 1, "u": 2,
            "b": [[".48", "10"]], "a": [[".52", "10"]],
        }, NOW)
        socket.apply_frame({
            "e": "depthUpdate", "s": "X", "U": 3, "u": 3,
            "b": [[".49", "5"]], "a": [],
        }, NOW)
        self.assertEqual(socket.books["X"].top().bid, Decimal(".49"))
        self.assertEqual(len(socket.dirty), 1)

    def test_public_depth_symbol_case_is_canonical(self) -> None:
        socket = PublicMarketSocket(AdmissionGovernor())
        socket.books["GEMI-TEST"] = LocalBook("GEMI-TEST")
        socket.apply_frame({
            "e": "depthUpdate", "s": "gemi-test", "U": 1, "u": 1,
            "b": [[".48", "10"]], "a": [[".52", "10"]],
        }, NOW)
        self.assertEqual(len(socket.books), 1)
        self.assertIsNotNone(socket.books["GEMI-TEST"].top())

    def test_intent_unknown_state_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = SQLiteJournal(path)
            ranked = rank_candidate(RankInput(RewardPool("p", Decimal("50")), rules(), book()), now=NOW)
            ledger = IntentLedger(store)
            intent = ledger.prepare(ranked.quote.yes, OrderPurpose.ENTRY)
            ledger.mark_sent(intent.client_order_id)
            store.close()
            reopened = SQLiteJournal(path)
            self.assertEqual(reopened.unresolved_ids(), (intent.client_order_id,))
            self.assertEqual(reopened.quick_check(), "ok")
            reopened.close()

    def test_terminal_intent_is_not_reported_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            leg = QuoteLeg("X", Outcome.YES, Side.BUY, Decimal(".40"), Decimal("10"))
            ledger = IntentLedger(store)
            intent = ledger.prepare(leg, OrderPurpose.ENTRY)
            ledger.mark_sent(intent.client_order_id)
            ledger.mark_terminal(intent.client_order_id)
            self.assertEqual(store.unresolved_ids(), ())
            store.close()

    def test_deadline_can_only_tighten_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            store.persist_deadline("X", "YES", NOW, NOW + timedelta(seconds=100), Decimal("10"))
            store.persist_deadline("X", "YES", NOW, NOW + timedelta(seconds=200), Decimal("10"))
            value = store.connection.execute("SELECT deadline FROM position_deadline").fetchone()[0]
            self.assertEqual(datetime.fromisoformat(value), NOW + timedelta(seconds=100))
            store.close()

    def test_windows_owner_lock_rejects_second_process_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.lock"
            first = WindowsOwnerLock(path)
            second = WindowsOwnerLock(path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()

    def test_structured_logging_redacts_secrets_and_persists_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJournal(Path(directory) / "journal.sqlite")
            runtime = configure_logging(Path(directory) / "logs", secrets=("actual-secret",))
            logger = OperationalLogger(runtime, store)
            logger.event(
                logging.ERROR, "order_reconciliation_failed",
                error_class=ErrorClass.RECONCILIATION.value,
                api_key="account-do-not-log-this",
                api_secret="actual-secret",
                symbol="GEMI-TEST",
            )
            runtime.close()
            content = (Path(directory) / "logs" / "gemini-bot.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("actual-secret", content)
            self.assertNotIn("account-do-not-log-this", content)
            row = store.connection.execute("SELECT kind,details_json FROM audit_event").fetchone()
            self.assertEqual(row[0], "order_reconciliation_failed")
            self.assertEqual(json.loads(row[1])["api_key"], "[REDACTED]")
            store.close()

    def test_exception_logging_keeps_class_and_redacts_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = configure_logging(Path(directory), secrets=("hidden-token",))
            logger = OperationalLogger(runtime)
            try:
                raise RuntimeError("transport failed hidden-token")
            except RuntimeError as exc:
                logger.exception("socket_failed", ErrorClass.TRANSPORT, exc, retry_seconds=2)
            runtime.close()
            record = json.loads((Path(directory) / "gemini-bot.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["error_class"], "transport")
            self.assertNotIn("hidden-token", record["exception"])

    def test_ambiguous_mutation_never_retries(self) -> None:
        decision = decide_recovery(FailureContext(
            ErrorClass.TRANSPORT, "order.place", mutation_attempted=True,
        ))
        self.assertEqual(decision.action, RecoveryAction.RECONCILE_MUTATION)

    def test_rate_limit_honors_longer_retry_after(self) -> None:
        decision = decide_recovery(FailureContext(
            ErrorClass.RATE_LIMIT, "reward_catalog", http_status=429,
            retry_after_seconds=120,
        ))
        self.assertEqual(decision.action, RecoveryAction.HONOR_COOLDOWN)
        self.assertEqual(decision.delay_seconds, 120)

    def test_repeated_transport_failure_halts(self) -> None:
        decision = decide_recovery(FailureContext(
            ErrorClass.TRANSPORT, "public_ws", consecutive_failures=5,
        ))
        self.assertEqual(decision.action, RecoveryAction.HALT)

    def test_private_rest_signature_contains_fields_and_no_body_secret(self) -> None:
        headers = private_headers(
            "/v1/prediction-markets/positions", {"limit": 100},
            "account-example", "do-not-expose", 123,
        )
        payload = json.loads(base64.b64decode(headers["X-GEMINI-PAYLOAD"]))
        self.assertEqual(payload["request"], "/v1/prediction-markets/positions")
        self.assertEqual(payload["limit"], 100)
        self.assertNotIn("do-not-expose", "".join(headers.values()))

    def test_rest_client_has_no_order_mutation_route(self) -> None:
        client = GeminiReadRestClient(AdmissionGovernor())
        with self.assertRaises(ValueError):
            asyncio.run(client.public_get("/v1/prediction-markets/order"))

    def test_catalog_deduplicates_shared_budget_identity(self) -> None:
        rewards = [
            {"event_ticker": "E1", "pool_id": 7, "daily_pool_usd": "100", "qualifying_maker_count": 2},
            {"event_ticker": "E2", "pool_id": 7, "daily_pool_usd": "100", "qualifying_maker_count": 3},
        ]
        def event(symbol: str) -> dict[str, object]:
            return {"status": "active", "contracts": [{
                "instrumentSymbol": symbol, "priceIncrement": ".01",
                "quantityMinimum": "1", "quantityIncrement": "1", "quoteAssetPrecision": 2,
                "status": "active", "marketState": "open", "expiryDate": "2026-09-16T00:00:00Z",
            }]}
        joined = join_reward_contracts(rewards, {"E1": event("A"), "E2": event("B")})
        self.assertEqual({item.pool.pool_id for item in joined}, {"shared:7"})

    def test_catalog_preserves_reward_pool_and_contract_timing_metadata(self) -> None:
        joined = join_reward_contracts(
            [{
                "event_ticker": "E", "pool_id": 9, "daily_pool_usd": "50",
                "ends_at": "2026-09-16T00:00:00Z", "pool_source": "category_default",
                "pool_event_count": 23, "recurring_duration_value": 5,
                "recurring_duration_unit": "MINUTES",
            }],
            {"E": {"status": "active", "contracts": [{
                "instrumentSymbol": "X", "status": "active", "marketState": "open",
                "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                "quoteAssetPrecision": 2,
                "effectiveDate": "2026-09-15T12:00:00Z",
                "expiryDate": "2026-09-16T00:00:00Z",
            }]}},
        )
        self.assertEqual(len(joined), 1)
        item = joined[0]
        self.assertEqual(item.trade_starts_at, "2026-09-15T12:00:00Z")
        self.assertEqual(item.pool_source, "category_default")
        self.assertEqual(item.pool_event_count, 23)
        self.assertEqual((item.recurring_duration_value, item.recurring_duration_unit), (5, "MINUTES"))

    def test_catalog_skips_incomplete_contract_without_losing_healthy_sibling(self) -> None:
        joined = join_reward_contracts(
            [{"event_ticker": "E", "daily_pool_usd": "50"}],
            {"E": {"status": "active", "contracts": [
                {
                    "instrumentSymbol": "BROKEN", "status": "active", "marketState": "open",
                    "quantityMinimum": "1", "quantityIncrement": "1",
                },
                {
                    "instrumentSymbol": "HEALTHY", "status": "active", "marketState": "open",
                    "priceIncrement": ".01", "quantityMinimum": "1", "quantityIncrement": "1",
                    "quoteAssetPrecision": 2,
                },
            ]}},
        )
        self.assertEqual([item.rules.symbol for item in joined], ["HEALTHY"])

    def test_catalog_treats_invalid_contract_numbers_as_unavailable(self) -> None:
        joined = join_reward_contracts(
            [{"event_ticker": "E", "daily_pool_usd": "50"}],
            {"E": {"status": "active", "contracts": [{
                "instrumentSymbol": "BROKEN", "status": "active", "marketState": "open",
                "priceIncrement": "not-a-number", "quantityMinimum": "1", "quantityIncrement": "1",
            }]}},
        )
        self.assertEqual(joined, [])

    def test_catalog_treats_invalid_contract_container_as_unavailable(self) -> None:
        rewards = [{"event_ticker": "E", "daily_pool_usd": "50"}]
        self.assertEqual(join_reward_contracts(rewards, {"E": {"status": "active", "contracts": None}}), [])
        self.assertEqual(join_reward_contracts(rewards, {"E": []}), [])


if __name__ == "__main__":
    unittest.main()
