"""Explicit operator-only September 16 breadth migration; never called by startup."""
from datetime import datetime, timezone
from decimal import Decimal

from .risk_windows import RiskWindowPolicy

MIGRATION = "operator_capacity_30_to_40_20260916"


def apply_approved_migration(journal, *, now: datetime) -> bool:
    """Caller must hold owner lock and fresh order-free/flat account proof.

    Atomically change only the complete current window's 30-slot ceiling.
    Replays are no-ops; no runtime path invokes this operator action.
    """
    start, end = RiskWindowPolicy(Decimal(30), 40).bounds(now)
    with journal.transaction():
        if journal.get_runtime_state(MIGRATION) is not None:
            return False
        row = journal.load_realized_risk_window(start)
        if (row is None or row['window_end'] != end or not row['classification_complete']
                or row['pending_order_cap'] != 30
                or not row['loss_high_water'].is_finite()
                or not Decimal(0) <= row['loss_high_water'] < Decimal(30)
                or not row['realized_pnl'].is_finite()
                or row['realized_pnl'] <= Decimal(-30)):
            raise RuntimeError("capacity migration requires the exact complete non-HARD 30-slot window")
        if (journal.connection.execute("SELECT 1 FROM order_intent WHERE state != 'terminal' LIMIT 1").fetchone()
                or journal.load_position_quantities()
                or journal.connection.execute("SELECT 1 FROM account_order LIMIT 1").fetchone()):
            raise RuntimeError("capacity migration requires terminal flat order-free journal")
        journal.connection.execute(
            "UPDATE realized_risk_window_state SET pending_order_cap=40 WHERE window_start=?",
            (start.isoformat(),))
        journal.set_runtime_state(MIGRATION, start.isoformat(), updated_at=now)
        journal.audit(MIGRATION, {"from":30, "to":40, "window_start":start.isoformat(),
                                  "loss_history_unchanged":True})
        return True


async def apply_with_fresh_proof(runtime) -> bool:
    from .reconciliation import build_account_snapshot, assert_stable_account_bracket
    if (not runtime.read_only or runtime._activation_consumed or runtime.lock._file is None
            or runtime.config.risk.max_pending_orders != 40
            or runtime.config.risk.risk_window_loss_usd != Decimal(30)):
        raise RuntimeError("capacity migration requires owned nonactivated approved read-only runtime")
    account = runtime.account
    version = account.fill_version
    before = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
    after = build_account_snapshot(await runtime.rest.positions(), await runtime.rest.active_orders())
    assert_stable_account_bracket(before, after)
    if (before.positions or after.positions or before.active_orders or after.active_orders
            or account.reducer.positions or account.reducer.pending_order_count()
            or not account.readiness.complete or not account.snapshots_fresh
            or not account.equity_classification_complete or version != account.fill_version
            or any(task.done() for task in runtime._tasks)):
        raise RuntimeError("capacity migration needs fresh stable flat classified REST/WS proof")
    return apply_approved_migration(runtime.journal, now=datetime.now(timezone.utc))
