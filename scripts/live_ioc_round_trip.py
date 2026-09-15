"""One bounded live LIMIT+IOC round trip for entry/exit commissioning.

This is not the reward-quoting runner. It requires an explicit dollar
authorization on every invocation, refuses pre-existing exposure, records
every intent before send, and uses authenticated position truth for exits.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gemini_bot.account_state import AccountReducer
from gemini_bot.config import load_dotenv
from gemini_bot.domain import Outcome, QuoteLeg, Side, canonical_symbol
from gemini_bot.execution import IntentLedger, IntentState
from gemini_bot.exit_pricing import exact_crossing_exit
from gemini_bot.live_ws import LiveWebSocket
from gemini_bot.public_ws import PublicMarketSocket
from gemini_bot.rate_limits import AdmissionGovernor
from gemini_bot.rest_client import GeminiReadRestClient
from gemini_bot.storage import SQLiteJournal, WindowsOwnerLock


MAX_COMMISSIONING_AUTHORIZATION = Decimal("10")
QUANTITY = Decimal("10")


def _signed_rest_position(rows: list[dict[str, object]], symbol: str) -> Decimal:
    total = Decimal("0")
    for row in rows:
        if canonical_symbol(str(row.get("symbol", ""))) != symbol:
            continue
        quantity = Decimal(str(row.get("totalQuantity", "0")))
        outcome = str(row.get("outcome", "")).upper()
        if outcome == "YES":
            total += quantity
        elif outcome == "NO":
            total -= quantity
        else:
            raise RuntimeError("REST position has an unknown outcome")
    return total


def _contract(event: dict[str, object], symbol: str) -> dict[str, object]:
    root = event.get("event", event)
    if not isinstance(root, dict) or root.get("status") != "active":
        raise RuntimeError("selected event is not active")
    for item in root.get("contracts", []):
        if isinstance(item, dict) and canonical_symbol(str(item.get("instrumentSymbol", ""))) == symbol:
            if item.get("status") != "active" or item.get("marketState") != "open":
                raise RuntimeError("selected contract is not open")
            return item
    raise RuntimeError("selected symbol is not part of the selected event")


async def _pump_public(socket: PublicMarketSocket) -> None:
    async for frame in socket.frames():
        socket.apply_frame(frame)


async def _pump_private(socket: LiveWebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    async for frame in socket.messages():
        await queue.put(frame)


async def _wait_for_book(socket: PublicMarketSocket, symbol: str, timeout: float = 12) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        top = socket.books.get(symbol).top() if symbol in socket.books else None
        if top is not None:
            age = datetime.now(timezone.utc) - top.observed_at
            if age.total_seconds() <= 3:
                return
        await asyncio.sleep(0.05)
    raise TimeoutError("fresh public book was not available")


async def _initial_account_snapshot(
    queue: asyncio.Queue[dict[str, object]], reducer: AccountReducer,
) -> Decimal:
    subscribed = False
    available_usd: Decimal | None = None
    positions_seen = False
    deadline = asyncio.get_running_loop().time() + 12
    while asyncio.get_running_loop().time() < deadline:
        frame = await asyncio.wait_for(queue.get(), timeout=3)
        if "status" in frame and frame.get("status") != 200:
            raise RuntimeError("authenticated subscription was rejected")
        if frame.get("status") == 200:
            subscribed = True
        if frame.get("e") == "positionReport":
            reducer.apply_position_report(frame, complete_snapshot=True)
            positions_seen = True
        if frame.get("e") == "balanceUpdate":
            for row in frame.get("B", []):
                if isinstance(row, dict) and str(row.get("a", "")).upper() == "USD":
                    available_usd = Decimal(str(row.get("f", "0")))
        if subscribed and positions_seen and available_usd is not None:
            return available_usd
    raise TimeoutError("complete account startup snapshot was not observed")


async def _wait_order_terminal(
    queue: asyncio.Queue[dict[str, object]],
    reducer: AccountReducer,
    ledger: IntentLedger,
    client_order_id: str,
    timeout: float = 20,
):
    acknowledged = False
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        frame = await asyncio.wait_for(queue.get(), timeout=3)
        if "status" in frame and frame.get("status") != 200:
            error = frame.get("error")
            code = error.get("code") if isinstance(error, dict) else "unknown"
            message = error.get("msg") if isinstance(error, dict) else "unavailable"
            safe_message = "".join(
                char for char in str(message)[:160]
                if char.isalnum() or char in " ._:-"
            )
            ledger.mark_terminal(client_order_id)
            raise RuntimeError(
                f"order request rejected status={frame.get('status')} "
                f"code={code} reason={safe_message or 'unavailable'}"
            )
        if frame.get("e") == "positionReport":
            reducer.apply_position_report(frame, complete_snapshot=True)
            continue
        if frame.get("e") != "orderUpdate":
            continue
        transition = reducer.apply_order_update(frame)
        order = reducer.orders[transition.order_id]
        if order.client_order_id != client_order_id:
            if ledger.owns(order.client_order_id):
                # IOC can emit a final late lifecycle event after the first
                # terminal observation. It remains owned and must not be
                # mistaken for an unrelated account order.
                continue
            raise RuntimeError("unexpected unowned account order appeared during exclusive canary")
        if not acknowledged:
            ledger.acknowledge(client_order_id, order.order_id)
            acknowledged = True
        if transition.became_terminal:
            ledger.mark_terminal(client_order_id)
            return order
    raise TimeoutError("order did not reach an authoritative terminal event")


async def _run(args: argparse.Namespace) -> dict[str, object]:
    raise RuntimeError("Legacy IOC commissioning is retired; use the permit-gated production maker canary")
    authorization = Decimal(args.authorization_usd)
    if authorization <= 0 or authorization > MAX_COMMISSIONING_AUTHORIZATION:
        raise ValueError("authorization must be positive and no greater than $10")
    symbol = canonical_symbol(args.symbol)
    outcome = Outcome(args.outcome.upper())
    env = load_dotenv(args.env)
    api_key = env.get("GEMINI_API_KEY", "")
    api_secret = env.get("GEMINI_API_SECRET", "")
    if not api_key.startswith("account-") or not api_secret:
        raise RuntimeError("valid account-scoped credentials are required")

    governor = AdmissionGovernor()
    rest = GeminiReadRestClient(governor, api_key=api_key, api_secret=api_secret)
    terms = await rest.terms_status()
    if not terms.get("hasAcceptedLatest"):
        raise RuntimeError("latest prediction-market terms are not accepted")
    if await rest.active_orders():
        raise RuntimeError("pre-existing active orders block the exclusive canary")
    existing_positions = await rest.positions()
    if existing_positions:
        raise RuntimeError("pre-existing positions block the exclusive canary")
    rewards = await rest.reward_events()
    if args.event_ticker.upper() not in {
        str(row.get("event_ticker", "")).upper() for row in rewards
    }:
        raise RuntimeError("selected event is not in the current reward catalog")
    contract = _contract(await rest.event(args.event_ticker), symbol)
    if Decimal(str(contract["quantityMinimum"])) > QUANTITY:
        raise RuntimeError("contract minimum exceeds the authorized test quantity")
    if QUANTITY % Decimal(str(contract["quantityIncrement"])) != 0:
        raise RuntimeError("test quantity is off the contract increment")

    public = PublicMarketSocket(governor)
    journal = SQLiteJournal(args.journal)
    ledger = IntentLedger(journal)
    private = LiveWebSocket(api_key, api_secret, governor, ledger)
    public_task = private_task = None
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=512)
    reducer = AccountReducer()
    try:
        await public.connect()
        await public.subscribe((symbol,))
        public_task = asyncio.create_task(_pump_public(public))
        await _wait_for_book(public, symbol)
        top = public.books[symbol].top()
        if top is None:
            raise RuntimeError("fresh top of book is unavailable")
        if outcome is Outcome.YES:
            entry_price = top.ask
            entry_depth = top.ask_size
        else:
            # Public event-contract depth is in YES space. The best NO ask is
            # the complement of the best YES bid, with the same displayed size.
            entry_price = Decimal("1") - top.bid
            entry_depth = top.bid_size
        if entry_depth < QUANTITY:
            raise RuntimeError("selected outcome ask lacks ten contracts of visible depth")
        entry_cost = entry_price * QUANTITY
        if entry_cost > authorization:
            raise RuntimeError("fresh entry cost exceeds the explicit authorization")

        await asyncio.sleep(1.05)
        await private.connect()
        private_task = asyncio.create_task(_pump_private(private, queue))
        await private.subscribe_account()
        available = await _initial_account_snapshot(queue, reducer)
        if reducer.positions or available < entry_cost:
            raise RuntimeError("account snapshot does not permit the bounded entry")
        if await rest.active_orders() or await rest.positions():
            raise RuntimeError("final REST bracket changed before entry")

        entry_leg = QuoteLeg(symbol, outcome, Side.BUY, entry_price, QUANTITY)
        entry_client_id = await private.place_ioc_entry(entry_leg)
        entry_order = await _wait_order_terminal(queue, reducer, ledger, entry_client_id)
        entry_fill = entry_order.cumulative_filled
        journal.audit("commissioning_entry_terminal", {
            "symbol": symbol,
            "outcome": outcome.value,
            "requested_quantity": str(QUANTITY),
            "filled_quantity": str(entry_fill),
            "limit_price": str(entry_price),
            "status": entry_order.status,
        })

        await asyncio.sleep(1.05)
        signed = _signed_rest_position(await rest.positions(), symbol)
        expected_signed = entry_fill if outcome is Outcome.YES else -entry_fill
        if signed != expected_signed:
            raise RuntimeError("entry fill and authenticated position do not reconcile")

        exit_filled = Decimal("0")
        attempts = 0
        while signed != 0 and attempts < 3:
            attempts += 1
            await _wait_for_book(public, symbol)
            exit_leg = exact_crossing_exit(public.books[symbol], signed)
            if exit_leg is None:
                raise RuntimeError("full haircut-adjusted depth is unavailable for exact exit")
            exit_client_id = await private.exit_position(exit_leg, signed_position=signed)
            exit_order = await _wait_order_terminal(queue, reducer, ledger, exit_client_id)
            exit_filled += exit_order.cumulative_filled
            await asyncio.sleep(1.05)
            signed = _signed_rest_position(await rest.positions(), symbol)

        final_orders = await rest.active_orders()
        if signed != 0 or final_orders:
            raise RuntimeError("canary did not finish flat with zero active orders")
        journal.audit("commissioning_round_trip_flat", {
            "symbol": symbol,
            "entry_filled": str(entry_fill),
            "exit_filled": str(exit_filled),
            "exit_attempts": attempts,
        })
        return {
            "symbol": symbol,
            "outcome": outcome.value,
            "entry_limit": str(entry_price),
            "entry_filled": str(entry_fill),
            "exit_filled": str(exit_filled),
            "exit_attempts": attempts,
            "final_position": "0",
            "active_orders": 0,
        }
    finally:
        for task in (private_task, public_task):
            if task is not None:
                task.cancel()
        await private.close()
        await public.close()
        journal.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--journal", type=Path, default=ROOT / "data" / "commissioning" / "canary.sqlite")
    parser.add_argument("--event-ticker", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--outcome", choices=("YES", "NO"), default="YES")
    parser.add_argument("--authorization-usd", required=True)
    args = parser.parse_args()
    lock_path = ROOT / "data" / "commissioning" / "mutation-owner.lock"
    with WindowsOwnerLock(lock_path):
        result = asyncio.run(_run(args))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
