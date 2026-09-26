"""Policy v9: shielded placement, 50% uptime ledger, category exclusion.

All venue, account and transport I/O is synthetic and offline.
"""
import policy_v8  # noqa: F401  (legacy suites share this process; v9 uses LIVE_POLICY)
import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import test_adaptive_queue as adaptive
import test_queue_pilot as fx
import test_repair_integration as repair
from gemini_bot.adaptive_queue import AdaptivePolicy, AdaptiveQueuePilot
from gemini_bot.config import ConfigurationError, load_policy
from gemini_bot.discovery import RewardUniverseLoader
from gemini_bot.domain import (
    BookTop, ContractRules, Outcome, QuoteLeg, QuotePair, RewardPool, Side, UptimeProgress,
    uptime_progress_key,
)
from gemini_bot.market_state import LocalBook
from gemini_bot.reward_ranking import RankInput, rank_market_candidates, select_sticky_markets
from gemini_bot.competition import CompetitionEvidence
from gemini_bot.shield import ShieldContext, ShieldPolicy
from gemini_bot.uptime import eastern_day_window
from gemini_bot.uptime_ledger import UptimeLedgerPolicy, UptimeStatus, admission_allowed, classify

NOW = fx.NOW
LIVE = policy_v8.LIVE_POLICY
LEDGER = UptimeLedgerPolicy(17, D("0.50"), 30, 120, 60)
SHIELD = ShieldPolicy(D(500), D(".60"), D(3), D(".10"), D(300))


def shielded_policy():
    return AdaptivePolicy((), adaptive.policy().limits, 'all_selected', 'shielded')


def progress(elapsed, counted, remaining, venue_remaining):
    return UptimeProgress(elapsed, counted, remaining,
                          remaining_venue_snapshots_estimate=venue_remaining)


def depth_book(bids, asks, seq=1):
    b = LocalBook("X")
    b.apply_snapshot([(D(p), D(q)) for p, q in bids], [(D(p), D(q)) for p, q in asks], seq, seq, NOW)
    return b


class V9ConfigTests(unittest.TestCase):
    def test_live_policy_is_v9_with_owner_values(self):
        strategy, risk = load_policy(LIVE)
        self.assertTrue(strategy.shielded)
        self.assertEqual(strategy.quote_quantity, D(50))
        self.assertEqual(risk.max_gross_reservation_usd, D(400))
        self.assertEqual(risk.max_market_exposure_usd, D(90))
        self.assertEqual(risk.risk_window_loss_usd, D(30))
        self.assertEqual(risk.max_pending_orders, 40)
        self.assertEqual(strategy.shield, SHIELD)
        self.assertEqual(strategy.scoring_day_boundary_hour, 17)
        self.assertIn("crypto", strategy.excluded_categories)
        self.assertEqual(strategy.quote_offset_ticks, 0)

    def test_v8_fixture_still_loads_as_stay_best(self):
        strategy, risk = load_policy(policy_v8.V8_POLICY)
        self.assertEqual(strategy.queue_pilot.placement_mode, 'stay_best')
        self.assertFalse(strategy.shielded)
        self.assertEqual(strategy.scoring_day_boundary_hour, 0)

    def mutate(self, change):
        raw = json.loads(LIVE.read_text())
        change(raw)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "policy.json"
            path.write_text(json.dumps(raw))
            return load_policy(path)

    def test_v9_schema_is_strict(self):
        for change in (
            lambda r: r.pop("shield"),
            lambda r: r["shield"].update(entry_contracts=500),
            lambda r: r["shield"].update(keep_fraction="1.2"),
            lambda r: r["uptime_ledger"].update(scoring_day_boundary_et="17:30"),
            lambda r: r["uptime_ledger"].update(margin_minutes="30"),
            lambda r: r.update(excluded_categories="crypto"),
            lambda r: r["queue_pilot"].update(placement_mode="stay_best"),
            lambda r: r.update(quote_quantity="30"),
            lambda r: r.update(policy_version=8),
        ):
            with self.assertRaises(ConfigurationError):
                self.mutate(change)


