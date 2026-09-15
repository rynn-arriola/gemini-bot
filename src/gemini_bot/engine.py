from __future__ import annotations

from dataclasses import dataclass

from .domain import EngineState


class SafetyViolation(RuntimeError):
    pass


@dataclass(slots=True)
class ReconciliationState:
    terms_accepted: bool = False
    balances_known: bool = False
    positions_known: bool = False
    open_orders_known: bool = False
    account_stream_live: bool = False
    public_stream_live: bool = False

    @property
    def complete(self) -> bool:
        return all(
            (
                self.terms_accepted,
                self.balances_known,
                self.positions_known,
                self.open_orders_known,
                self.account_stream_live,
                self.public_stream_live,
            )
        )


class LiveEngineGate:
    """Production activation gate. Unknown state never becomes permission."""

    def __init__(self) -> None:
        self.state = EngineState.PAUSED
        self.reconciliation = ReconciliationState()

    def begin_reconciliation(self) -> None:
        if self.state not in (EngineState.PAUSED, EngineState.HALTED):
            raise SafetyViolation("reconciliation may only begin from paused/halted")
        self.state = EngineState.RECONCILING

    def mark_ready(self) -> None:
        if self.state is not EngineState.RECONCILING or not self.reconciliation.complete:
            raise SafetyViolation("cannot become ready with unknown account or stream state")
        self.state = EngineState.READY

    def activate(self, operator_activation: bool) -> None:
        if not operator_activation:
            raise SafetyViolation("explicit operator activation is required")
        if self.state is not EngineState.READY or not self.reconciliation.complete:
            raise SafetyViolation("engine is not reconciled and ready")
        self.state = EngineState.QUOTING

    def halt(self) -> None:
        self.state = EngineState.HALTED

    @property
    def may_submit(self) -> bool:
        return self.state is EngineState.QUOTING and self.reconciliation.complete

