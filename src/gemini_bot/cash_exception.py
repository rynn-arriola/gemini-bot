"""Explicit, single-incident bookkeeping exception; never called by trading.

Amounts stay in the private journal. This is owner acceptance of unexplained
cash, NOT proof of a venue rounding rule or authenticated non-trading activity.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from .storage import SQLiteJournal


INCIDENT = "owner-approved-unexplained-cash-20260916"
EXPIRES = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
_KEY = "one_time_cash_exception_20260916"
_APPLIED = _KEY + "_applied"


def _check_time(now: datetime) -> None:
    if now.tzinfo is None or not datetime(2026, 9, 16, 4, tzinfo=timezone.utc) <= now < EXPIRES:
        raise RuntimeError("cash exception is outside its incident approval date")


def ledger_digest(journal: SQLiteJournal) -> str:
    """Bind economic history, not telemetry or publication timestamps."""
    tables = ("execution_fill", "position_deadline", "risk_window_state",
              "realized_risk_window_state", "authenticated_cash_adjustment")
    state = {table: sorted(journal.connection.execute(f"SELECT * FROM {table}").fetchall())
             for table in tables}
    state["order_intent"] = journal.connection.execute(
        "SELECT client_order_id,symbol,outcome,side,purpose,price,quantity,state,server_order_id "
        "FROM order_intent ORDER BY client_order_id"
    ).fetchall()
    state["runtime_state"] = journal.connection.execute(
        "SELECT key,value FROM runtime_state WHERE key IN "
        "('credential_identity','expected_confirmed_usd','expected_positions',"
        "'cash_reconciliation_watermark') ORDER BY key"
    ).fetchall()
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cash(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
        # Confirmed ledger cash is signed, just like AccountRuntime's existing
        # cash model. This adjustment does not grant spendable funds or credit.
        if result.is_finite():
            return result
    except (ArithmeticError, ValueError):
        pass
    raise RuntimeError("cash exception requires valid finite cash evidence")


def confirmed_usd(rows: list[dict]) -> Decimal:
    matches = [row for row in rows if str(row.get("currency", "")).upper() == "USD"]
    if len(matches) != 1:
        raise RuntimeError("cash exception requires one confirmed USD ledger row")
    return _cash(matches[0].get("amount"))


def record_approval(journal: SQLiteJournal, *, identity: str, observed_cash: Decimal,
                    positions: dict[str, Decimal], now: datetime) -> None:
    _check_time(now)
    with journal.transaction():
        if journal.get_runtime_state(_KEY) is not None or journal.get_runtime_state(_APPLIED) is not None:
            raise RuntimeError("this incident approval already exists; it cannot be rebound")
        if not identity or journal.get_runtime_state("credential_identity") != identity:
            raise RuntimeError("cash exception account identity does not match")
        if journal.connection.execute("SELECT 1 FROM order_intent WHERE state != 'terminal' LIMIT 1").fetchone():
            raise RuntimeError("cash exception requires terminal durable intents")
        expected = _cash(journal.get_runtime_state("expected_confirmed_usd"))
        actual = _cash(observed_cash)
        if not Decimal("0") < abs(actual - expected) < Decimal("0.01"):
            raise RuntimeError("cash exception is not the approved nonzero sub-cent discrepancy")
        saved = journal.get_runtime_state("expected_positions")
        if saved is None or {k: Decimal(v) for k, v in json.loads(saved).items()} != positions:
            raise RuntimeError("cash exception requires exact expected positions")
        record = dict(incident=INCIDENT, cause="unexplained_owner_accepted", identity=identity,
                      expected=str(expected), observed=str(actual), delta=str(actual - expected),
                      ledger_digest=ledger_digest(journal), approved_at=now.isoformat(),
                      expires_at=EXPIRES.isoformat())
        journal.set_runtime_state(_KEY, json.dumps(record, sort_keys=True), updated_at=now)
        journal.audit("one_time_unexplained_cash_approval_recorded", {"incident": INCIDENT})


def apply_approval(journal: SQLiteJournal, *, identity: str, observed_cash: Decimal,
                   now: datetime) -> None:
    """Atomic local commit. Caller must own lock and prove fresh order-free account."""
    _check_time(now)
    with journal.transaction():
        raw = journal.get_runtime_state(_KEY)
        if raw is None or journal.get_runtime_state(_APPLIED) is not None:
            raise RuntimeError("cash exception is missing or already consumed")
        record = json.loads(raw)
        if (record.get("incident") != INCIDENT or record.get("cause") != "unexplained_owner_accepted"
                or record.get("expires_at") != EXPIRES.isoformat()
                or record.get("identity") != identity
                or journal.get_runtime_state("credential_identity") != identity
                or record.get("ledger_digest") != ledger_digest(journal)):
            raise RuntimeError("cash exception account or ledger evidence changed")
        expected, actual = _cash(record.get("expected")), _cash(record.get("observed"))
        if (actual != _cash(observed_cash)
                or expected != _cash(journal.get_runtime_state("expected_confirmed_usd"))
                or not Decimal("0") < abs(actual - expected) < Decimal("0.01")
                or str(actual - expected) != record.get("delta")):
            raise RuntimeError("cash exception no longer matches its exact approved discrepancy")
        if journal.connection.execute("SELECT 1 FROM order_intent WHERE state != 'terminal' LIMIT 1").fetchone():
            raise RuntimeError("cash exception requires terminal durable intents")
        # Preserve watermark: the unexplained delta is NOT authenticated history
        # and cannot silently mark earlier deposits/rewards as already reconciled.
        journal.set_runtime_state("expected_confirmed_usd", str(actual), updated_at=now)
        journal.set_runtime_state(_APPLIED, INCIDENT, updated_at=now)
        journal.audit("one_time_unexplained_cash_exception_applied", {"incident": INCIDENT})


async def apply_with_fresh_proof(runtime) -> None:
    """No activation, mutation, rounding, fill import or loss/deadline rewrite."""
    from .reconciliation import build_account_snapshot, assert_stable_account_bracket

    if not runtime.read_only or runtime._activation_consumed or runtime.lock._file is None:
        raise RuntimeError("cash exception requires an owned nonactivated read-only runtime")
    account = runtime.account
    version = account.fill_version
    before = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
    cash_before = confirmed_usd(await runtime.rest.account_balances())
    after = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
    cash_after = confirmed_usd(await runtime.rest.account_balances())
    assert_stable_account_bracket(before, after)
    if (before.active_orders or after.active_orders or account.reducer.pending_order_count()
            or not account.readiness.complete or not account.snapshots_fresh
            or account.unowned_order_seen or account.missing_execution_orders
            or version != account.fill_version
            or after.signed_positions != account.reducer.positions
            or account.expected_positions != account.reducer.positions
            or cash_before != cash_after or cash_after != account.confirmed_usd):
        raise RuntimeError("cash exception requires fresh stable order-free REST/WS agreement")
    for task in runtime._tasks:
        if task.done():
            raise RuntimeError("cash exception account observation task stopped")
    apply_approval(runtime.journal, identity=hashlib.sha256(runtime.config.api_key.encode()).hexdigest(),
                   observed_cash=cash_after, now=datetime.now(timezone.utc))
    account.expected_confirmed_usd = cash_after
