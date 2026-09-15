from __future__ import annotations

import base64
import hashlib
import hmac
import time


class NonceFactory:
    """Monotonic Unix-second nonces for the empirically verified WS path."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        value = int(time.time())
        if value <= self._last:
            value = self._last + 1
        self._last = value
        return value


def websocket_auth_headers(api_key: str, api_secret: str, nonce: int) -> dict[str, str]:
    payload = base64.b64encode(str(nonce).encode("ascii")).decode("ascii")
    signature = hmac.new(api_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha384).hexdigest()
    return {
        "X-GEMINI-APIKEY": api_key,
        "X-GEMINI-NONCE": str(nonce),
        "X-GEMINI-PAYLOAD": payload,
        "X-GEMINI-SIGNATURE": signature,
    }
