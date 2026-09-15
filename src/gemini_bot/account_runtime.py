from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any
import time
import json

from .account_state import AccountReducer
from .execution import IntentLedger, IntentState
from .storage import SQLiteJournal


def _event_time(frame: dict[str, Any]) -> datetime:
    raw = int(frame.get("T", frame.get("E", 0)))
    if raw <= 0:
        return datetime.now(timezone.utc)
    seconds = raw / (1_000_000_000 if raw > 10**15 else 1_000)
    return datetime.fromtimestamp(seconds, timezone.utc)


@dataclass(frozen=True, slots=True)
class AccountReadiness:
    subscription_acknowledged: bool
    balances_seen: bool
    positions_seen: bool

    @property
    def complete(self) -> bool:
        return self.subscription_acknowledged and self.balances_seen and self.positions_seen


class AccountRuntime:
    """Single reducer for private WebSocket state and durable execution facts."""

    def __init__(self, ledger: IntentLedger, journal: SQLiteJournal) -> None:
        self.ledger = ledger
        self.journal = journal
        self.reducer = AccountReducer()
        self.subscription_acknowledged = False
        self.balances_seen = False
        self.positions_seen = False
        self.complete_position_snapshots = True
        self.available_usd: Decimal | None = None
        self.confirmed_usd: Decimal | None = None
        self.events: dict[str, asyncio.Event] = {}
        self.safety_symbols: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.unowned_order_seen = False
        self._known_account_activity_at = 0.0
        self._unmatched_balance_change_at: float | None = None
        self.last_balance_at: float | None = None
        self.last_position_at: float | None = None
        saved_cash = journal.get_runtime_state("expected_confirmed_usd")
        self.expected_confirmed_usd = None if saved_cash is None else Decimal(saved_cash)
        self.inventory_age_seconds = 300
        self.exit_before_expiry_seconds = 300
        self.expiry: dict[str, datetime] = {}
        self.hard_exit_cutoff: dict[str, datetime] = {}
        self.blocked_entries: set[str] = set()
        self.changed = asyncio.Event()
        saved_positions = journal.get_runtime_state("expected_positions")
        self.expected_positions = None if saved_positions is None else {
            key: Decimal(value) for key, value in json.loads(saved_positions).items()
        }

    @property
    def readiness(self) -> AccountReadiness:
        return AccountReadiness(
            self.subscription_acknowledged,
            self.balances_seen,
            self.positions_seen,
        )

    @property
    def equity_classification_complete(self) -> bool:
        return (
            self.confirmed_usd is not None
            and self.expected_confirmed_usd == self.confirmed_usd
            and self.expected_positions == self.reducer.positions
            and not self.unowned_order_seen
        )

    @property
    def snapshots_fresh(self) -> bool:
        now = time.monotonic()
        return all(value is not None and 0 <= now - value <= 3 for value in (self.last_balance_at, self.last_position_at))

    def _known_account_activity(self) -> None:
        # Timing proximity is never evidence that a cash movement is a fill.
        self._known_account_activity_at = time.monotonic()

    def _persist_expected_cash(self) -> None:
        if self.expected_confirmed_usd is not None:
            self.journal.set_runtime_state(
                "expected_confirmed_usd", str(self.expected_confirmed_usd),
                updated_at=datetime.now(timezone.utc),
            )

    def switch_to_deltas(self) -> None:
        self.complete_position_snapshots = False

    def _persist_expected_positions(self) -> None:
        self.journal.set_runtime_state(
            "expected_positions", json.dumps({k: str(v) for k, v in self.expected_positions.items()}),
            updated_at=datetime.now(timezone.utc),
        )

    def _signal(self, client_order_id: str) -> None:
        self.events.setdefault(client_order_id, asyncio.Event()).set()

    def prune_terminal(self, keep: int = 512) -> None:
        terminal = [key for key, value in self.reducer.orders.items() if not value.working]
        for key in terminal[:-keep]:
            order = self.reducer.orders.pop(key)
            self.events.pop(order.client_order_id, None)
        self.ledger.prune_terminal(keep)

    async def wait_for_order_event(self, client_order_id: str, timeout: float = 10) -> None:
        event = self.events.setdefault(client_order_id, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=timeout)
        event.clear()

    def apply(self, frame: dict[str, Any]) -> None:
        if "status" in frame:
            if frame.get("status") != 200:
                raise RuntimeError("private WebSocket request was rejected")
            self.subscription_acknowledged = True
            return
        event_type = frame.get("e")
        if event_type in {"balanceUpdate", "positionReport", "orderUpdate"}:
            self.changed.set()
        if event_type == "balanceUpdate":
            self.last_balance_at = time.monotonic()
            self.balances_seen = True
            for row in frame.get("B", []):
                if isinstance(row, dict) and str(row.get("a", "")).upper() == "USD":
                    available = Decimal(str(row.get("f", "0")))
                    confirmed = Decimal(str(row.get("c", "0")))
                    if self.expected_confirmed_usd is None:
                        self.expected_confirmed_usd = confirmed
                        self._persist_expected_cash()
                    self.available_usd = available
                    self.confirmed_usd = confirmed
            return
        if event_type == "positionReport":
            self.last_position_at = time.monotonic()
            if any(
                isinstance(amount, dict) and amount.get("t") == "settlement_payout"
                for row in frame.get("P", []) if isinstance(row, dict)
                for amount in row.get("a", [])
            ):
                self._known_account_activity()
            self.reducer.apply_position_report(
                frame,
                complete_snapshot=self.complete_position_snapshots,
            )
            if self.expected_positions is None:
                self.expected_positions = dict(self.reducer.positions)
                self._persist_expected_positions()
            self.positions_seen = True
            return
        if event_type != "orderUpdate":
            return

        transition = self.reducer.apply_order_update(frame)
        order = self.reducer.orders[transition.order_id]
        intent = self.ledger.get(order.client_order_id)
        if intent is None:
            if not self.journal.knows_client_order_id(order.client_order_id):
                self.unowned_order_seen = True
                raise RuntimeError("unowned account order event")
        elif intent.state is IntentState.SENT_UNKNOWN:
            self.ledger.acknowledge(order.client_order_id, order.order_id)

        if transition.newly_filled > 0:
            self._known_account_activity()
            trade_id = str(frame.get("t", ""))
            if trade_id:
                recorded = self.journal.record_fill(
                    trade_id=trade_id,
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side.value,
                    outcome=order.outcome.value,
                    quantity=transition.newly_filled,
                    price=Decimal(str(frame.get("L", frame.get("p", "0")))),
                    fee=Decimal(str(frame.get("n", "0"))),
                    event_at=_event_time(frame),
                )
                if recorded and self.expected_confirmed_usd is not None:
                    price = Decimal(str(frame.get("L", frame.get("p", "0"))))
                    if not Decimal("0") < price < Decimal("1"):
                        raise RuntimeError("fill cash cannot be classified without execution price")
                    cash = transition.newly_filled * price
                    self.expected_confirmed_usd += (
                        -cash if order.side.value == "BUY" else cash
                    ) - Decimal(str(frame.get("n", "0")))
                    self._persist_expected_cash()
                if recorded and self.expected_positions is not None:
                    change = transition.newly_filled * (1 if order.outcome.value == "YES" else -1) * (1 if order.side.value == "BUY" else -1)
                    self.expected_positions[order.symbol] = self.expected_positions.get(order.symbol, Decimal("0")) + change
                    if self.expected_positions[order.symbol] == 0:
                        self.expected_positions.pop(order.symbol)
                    self._persist_expected_positions()
            else:
                raise RuntimeError("fill without durable trade identity requires reconciliation")
            if transition.cancel_other_entry_orders:
                self.blocked_entries.add(order.symbol)
                opened = min(_event_time(frame), datetime.now(timezone.utc))
                deadline = opened + timedelta(seconds=self.inventory_age_seconds)
                if order.symbol in self.expiry:
                    deadline = min(deadline, self.expiry[order.symbol] - timedelta(seconds=self.exit_before_expiry_seconds))
                if order.symbol in self.hard_exit_cutoff:
                    deadline = min(deadline, self.hard_exit_cutoff[order.symbol])
                # Persist before waiting for cancellation or a delayed position report.
                self.journal.persist_deadline(
                    order.symbol, order.outcome.value, opened, deadline, order.cumulative_filled,
                )
                try:
                    self.safety_symbols.put_nowait(order.symbol)
                except asyncio.QueueFull as exc:
                    raise RuntimeError("safety-action queue overflow") from exc

        if transition.became_terminal:
            if intent is None:
                self.journal.resolve_intent_terminal(order.client_order_id, order.order_id)
            else:
                current = self.ledger.get(order.client_order_id)
                if current is not None and current.state in {
                    IntentState.SENT_UNKNOWN,
                    IntentState.ACKNOWLEDGED,
                }:
                    self.ledger.mark_terminal(order.client_order_id)
        self.journal.audit("order_transition", {
            "client_order_id": order.client_order_id, "order_id": order.order_id,
            "symbol": order.symbol, "status": order.status,
        })
        self._signal(order.client_order_id)
