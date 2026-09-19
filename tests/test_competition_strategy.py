"""Offline strategy/recovery checks; all network/mutation adapters are fake."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import unittest
from unittest.mock import AsyncMock

from gemini_bot.account_state import AccountReducer
from gemini_bot.catalog import RewardedContract
from gemini_bot.competition import measure_competition, UNSEEN_CONTRACT_SCORE_PRIOR
from gemini_bot.discovery import RewardUniverseLoader
from gemini_bot.domain import RewardPool, market_owner_key, UptimeProgress
from gemini_bot.market_state import LocalBook
from gemini_bot.profitability import ProfitabilityEvidence, maker_fee_rate_at, estimated_round_trip_fees
from gemini_bot.rank_report import rank_live_universe, public_rank_rows, portfolio_reward_report
from gemini_bot.reward_uptime_report import ExchangeRewardUptime, parse_reward_results
from gemini_bot.reward_ranking import (
    RankInput, rank_market_candidates, rank_candidate, select_sticky_markets,
    pool_reward_proxy, marginal_reward_proxy,
)
from gemini_bot.uptime import sample_and_load_progress
import test_core as core
import test_repair_integration as fixtures

NOW = core.NOW


def depth(symbol="X", size="100", extra=False):
    b = LocalBook(symbol)
    bids, asks = [(D(".48"), D(size))], [(D(".52"), D(size))]
    if extra:
        bids += [(D(".45"), D(size)), (D(".10"), D("99999"))]
        asks += [(D(".55"), D(size)), (D(".90"), D("99999"))]
    b.apply_snapshot(bids, asks, 1, 1, NOW)
    return b


def item(symbol="X", pool="p", size="100", makers=2, budget="100", cost="0"):
    b = depth(symbol, size)
    return RankInput(
        RewardPool(pool, D(budget), qualifying_maker_count=makers),
        core.rules(symbol), b.top(), event_ticker=symbol,
        competition=measure_competition(b, tick=D(".01")),
        profitability=ProfitabilityEvidence(expected_execution_cost_usd=D(cost)),
    )


def choose(rows, *, sticky=(), locked=(), maximum=15):
    return select_sticky_markets(
        rows, sticky_pool_ids=set(sticky), locked_pool_ids=set(locked),
        permitted_symbols={c.symbol for c in rows}, maximum_active_pools=maximum,
        actionable=lambda c: not set(c.reasons) - {"50% uptime is no longer reachable in the estimated window"},
        entry_time_valid=lambda _: True,
    )


class CompetitionTests(unittest.TestCase):
    def test_depth_size_and_distance_matter_not_just_headcount(self):
        ranked = rank_market_candidates([
            item("BIG", "a", "5000", 2), item("SMALL", "b", "50", 10),
        ], now=NOW, quantity=D("20"))
        self.assertEqual(ranked[0].symbol, "SMALL")
        self.assertGreater(ranked[0].expected_net_profit_usd, ranked[1].expected_net_profit_usd)

    def test_count_only_breaks_equal_economics_ties(self):
        ranked = rank_market_candidates([item("MANY", "a", makers=10), item("FEW", "b", makers=2)], now=NOW)
        self.assertEqual(ranked[0].symbol, "FEW")
        self.assertEqual(ranked[0].expected_reward_usd, ranked[1].expected_reward_usd)
        self.assertTrue(all(c.eligible_now for c in rank_market_candidates([item(makers=99999)], now=NOW)))

    def test_unknown_and_zero_count_do_not_mean_free_pool(self):
        zero = rank_candidate(item(makers=0), now=NOW)
        missing = rank_candidate(item(makers=None), now=NOW)
        self.assertEqual(zero.expected_reward_usd, missing.expected_reward_usd)
        self.assertLess(zero.estimated_score_share, 1)
        absent = rank_candidate(replace(item(), competition=None), now=NOW)
        self.assertEqual(absent.pool_competition_score_proxy, UNSEEN_CONTRACT_SCORE_PRIOR)

    def test_more_of_our_size_does_not_resize_competitors(self):
        small, big = [rank_candidate(item(), now=NOW, quantity=D(q)) for q in ("10", "20")]
        self.assertEqual(small.pool_competition_score_proxy, big.pool_competition_score_proxy)
        self.assertGreater(big.expected_reward_usd, small.expected_reward_usd)
        self.assertLess(big.expected_reward_usd, small.expected_reward_usd * 2)

    def test_full_depth_and_no_aggregate_level_cap(self):
        plain = measure_competition(depth(size="500"), tick=D(".01"))
        larger = measure_competition(depth(size="1000"), tick=D(".01"))
        self.assertEqual(larger.weighted_depth, plain.weighted_depth * 2)
        extra = measure_competition(depth(size="500", extra=True), tick=D(".01"))
        self.assertGreater(extra.weighted_depth, plain.weighted_depth)
        self.assertEqual(extra.levels, 4)  # far-away levels don't score

    def test_unseen_contract_and_event_prior_explicit_without_summing_makers(self):
        a = replace(item("A"), event_ticker="E", event_contract_count=4, pool_event_count=3)
        b = replace(item("B"), event_ticker="E", event_contract_count=4, pool_event_count=3)
        ranked = rank_market_candidates([a, b], now=NOW)
        self.assertEqual(ranked[0].unobserved_competition_units, 5)  # 2 events+2 contracts+reserve
        self.assertEqual(ranked[0].pool_competition_score_proxy, ranked[1].pool_competition_score_proxy)

    def test_shared_budget_disagreement_and_duplicate_symbol_rejected(self):
        for rows in ([item("A"), item("B", budget="101")], [item(), item()]):
            with self.assertRaises(ValueError):
                rank_market_candidates(rows, now=NOW)

    def test_sub_dollar_market_allowed_but_nonpositive_net_not(self):
        small = rank_candidate(item(budget="1"), now=NOW)
        self.assertTrue(small.eligible_now)
        self.assertTrue(0 < small.expected_reward_usd < 1)
        loss = rank_candidate(item(budget="1", cost="1"), now=NOW)
        self.assertFalse(loss.eligible_now)

    def test_headcount_change_does_not_change_admission_or_cancel_incumbent(self):
        a = rank_candidate(item("A", makers=10000), now=NOW)
        b = rank_candidate(item("B", "other", size="1", budget="1000"), now=NOW)
        selected, _ = choose([b, a], sticky=[a.owner_key], maximum=1)
        self.assertEqual([c.symbol for c in selected], ["A"])

    def test_two_markets_one_pool_budget_and_distinct_owners(self):
        rows = rank_market_candidates([item("A"), item("B")], now=NOW)
        selected, owners = choose(rows, maximum=2)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(owners), 2)
        total_score = sum(c.own_daily_score_proxy for c in selected)
        total_reward = pool_reward_proxy(selected[0], total_score)
        self.assertLess(total_reward, sum(c.expected_reward_usd for c in selected))
        self.assertLess(total_reward, D("100"))
        self.assertTrue(all(c.marginal_reward_usd_proxy < c.expected_reward_usd for c in selected))

    def test_next_slot_compares_marginal_not_repeated_full_pool_reward(self):
        rows = rank_market_candidates([item("A"), item("B"), item("C", "other", budget="51")], now=NOW)
        # A+B share a larger denominator as well as a budget.
        a = next(c for c in rows if c.symbol == "A")
        b = next(c for c in rows if c.symbol == "B")
        c = next(c for c in rows if c.symbol == "C")
        between = (b.expected_reward_usd + marginal_reward_proxy(b, a.own_daily_score_proxy)) / 2
        c = replace(c, daily_pool_usd=c.daily_pool_usd * between / c.expected_reward_usd,
                    expected_reward_usd=between, expected_net_profit_usd=between)
        selected, _ = choose([a, b, c], sticky=[a.owner_key], maximum=2)
        self.assertEqual({c.symbol for c in selected}, {"A", "C"})

    def test_held_symbol_reserves_one_owner_not_entire_pool(self):
        rows = rank_market_candidates([item("B")], now=NOW)
        held = market_owner_key("p", "A")
        selected, owners = choose(rows, locked=[held], maximum=2)
        self.assertEqual([c.symbol for c in selected], ["B"])
        self.assertEqual(owners, {held, rows[0].owner_key})

    def test_breadth_and_unsafe_incumbent(self):
        rows = rank_market_candidates([item(f"S{i}", f"P{i}") for i in range(32)], now=NOW)
        selected, owners = choose(rows)
        self.assertEqual(len(selected), 15)
        self.assertEqual(len(owners), 15)
        stale = replace(rows[0], reasons=("stale market data",))
        selected, _ = choose([stale, rows[1]], sticky=[stale.owner_key], maximum=1)
        self.assertEqual([c.symbol for c in selected], [rows[1].symbol])

    def test_own_orders_subtracted_without_mutating_public_book(self):
        b = depth()
        account = AccountReducer()
        for n, outcome in enumerate(("YES", "NO")):
            account.apply_order_update({"e": "orderUpdate", "i": str(n), "c": "owned"+str(n),
                                       "s": "X", "S": "BUY", "O": outcome, "q": "20", "z": "20",
                                       "p": ".48", "X": "OPEN"})
        contract = RewardedContract("E", RewardPool("p", D("100")), core.rules("X"), "")
        rows = rank_live_universe([contract], {"X": b}, now=NOW, quantity=D("20"), offset_ticks=2,
                                  account=account)
        expected = measure_competition(depth(size="80"), tick=D(".01"))
        self.assertEqual(rows[0].observed_competition_score_proxy, expected.weighted_depth)
        self.assertEqual(b.bids[D(".48")], D("100"))
        self.assertEqual(b.asks[D(".52")], D("100"))

    def test_stale_other_shard_never_supplies_competition(self):
        contract = RewardedContract("E", RewardPool("p", D("100")), core.rules("X"), "")
        rows = rank_live_universe([contract], {"X": depth()}, now=NOW, quantity=D("20"), offset_ticks=2,
                                  market_data_verified_at={"OTHER": NOW})
        self.assertEqual(rows, [])
        rows = rank_live_universe([contract], {"X": depth()}, now=NOW+timedelta(seconds=4),
                                  quantity=D("20"), offset_ticks=2)
        self.assertIsNone(rows[0].observed_competition_score_proxy)
        self.assertFalse(rows[0].eligible_now)

    def test_selection_scores_actual_resting_prices_but_replacement_uses_current_target(self):
        account = AccountReducer()
        for n, outcome in enumerate(("YES", "NO")):
            account.apply_order_update({"e": "orderUpdate", "i": str(n), "c": "owned"+str(n),
                                       "s": "X", "S": "BUY", "O": outcome, "q": "20", "z": "20",
                                       "p": ".43", "X": "OPEN"})
        contract = RewardedContract("E", RewardPool("p", D("100")), core.rules("X"), "")
        args = dict(now=NOW, quantity=D("20"), offset_ticks=2, account=account)
        actual = rank_live_universe([contract], {"X": depth()}, evaluate_working_quotes=True, **args)[0]
        target = rank_live_universe([contract], {"X": depth()}, **args)[0]
        self.assertEqual(actual.scoring_yes_price, D(".43"))
        self.assertEqual(actual.scoring_no_price, D(".43"))
        self.assertLess(actual.own_daily_score_proxy, target.own_daily_score_proxy)
        self.assertEqual(actual.quote.yes.price, D(".46"))
        self.assertEqual(actual.quote.no.price, D(".46"))

    def test_report_does_not_claim_pool_headcount_or_exact_payout(self):
        row = public_rank_rows([rank_candidate(item(), now=NOW)])[0]
        self.assertEqual(row["ranking_version"], 3)
        self.assertFalse(row["exact_shared_pool_allocation_known"])
        self.assertTrue(row["standalone_reward_not_additive_within_pool"])

    def test_dated_fee_waiver_keeps_taker_exit_fee(self):
        self.assertEqual(maker_fee_rate_at(NOW), 0)
        self.assertGreater(maker_fee_rate_at(datetime(2027, 1, 1, tzinfo=timezone.utc)), 0)
        args = dict(quantity=D("20"), entry_price=D(".5"), exit_price=D(".5"), maker_rebate_fraction=D("0"))
        self.assertEqual(estimated_round_trip_fees(**args, now=NOW), D(".35"))
        self.assertGreater(estimated_round_trip_fees(**args), D(".35"))

    def test_short_recurring_member_does_not_claim_full_day_of_pool_score(self):
        short = replace(item(), pool_source="category_default", pool_event_count=24,
                        uptime=UptimeProgress(window_start=NOW, window_end=NOW+timedelta(hours=1)))
        day = replace(short, uptime=UptimeProgress(window_start=NOW, window_end=NOW+timedelta(hours=24)))
        a, b = [rank_candidate(i, now=NOW) for i in (short, day)]
        self.assertAlmostEqual(a.own_daily_score_proxy * 24, b.own_daily_score_proxy)
        self.assertLess(a.expected_reward_usd, b.expected_reward_usd)

    def test_portfolio_totals_are_deduplicated_and_payout_floor_is_reporting_only(self):
        rows = rank_market_candidates([item("A", budget="1"), item("B", budget="1")], now=NOW)
        selected, _ = choose(rows)
        report = portfolio_reward_report(selected)
        self.assertEqual(len(report["pools"]), 1)
        self.assertLess(D(report["reward_usd_proxy"]), D("1"))
        self.assertFalse(report["payout_floor_used_for_admission"])
        self.assertFalse(report["qualified_or_paid_guaranteed"])
        self.assertEqual(len(selected), 2)

    def test_added_shared_member_cannot_make_retained_market_unprofitable(self):
        rows = rank_market_candidates([item("A"), item("B")], now=NOW)
        a, b = rows
        a = replace(a, expected_execution_cost_usd=(
            a.expected_reward_usd + marginal_reward_proxy(a, b.own_daily_score_proxy)
        ) / 2)
        selected, _ = choose([a, b], sticky=[a.owner_key])
        self.assertEqual([c.symbol for c in selected], ["A"])

    def test_overfull_or_duplicate_owner_state_blocks_selection(self):
        rows = rank_market_candidates([item()], now=NOW)
        with self.assertRaises(ValueError):
            choose(rows, locked={f"p{i}" for i in range(16)})
        with self.assertRaises(ValueError):
            choose(rows * 2)


class RewardResultTests(unittest.IsolatedAsyncioTestCase):
    def raw(self, *, status="BELOW_THRESHOLD", total=".50", paid_at=None):
        return [{"payout_date": "2026-09-15", "payout_status": status, "total_reward_usd": total,
                 "paid_at": paid_at, "events": [{"event_id": 1, "snapshot_count": 800,
                 "total_snapshots": 1440, "normalized_score": ".0001", "event_reward_usd": ".49"}]}]

    def test_below_threshold_is_not_paid_and_event_rounding_does_not_replace_total(self):
        result = parse_reward_results(self.raw())[0]
        self.assertEqual(result["payout_status"], "BELOW_THRESHOLD")
        self.assertEqual(result["reported_total_reward_usd"], "0.50")
        self.assertIsNone(result["paid_reward_usd"])
        self.assertIsNone(result["net_profit_after_trading_usd"])
        self.assertFalse(result["events"][0]["contract_reward_attribution_available"])

    def test_paid_requires_status_and_timestamp(self):
        self.assertIsNone(parse_reward_results(self.raw(status="PAID"))[0]["paid_reward_usd"])
        result = parse_reward_results(self.raw(status="PAID", total="2", paid_at=NOW.isoformat()))[0]
        self.assertEqual(result["paid_reward_usd"], "2")

    async def test_reward_history_survives_restart_but_is_not_fresh_uptime(self):
        cache = ExchangeRewardUptime()
        rest = AsyncMock()
        rest.liquidity_reward_daily.return_value = self.raw()
        await cache.refresh(rest, now=NOW, clock=0)
        restored = ExchangeRewardUptime()
        restored.restore_reward_history(cache.reward_history_payload(), now=NOW)
        self.assertEqual(restored.reward_results, cache.reward_results)
        self.assertEqual(restored.rows, {})
        self.assertTrue(restored.metadata(now=NOW)["stale"])

    async def test_bad_amount_refresh_retains_old_evidence(self):
        cache = ExchangeRewardUptime()
        rest = AsyncMock()
        rest.liquidity_reward_daily.return_value = self.raw()
        await cache.refresh(rest, now=NOW, clock=0)
        old = cache.reward_history_payload()
        rest.liquidity_reward_daily.return_value = self.raw(total="NaN")
        self.assertFalse(await cache.refresh(rest, now=NOW, clock=300))
        self.assertEqual(old, cache.reward_history_payload())
        self.assertIsNotNone(cache.error)

    def test_corrupt_reporting_history_is_unavailable_not_fabricated(self):
        cache = ExchangeRewardUptime()
        cache.restore_reward_history('[{"payout_date":"bad"}]', now=NOW)
        self.assertEqual(cache.reward_results, [])
        self.assertTrue(cache.history_incomplete)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_first_contract_cannot_hide_good_alternative(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [{"event_ticker": "E", "pool_id": 1, "daily_pool_usd": "100"}]
        def contract(symbol, bid, ask):
            return {"instrumentSymbol": symbol, "status": "active", "marketState": "open",
                    "quantityMinimum": "1", "quantityIncrement": "1", "priceIncrement": ".01",
                    "quoteAssetPrecision": 2,
                    "prices": {"bestBid": bid, "bestAsk": ask}}
        rest.event.return_value = {"status": "active", "contracts": [
            contract("BAD", ".01", ".02"), contract("GOOD", ".48", ".52"),
            contract("ALSO", ".49", ".51"),
        ]}
        rows = await RewardUniverseLoader(rest).load(now=NOW)
        self.assertEqual({c.rules.symbol for c in rows}, {"GOOD", "ALSO"})
        self.assertEqual({c.event_contract_count for c in rows}, {3})

    async def test_all_incumbents_in_shared_pool_kept_and_reads_bounded(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [
            {"event_ticker": f"E{i}", "pool_id": i//10, "daily_pool_usd": "100", "qualifying_maker_count": i}
            for i in range(100)
        ]
        rest.event.side_effect = lambda ticker: {"status": "active", "contracts": [{
            "instrumentSymbol": ticker, "status": "active", "marketState": "open",
            "quantityMinimum": "1", "quantityIncrement": "1", "priceIncrement": ".01",
            "quoteAssetPrecision": 2,
            "prices": {"bestBid": ".48", "bestAsk": ".52"},
        }]}
        pinned = {market_owner_key("shared:0", f"E{i}"): (f"E{i}", f"E{i}") for i in range(8)}
        rows = await RewardUniverseLoader(rest).load(now=NOW, preferred=pinned)
        self.assertTrue({f"E{i}" for i in range(8)} <= {c.rules.symbol for c in rows})
        self.assertLessEqual(len(rows), 32)
        self.assertLessEqual(rest.event.await_count, 64)


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_legacy_identity_migrates_without_overwriting_old_history(self):
        raw = '{"p":["E","X"]}'
        self.r.journal.set_runtime_state("sticky_markets_v1", raw, updated_at=NOW)
        self.r._load_sticky_markets()
        self.assertEqual(self.r._sticky_markets, {market_owner_key("p", "X"): ("E", "X")})
        self.assertEqual(self.r.journal.get_runtime_state("sticky_markets_v1"), raw)

    async def test_two_same_pool_owners_survive_save_reload_and_inventory_lock(self):
        rows = rank_market_candidates([item("A"), item("B")], now=NOW)
        self.r._save_sticky_markets(rows, set())
        self.r._sticky_markets.clear()
        self.r._load_sticky_markets()
        self.assertEqual(len(self.r._sticky_markets), 2)
        self.r.account.reducer.positions["A"] = D("20")
        self.assertEqual(self.r._inventory_locked_pool_ids(), {rows[0].owner_key})

    async def test_bad_v2_owner_never_falls_back_to_legacy(self):
        self.r.journal.set_runtime_state("sticky_markets_v1", '{"p":["E","X"]}', updated_at=NOW)
        self.r.journal.set_runtime_state("sticky_markets_v2", '{"p":["E","X"]}', updated_at=NOW)
        with self.assertRaises(RuntimeError):
            self.r._load_sticky_markets()

    async def test_event_uptime_uses_any_qualifying_contract_not_first_only(self):
        contracts = [RewardedContract("E", RewardPool("p", D("100")), core.rules(s), "") for s in ("A", "B")]
        account = AccountReducer()
        account.apply_order_update({"e": "orderUpdate", "i": "b", "c": "owned", "s": "B",
                                    "S": "BUY", "O": "YES", "q": "20", "z": "20", "p": ".46", "X": "OPEN"})
        progress = sample_and_load_progress(
            self.r.journal, contracts, account, now=NOW, books={s: depth(s) for s in ("A", "B")},
        )
        self.assertEqual(progress["p"].qualifying_snapshots, 1)
        rows = self.r.journal.connection.execute("SELECT qualifying_samples FROM reward_uptime_v2").fetchall()
        self.assertEqual(rows, [(1,)])