class UptimeLedgerTests(unittest.TestCase):
    def test_scoring_window_runs_five_pm_to_five_pm_eastern(self):
        start, end = eastern_day_window(datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc), 17)
        self.assertEqual(start, datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc))
        start, _ = eastern_day_window(datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc), 17)
        self.assertEqual(start, datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc))

    def test_dst_end_window_has_twenty_five_hours(self):
        start, end = eastern_day_window(datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc), 17)
        self.assertEqual(end - start, timedelta(hours=25))

    def test_midnight_boundary_unchanged_for_legacy(self):
        start, end = eastern_day_window(datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc))

    def test_statuses(self):
        # 1440-minute window: bare 720, target 750.
        self.assertIs(classify(progress(800, 750, 600, 640), LEDGER).status, UptimeStatus.LOCKED)
        self.assertIs(classify(progress(400, 380, 1000, 1040), LEDGER).status, UptimeStatus.ON_TRACK)
        # Needs 150 more; 300 quotable leaves 150 spare (on track), 250 leaves 100 (at risk).
        self.assertIs(classify(progress(1000, 600, 300, 440), LEDGER).status, UptimeStatus.ON_TRACK)
        self.assertIs(classify(progress(1000, 600, 250, 440), LEDGER).status, UptimeStatus.AT_RISK)
        self.assertIs(classify(progress(1100, 600, 200, 340), LEDGER).status, UptimeStatus.AT_RISK)
        self.assertIs(classify(progress(1200, 500, 200, 240), LEDGER).status, UptimeStatus.LOST)
        self.assertIs(classify(None, LEDGER).status, UptimeStatus.UNKNOWN)

    def test_admission_requires_spare_minutes(self):
        self.assertTrue(admission_allowed(classify(progress(10, 0, 1400, 1430), LEDGER), LEDGER))
        # 780 remaining, target 750: spare 30 < 60.
        self.assertFalse(admission_allowed(classify(progress(660, 0, 780, 780), LEDGER), LEDGER))
        self.assertFalse(admission_allowed(classify(None, LEDGER), LEDGER))


