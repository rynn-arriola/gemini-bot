"""Read public WebSocket connection limits; no account credentials."""
import asyncio
import json
from websockets.asyncio.client import connect


async def main():
    async with connect("wss://ws.gemini.com", proxy=None, open_timeout=10, close_timeout=3) as ws:
        await ws.send(json.dumps({"id": 1, "method": "conninfo", "params": {}}))
        print(await asyncio.wait_for(ws.recv(), 10))


if __name__ == "__main__":
    asyncio.run(main())
