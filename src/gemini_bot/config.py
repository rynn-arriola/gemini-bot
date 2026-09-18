from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import json
import os
from .capacity import MAX_PENDING_ORDERS
from .queue_pilot import PilotPolicy, parse_pilot_policy


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    quote_quantity: Decimal = Decimal("10")
    quote_offset_ticks: int = 2
    minimum_uptime: Decimal = Decimal("0.50")
    target_uptime: Decimal = Decimal("0.75")
    max_quote_distance: Decimal = Decimal("0.10")
    reward_size_cap: Decimal = Decimal("250")
    stale_book_seconds: Decimal = Decimal("3")
    queue_pilot: PilotPolicy | None = None

    def validate(self) -> None:
        if self.queue_pilot is not None and (
            self.quote_quantity != Decimal("15") or self.quote_offset_ticks not in {1, 2}
        ):
            raise ConfigurationError("queue pilot requires size 15 and nonpilot offset 1 or 2")
        if self.quote_quantity < Decimal("10"):
            raise ConfigurationError("quote_quantity must meet Gemini's 10-contract reward minimum")
        if self.quote_offset_ticks < 0:
            raise ConfigurationError("quote_offset_ticks cannot be negative")
        if not Decimal("0.50") <= self.target_uptime <= Decimal("1"):
            raise ConfigurationError("target_uptime must be between 0.50 and 1.00")
        if self.target_uptime < self.minimum_uptime:
            raise ConfigurationError("target_uptime cannot be below minimum_uptime")


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_gross_reservation_usd: Decimal
    max_market_exposure_usd: Decimal
    max_stressed_exit_loss_usd: Decimal
    risk_window_loss_usd: Decimal
    max_two_sided_quotes: int = 15
    max_tracked_markets: int = 32
    max_pending_orders: int = 30
    max_inventory_age_seconds: int = 300
    force_exit_before_expiry_seconds: int = 300
    sports_exit_before_start_seconds: int = 1200

    def validate(self) -> None:
        if self.max_gross_reservation_usd <= 0:
            raise ConfigurationError("max_gross_reservation_usd must be positive")
        if not 2 <= self.max_pending_orders <= MAX_PENDING_ORDERS:
            raise ConfigurationError("max_pending_orders exceeds the approved cap of 40")
        if not Decimal("0") < self.max_market_exposure_usd <= self.max_gross_reservation_usd:
            raise ConfigurationError("market exposure must be positive and no greater than gross exposure")
        if not Decimal("0") < self.max_stressed_exit_loss_usd < self.max_market_exposure_usd:
            raise ConfigurationError("stressed-exit entry cap must be positive and below market exposure")
        if self.risk_window_loss_usd <= 0:
            raise ConfigurationError("risk_window_loss_usd must be positive")
        if not 1 <= self.max_two_sided_quotes <= self.max_pending_orders // 2:
            raise ConfigurationError("two-sided quote count exceeds pending-order capacity")
        if self.max_tracked_markets < self.max_two_sided_quotes:
            raise ConfigurationError("tracked-market capacity cannot be below quoting capacity")
        if self.max_inventory_age_seconds <= 0:
            raise ConfigurationError("max_inventory_age_seconds must be positive")
        if self.force_exit_before_expiry_seconds <= 0:
            raise ConfigurationError("force_exit_before_expiry_seconds must be positive")
        if self.sports_exit_before_start_seconds <= 0:
            raise ConfigurationError("sports_exit_before_start_seconds must be positive")


@dataclass(frozen=True, slots=True)
class LiveConfig:
    api_key: str
    api_secret: str
    live_acknowledgement: str
    start_paused: bool
    strategy: StrategyConfig
    risk: RiskConfig

    def validate_base(self) -> None:
        self.strategy.validate()
        self.risk.validate()
        if not self.api_key.startswith("account-"):
            raise ConfigurationError("Gemini Predictions requires an account-scoped API key")
        if not self.api_secret:
            raise ConfigurationError("API secret is required")
        if not self.start_paused:
            raise ConfigurationError("production startup must be paused; activation follows reconciliation")

    def validate(self) -> None:
        self.validate_base()
        if self.live_acknowledgement != "I_UNDERSTAND_THIS_TRADES_REAL_MONEY":
            raise ConfigurationError("exact live acknowledgement is required")


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"missing required setting: {name}")
    return value


