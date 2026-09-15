from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .rate_limits import AdmissionGovernor, Priority


PRODUCTION_REST = "https://api.gemini.com"
PUBLIC_EXACT = frozenset({
    "/v1/prediction-markets/events",
    "/v1/prediction-markets/liquidity-rewards/config",
    "/v1/prediction-markets/liquidity-rewards/events",
    "/v1/prediction-markets/maker-rebate/rates",
})
PRIVATE_READS = frozenset({
    "/v1/prediction-markets/terms/status",
    "/v1/prediction-markets/orders/active",
    "/v1/prediction-markets/orders/history",
    "/v1/prediction-markets/positions",
    "/v1/prediction-markets/positions/settled",
    "/v1/prediction-markets/liquidity-rewards/summary/daily",
    "/v1/prediction-markets/liquidity-rewards/summary/total",
})


class ApiError(RuntimeError):
    def __init__(self, status: int | None, reason: str, retry_after: float | None = None) -> None:
        super().__init__(f"Gemini API error status={status} reason={reason}")
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def private_headers(
    path: str,
    fields: dict[str, object],
    api_key: str,
    api_secret: str,
    nonce: int,
) -> dict[str, str]:
    payload_data = {"request": path, "nonce": nonce, **fields}
    payload = base64.b64encode(
        json.dumps(payload_data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(api_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha384).hexdigest()
    return {
        "X-GEMINI-APIKEY": api_key,
        "X-GEMINI-PAYLOAD": payload,
        "X-GEMINI-SIGNATURE": signature,
        "Content-Length": "0",
        "Content-Type": "text/plain",
        "Cache-Control": "no-cache",
    }


def _safe_reason(data: bytes) -> str:
    try:
        value = json.loads(data)
        reason = value.get("reason", value.get("error", "unavailable"))
    except Exception:
        return "unavailable"
    text = str(reason)
    return text if text.replace("_", "").isalnum() and len(text) <= 80 else "unavailable"


class GeminiReadRestClient:
    """Bounded REST reads only. Trading mutations intentionally do not exist."""

    def __init__(
        self,
        governor: AdmissionGovernor,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = PRODUCTION_REST,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.governor = governor
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._private_lock = asyncio.Lock()
        self._opener = urllib.request.build_opener(NoRedirect)

    @staticmethod
    def _public_allowed(path: str) -> bool:
        return path in PUBLIC_EXACT or (
            path.startswith("/v1/prediction-markets/events/")
            and path.count("/") == 4
        )

    async def _pace(self, lane: str, priority: Priority) -> None:
        delay = self.governor.admit(lane, priority)
        while delay:
            await asyncio.sleep(delay)
            delay = self.governor.admit(lane, priority)

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                value = json.load(response)
                if not isinstance(value, dict):
                    raise ApiError(response.status, "invalid_json_shape")
                return value
        except urllib.error.HTTPError as exc:
            data = exc.read(4096)
            retry = exc.headers.get("Retry-After")
            try:
                retry_after = float(retry) if retry is not None else None
            except ValueError:
                retry_after = None
            raise ApiError(exc.code, _safe_reason(data), retry_after) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ApiError(None, type(exc).__name__) from exc

    async def public_get(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        if not self._public_allowed(path):
            raise ValueError("public REST route is not approved")
        await self._pace("public_rest", Priority.REWARD_METADATA)
        query = urllib.parse.urlencode(params or {})
        url = self.base_url + path + (("?" + query) if query else "")
        request_factory = lambda: urllib.request.Request(
            url, method="GET", headers={"Accept": "application/json"},
        )
        return await self._read_with_cooldown(request_factory, "public_rest")

    async def _read_with_cooldown(
        self,
        request_factory: Callable[[], urllib.request.Request],
        lane: str,
    ) -> dict[str, Any]:
        priority = (
            Priority.ACCOUNT_RECONCILIATION
            if lane == "private_rest" else Priority.REWARD_METADATA
        )
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self._open_json, request_factory())
            except ApiError as exc:
                if exc.status in {418, 429} or exc.retry_after is not None:
                    delay = max(60.0, exc.retry_after or 0.0)
                    # An IP/account limit can span public and private endpoints.
                    for affected in ("public_rest", "private_rest"):
                        self.governor.apply_server_cooldown(affected, delay)
                retryable = exc.status is None or exc.status == 408 or (
                    exc.status is not None and 500 <= exc.status < 600
                )
                if not retryable or attempt == 2:
                    raise
                # Reads are idempotent. Back off and admit every retry through
                # the same shared lane so retry traffic remains quota-accounted.
                await asyncio.sleep(2 ** attempt)
                await self._pace(lane, priority)
        raise AssertionError("unreachable REST retry state")

    async def private_read(
        self,
        path: str,
        fields: dict[str, object] | None = None,
        *,
        method: str = "POST",
    ) -> dict[str, Any]:
        if path not in PRIVATE_READS:
            raise ValueError("private REST route is not an approved reconciliation read")
        if method not in {"GET", "POST"}:
            raise ValueError("private read method must be GET or POST")
        if not self.api_key or not self.api_secret:
            raise ValueError("private read requires credentials")
        async with self._private_lock:
            await self._pace("private_rest", Priority.ACCOUNT_RECONCILIATION)
            def request_factory() -> urllib.request.Request:
                # Rebuild authentication for every idempotent read attempt so
                # a timed-out request's nonce/signature is never replayed.
                headers = private_headers(
                    path, fields or {}, self.api_key or "", self.api_secret or "",
                    int(time.time()),
                )
                return urllib.request.Request(
                    self.base_url + path, method=method, headers=headers, data=None,
                )
            return await self._read_with_cooldown(request_factory, "private_rest")

    async def terms_status(self) -> dict[str, Any]:
        return await self.private_read("/v1/prediction-markets/terms/status", method="GET")

    async def reward_config(self) -> dict[str, Any]:
        return await self.public_get("/v1/prediction-markets/liquidity-rewards/config")

    async def maker_rebate_rates(self) -> list[dict[str, Any]]:
        response = await self.public_get("/v1/prediction-markets/maker-rebate/rates")
        rows = response.get("rate_rules")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ApiError(200, "invalid_maker_rebate_rates_shape")
        return rows

    async def reward_events(self, *, ceiling: int = 3000) -> list[dict[str, Any]]:
        # Offset pagination is not a snapshot. If Gemini inserts or reorders an
        # event while pages are being read, the boundary item can appear twice
        # and another item can be skipped. Restart the bounded read rather than
        # accepting an incomplete catalog or failing a live safety refresh on a
        # transient duplicate.
        for attempt in range(3):
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            retry_snapshot = False
            for offset in range(0, ceiling, 100):
                page = await self.public_get(
                    "/v1/prediction-markets/liquidity-rewards/events",
                    {"limit": 100, "offset": offset, "sort": "title_asc"},
                )
                events = page.get("events")
                pagination = page.get("pagination")
                if not isinstance(events, list) or not isinstance(pagination, dict):
                    raise ApiError(200, "invalid_reward_catalog_shape")
                try:
                    total = int(pagination.get("total", 0))
                except (TypeError, ValueError) as exc:
                    raise ApiError(200, "invalid_reward_catalog_total") from exc
                if total > ceiling:
                    raise ApiError(200, "reward_catalog_ceiling_exceeded")
                for row in events:
                    if not isinstance(row, dict) or not row.get("event_ticker"):
                        raise ApiError(200, "invalid_reward_event")
                    ticker = str(row["event_ticker"])
                    if ticker in seen:
                        retry_snapshot = True
                        break
                    seen.add(ticker)
                    rows.append(row)
                if retry_snapshot:
                    break
                if offset + len(events) >= total:
                    if len(rows) != total:
                        retry_snapshot = True
                        break
                    return rows
            if not retry_snapshot:
                raise ApiError(200, "reward_catalog_ceiling_exceeded")
            if attempt < 2:
                await asyncio.sleep(0)
        raise ApiError(200, "unstable_reward_catalog")

    async def event(self, event_ticker: str) -> dict[str, Any]:
        safe = urllib.parse.quote(event_ticker, safe="")
        return await self.public_get(f"/v1/prediction-markets/events/{safe}")

    async def active_orders(self, *, ceiling: int = 1000) -> list[dict[str, Any]]:
        return await self._private_pages("/v1/prediction-markets/orders/active", "orders", ceiling)

    async def positions(self, *, ceiling: int = 1000) -> list[dict[str, Any]]:
        return await self._private_pages("/v1/prediction-markets/positions", "positions", ceiling)

    async def order_history(self, *, ceiling: int = 1000) -> list[dict[str, Any]]:
        return await self._private_pages("/v1/prediction-markets/orders/history", "orders", ceiling)

    async def recent_order_history(
        self, *, symbol: str | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read one bounded, optionally symbol-filtered history page."""
        if not 1 <= limit <= 1000:
            raise ValueError("recent order-history limit must be between 1 and 1000")
        fields: dict[str, object] = {"limit": limit, "offset": 0}
        if symbol:
            fields["symbol"] = symbol
        page = await self.private_read(
            "/v1/prediction-markets/orders/history", fields,
        )
        values = page.get("orders")
        if not isinstance(values, list):
            raise ApiError(200, "invalid_orders_shape")
        if any(not isinstance(value, dict) for value in values):
            raise ApiError(200, "invalid_orders_row")
        return values

    async def filled_order_history(
        self,
        *,
        symbol: str,
        closed_from: datetime,
        closed_to: datetime,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read a bounded authenticated window of fully filled orders.

        Gemini documents ``from`` as inclusive and ``to`` as exclusive.  A
        full response is deliberately ambiguous because time-window requests
        do not support offset pagination.
        """
        if not symbol or symbol != symbol.upper():
            raise ValueError("filled order history requires an uppercase symbol")
        if closed_from.tzinfo is None or closed_to.tzinfo is None:
            raise ValueError("filled order-history bounds must be timezone-aware")
        start = closed_from.astimezone(timezone.utc)
        end = closed_to.astimezone(timezone.utc)
        if start >= end:
            raise ValueError("filled order-history bounds must increase")
        if not 1 <= limit <= 1000:
            raise ValueError("filled order-history limit must be between 1 and 1000")
        page = await self.private_read(
            "/v1/prediction-markets/orders/history",
            {
                "status": "filled",
                "symbol": symbol,
                "from": int(start.timestamp() * 1000),
                "to": int(end.timestamp() * 1000),
                "limit": limit,
            },
        )
        values = page.get("orders")
        if not isinstance(values, list):
            raise ApiError(200, "invalid_orders_shape")
        if any(not isinstance(value, dict) for value in values):
            raise ApiError(200, "invalid_orders_row")
        if len(values) >= limit:
            raise ApiError(200, "filled_orders_window_ceiling_exceeded")
        return values

    async def _private_pages(self, path: str, key: str, ceiling: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        for offset in range(0, ceiling, 100):
            page = await self.private_read(path, {"limit": 100, "offset": offset})
            values = page.get(key)
            if not isinstance(values, list):
                raise ApiError(200, f"invalid_{key}_shape")
            if any(not isinstance(value, dict) for value in values):
                raise ApiError(200, f"invalid_{key}_row")
            fingerprint = json.dumps(values, sort_keys=True, separators=(",", ":"))
            if values and fingerprint in seen_pages:
                raise ApiError(200, f"duplicate_{key}_page")
            seen_pages.add(fingerprint)
            rows.extend(values)
            total = page.get("total")
            pagination = page.get("pagination")
            if isinstance(pagination, dict):
                total = pagination.get("total", total)
            if len(values) < 100 or (total is not None and offset + len(values) >= int(total)):
                return rows
        raise ApiError(200, f"{key}_ceiling_exceeded")
