"""Owner-approved one-pool maker commissioning tool.

This script is intentionally unusable without the same short-lived, one-use
activation permit and live acknowledgement as the production runner. It never
expands beyond one pool and always requests a final cancel/flat drain.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path

from gemini_bot.config import load_live_config
from gemini_bot.orchestrator import ProductionOrchestrator


async def run(args: argparse.Namespace) -> None:
    config = load_live_config(args.env)
    runtime = ProductionOrchestrator(config, args.directory)
    try:
        prepared = await runtime.prepare()
        if prepared.account.positions or prepared.account.active_orders:
            raise RuntimeError("maker canary requires a flat account with zero active orders")
        await runtime.activate(
            args.activation_permit, commissioning_seconds=args.rest_seconds,
            single_leg=args.mode == "one-leg",
            ignore_uptime_proxy=args.ignore_uptime_proxy,
            expected_yes_price=args.expected_yes_price,
            expected_no_price=args.expected_no_price,
        )
        print("Maker canary finished with authenticated proof of zero positions and zero active orders.")
    finally:
        await runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded live Gemini maker commissioning")
    parser.add_argument("--mode", choices=("one-leg", "two-sided"), required=True)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--activation-permit", type=Path, required=True)
    parser.add_argument("--rest-seconds", type=int, default=5, choices=range(2, 31))
    parser.add_argument(
        "--ignore-uptime-proxy",
        action="store_true",
        help="mechanics-only: ignore only the uncertain daily uptime proxy",
    )
    parser.add_argument(
        "--expected-yes-price",
        type=Decimal,
        help="exact owner-reviewed YES price required for commissioning",
    )
    parser.add_argument(
        "--expected-no-price",
        type=Decimal,
        help="exact owner-reviewed NO price required for two-sided commissioning",
    )
    args = parser.parse_args()
    if args.mode == "one-leg" and args.expected_yes_price is None:
        parser.error("one-leg commissioning requires --expected-yes-price")
    if args.mode == "one-leg" and args.expected_no_price is not None:
        parser.error("--expected-no-price is restricted to two-sided commissioning")
    if args.mode == "two-sided" and (
        args.expected_yes_price is None or args.expected_no_price is None
    ):
        parser.error("two-sided commissioning requires both exact expected prices")
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
