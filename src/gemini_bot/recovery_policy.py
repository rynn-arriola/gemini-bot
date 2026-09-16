"""Only known transport/read failures authorize automatic operational retry."""
from websockets.exceptions import ConnectionClosed, InvalidStatus
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from .rest_client import ApiError


class NoMarketsAvailable(RuntimeError):
    """Read-only startup may wait for the next enabled, tradeable catalog."""


def transient_failure(exc: BaseException) -> bool:
    if isinstance(exc, ApiError):
        return exc.status in {429, 500, 502, 503, 504} or (
            exc.status is None and exc.reason in {
                "URLError", "TimeoutError", "ConnectionError", "ConnectionResetError",
            }
        )
    if isinstance(exc, InvalidStatus):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (ConnectionClosed, ConnectionError, TimeoutError))


def retry_after_seconds(exc: BaseException) -> float:
    raw = exc.retry_after if isinstance(exc, ApiError) else (
        exc.response.headers.get('Retry-After') if isinstance(exc, InvalidStatus) else None
    )
    if raw is None:
        return 60.0 if isinstance(exc, InvalidStatus) and exc.response.status_code == 429 else 0.0
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        stamp = parsedate_to_datetime(str(raw))
        if stamp.tzinfo is None:
            raise ValueError('server retry date lacks timezone')
        delay = (stamp - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(delay):
        raise ValueError('server retry delay is not finite')
    return max(0.0, delay)
