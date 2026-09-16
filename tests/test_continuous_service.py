"""Offline continuous-service acceptance. No real credentials or venue I/O."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_repair_integration as fixtures
import test_overnight_continuity as overnight
import test_stream_recovery as recovery
from gemini_bot.activation import consume_activation_permit, finish_supervised_activation
from gemini_bot.cli import _run_live, LiveRunnerExit
from gemini_bot.continuity import RestingObservation
from gemini_bot.domain import EngineState, Outcome
from gemini_bot.live_ws import EntryStateChangedBeforeSend
from gemini_bot.recovery_policy import transient_failure, retry_after_seconds, NoMarketsAvailable
from gemini_bot.rest_client import ApiError


class ContinuousPermitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = overnight.OvernightPermitTests()
        self.fixture.setUp()
        self.fixture.value.update(permit_version=7, expires_at=None)

    def tearDown(self):
        self.fixture.tearDown()

    def test_explicit_continuous_authority_outlives_day_without_extending_override(self):
        p = self.fixture.load(overnight.MIDNIGHT + timedelta(days=60))
        self.assertIsNone(p.expires_at)
        self.assertEqual(p.uptime_override_until, overnight.MIDNIGHT)

    def test_null_expiry_is_not_accepted_by_legacy_permits(self):
        for version in (5, 6):
            self.fixture.value['permit_version'] = version
            with self.assertRaises(ValueError):
                self.fixture.load(overnight.MIDNIGHT)

    def test_continuous_schema_rejects_accidental_finite_expiry(self):
        self.fixture.value['expires_at'] = (overnight.MIDNIGHT + timedelta(hours=2)).isoformat()
        with self.assertRaisesRegex(ValueError, 'explicit null'):
            self.fixture.load(overnight.MIDNIGHT)

    def test_continuous_override_cannot_be_indefinite(self):
        self.fixture.value['uptime_override_until'] = (overnight.MIDNIGHT + timedelta(days=2)).isoformat()
        with self.assertRaises(ValueError):
            self.fixture.load(overnight.MIDNIGHT)

    def test_repeat_reconciled_recovery_is_same_session_only(self):
        for _ in range(8):
            p = self.fixture.load(overnight.MIDNIGHT)
            consume_activation_permit(p, self.fixture.store, supervisor_session_id='s')
            finish_supervised_activation(p, self.fixture.store, supervisor_session_id='s', outcome='recoverable_flat')
        p = self.fixture.load(overnight.MIDNIGHT)
        with self.assertRaises(ValueError):
            consume_activation_permit(p, self.fixture.store, supervisor_session_id='other')

    def test_active_closed_and_attention_authority_cannot_reactivate(self):
        p = self.fixture.load(overnight.MIDNIGHT)
        consume_activation_permit(p, self.fixture.store, supervisor_session_id='s')
        with self.assertRaises(ValueError):
            self.fixture.load(overnight.MIDNIGHT)
        for outcome in ('closed', 'attention'):
            state = {'session_id': 's', 'activations': 1, 'status': outcome}
            self.fixture.store.set_runtime_state('consumed_permit:approval-1', json.dumps(state), updated_at=overnight.MIDNIGHT)
            with self.assertRaises(ValueError):
                self.fixture.load(overnight.MIDNIGHT)

    def test_account_build_policy_binding_is_unchanged(self):
        for field in ('account_fingerprint', 'build_sha256', 'policy_sha256'):
            value = self.fixture.value[field]
            self.fixture.value[field] = 'wrong'
            with self.assertRaises(ValueError):
                self.fixture.load(overnight.MIDNIGHT)
            self.fixture.value[field] = value


class RestingRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeRepairTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continuous_service = True
        self.r._cancel_symbol_entries = AsyncMock()
        self.r._quote_pair_locked = AsyncMock()
        self.c = fixtures.candidate(self.r.config.strategy.quote_quantity)
        for leg in (self.c.quote.yes, self.c.quote.no):
            intent = self.r.ledger.prepare(leg, fixtures.OrderPurpose.ENTRY)
            self.r.ledger.mark_sent(intent.client_order_id)
            self.r.account.apply({'e': 'orderUpdate', 'i': leg.outcome.value, 'c': intent.client_order_id,
                's': 'X', 'S': 'BUY', 'O': leg.outcome.value,
                'q': str(leg.quantity), 'z': str(leg.quantity),
                'p': str(leg.price), 'X': 'NEW'})

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_safe_farther_pair_is_retained_without_cancel_or_new_intent(self):
        for o in self.r.account.reducer.orders.values():
            o.price -= D('.01')
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.r._quote_pair_locked.assert_not_awaited()
        self.assertEqual(self.r._retained_pairs, 1)

    async def test_legacy_mode_still_reprices_exactly(self):
        self.r._continuous_service = False
        for o in self.r.account.reducer.orders.values():
            o.price -= D('.01')
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_unsafe_price_still_cancels(self):
        self.r.account.reducer.orders['YES'].price += D('.01')
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_outside_reward_distance_still_replaces(self):
        self.r.account.reducer.orders['YES'].price = D('.39')
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_exact_reward_boundary_still_requires_economic_eligibility(self):
        self.r.account.reducer.orders['YES'].price = D('.40')
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_profitable_reward_boundary_pair_does_not_churn(self):
        c = self.r.universe[0]
        self.r.universe = [replace(c, pool=replace(c.pool, daily_budget_usd=D('10000')))]
        self.r.account.reducer.orders['YES'].price = D('.40')
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_not_awaited()
        self.assertEqual(self.r._retained_pairs, 1)

    async def test_lower_scoring_resting_pair_must_still_pass_payout_and_net_gates(self):
        c = self.r.universe[0]
        self.r.universe = [replace(c, pool=replace(c.pool, daily_budget_usd=D('10')))]
        for o in self.r.account.reducer.orders.values():
            o.price = D('.41')  # Within distance, but much lower scoring than target.
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.assertEqual(self.r._retained_pairs, 0)

    async def test_exact_cached_target_does_not_bypass_fresh_retention_failure(self):
        self.r._retain_resting_pair = lambda *_: False
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_partial_pair_is_not_retained(self):
        self.r.account.reducer.orders['YES'].remaining_quantity = D(9)
        self.r.account.reducer.orders['YES'].cumulative_filled = D(1)
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')

    async def test_ineligible_candidate_is_canceled_not_preserved_for_uptime(self):
        await self.r._maintain_candidate(replace(self.c, eligible_now=False, reasons=('unsafe',)))
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.r._quote_pair_locked.assert_not_awaited()

    async def test_paused_reward_program_blocks_send_and_cancels_existing_pair(self):
        self.r.rest.reward_config = AsyncMock(return_value={'enabled': False})
        self.assertTrue(await self.r._reward_pools_still_active())
        self.assertEqual(self.r.gate.state, EngineState.QUOTING)
        with self.assertRaises(EntryStateChangedBeforeSend):
            self.r._validate_send(self.fixture.payload())
        await self.r._maintain_candidate(self.c)
        self.r._cancel_symbol_entries.assert_awaited_once_with('X')
        self.r._quote_pair_locked.assert_not_awaited()

    async def test_disabled_legacy_reward_program_still_drains(self):
        self.r._continuous_service = False
        self.r.rest.reward_config = AsyncMock(return_value={'enabled': False})
        self.assertFalse(await self.r._reward_pools_still_active())

    async def test_metadata_read_failure_does_not_clear_pause(self):
        self.r._reward_program_paused = True
        self.r.rest.reward_config = AsyncMock(return_value={'enabled': True})
        self.r.rest.reward_events = AsyncMock(side_effect=ApiError(503, 'maintenance'))
        with self.assertRaises(ApiError):
            await self.r._reward_pools_still_active()
        self.assertTrue(self.r._reward_program_paused)

    async def test_complete_empty_metadata_refresh_returns_to_standby_not_shutdown(self):
        self.r._reward_program_paused = True
        self.r._continue_after_override = self.r._catalog_scope = True
        self.r.rest.reward_config = AsyncMock(return_value={'enabled': True})
        self.r.rest.reward_events = AsyncMock(return_value=[])
        self.r._rollover_flat_markets = AsyncMock()
        self.assertTrue(await self.r._reward_pools_still_active())
        self.assertFalse(self.r._reward_program_paused)

    async def test_stale_account_or_book_cannot_retain(self):
        working = list(self.r.account.reducer.orders.values())
        self.r.public.books['X'].synchronized = False
        self.assertFalse(self.r._retain_resting_pair(self.c, working))
        self.r.public.books['X'].synchronized = True
        self.r.account.last_position_at = 0
        self.assertFalse(self.r._retain_resting_pair(self.c, working))

    async def test_telemetry_marks_transport_gap_without_reward_credit(self):
        self.r._sticky_pool_ids = {self.r.universe[0].pool.pool_id}
        with patch('gemini_bot.orchestrator.time.monotonic', return_value=10):
            self.r._observe_resting()
        self.r._stream_holds.add('public')
        with patch('gemini_bot.orchestrator.time.monotonic', return_value=11):
            self.r._observe_resting()
        view = self.r._continuity_view()['symbols']['X']
        self.assertFalse(view['qualifying_at_last_observation'])
        self.assertEqual(view['qualifying_seconds'], 0)
        self.assertEqual(view['current_gap_seconds'], 1)


class ContinuousMidnightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = overnight.MidnightRuntimeTests()
        await self.fixture.asyncSetUp()
        self.fixture.r._continuous_service = True
        self.fixture.r._permit_expires_at = None

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_v7_midnight_ends_override_without_canceling_healthy_orders(self):
        await self.fixture.test_override_ends_exactly_at_midnight_without_draining()
        await self.fixture.test_healthy_orders_stay_resting_across_midnight()

    async def test_v7_midnight_preserves_old_uptime_loss_and_deadlines(self):
        await self.fixture.test_new_day_recomputes_eligibility_and_preserves_previous_day_rows()
        await self.fixture.test_midnight_rebuild_does_not_erase_risk_or_extend_inventory_deadline()


class LongRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = recovery.StreamRecoveryTests()
        await self.fixture.asyncSetUp()
        self.r = self.fixture.runtime
        self.r._continuous_service = True

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        self.fixture.doCleanups()

    async def test_more_than_three_transient_connect_failures_can_recover(self):
        self.r.public.connect.side_effect = [ConnectionError()] * 4 + [None]
        real_sleep = asyncio.sleep
        async def fast_sleep(delay):
            await real_sleep(0)
        with patch('gemini_bot.orchestrator.asyncio.sleep', side_effect=fast_sleep):
            await self.r._recover_stream('public', ConnectionError())
            await asyncio.wait_for(self.r._stream_recovery_task, 2)
        self.assertEqual(self.r.public.connect.await_count, 5)
        self.assertFalse(self.r._stream_holds)
        self.r.private.place_maker.assert_not_awaited()

    async def test_outage_can_exceed_old_total_budget_without_halting(self):
        ready = asyncio.Event()
        self.r.public.connect.side_effect = ready.wait
        with patch('gemini_bot.orchestrator._STREAM_RECOVERY_SECONDS', .02):
            task = asyncio.create_task(self.r._recover_stream('public', ConnectionError()))
            await asyncio.sleep(.08)
            self.assertEqual(self.r.gate.state, EngineState.QUOTING)
            self.assertTrue(self.r._stream_holds)
            ready.set()
            await task
            await asyncio.wait_for(self.r._stream_recovery_task, 2)
        self.assertFalse(self.r._stream_holds)

    async def test_integrity_failure_is_not_retried(self):
        self.r.public.connect.side_effect = ValueError('bad protocol')
        with self.assertRaises(ValueError):
            await self.r._recover_stream('public', ConnectionError())
        with self.assertRaises(ValueError):
            await self.r._stream_recovery_task
        self.assertEqual(self.r.public.connect.await_count, 1)
        self.assertEqual(self.r.gate.state, EngineState.HALTED)

    async def test_stop_interrupts_long_transport_backoff(self):
        self.r.public.connect.side_effect = ConnectionError()
        self.r._recovery_stop_requested = lambda: self.r.public.connect.await_count > 0
        with self.assertRaisesRegex(RuntimeError, 'stop'):
            await self.r._reconnect_stream('public')
        self.assertEqual(self.r.public.connect.await_count, 1)

    async def test_unknown_execution_is_still_not_replayed(self):
        intent = self.r.ledger.prepare(fixtures.candidate().quote.yes, fixtures.OrderPurpose.ENTRY)
        self.r.ledger.mark_sent(intent.client_order_id)
        with self.assertRaisesRegex(RuntimeError, 'unknown mutation'):
            await self.r._recover_stream('private', ConnectionError())
        self.r.private.connect.assert_not_awaited()

    async def test_connected_but_unproven_account_does_not_resume_indefinitely(self):
        from gemini_bot.stream_recovery import RecoveryPending
        self.r._recovery_account_proof = AsyncMock(side_effect=RecoveryPending('terminal evidence absent'))
        with patch('gemini_bot.orchestrator._STREAM_RECOVERY_SECONDS', .02):
            await self.r._recover_stream('public', ConnectionError())
            with self.assertRaisesRegex(RuntimeError, 'proof budget'):
                await asyncio.wait_for(self.r._stream_recovery_task, 2)
        self.assertTrue(self.r._stream_holds)
        self.assertEqual(self.r.gate.state, EngineState.HALTED)

    async def test_proven_quarantine_does_not_poll_rest_through_public_outage(self):
        waiting = asyncio.Event()
        self.r.public.connect.side_effect = waiting.wait
        task = asyncio.create_task(self.r._recover_stream('public', ConnectionError()))
        await asyncio.sleep(1.1)
        # One bracket to quarantine, not ongoing high-frequency REST polling.
        self.assertEqual(self.r.rest.active_orders.await_count, 2)
        waiting.set()
        await task
        await asyncio.wait_for(self.r._stream_recovery_task, 2)

    async def test_recovery_does_not_extend_overdue_inventory_deadline(self):
        deadline = fixtures.NOW - timedelta(seconds=10)
        self.r.journal.persist_deadline('X', 'YES', fixtures.NOW - timedelta(minutes=6), deadline, D(10))
        await self.r._recover_stream('public', ConnectionError())
        await asyncio.wait_for(self.r._stream_recovery_task, 2)
        self.assertEqual(self.r.journal.get_deadline('X', 'YES'), deadline)


class ContinuityCounterTests(unittest.TestCase):
    def test_read_throttle_delay_is_not_capped_or_forgotten(self):
        self.assertEqual(retry_after_seconds(ApiError(429, 'limit', retry_after=900)), 900)
        with self.assertRaises(ValueError):
            retry_after_seconds(ApiError(429, 'limit', retry_after=float('inf')))

    def test_known_qualifying_seconds_and_gap_are_separate(self):
        value = RestingObservation()
        for clock, qualifying in [(1, True), (2, True), (3, False), (4, False), (5, True)]:
            value.observe(clock, qualifying)
        self.assertEqual(value.qualifying_seconds, 1)
        self.assertEqual(value.longest_gap_seconds, 3)
        self.assertEqual(value.current_gap_seconds, 0)

    def test_missing_observations_are_not_credited(self):
        value = RestingObservation()
        value.observe(1, True)
        value.observe(101, True)
        self.assertEqual(value.qualifying_seconds, 0)
        self.assertEqual(value.unobserved_seconds, 100)

    def test_only_known_transport_failures_are_retryable(self):
        for exc in (ConnectionError(), TimeoutError(), ApiError(503, 'maintenance'), ApiError(429, 'limit')):
            self.assertTrue(transient_failure(exc))
        for exc in (ValueError(), RuntimeError(), PermissionError(), ApiError(401, 'auth'), ApiError(400, 'bad')):
            self.assertFalse(transient_failure(exc))


class StartupWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_v7_fatal_flat_exit_is_not_classified_as_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'permit.json'
            path.write_text(json.dumps({'permit_version': 7}))
            (Path(directory) / 'status.json').write_text(json.dumps({'phase': 'stopped_flat'}))
            runtime = AsyncMock()
            runtime._activation_consumed = True
            runtime._continuous_service = True
            runtime._recoverable_exit = False
            runtime.activate.side_effect = ValueError('software defect')
            with patch('gemini_bot.cli.load_live_config'), patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime):
                with self.assertRaises(LiveRunnerExit) as result:
                    await _run_live(path, Path(directory), path, ignore_uptime_proxy=False, supervisor_session_id='s')
            self.assertEqual(result.exception.code, 3)

    async def test_legacy_unknown_exception_still_cannot_restart_after_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'permit.json'
            path.write_text(json.dumps({'permit_version': 6}))
            (Path(directory) / 'status.json').write_text(json.dumps({'phase': 'stopped_flat'}))
            runtime = AsyncMock()
            runtime._activation_consumed = True
            runtime._continuous_service = False
            runtime.activate.side_effect = KeyError('software defect')
            with patch('gemini_bot.cli.load_live_config'), patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime):
                with self.assertRaises(KeyError):
                    await _run_live(path, Path(directory), path, ignore_uptime_proxy=False, supervisor_session_id='s')

    async def test_continuous_startup_wait_is_read_only_and_closes_failed_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'permit.json'
            path.write_text(json.dumps({'permit_version': 7}))
            runtime = AsyncMock()
            runtime._activation_consumed = False
            for exc in (ApiError(503, 'maintenance'), NoMarketsAvailable('empty')):
                runtime.prepare.side_effect = exc
                with patch('gemini_bot.cli.load_live_config'), patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime):
                    with self.assertRaises(LiveRunnerExit) as result:
                        await _run_live(path, Path(directory), path, ignore_uptime_proxy=False, supervisor_session_id='s')
                self.assertEqual(result.exception.code, 6)
            runtime.activate.assert_not_awaited()
            self.assertEqual(runtime.close.await_count, 2)

    async def test_unknown_startup_failure_does_not_gain_retry_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'permit.json'
            path.write_text(json.dumps({'permit_version': 7}))
            runtime = AsyncMock()
            runtime._activation_consumed = False
            runtime.prepare.side_effect = RuntimeError('unknown ownership')
            with patch('gemini_bot.cli.load_live_config'), patch('gemini_bot.cli.ProductionOrchestrator', return_value=runtime):
                with self.assertRaisesRegex(RuntimeError, 'unknown ownership'):
                    await _run_live(path, Path(directory), path, ignore_uptime_proxy=False, supervisor_session_id='s')
            runtime.activate.assert_not_awaited()


class NativeSupervisorTests(unittest.TestCase):
    def run_harness(self, results, version=7, preparation='missing'):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = subprocess.run(['powershell', '-NoProfile', '-File', str(root / 'tests/supervisor_harness.ps1'),
                '-RuntimePath', directory, '-SupervisorScript', str(root / 'scripts/supervise_production.ps1'),
                '-Version', str(version), '-Results', results, '-Preparation', preparation], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((path / 'harness-counts.json').exists(), result.stdout + result.stderr)
            counts = json.loads((path / 'harness-counts.json').read_text(encoding='utf-8-sig'))
            events = [json.loads(line) for line in (path / 'supervisor.jsonl').read_text(encoding='utf-8-sig').splitlines()]
            self.assertFalse(any('supervisor exception' in r['reason'] for r in events), events)
            return counts, events

    def test_startup_outage_retries_beyond_legacy_lifetime_cap(self):
        counts, events = self.run_harness('6,6,6,6,3')
        self.assertEqual(counts['launches'], 5)
        self.assertIn('BACKOFF', [r['state'] for r in events])
        self.assertIn('ATTENTION_REQUIRED', [r['state'] for r in events])

    def test_server_cooldown_survives_runner_boundary_and_operator_can_stop(self):
        counts, events = self.run_harness('cooldown,3')
        self.assertEqual(counts['launches'], 1)
        self.assertIn('BACKOFF', [r['state'] for r in events])
        self.assertEqual(events[-1]['state'], 'STOPPED')

    def test_fast_flat_crash_loop_enters_attention(self):
        counts, events = self.run_harness('5,5,5')
        self.assertEqual(counts['launches'], 3)
        self.assertIn('ATTENTION_REQUIRED', [r['state'] for r in events])

    def test_clean_nonoperator_drain_does_not_restart_or_exit_service(self):
        counts, events = self.run_harness('0')
        self.assertEqual(counts['launches'], 1)
        self.assertIn('ATTENTION_REQUIRED', [r['state'] for r in events])

    def test_unknown_or_cross_session_result_never_restarts(self):
        for result in ('missing', 'cross', '3'):
            with self.subTest(result=result):
                counts, events = self.run_harness(result)
                self.assertEqual(counts['launches'], 1)
                self.assertIn('ATTENTION_REQUIRED', [r['state'] for r in events])

    def test_legacy_clean_exit_still_stops(self):
        counts, events = self.run_harness('0', version=6)
        self.assertEqual(counts['launches'], 1)
        self.assertNotIn('ATTENTION_REQUIRED', [r['state'] for r in events])

    def test_preparation_result_is_attempt_bound_and_does_not_authorize_restart(self):
        for mode in ('passed','wrong','missing'):
            counts, events = self.run_harness('3', preparation=mode)
            self.assertEqual(counts['launches'],1)
            attention = next(r for r in events if r['state']=='ATTENTION_REQUIRED')
            self.assertEqual('reconciliation passed' in attention['reason'],mode=='passed')


if __name__ == '__main__':
    unittest.main()