def load_dotenv(path: Path) -> dict[str, str]:
    """Read a simple local dotenv without logging secret values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "live_policy.json"


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> tuple[StrategyConfig, RiskConfig]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("approved live policy cannot be read") from exc
    required = {
        "policy_version", "platform", "quote_quantity", "quote_offset_ticks", "target_uptime",
        "max_pending_orders", "max_two_sided_quotes", "max_tracked_markets",
        "max_gross_reservation_usd", "max_market_exposure_usd",
        "max_stressed_exit_loss_usd", "risk_window_stop_loss_usd",
        "risk_window_accounting", "risk_window_timezone", "risk_window_start_hours",
        "risk_window_pending_order_policy",
        "max_inventory_age_seconds", "force_exit_before_expiry_seconds", "monetary_stop_authorities",
    }
    version = raw.get("policy_version")
    if version == 3:
        required.add("queue_pilot")
    if set(raw) != required or version not in {2, 3} or raw["platform"] != "windows":
        raise ConfigurationError("approved live policy schema/version/platform mismatch")
    if raw["risk_window_timezone"] != "America/New_York" or raw["risk_window_start_hours"] != [0, 6, 12, 18]:
        raise ConfigurationError("risk-window clock does not match approved Eastern windows")
    if raw["monetary_stop_authorities"] != 1:
        raise ConfigurationError("policy must contain exactly one monetary stop authority")
    if raw["risk_window_accounting"] != "closed_fill_realized_pnl":
        raise ConfigurationError("risk-window accounting must use closed-fill realized P&L")
    if raw["risk_window_pending_order_policy"] != "fixed_40_until_stop":
        raise ConfigurationError("risk-window pending-order policy must remain fixed until stop")
    if raw["max_pending_orders"] != MAX_PENDING_ORDERS:
        raise ConfigurationError("fixed pending-order policy requires exactly 40 orders")
    try:
        pilot = parse_pilot_policy(raw.get("queue_pilot"))
    except (ValueError, TypeError, ArithmeticError, AttributeError) as exc:
        raise ConfigurationError("invalid queue pilot calibration") from exc
    strategy = StrategyConfig(
        quote_quantity=Decimal(raw["quote_quantity"]),
        quote_offset_ticks=int(raw["quote_offset_ticks"]),
        target_uptime=Decimal(raw["target_uptime"]),
        queue_pilot=pilot,
    )
    risk = RiskConfig(
        max_gross_reservation_usd=Decimal(raw["max_gross_reservation_usd"]),
        max_market_exposure_usd=Decimal(raw["max_market_exposure_usd"]),
        max_stressed_exit_loss_usd=Decimal(raw["max_stressed_exit_loss_usd"]),
        risk_window_loss_usd=Decimal(raw["risk_window_stop_loss_usd"]),
        max_two_sided_quotes=int(raw["max_two_sided_quotes"]),
        max_tracked_markets=int(raw["max_tracked_markets"]),
        max_pending_orders=int(raw["max_pending_orders"]),
        max_inventory_age_seconds=int(raw["max_inventory_age_seconds"]),
        force_exit_before_expiry_seconds=int(raw["force_exit_before_expiry_seconds"]),
        sports_exit_before_start_seconds=int(raw.get("sports_exit_before_start_seconds", 1200)),
    )
    strategy.validate()
    risk.validate()
    return strategy, risk


def load_live_config(dotenv_path: Path | None = None) -> LiveConfig:
    config = load_preflight_config(dotenv_path)
    config.validate()
    return config


def load_preflight_config(dotenv_path: Path | None = None) -> LiveConfig:
    """Load credentials/policy for read-only preparation without authorizing orders."""
    env = dict(os.environ)
    if dotenv_path is not None:
        env = {**load_dotenv(dotenv_path), **env}
    strategy, risk = load_policy()
    config = LiveConfig(
        api_key=_required(env, "GEMINI_API_KEY"),
        api_secret=_required(env, "GEMINI_API_SECRET"),
        live_acknowledgement=env.get("GEMINI_LIVE_ACKNOWLEDGEMENT", "").strip(),
        start_paused=env.get("GEMINI_START_PAUSED", "true").lower() == "true",
        strategy=strategy,
        risk=risk,
    )
    config.validate_base()
    return config
