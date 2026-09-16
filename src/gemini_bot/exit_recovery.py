"""Non-authoritative scheduling state; durable fills/deadlines remain authoritative."""
from dataclasses import dataclass
from decimal import Decimal


class ExitDeferred(RuntimeError):
    """Known no-send/terminal boundary; never an unknown mutation outcome."""


@dataclass(slots=True)
class ExitWait:
    reason: str
    retry_at: float
    rounds: int
    residual: Decimal | None = None
    proved_at: float = 0.0
    fill_version: int = -1
    generation: tuple[int | tuple[int, int], int] = (-1, -1)
