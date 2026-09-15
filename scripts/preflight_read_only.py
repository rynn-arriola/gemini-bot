"""Bounded, explicitly non-ordering production preflight on native Windows."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from gemini_bot.config import load_preflight_config
from gemini_bot.domain import EngineState
from gemini_bot.live_ws import LiveWebSocket
from gemini_bot.orchestrator import ProductionOrchestrator
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.rank_report import public_rank_rows, rank_live_universe
from gemini_bot.rate_limits import Priority
from gemini_bot.reconciliation import assert_position_consistency, assert_stable_account_bracket, build_account_snapshot
from gemini_bot.runtime_files import atomic_write_json


class ReadOnlyAccountSocket(LiveWebSocket):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._uri = self._uri.replace("cancelOnDisconnect=true", "cancelOnDisconnect=false")
        self.received = Counter()
        self.requests = 0
        self.blocked_mutations = 0

    async def _deny(self, *args, **kwargs):
        self.blocked_mutations += 1
        raise RuntimeError("preflight prohibits every order mutation")

    place_maker = _deny
    place_ioc_entry = _deny
    exit_position = _deny
    cancel = _deny
    cancel_all = _deny

    async def _send(self, payload, priority):
        allowed = {"orders@account", "balances@account@1s", "positions@account@1s"}
        if (payload.get("method") != "SUBSCRIBE" or not isinstance(payload.get("params"), list)
                or not set(payload["params"]) <= allowed):
            await self._deny()
        self.requests += 1
        await super()._send(payload, priority)

    async def messages(self):
        async for frame in super().messages():
            self.received[str(frame.get("e", "response"))] += 1
            yield frame


class ObservedPublicSocket(PublicMarketSocket):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received = Counter()
        self.requests = 0

    async def _send(self, method, params, priority):
        if method != "SUBSCRIBE":
            raise RuntimeError("preflight allows only initial public subscriptions")
        self.requests += 1
        await super()._send(method, params, priority)

    async def frames(self):
        async for frame in super().frames():
            self.received[str(frame.get("e", "response"))] += 1
            yield frame


def sample(runtime):
    now = datetime.now(timezone.utc)
    ranked = rank_live_universe(
        runtime.universe, runtime.public.books, now=now,
        quantity=runtime.config.strategy.quote_quantity,
        offset_ticks=runtime.config.strategy.quote_offset_ticks,
        uptime=runtime._uptime, account=runtime.account.reducer,
        market_data_verified_at=runtime._market_data_verified_at(),
    )
    reasons = Counter(reason for item in ranked for reason in item.reasons)
    return {
        "at": now.isoformat(), "tracked": len(runtime.universe),
        "synchronized": sum(book.synchronized for book in runtime.public.books.values()),
        "fresh_books": sum(runtime._book_fresh(symbol) for symbol in runtime.public.books),
        "two_sided_books": sum(book.top() is not None for book in runtime.public.books.values()),
        "public_stream_fresh": runtime._public_stream_fresh(),
        "account_snapshots_fresh": runtime.account.snapshots_fresh,
        "cash_and_positions_matched": runtime.account.equity_classification_complete,
        "eligible_candidates": sum(item.eligible_now for item in ranked),
        "entry_time_valid": sum(runtime._entry_time_valid(item.symbol) for item in ranked),
        "rejection_reasons": dict(reasons),
        "public_frames": dict(runtime.public.received),
        "private_frames": dict(runtime.private.received),
        "public_requests": runtime.public.requests, "private_requests": runtime.private.requests,
        "depth_gaps": len(runtime.public.resync_required),
        "dropped_dirty_work": runtime.public.dirty.dropped,
        "dropped_log_records": runtime._logging.dropped_records,
        "top_candidates": public_rank_rows(ranked[:5], target_uptime=runtime.config.strategy.target_uptime),
        "book_readiness": [
            {"symbol": symbol, "bid_levels": len(book.bids), "ask_levels": len(book.asks),
             "two_sided": book.top() is not None, "synchronized": book.synchronized,
             "age_seconds": None if book.observed_at is None else round((now - book.observed_at).total_seconds(), 3)}
            for symbol, book in runtime.public.books.items()
        ],
    }


async def run(env: Path, directory: Path, seconds: int) -> int:
    if not 1 <= seconds <= 300:
        raise ValueError("preflight duration must be 1-300 seconds")
    config = load_preflight_config(env)
    runtime = ProductionOrchestrator(config, directory)
    runtime.private = ReadOnlyAccountSocket(config.api_key, config.api_secret, runtime.governor, runtime.ledger)
    runtime.public = ObservedPublicSocket(runtime.governor)
    report = {"read_only": True, "requested_seconds": seconds, "started_at": datetime.now(timezone.utc).isoformat(),
              "cancel_on_disconnect": False, "orders_submitted": 0, "cancels_submitted": 0,
              "activation_permit_consumed": False, "samples": [], "result": "running",
              "standard_sell_compatibility": "not_proven_by_read_only_observation"}
    started = time.monotonic()
    cpu_started = time.process_time()
    deadline = started + seconds
    phase = "startup"
    errors = []
    rest_calls = Counter()
    original_public = runtime.rest.public_get
    original_private = runtime.rest.private_read
    async def public_read(*args, **kwargs):
        rest_calls["public"] += 1
        return await original_public(*args, **kwargs)
    async def private_read(*args, **kwargs):
        rest_calls["private"] += 1
        return await original_private(*args, **kwargs)
    runtime.rest.public_get = public_read
    runtime.rest.private_read = private_read
    try:
        startup_blocked = False
        try:
            prepared = await asyncio.wait_for(runtime.prepare(timeout=30), timeout=min(150, seconds))
            baseline = prepared.account
        except TimeoutError as exc:
            if not runtime._tasks or any(task.done() for task in runtime._tasks):
                raise
            # Observe the failure without opening the trading gate or modifying
            # production readiness rules. Complete the requested timed diagnostic.
            startup_blocked = True
            report["startup_blocked"] = True
            report["startup_error"] = str(exc)
            errors.append("production startup readiness timed out")
            baseline = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
            assert_position_consistency(baseline, runtime.account.reducer.positions)
        report["startup_seconds"] = round(time.monotonic() - started, 3)
        report["account_flat_at_start"] = not baseline.positions and not baseline.active_orders
        phase = "observation"
        next_rest = time.monotonic() + 60
        while time.monotonic() < deadline:
            for task in runtime._tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("required stream ended")
            if runtime.gate.state is not EngineState.READY and not startup_blocked:
                raise RuntimeError("production preparation gate is no longer ready/paused")
            observation = sample(runtime)
            report["samples"].append(observation)
            # Public candidate prices are safe; never serialize raw account frames or amounts.
            print(json.dumps({"phase": phase, "elapsed_seconds": round(time.monotonic() - started),
                              "tracked": observation["tracked"], "fresh_books": observation["fresh_books"],
                              "eligible_candidates": observation["eligible_candidates"],
                              "account_fresh": observation["account_snapshots_fresh"],
                              "account_matched": observation["cash_and_positions_matched"]}), flush=True)
            atomic_write_json(directory / "preflight-report.json", report)
            while runtime.public.dirty.pop() is not None:
                pass
            if time.monotonic() >= next_rest and deadline - time.monotonic() > 20:
                snapshot = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
                assert_stable_account_bracket(baseline, snapshot)
                assert_position_consistency(snapshot, runtime.account.reducer.positions)
                report["rest_reconciliations"] = report.get("rest_reconciliations", 0) + 1
                next_rest = time.monotonic() + 60
            await asyncio.sleep(min(15, max(0, deadline - time.monotonic())))
        phase = "final_reconciliation"
        final = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
        assert_stable_account_bracket(baseline, final)
        assert_position_consistency(final, runtime.account.reducer.positions)
        report["account_flat_at_finish"] = not final.positions and not final.active_orders
        report["final"] = sample(runtime)
        if not all(item["account_snapshots_fresh"] and item["cash_and_positions_matched"] for item in report["samples"]):
            errors.append("account freshness or classification failed during observation")
        if not report["final"]["eligible_candidates"]:
            errors.append("no candidates passed current local reward and freshness gates")
        report["result"] = "read_only_checks_passed" if not errors else "blocked"
    except Exception as exc:
        report["result"] = "blocked"
        report["failed_phase"] = phase
        report["error_type"] = type(exc).__name__
        # Only our known static runtime errors are copied; no HTTP payloads or account values.
        if type(exc) in {RuntimeError, TimeoutError}:
            report["error"] = str(exc)
        if runtime.universe:
            report["diagnostic"] = sample(runtime)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["cpu_percent_one_core"] = round(100 * (time.process_time() - cpu_started) / max(.001, time.monotonic() - started), 3)
        report["rest_requests"] = dict(rest_calls)
        report["blocked_mutation_attempts"] = runtime.private.blocked_mutations
        report["issues"] = errors
        await runtime.close()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["connections_closed"] = runtime.private._connection is None and runtime.public._connection is None
        atomic_write_json(directory / "preflight-report.json", report)
        atomic_write_json(directory / "status.json", {"phase": "preflight_finished", "activated": False,
                          "result": report["result"], "updated_at": report["finished_at"]})
        print(json.dumps({key: value for key, value in report.items() if key not in {"samples", "final", "diagnostic"}}), flush=True)
    return 0 if report["result"] == "read_only_checks_passed" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.env, args.directory, args.seconds)))
