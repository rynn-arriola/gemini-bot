"""Bounded credential/data checks. No order or account mutation methods exist here."""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials():
    values = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    return values["GEMINI_API_KEY"].strip(), values["GEMINI_API_SECRET"].strip()


def signature(payload, secret):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha384).hexdigest()


def terms_status(key, secret):
    path = "/v1/prediction-markets/terms/status"
    payload = base64.b64encode(json.dumps({"request": path, "nonce": int(time.time())}).encode()).decode()
    req = urllib.request.Request("https://api.gemini.com" + path, method="GET", headers={
        "X-GEMINI-APIKEY": key, "X-GEMINI-PAYLOAD": payload,
        "X-GEMINI-SIGNATURE": signature(payload, secret), "Content-Length": "0",
        "Cache-Control": "no-cache", "Content-Type": "text/plain",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as res:
            data = json.load(res)
            return {"http_status": res.status, "hasAcceptedLatest": data.get("hasAcceptedLatest")}
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read()).get("reason", "")
        except Exception:
            reason = ""
        return {"http_status": exc.code, "reason": reason if re.fullmatch(r"[A-Za-z0-9_]{1,80}", reason) else "unavailable"}
    except Exception as exc:
        return {"error_type": type(exc).__name__}


async def main():
    key, secret = credentials()
    result = {"captured_at": time.time(), "account_scoped": key.startswith("account-"),
              "trading_permissions": "not_tested", "terms": await asyncio.to_thread(terms_status, key, secret)}
    await asyncio.sleep(1.2)
    nonce = str(int(time.time()))
    payload = base64.b64encode(nonce.encode()).decode()
    headers = {"X-GEMINI-APIKEY": key, "X-GEMINI-NONCE": nonce,
               "X-GEMINI-PAYLOAD": payload, "X-GEMINI-SIGNATURE": signature(payload, secret)}
    try:
        async with connect("wss://ws.gemini.com", additional_headers=headers, open_timeout=15,
                           close_timeout=3, max_queue=64, proxy=None) as ws:
            result["private_ws_connected"] = True
            await ws.send(json.dumps({"id": 1, "method": "SUBSCRIBE", "params": [
                "positions@account@1s", "balances@account@1s", "orders@account"]}))
            counts = Counter()
            until = time.monotonic() + 15
            while time.monotonic() < until:
                try:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), until - time.monotonic()))
                except TimeoutError:
                    break
                if "error" in frame:
                    result["subscription_error_code"] = frame["error"].get("code")
                elif "id" in frame:
                    result["subscription_ack"] = True
                else:
                    counts[frame.get("e", "untyped")] += 1
            result["private_frame_types"] = dict(counts)
    except Exception as exc:
        result["private_ws_connected"] = False
        result["private_ws_error_type"] = type(exc).__name__
        response = getattr(exc, "response", None)
        result["private_ws_http_status"] = getattr(response, "status_code", None)
    try:
        async with connect("wss://ws.gemini.com", open_timeout=15, close_timeout=3, proxy=None) as ws:
            await ws.send(json.dumps({"id": 2, "method": "depth", "params": {
                "symbol": "GEMI-ZEC2609152100-HI1160", "limit": 20}}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            result["public_depth_response"] = frame
    except Exception as exc:
        result["public_ws_error_type"] = type(exc).__name__
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "connection_probe.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