class ShieldedAdviceTests(unittest.TestCase):
    def setUp(self):
        self.p = AdaptiveQueuePilot(shielded_policy(), quote_quantity=D(50))
        self.p.sync_selected({'X'})
        self.book = depth_book([(".50", "300"), (".49", "450")], [(".52", "300"), (".53", "450")])
        self.p.observe_book(self.book, generation=(0, 0), at=NOW, orders=self.orders())
        self.p.proof_required.clear()
        self.p.anchor_send_at('X', Outcome.YES, 'cYES', D('.49'), D(400), NOW)
        self.p.anchor_send_at('X', Outcome.NO, 'cNO', D('.47'), D(400), NOW)

    def orders(self):
        return (fx.order(Outcome.YES, 'cYES', D('.49'), D(50)),
                fx.order(Outcome.NO, 'cNO', D('.47'), D(50)))

    def context(self, bids, asks, targets=None, uptime_first=False):
        ext = depth_book(bids, asks)
        mid = ext.top().midpoint
        return ShieldContext(SHIELD, ext, {Outcome.YES: mid, Outcome.NO: 1 - mid},
                             targets or {Outcome.YES: D('.49'), Outcome.NO: D('.47')}, uptime_first)

    def advice(self, context, at=NOW, **kwargs):
        return self.p.advice('X', now=at, generation=(0, 0), orders=self.orders(), safe=True,
                             shield=context, **kwargs)

    def test_intact_shield_keeps_pair(self):
        ctx = self.context([(".50", "300"), (".49", "400")], [(".52", "300"), (".53", "400")])
        self.assertEqual(self.advice(ctx), 'keep')

    def test_draining_better_level_moves_only_that_leg(self):
        ctx = self.context([(".50", "20"), (".49", "400")], [(".52", "300"), (".53", "400")])
        self.p.markets['X'].anchors[Outcome.YES].scenario.trade(D(300))
        self.assertEqual(self.advice(ctx), 'withdraw:YES')
        self.assertEqual(self.p.reasons['X'], 'near_fill_reposition')

    def test_missing_anchor_counts_nothing_ahead_at_own_level(self):
        self.p.markets['X'].anchors.pop(Outcome.NO)
        ctx = self.context([(".50", "300"), (".49", "400")], [(".52", "200"), (".53", "400")])
        self.assertEqual(self.advice(ctx), 'withdraw:NO')

    def test_upgrade_is_rate_limited_and_disabled_by_breach(self):
        closer = {Outcome.YES: D('.50'), Outcome.NO: D('.47')}
        ctx = self.context([(".50", "600"), (".49", "400")], [(".52", "300"), (".53", "400")], closer)
        self.assertEqual(self.advice(ctx), 'withdraw:YES')
        self.assertEqual(self.p.reasons['X'], 'shield_upgrade')
        self.assertEqual(self.advice(ctx, at=NOW + timedelta(seconds=10)), 'keep')
        self.assertEqual(self.advice(ctx, at=NOW + timedelta(seconds=301)), 'withdraw:YES')
        self.p.cancel_envelope_breached = True
        self.assertEqual(self.advice(ctx, at=NOW + timedelta(seconds=700)), 'keep')

    def test_breach_never_disables_near_fill_reposition(self):
        self.p.cancel_envelope_breached = True
        ctx = self.context([(".50", "10"), (".49", "10")], [(".52", "300"), (".53", "400")])
        self.p.markets['X'].anchors[Outcome.YES].scenario.trade(D(400))
        self.assertEqual(self.advice(ctx), 'withdraw:YES')

    def test_uptime_first_skips_upgrades(self):
        closer = {Outcome.YES: D('.50'), Outcome.NO: D('.47')}
        ctx = self.context([(".50", "600"), (".49", "400")], [(".52", "300"), (".53", "400")],
                           closer, uptime_first=True)
        self.assertEqual(self.advice(ctx), 'keep')

    def test_missing_context_withdraws(self):
        self.assertEqual(self.advice(None), 'withdraw')


class V9DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_excludes_crypto_and_prefers_uncontested_long_lived_events(self):
        rows = [
            {"event_ticker": "BTC1", "category": "Bitcoin", "daily_pool_usd": "150", "pool_id": 70,
             "qualifying_maker_count": 0, "ends_at": "2026-09-26T00:10:00Z"},
            {"event_ticker": "CF-A", "category": "College Football", "daily_pool_usd": "5000", "pool_id": 68,
             "qualifying_maker_count": 30, "ends_at": "2027-01-20T00:00:00Z"},
            {"event_ticker": "CF-B", "category": "College Football", "daily_pool_usd": "5000", "pool_id": 68,
             "qualifying_maker_count": 0, "ends_at": "2026-09-27T00:00:00Z"},
            {"event_ticker": "CF-C", "category": "College Football", "daily_pool_usd": "5000", "pool_id": 68,
             "qualifying_maker_count": 0, "ends_at": "2026-09-28T00:00:00Z"},
        ]
        read = []

        class Rest:
            async def reward_events(self):
                return rows

            async def event(self, ticker):
                read.append(ticker)
                return {"event": {"ticker": ticker, "category": "Sports", "contracts": []}}

        loader = RewardUniverseLoader(Rest())
        loader.excluded_categories = frozenset({"bitcoin", "crypto"})
        loader.per_pool_reads = 24
        loader.prefer_uncontested = True
        await loader.load(maximum_pools=8, now=NOW)
        self.assertNotIn("BTC1", read)
        self.assertEqual(read, ["CF-C", "CF-B", "CF-A"])


