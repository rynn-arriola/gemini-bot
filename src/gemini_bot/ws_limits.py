"""Connection handshake rate discovery; no account or order mutation."""
from __future__ import annotations

import asyncio
import json
from .rate_limits import AdmissionGovernor
from .ws_requests import check_response


async def pace_connection(governor: AdmissionGovernor) -> None:
    while (delay := governor.admit_websocket_connection()) > 0:
        await asyncio.sleep(delay)


def observe_response(governor: AdmissionGovernor, lane: str, frame: dict) -> None:
    if frame.get("status") in {418, 429} or (frame.get("error") or {}).get("code") == -1003:
        error = frame.get("error") or {}
        details = error.get("data") or {}
        retry = frame.get("retryAfter", details.get("retryAfter", 60))
        try:
            seconds = max(60.0, float(retry))
        except (TypeError, ValueError):
            seconds = 60.0
        # Scope may be shared. Never let the second socket bypass a cooldown.
        for affected in ("public_ws", "private_ws"):
            governor.apply_server_cooldown(affected, seconds)
    if "rateLimits" in frame:
        governor.observe_websocket_limits(lane, frame["rateLimits"])


async def receive_connection_limits(connection, governor: AdmissionGovernor, lane: str, request_id: int) -> list[dict]:
    buffered = []
    async with asyncio.timeout(10):
        while True:
            raw = await connection.recv()
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                raise ValueError("invalid WebSocket handshake response")
            observe_response(governor, lane, frame)
            if frame.get("id") == request_id:
                check_response(frame, "conninfo")
                if "rateLimits" not in frame:
                    raise RuntimeError("conninfo failed to establish WebSocket limits")
                return buffered
            buffered.append(frame)
            if len(buffered) > 256:
                raise RuntimeError("WebSocket limits handshake buffer exceeded")
