"""Bounded request correlation; only known read failures permit recovery."""
from .rest_client import ApiError


READ_METHODS = frozenset({"conninfo", "SUBSCRIBE", "UNSUBSCRIBE", "LIST_SUBSCRIPTIONS", "ping", "time"})


class WebSocketRequestRejected(RuntimeError):
    """Safe diagnostic metadata only; never authority to replay a mutation."""

    def __init__(self, frame: dict, method: str | None) -> None:
        allowed = READ_METHODS | {"order.place", "order.cancel", "order.cancel_all", "order.cancel_session", "depth"}
        self.method = method if method in allowed else "unknown"
        status = frame.get("status")
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        error = frame.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        self.code = code if type(code) is int and -100000 <= code <= 100000 else None
        # Never retain free-form venue text/data or request payloads. These can
        # contain credentials or sensitive account details, even in an error.
        super().__init__(
            "WebSocket request rejected; mutation or protocol reconciliation required "
            f"(method={self.method}, status={self.status}, code={self.code})"
        )

    @property
    def service_failure(self) -> bool:
        # This classification alone grants no retry. The orchestrator requires
        # complete terminal/flat proof and the existing supervisor burst budget.
        return (self.method in {"order.place", "order.cancel", "order.cancel_all"}
                and ((self.status == 429 and self.code == -1003)
                     or (self.status in {500, 502, 503, 504} and self.code == -1000)))


def check_response(frame: dict, method: str | None) -> None:
    if frame.get("status") == 200:
        return
    status = frame.get("status")
    if method in READ_METHODS and status in {429, 500, 502, 503, 504}:
        error = frame.get("error") or {}
        raw = frame.get("retryAfter", (error.get("data") or {}).get("retryAfter"))
        try:
            delay = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            delay = None
        if status == 429:
            delay = max(60, delay or 0)
        raise ApiError(status, "WebSocketReadServiceUnavailable", delay)
    # Even a correlated mutation error is not permission to replay. Never put
    # venue text, signed fields or identifiers into an exception message.
    raise WebSocketRequestRejected(frame, method)


class RequestTracker:
    def __init__(self) -> None:
        self.pending: dict[int, str] = {}

    def register(self, request_id: int, method: str) -> None:
        if request_id in self.pending or len(self.pending) >= 256:
            raise RuntimeError("WebSocket request correlation bound exceeded")
        self.pending[request_id] = method

    def response(self, frame: dict) -> str | None:
        if "status" not in frame:
            return None
        method = self.pending.pop(frame.get("id"), None)
        if method is None:
            raise RuntimeError("uncorrelated WebSocket response")
        check_response(frame, method)
        return method
