"""Pool breadth changes research priority, never venue eligibility or budgets."""
from decimal import Decimal as D
import unittest
from unittest.mock import AsyncMock

from gemini_bot.catalog import RewardedContract
from gemini_bot.discovery import rank_discovery_pools, select_pool_representatives, RewardUniverseLoader
from gemini_bot.domain import RewardPool
import test_core as core


def row(ticker, budget="250", count=1, pool=None, makers=10):
    return dict(event_ticker=ticker, daily_pool_usd=budget, pool_event_count=count,
                pool_id=pool, qualifying_maker_count=makers)


def contract(ticker, pool, count):
    return RewardedContract(ticker, RewardPool(pool, D("250")), core.rules(ticker),
                            "", event_contract_count=count)


class BreadthTests(unittest.TestCase):
    def test_smaller_budget_narrow_scope_beats_large_broad_scope(self):
        ranked = rank_discovery_pools([row("BROAD", "500", 197, 54, 1), row("SMALL")])
        self.assertEqual([p.pool_id for p in ranked], ["event:SMALL", "shared:54"])
        self.assertEqual(ranked[0].priority_proxy, D(250))
        self.assertGreater(ranked[1].priority_proxy, 0)

    def test_broad_pool_not_banned_and_budget_can_outweigh_breadth(self):
        rows = [row("BROAD", "1000", 4, 54), row("SMALL")]
        self.assertEqual(rank_discovery_pools(rows)[0].pool_id, "shared:54")
        self.assertEqual(len(select_pool_representatives(rows, maximum_pools=32)), 2)

    def test_more_events_or_contracts_reduce_priority(self):
        rows = [row("A", count=1), row("B", count=4)]
        ranked = rank_discovery_pools(rows)
        self.assertGreater(ranked[0].priority_proxy, ranked[1].priority_proxy)
        ranked = rank_discovery_pools(rows, [contract("A", "event:A", 45), contract("B", "event:B", 2)])
        self.assertEqual(ranked[0].pool_id, "event:B")

    def test_unique_pool_budget_and_observed_events_count_once(self):
        rows = [row("A", "500", 1, 54), row("B", "500", 1, 54)]
        ranked = rank_discovery_pools(rows + [rows[0]])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].daily_budget_usd, 500)
        self.assertEqual(ranked[0].event_count, 2)

    def test_unseen_events_use_explicit_contract_imputation_not_zero(self):
        rows = [row("A", count=5, pool=54), row("B", count=5, pool=54)]
        contracts = [contract("A", "shared:54", 4), contract("B", "shared:54", 5)]
        p = rank_discovery_pools(rows, contracts + [contracts[0]])[0]
        self.assertEqual(p.observed_contract_events, 2)
        self.assertEqual(p.estimated_contract_count, 9 + 3 * 5)
        self.assertEqual(p.event_count, 5)

    def test_missing_count_uses_distinct_catalog_scope(self):
        rows = [row("A", count=None, pool=54), row("B", count=None, pool=54)]
        self.assertEqual(rank_discovery_pools(rows)[0].event_count, 2)

    def test_headcount_only_tiebreak_not_budget_denominator(self):
        rows = [row("BROAD", "500", 197, 54, 1), row("SMALL", makers=10000)]
        self.assertEqual(rank_discovery_pools(rows)[0].pool_id, "event:SMALL")
        same = rank_discovery_pools([row("A", makers=20), row("B", makers=5)])
        self.assertEqual(same[0].pool_id, "event:B")
        self.assertEqual(same[0].priority_proxy, same[1].priority_proxy)

    def test_deterministic_with_duplicate_or_reordered_rows(self):
        rows = [row("A", pool=1), row("B", pool=1), row("C", pool=2)]
        expected = [p.pool_id for p in rank_discovery_pools(rows)]
        self.assertEqual(expected, [p.pool_id for p in rank_discovery_pools(list(reversed(rows)))])

    def test_conflicting_invalid_metadata_fails_closed(self):
        for rows in ([row("A", pool=1), row("B", "500", pool=1)],
                     [row("A", "NaN")], [row("A", count=0)], [row("A", count=-1)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                rank_discovery_pools(rows)


class LoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_incumbent_kept_but_its_pool_does_not_own_reserve_priority(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [row("OLD", "500", 197, 54), row("NEW")]
        def event(ticker):
            return {"status":"active", "contracts":[{
                "instrumentSymbol":ticker + str(i), "status":"active", "marketState":"open",
                "quantityMinimum":"1", "quantityIncrement":"1", "priceIncrement":".01",
                "quoteAssetPrecision":2,
                "prices":{"bestBid":".48", "bestAsk":".52"},
            } for i in range(2)]}
        rest.event.side_effect = event
        rows = await RewardUniverseLoader(rest).load(maximum_pools=2, now=core.NOW,
            preferred={"old-owner":("OLD","OLD0")}, quantity=D(15))
        self.assertEqual([r.rules.symbol for r in rows], ["OLD0", "NEW0"])
        self.assertEqual(rest.event.await_count, 2)

    async def test_contract_breadth_changes_actual_watch_allocation(self):
        rest = AsyncMock()
        rest.reward_events.return_value = [row("MANY", "500"), row("FEW", "250")]
        def event(ticker):
            return {"status":"active", "contracts":[{
                "instrumentSymbol":ticker + str(i), "status":"active", "marketState":"open",
                "quantityMinimum":"1", "quantityIncrement":"1", "priceIncrement":".01",
                "quoteAssetPrecision":2,
                "prices":{"bestBid":".48", "bestAsk":".52"},
            } for i in range(45 if ticker == "MANY" else 2)]}
        rest.event.side_effect = event
        rows = await RewardUniverseLoader(rest).load(maximum_pools=2, now=core.NOW, quantity=D(15))
        self.assertEqual({r.event_ticker for r in rows}, {"FEW"})
