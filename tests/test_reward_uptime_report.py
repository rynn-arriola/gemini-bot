"""Offline reporting regressions; no exchange connection or order mutation."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from gemini_bot.catalog import join_reward_contracts
from gemini_bot.rank_report import public_rank_rows
from gemini_bot.reward_uptime_report import ExchangeRewardUptime, event_identity, parse_summaries
import test_repair_integration as repair_fixtures
from test_review_regressions import candidate

NOW = datetime(2026, 9, 15, 22, tzinfo=timezone.utc)
DAY = date(2026, 9, 15)


def summaries(q=32, total=60, *, day="2026-09-15", event_id=123):
    return [{"payout_date": day, "payout_status": "PENDING", "events": [{
        "event_id": event_id, "snapshot_count": q, "total_snapshots": total,
    }]}]


class CountParserTests(unittest.TestCase):
    def test_ratio_and_integer_threshold(self):
        row = parse_summaries(summaries(), DAY, DAY)[("2026-09-15", "123")]
        self.assertEqual(row["percentage"], "53.3333")
        self.assertEqual(row["required_snapshots_at_reported_total"], 30)
        self.assertTrue(row["meets_50_percent_on_reported_counts"])
        self.assertFalse(row["final_eligibility_confirmed"])
        self.assertIsNone(row["scored_through_at"])
        self.assertEqual(row["snapshot_cadence_seconds"],60)
        row = parse_summaries(summaries(30,61), DAY, DAY)[("2026-09-15", "123")]
        self.assertEqual(row["required_snapshots_at_reported_total"],31)
        self.assertFalse(row["meets_50_percent_on_reported_counts"])

    def test_zero_denominator_unknown_not_zero_uptime(self):
        row = parse_summaries(summaries(0,0), DAY, DAY)[("2026-09-15", "123")]
        self.assertIsNone(row["percentage"])
        self.assertIsNone(row["meets_50_percent_on_reported_counts"])

    def test_invalid_counts_rejected(self):
        for q,total in [(True,60), (1,False), ("3",60), (3,60.0), (-1,60), (61,60), (1,0), (1,None)]:
            with self.subTest(q=q,total=total), self.assertRaises(ValueError):
                parse_summaries(summaries(q,total), DAY, DAY)

    def test_dates_duplicates_and_bounds(self):
        for rows in [summaries(day="2026-09-16"), summaries()*2,
                     [{**summaries()[0],"events":summaries()[0]["events"]*2}],
                     summaries(day="20260915")]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_summaries(rows, DAY, DAY)
        self.assertEqual(parse_summaries([],DAY,DAY),{})

    def test_event_identity_does_not_use_title_or_pool(self):
        self.assertEqual(event_identity("000123"),"123")
        for value in [None, True, 123.0, "SOL", "", 0, -1, "１２３", 2**63]:
            self.assertIsNone(event_identity(value))

    def test_catalog_preserves_event_id_for_every_contract(self):
        rows=join_reward_contracts([{"event_ticker":"E","daily_pool_usd":"50"}], {
            "E":{"id":123,"status":"active","contracts":[{
                "instrumentSymbol":"X","priceIncrement":".01","quantityMinimum":"1",
                "quantityIncrement":"1","status":"active","marketState":"open",
            }]},
        })
        self.assertEqual(rows[0].event_id,"123")


class ReportCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache=ExchangeRewardUptime()
        self.rest=SimpleNamespace(liquidity_reward_daily=AsyncMock(return_value=summaries()))

    async def test_account_counts_and_fetch_metadata(self):
        await self.cache.refresh(self.rest,now=NOW,clock=0)
        view=self.cache.event_view(123,now=NOW+timedelta(seconds=1))
        self.assertEqual(view["current_percentage"],"53.3333")
        self.assertEqual(view["current_report"]["snapshot_count"],32)
        self.assertEqual(view["requested_scoring_date"],"2026-09-15")
        self.assertIsNotNone(view["fetched_at"])
        self.assertFalse(view["used_for_trading_decisions"])
        self.assertFalse(view["live_updates_guaranteed"])
        self.rest.liquidity_reward_daily.assert_awaited_once_with(date_from="2026-09-14",date_to="2026-09-15")

    async def test_missing_rows_are_unavailable(self):
        self.rest.liquidity_reward_daily.return_value=[]
        await self.cache.refresh(self.rest,now=NOW,clock=0)
        view=self.cache.event_view(123,now=NOW+timedelta(seconds=1))
        self.assertIsNone(view["current_percentage"])
        self.assertIsNone(view["current_report"])
        self.assertEqual(view["availability"],"no_current_day_report")

    async def test_wrong_event_and_zero_denominator(self):
        self.rest.liquidity_reward_daily.return_value=summaries(0,0)
        await self.cache.refresh(self.rest,now=NOW,clock=0)
        self.assertEqual(self.cache.event_view(123,now=NOW+timedelta(seconds=1))["availability"],"zero_denominator")
        self.assertIsNone(self.cache.event_view(456,now=NOW+timedelta(seconds=1))["last_report"])

    async def test_failure_retains_evidence_but_removes_current_percentage(self):
        await self.cache.refresh(self.rest,now=NOW,clock=0)
        self.rest.liquidity_reward_daily.side_effect=RuntimeError("SECRET RESPONSE MUST NOT LEAK")
        await self.cache.refresh(self.rest,now=NOW+timedelta(minutes=5),clock=300)
        view=self.cache.event_view(123,now=NOW+timedelta(minutes=5,seconds=1))
        self.assertEqual(view["availability"],"fetch_failed")
        self.assertIsNone(view["current_percentage"])
        self.assertEqual(view["last_report"]["percentage"],"53.3333")
        self.assertNotIn("SECRET",json.dumps(view))

    async def test_stale_data_not_current(self):
        await self.cache.refresh(self.rest,now=NOW,clock=0)
        view=self.cache.event_view(123,now=NOW+timedelta(minutes=16))
        self.assertTrue(view["stale"])
        self.assertIsNone(view["current_percentage"])

    async def test_eastern_date_rollover_never_borrows_yesterday(self):
        before=datetime(2026,9,16,3,59,tzinfo=timezone.utc)
        await self.cache.refresh(self.rest,now=before,clock=0)
        view=self.cache.event_view(123,now=before+timedelta(minutes=2))
        self.assertEqual(view["requested_scoring_date"],"2026-09-16")
        self.assertIsNone(view["current_percentage"])
        self.assertEqual(view["last_report"]["scoring_date"],"2026-09-15")

    async def test_refresh_cadence_and_no_concurrent_requests(self):
        entered=asyncio.Event()
        release=asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await release.wait()
            return summaries()
        self.rest.liquidity_reward_daily.side_effect=blocked
        task=asyncio.create_task(self.cache.refresh(self.rest,now=NOW,clock=0))
        await entered.wait()
        self.assertFalse(await self.cache.refresh(self.rest,now=NOW,clock=400))
        release.set()
        await task
        self.assertFalse(await self.cache.refresh(self.rest,now=NOW,clock=299))
        self.assertEqual(self.rest.liquidity_reward_daily.await_count,1)

    async def test_bad_refresh_atomic_and_cancellation_propagates(self):
        await self.cache.refresh(self.rest,now=NOW,clock=0)
        self.rest.liquidity_reward_daily.return_value=summaries(61,60)
        self.assertFalse(await self.cache.refresh(self.rest,now=NOW,clock=300))
        self.assertEqual(self.cache.rows[("2026-09-15","123")]["snapshot_count"],32)
        self.rest.liquidity_reward_daily.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.cache.refresh(self.rest,now=NOW,clock=600)


class ReportingWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=repair_fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.runtime=self.fixture.runtime
        self.runtime.universe=[replace(self.runtime.universe[0],event_id="123")]
        self.runtime.ranked=[replace(candidate(), event_ticker="E")]
        self.runtime.reward_reporting_rest.liquidity_reward_daily=AsyncMock(return_value=summaries())

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_rank_report_is_enriched_without_changing_ranking_or_risk(self):
        r=self.runtime
        before=public_rank_rows(r.ranked)
        state=(r.gate.state,r._dynamic_pending_cap,dict(r._uptime))
        await r.exchange_reward_uptime.refresh(r.reward_reporting_rest,now=NOW,clock=0)
        r._write_rank_report(NOW+timedelta(seconds=1))
        report=json.loads((r.directory/"rank-report.json").read_text())
        row=report["candidates"][0]
        view=row.pop("exchange_reported_uptime")
        self.assertEqual(view["current_percentage"],"53.3333")
        self.assertEqual(row,before[0])
        self.assertEqual((r.gate.state,r._dynamic_pending_cap,dict(r._uptime)),state)
        self.assertIs(r.reward_reporting_rest.governor,r.governor)
        self.assertIsNot(r.reward_reporting_rest._private_lock,r.rest._private_lock)

    async def test_duplicate_event_identity_is_not_attached_to_two_tickers(self):
        r=self.runtime
        r.universe.append(replace(r.universe[0],event_ticker="OTHER"))
        await r.exchange_reward_uptime.refresh(r.reward_reporting_rest,now=NOW,clock=0)
        r._write_rank_report(NOW+timedelta(seconds=1))
        report=json.loads((r.directory/"rank-report.json").read_text())
        for view in report["exchange_uptime_by_event"].values():
            self.assertIsNone(view["current_percentage"])

    async def test_background_refresh_failure_does_not_halt_and_close_cancels_task(self):
        r=self.runtime
        r.reward_reporting_rest.liquidity_reward_daily.side_effect=RuntimeError("offline failure")
        r._reward_reporting_task=asyncio.create_task(r._reward_reporting_loop())
        await asyncio.sleep(.02)
        self.assertTrue(r.gate.may_submit)
        self.assertEqual(r.exchange_reward_uptime.error,"request_or_response_failed")
        await r.close()
        self.assertIsNone(r._reward_reporting_task)


if __name__ == "__main__":
    unittest.main()
