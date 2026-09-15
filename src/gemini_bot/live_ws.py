from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
import json
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .auth import NonceFactory, websocket_auth_headers
from .execution import (
    IntentLedger,
    OrderPurpose,
    cancel_request,
    exit_request,
    immediate_entry_request,
    place_request,
)
from .domain import QuoteLeg
from .rate_limits import AdmissionGovernor, Priority


PRODUCTION_WS = "wss://ws.gemini.com"


class LiveWebSocket:
    """Authenticated production transport; no simulation or automatic mutations."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        governor: AdmissionGovernor,
        ledger: IntentLedger,
        *,
        uri: str = PRODUCTION_WS,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._governor = governor
        self._ledger = ledger
        self._uri = f"{uri}?snapshot=-1&cancelOnDisconnect=true"
        self._nonce = NonceFactory()
        self._connection: ClientConnection | None = None
        self._request_id = 0
        self._send_lock = asyncio.Lock()
        self.mutation_guard = None

    async def connect(self) -> None:
        headers = websocket_auth_headers(self._api_key, self._api_secret, self._nonce.next())
        self._connection = await websockets.connect(
            self._uri,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_queue=2048,
            max_size=2**20,
            compression=None,
            proxy=None,
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(self, payload: dict[str, Any], priority: Priority) -> None:
        if self._connection is None:
            raise ConnectionError("authenticated WebSocket is not connected")
        async with self._send_lock:
            delay = self._governor.admit("private_ws", priority)
            while delay:
                await asyncio.sleep(delay)
                delay = self._governor.admit("private_ws", priority)
            if payload.get("method") == "order.place" and self.mutation_guard is not None:
                self.mutation_guard(payload)
            await self._connection.send(json.dumps(payload, separators=(",", ":")))

    async def subscribe_account(self) -> None:
        await self._send(
            {
                "id": self._next_id(),
                "method": "SUBSCRIBE",
                "params": ["orders@account", "balances@account@1s", "positions@account@1s"],
            },
            Priority.ACCOUNT_RECONCILIATION,
        )

    async def switch_to_account_deltas(self) -> None:
        """Use complete snapshots for startup, then lower-CPU change streams."""
        await self._send(
            {
                "id": self._next_id(),
                "method": "SUBSCRIBE",
                "params": ["balances@account", "positions@account"],
            },
            Priority.ACCOUNT_RECONCILIATION,
        )
        await self._send(
            {
                "id": self._next_id(),
                "method": "UNSUBSCRIBE",
                "params": ["balances@account@1s", "positions@account@1s"],
            },
            Priority.ACCOUNT_RECONCILIATION,
        )

    async def place_maker(self, leg: QuoteLeg) -> str:
        intent = self._ledger.prepare(leg, OrderPurpose.ENTRY)
        payload = place_request(self._next_id(), intent)
        # Persist/own the intent before the only network mutation. Once send is
        # attempted its outcome is ambiguous until account reconciliation.
        self._ledger.mark_sent(intent.client_order_id)
        await self._send(payload, Priority.ORDER_SAFETY)
        return intent.client_order_id

    async def place_ioc_entry(self, leg: QuoteLeg) -> str:
        """Commissioning-only aggressive limit; never used by reward quoting."""
        intent = self._ledger.prepare(leg, OrderPurpose.ENTRY)
        payload = immediate_entry_request(self._next_id(), intent)
        self._ledger.mark_sent(intent.client_order_id)
        await self._send(payload, Priority.ORDER_SAFETY)
        return intent.client_order_id

    async def cancel(self, order_id: str) -> None:
        await self._send(cancel_request(self._next_id(), order_id), Priority.EMERGENCY_CANCEL)

    async def exit_position(self, leg: QuoteLeg, *, signed_position: Decimal) -> str:
        intent = self._ledger.prepare(leg, OrderPurpose.EXIT)
        payload = exit_request(self._next_id(), intent, signed_position=signed_position)
        self._ledger.mark_sent(intent.client_order_id)
        await self._send(payload, Priority.ORDER_SAFETY)
        return intent.client_order_id

    async def cancel_all(self) -> None:
        await self._send(
            {"id": self._next_id(), "method": "order.cancel_all", "params": {}},
            Priority.EMERGENCY_CANCEL,
        )

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._connection is None:
            raise ConnectionError("authenticated WebSocket is not connected")
        async for raw in self._connection:
            if not isinstance(raw, str):
                continue
            value = json.loads(raw)
            if isinstance(value, dict):
                if value.get("status") in {418, 429}:
                    self._governor.apply_server_cooldown("private_ws", 60)
                yield value
