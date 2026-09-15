from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .operational_logging import ErrorClass


class RecoveryAction(str, Enum):
    RETRY_READ = "retry_read"
    RECONNECT_AND_RECONCILE = "reconnect_and_reconcile"
    RECONCILE_MUTATION = "reconcile_mutation"
    RESYNC_BOOK = "resync_book"
    HONOR_COOLDOWN = "honor_cooldown"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class FailureContext:
    error_class: ErrorClass
    operation: str
    mutation_attempted: bool = False
    http_status: int | None = None
    websocket_code: int | None = None
    consecutive_failures: int = 1
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    delay_seconds: float = 0.0


def decide_recovery(failure: FailureContext) -> RecoveryDecision:
    """Map a categorized failure to a bounded, mutation-safe response."""
    if failure.consecutive_failures < 1:
        raise ValueError("consecutive_failures must be positive")
    if failure.http_status == 418:
        return RecoveryDecision(RecoveryAction.HALT, "venue ban response")
    if failure.http_status in {401, 403}:
        return RecoveryDecision(RecoveryAction.HALT, "authentication or permission failure")
    if failure.mutation_attempted:
        return RecoveryDecision(
            RecoveryAction.RECONCILE_MUTATION,
            "mutation outcome may be ambiguous; never retry blindly",
        )
    if failure.error_class is ErrorClass.INVARIANT:
        return RecoveryDecision(RecoveryAction.HALT, "safety invariant violation")
    if failure.error_class is ErrorClass.STORAGE:
        return RecoveryDecision(RecoveryAction.HALT, "durable state unavailable")
    if failure.error_class is ErrorClass.PROTOCOL and failure.operation == "depth_sequence":
        return RecoveryDecision(RecoveryAction.RESYNC_BOOK, "market-book sequence gap")
    if failure.http_status == 429 or failure.websocket_code == -1003 or failure.error_class is ErrorClass.RATE_LIMIT:
        delay = max(60.0, failure.retry_after_seconds or 0.0)
        return RecoveryDecision(RecoveryAction.HONOR_COOLDOWN, "venue rate limit", delay)
    if failure.consecutive_failures >= 5:
        return RecoveryDecision(RecoveryAction.HALT, "bounded recovery attempts exhausted")
    if failure.error_class is ErrorClass.TRANSPORT:
        delay = min(60.0, 2.0 ** failure.consecutive_failures)
        return RecoveryDecision(RecoveryAction.RECONNECT_AND_RECONCILE, "transport continuity lost", delay)
    if failure.http_status is not None and 500 <= failure.http_status <= 599:
        delay = min(60.0, 2.0 ** failure.consecutive_failures)
        return RecoveryDecision(RecoveryAction.RETRY_READ, "transient read service failure", delay)
    if failure.error_class is ErrorClass.RECONCILIATION:
        return RecoveryDecision(RecoveryAction.RECONNECT_AND_RECONCILE, "account state disagreement", 2.0)
    return RecoveryDecision(RecoveryAction.HALT, "unclassified failure is not safe to retry")

