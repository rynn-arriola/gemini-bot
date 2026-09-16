"""Bounded request correlation; only known read failures permit recovery."""
from .rest_client import ApiError


READ_METHODS = frozenset({"conninfo", "SUBSCRIBE", "UNSUBSCRIBE", "LIST_SUBSCRIPTIONS", "ping", "time"})


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
    raise RuntimeError("WebSocket request rejected; mutation or protocol reconciliation required")


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