class V9DiscoveryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_loader_uses_scoring_day_boundary(self):
        from unittest.mock import patch
        import gemini_bot.discovery as discovery
        seen = []

        def window(item, now, **kwargs):
            seen.append(kwargs.get("day_boundary_hour"))
            return NOW, NOW, "test"

        reward = {"event_ticker": "G", "category": "College Football", "daily_pool_usd": "5000",
                  "pool_id": 68, "qualifying_maker_count": 0, "ends_at": "2026-09-28T00:00:00Z"}
        event = {"event": {"ticker": "G", "category": "Sports", "status": "active", "contracts": [{
            "instrumentSymbol": "GEMI-G-A", "status": "active", "marketState": "open",
            "prices": {"bestBid": "0.40", "bestAsk": "0.41"}}]}}

        class Rest:
            async def reward_events(self):
                return [reward]

            async def event(self, ticker):
                return event

        loader = RewardUniverseLoader(Rest())
        loader.day_boundary_hour = 17
        with patch.object(discovery, "estimated_reward_window", side_effect=window), \
                patch.object(discovery, "join_reward_contracts", return_value=[
                    replace(repair_contract(), event_ticker="G")]):
            await loader.load(maximum_pools=4, now=NOW)
        self.assertEqual(seen, [17])


def repair_contract():
    from gemini_bot.catalog import RewardedContract
    return RewardedContract("E", repair.candidate_pool(), repair.candidate_rules(),
                            (NOW + timedelta(hours=2)).isoformat())


class V9RankingTests(unittest.TestCase):
    def item(self, makers, depth):
        return RankInput(
            RewardPool("pool", D("250"), qualifying_maker_count=makers),
            ContractRules("X", D(".01"), D("1"), D("1")),
            BookTop("X", D(".48"), D("100"), D(".52"), D("100"), 1, NOW),
            event_ticker="E", competition=CompetitionEvidence(D(depth), D(0), D(0), 1),
        )

    def test_zero_maker_depth_is_treated_as_house_liquidity(self):
        contested = rank_market_candidates([self.item(0, 5000)], quantity=D(50), offset_ticks=0, now=NOW)[0]
        uncontested = rank_market_candidates([self.item(0, 5000)], zero_maker_depth_is_house=True,
                                             quantity=D(50), offset_ticks=0, now=NOW)[0]
        self.assertGreater(uncontested.estimated_score_share, contested.estimated_score_share)
        still = rank_market_candidates([self.item(3, 5000)], zero_maker_depth_is_house=True,
                                       quantity=D(50), offset_ticks=0, now=NOW)[0]
        self.assertEqual(still.estimated_score_share, contested.estimated_score_share)

    def test_insufficient_shield_is_a_reason(self):
        ranked = rank_market_candidates([replace(self.item(0, 0), quote_error="insufficient_shield")],
                                        quantity=D(50), offset_ticks=0, now=NOW)[0]
        self.assertIsNone(ranked.quote)
        self.assertIn("insufficient_shield", ranked.reasons)

    def test_retain_incumbents_skips_economics_removal(self):
        c = rank_market_candidates([self.item(0, 0)], quantity=D(50), offset_ticks=0, now=NOW)[0]
        c = replace(c, expected_execution_cost_usd=D(10_000))
        kwargs = dict(sticky_pool_ids={c.owner_key}, locked_pool_ids=set(), permitted_symbols={"X"},
                      maximum_active_pools=4, actionable=lambda _: True, entry_time_valid=lambda _: True)
        self.assertEqual(select_sticky_markets([c], **kwargs)[0], [])
        kept, _ = select_sticky_markets([c], retain_incumbents=True, **kwargs)
        self.assertEqual([k.symbol for k in kept], ["X"])


class ShieldedExecutionTests(unittest.IsolatedAsyncioTestCase):
    """Near-fill repositioning through the real send guard and transport."""

    async def asyncTearDown(self):
        await fx.PilotIntegrationTests.asyncTearDown(self)

    async def asyncSetUp(self):
        # Reuse the pilot harness wiring (real guards, synthetic transport).
        await fx.PilotIntegrationTests.asyncSetUp(self)
        strategy, risk = load_policy(LIVE)
        self.r.config = replace(self.r.config, strategy=replace(strategy, entry_quality=None), risk=risk)
        self.r.queue_pilot = AdaptiveQueuePilot(strategy.queue_pilot, quote_quantity=D(50))
        self.r.queue_pilot.sync_selected({'X'})
        self.r.queue_pilot.proof_required.clear()
        self.r._pilot_proved['X'] = (time.monotonic(), (0, 0), self.r.account.fill_version)
        self.r.universe = [replace(self.r.universe[0], event_ticker="E")]
        self.r._uptime = {uptime_progress_key("E", "pool"): progress(100, 90, 1300, 1340)}
        self.set_book([(".50", "300"), (".49", "400"), (".48", "600")],
                      [(".52", "300"), (".53", "400"), (".54", "600")], seq=2)
        self.cancelled = []

        async def cancel(order_id, **kwargs):
            self.cancelled.append(order_id)
            o = self.r.account.reducer.orders[order_id]
            self.r.account.apply(dict(e='orderUpdate', i=order_id, c=o.client_order_id, s='X', S='BUY',
                                      O=o.outcome.value, p=str(o.price), q=str(o.original_quantity),
                                      z='0', Z='0', X='CANCELED'))
        self.r._cancel_order_and_confirm = cancel
        self.r.rest.positions = AsyncMock(return_value=[])
        self.r.rest.active_orders = AsyncMock(side_effect=self.active_rows)

    def set_book(self, bids, asks, seq):
        self.r.public.books['X'] = depth_book(bids, asks, seq)
        self.r._observe_queue_pilot('X')

    async def active_rows(self):
        return [dict(orderId=o.order_id, clientOrderId=o.client_order_id, symbol='X', side='BUY',
                     outcome=o.outcome.value, quantity=str(o.original_quantity),
                     remainingQuantity=str(o.remaining_quantity), price=str(o.price), status='OPEN')
                for o in self.r.account.reducer.orders.values() if o.working]

    def candidate(self):
        return replace(repair.candidate(D(50)), event_ticker="E", quote=QuotePair(
            QuoteLeg('X', Outcome.YES, Side.BUY, D('.49'), D(50)),
            QuoteLeg('X', Outcome.NO, Side.BUY, D('.47'), D(50))))

    async def place_pair(self):
        await self.r._quote_pair_locked(self.candidate())
        self.assertEqual([(p['params']['eventOutcome'], p['params']['price']) for p in self.sent],
                         [('YES', '0.49'), ('NO', '0.47')])
        # Public depth now includes our own resting 50 on each side.
        self.set_book([(".50", "300"), (".49", "450"), (".48", "600")],
                      [(".52", "300"), (".53", "450"), (".54", "600")], seq=3)
        self.r.ranked = [self.candidate()]
        self.r._sticky_markets = {self.candidate().owner_key: ("E", "X")}

    async def test_pair_rests_behind_the_shield_not_at_the_touch(self):
        await self.place_pair()
        anchors = self.r.queue_pilot.markets['X'].anchors
        self.assertEqual(anchors[Outcome.YES].scenario.ahead_scenario, D(400))
        self.assertEqual(self.r._pilot_advice('X'), 'keep')
        self.assertFalse(await self.r._cancel_too_aggressive_quote('X'))
        self.assertEqual(self.cancelled, [])

    async def test_near_fill_moves_only_that_leg_deeper(self):
        await self.place_pair()
        # Better YES volume drains and trades eat the queue ahead of us.
        self.set_book([(".50", "20"), (".49", "150"), (".48", "600")],
                      [(".52", "300"), (".53", "450"), (".54", "600")], seq=4)
        self.assertEqual(self.r._pilot_advice('X'), 'withdraw:YES')
        yes_id = self.r._working_entry('X', Outcome.YES).order_id
        no_order = self.r._working_entry('X', Outcome.NO)
        # Our cancelled 50 leaves the public .49 level at 100 external contracts.
        original = self.r._cancel_order_and_confirm

        async def cancel(order_id, **kwargs):
            await original(order_id)
            self.set_book([(".50", "20"), (".49", "100"), (".48", "600")],
                          [(".52", "300"), (".53", "450"), (".54", "600")], seq=5)
        self.r._cancel_order_and_confirm = cancel
        self.assertTrue(await self.r._cancel_too_aggressive_quote('X'))
        self.assertEqual(self.cancelled, [yes_id])
        self.assertEqual(self.sent[-1]['params']['eventOutcome'], 'YES')
        self.assertEqual(self.sent[-1]['params']['price'], '0.48')
        self.assertTrue(no_order.working)
        self.assertIs(self.r._working_entry('X', Outcome.NO), no_order)
        rows = self.r.journal.connection.execute(
            "SELECT COUNT(*) FROM audit_event WHERE kind='shield_leg_reposition'").fetchone()[0]
        self.assertEqual(rows, 1)

    async def test_fill_during_leg_cancel_hands_over_to_fill_safety(self):
        await self.place_pair()
        yes = self.r._working_entry('X', Outcome.YES)

        async def cancel_after_fill(order_id, **kwargs):
            self.cancelled.append(order_id)
            self.r.account.apply(dict(e='orderUpdate', i=order_id, c=yes.client_order_id, s='X', S='BUY',
                                      O='YES', p='.49', q='50', z='0', Z='7', X='CANCELED', t='fill-1',
                                      L='.49', n='0', T=int(NOW.timestamp() * 1000)))
        self.r._cancel_order_and_confirm = cancel_after_fill
        sends = len(self.sent)
        self.assertFalse(await self.r._replace_entry_leg('X', Outcome.YES, "test"))
        await self.r._place_single_leg('X', Outcome.YES)
        self.assertEqual(len(self.sent), sends)
        self.assertIn('X', self.r.queue_pilot.proof_required)
        self.assertIn('X', self.r.account.blocked_entries)

    async def test_lost_event_is_not_actionable_and_admission_needs_spare_minutes(self):
        candidate = replace(self.candidate(), reasons=())
        self.assertTrue(self.r._candidate_actionable(candidate))
        self.r._uptime = {uptime_progress_key("E", "pool"): progress(1200, 500, 200, 240)}
        self.assertFalse(self.r._candidate_actionable(candidate))
        self.r._uptime = {uptime_progress_key("E", "pool"): progress(660, 0, 780, 780)}
        self.assertFalse(self.r._candidate_actionable(candidate))  # new event, spare < 60
        self.r._sticky_markets = {candidate.owner_key: ("E", "X")}
        self.assertTrue(self.r._candidate_actionable(candidate))  # incumbent keeps quoting

    async def test_rank_report_exposes_ledger_status(self):
        row = self.r._uptime_ledger_row("E", "pool", None, NOW)
        self.assertEqual(row["status"], "on_track")
        self.assertEqual((row["counted"], row["target"]), (90, 750))
        self.assertTrue(row["boundary_is_inferred_not_documented"])
        self.assertIsNone(row["previous_window_check"])

    async def test_economics_never_pause_a_held_event(self):
        candidate = replace(self.candidate(), reasons=("non-positive conservative net-profit estimate",))
        self.assertFalse(self.r._candidate_actionable(candidate))
        self.r._sticky_markets = {candidate.owner_key: ("E", "X")}
        self.assertTrue(self.r._candidate_actionable(candidate))
        stale = replace(candidate, reasons=("stale market data",))
        self.assertFalse(self.r._candidate_actionable(stale))


if __name__ == "__main__":
    unittest.main()
