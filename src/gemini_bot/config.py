from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import json
import os
from .capacity import LEGACY_PENDING_ORDERS, MAX_PENDING_ORDERS
from .queue_pilot import PilotPolicy, parse_pilot_policy
from .adaptive_queue import AdaptivePolicy, parse_adaptive_policy
from .entry_quality import EntryQualityPolicy
from .shield import AnchorPolicy, ShieldPolicy
from .uptime_ledger import UptimeLedgerPolicy


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
    queue_pilot: PilotPolicy | AdaptivePolicy | None = None
    entry_quality: EntryQualityPolicy | None = None
    # Policy v9 only: depth-shielded placement, category exclusion and the
    # per-event 50% uptime ledger on Gemini's inferred 5 pm ET scoring day.
    shield: ShieldPolicy | None = None
    excluded_categories: frozenset[str] = frozenset()
    uptime_ledger: UptimeLedgerPolicy | None = None
    anchor: AnchorPolicy | None = None

    @property
    def shielded(self) -> bool:
        return (isinstance(self.queue_pilot, AdaptivePolicy)
                and self.queue_pilot.placement_mode == 'shielded')

    @property
    def scoring_day_boundary_hour(self) -> int:
        if not self.shielded or self.uptime_ledger is None:
            return 0
        return self.uptime_ledger.scoring_day_boundary_hour

    def validate(self) -> None:
        pilot_at_zero = (isinstance(self.queue_pilot, AdaptivePolicy)
                         and self.queue_pilot.placement_mode in {'stay_best', 'shielded'})
        if pilot_at_zero and self.quote_offset_ticks != 0:
            raise ConfigurationError('stay_best/shielded placement requires zero-tick offset')
        if isinstance(self.queue_pilot, AdaptivePolicy) and not pilot_at_zero and self.quote_offset_ticks != 1:
            raise ConfigurationError("adaptive pilot requires one-tick fallback")
        approved_sizes = {Decimal("25")} if self.shielded else {Decimal("15"), Decimal("30")}
        if self.queue_pilot is not None and (
            self.quote_quantity not in approved_sizes
            or (not pilot_at_zero and self.quote_offset_ticks not in {1, 2})
        ):
            raise ConfigurationError(
                "queue policy requires an approved quote size and placement offset"
            )
        if self.shielded and (self.shield is None or self.uptime_ledger is None or self.anchor is None):
            raise ConfigurationError("shielded placement requires shield, uptime-ledger and anchor policies")
        # Shield, ledger and category settings take effect only in shielded mode.
        if self.shielded:
            try:
                self.shield.validate()
                self.uptime_ledger.validate()
                self.anchor.validate()
                if self.anchor.quantity >= self.quote_quantity:
                    raise ValueError("anchor must be smaller than the scoring quote")
            except ValueError as exc:
                raise ConfigurationError("invalid shield/uptime-ledger policy") from exc
        if self.quote_quantity < Decimal("10"):
            raise ConfigurationError("quote_quantity must meet Gemini's 10-contract reward minimum")
        if self.quote_offset_ticks < 0:
            raise ConfigurationError("quote_offset_ticks cannot be negative")
        if not Decimal("0.50") <= self.target_uptime <= Decimal("1"):
            raise ConfigurationError("target_uptime must be between 0.50 and 1.00")
        if self.target_uptime < self.minimum_uptime:
            raise ConfigurationError("target_uptime cannot be below minimum_uptime")
        if self.entry_quality is not None:
            try:
                self.entry_quality.validate()
            except ValueError as exc:
                raise ConfigurationError("invalid entry-quality policy") from exc


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
            raise ConfigurationError(f"max_pending_orders exceeds the approved cap of {MAX_PENDING_ORDERS}")
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


def load_policy(path: Path | None = None) -> tuple[StrategyConfig, RiskConfig]:
    path = DEFAULT_POLICY_PATH if path is None else path
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
    if version in {3, 4, 5, 6, 7, 8, 9}:
        required.add("queue_pilot")
    if version in {8, 9}:
        required.add("entry_quality")
    if version == 9:
        required.update({"shield", "excluded_categories", "uptime_ledger", "anchor"})
    if set(raw) != required or version not in {2, 3, 4, 5, 6, 7, 8, 9} or raw["platform"] != "windows":
        raise ConfigurationError("approved live policy schema/version/platform mismatch")
    if raw["risk_window_timezone"] != "America/New_York" or raw["risk_window_start_hours"] != [0, 6, 12, 18]:
        raise ConfigurationError("risk-window clock does not match approved Eastern windows")
    if raw["monetary_stop_authorities"] != 1:
        raise ConfigurationError("policy must contain exactly one monetary stop authority")
    if raw["risk_window_accounting"] != "closed_fill_realized_pnl":
        raise ConfigurationError("risk-window accounting must use closed-fill realized P&L")
    pending_orders = MAX_PENDING_ORDERS if version == 9 else LEGACY_PENDING_ORDERS
    if raw["risk_window_pending_order_policy"] != f"fixed_{pending_orders}_until_stop":
        raise ConfigurationError("risk-window pending-order policy must remain fixed until stop")
    if raw["max_pending_orders"] != pending_orders:
        raise ConfigurationError("fixed pending-order policy requires exactly 40 orders")
    try:
        adaptive_versions = {4, 5, 6, 7, 8, 9}
        pilot = (parse_adaptive_policy(raw.get("queue_pilot")) if version in adaptive_versions
                 else parse_pilot_policy(raw.get("queue_pilot")))
        if version in adaptive_versions and pilot.scope != ('all_selected' if version in {5, 6, 7, 8, 9} else 'research'):
            raise ValueError('adaptive scope requires its exact policy version')
        expected_mode = ('shielded' if version == 9 else 'stay_best' if version in {6, 7, 8} else 'adaptive')
        if version in adaptive_versions and pilot.placement_mode != expected_mode:
            raise ValueError('placement mode requires its exact policy version')
        if version in adaptive_versions and pilot.same_price_refresh_minimum_seconds != (Decimal('300') if version in {7, 8} else 0):
            raise ValueError('same-price refresh interval requires its exact policy version')
        if version in adaptive_versions and pilot.same_price_refresh_minimum_gain != (Decimal('500') if version in {7, 8} else 0):
            raise ValueError('same-price refresh gain requires its exact policy version')
    except (ValueError, TypeError, ArithmeticError, AttributeError) as exc:
        raise ConfigurationError("invalid queue pilot calibration") from exc
    quality = None
    if version in {8, 9}:
        value = raw["entry_quality"]
        if not isinstance(value, dict) or set(value) != {
            "minimum_exit_depth_fraction", "minimum_retained_value_fraction",
            "depth_haircut", "crypto_entry_before_expiry_seconds",
        }:
            raise ConfigurationError("invalid entry-quality policy schema")
        try:
            if type(value["crypto_entry_before_expiry_seconds"]) is not int:
                raise ValueError("crypto cutoff must be an integer")
            quality = EntryQualityPolicy(
                Decimal(value["minimum_exit_depth_fraction"]),
                Decimal(value["minimum_retained_value_fraction"]),
                Decimal(value["depth_haircut"]),
                value["crypto_entry_before_expiry_seconds"],
            )
            quality.validate()
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ConfigurationError("invalid entry-quality policy") from exc
    shield = ledger = anchor = None
    excluded: frozenset[str] = frozenset()
    if version == 9:
        shield, ledger, excluded = _parse_v9_extensions(raw)
        anchor = _parse_anchor(raw["anchor"])
    strategy = StrategyConfig(
        quote_quantity=Decimal(raw["quote_quantity"]),
        quote_offset_ticks=int(raw["quote_offset_ticks"]),
        target_uptime=Decimal(raw["target_uptime"]),
        queue_pilot=pilot,
        entry_quality=quality,
        shield=shield,
        excluded_categories=excluded,
        uptime_ledger=ledger,
        anchor=anchor,
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


def _parse_v9_extensions(raw: dict) -> tuple[ShieldPolicy, UptimeLedgerPolicy, frozenset[str]]:
    shield_raw, ledger_raw, excluded_raw = raw["shield"], raw["uptime_ledger"], raw["excluded_categories"]
    shield_keys = {"entry_contracts", "keep_fraction", "flow_multiplier",
                   "max_midpoint_distance", "upgrade_minimum_seconds"}
    ledger_keys = {"scoring_day_boundary_et", "margin_minutes",
                   "at_risk_spare_minutes", "admission_spare_minutes"}
    if (not isinstance(shield_raw, dict) or set(shield_raw) != shield_keys
            or not isinstance(ledger_raw, dict) or set(ledger_raw) != ledger_keys
            or not isinstance(excluded_raw, list)
            or any(not isinstance(v, str) or not v.strip() for v in excluded_raw)):
        raise ConfigurationError("invalid v9 shield/uptime/category policy schema")
    try:
        if any(isinstance(v, bool) or not isinstance(v, str) for v in shield_raw.values()):
            raise ValueError("shield values must be exact decimal text")
        shield = ShieldPolicy(**{k: Decimal(v) for k, v in shield_raw.items()})
        boundary = ledger_raw["scoring_day_boundary_et"]
        if not isinstance(boundary, str) or len(boundary) != 5 or boundary[2:] != ":00":
            raise ValueError("scoring-day boundary must be a whole ET hour like 17:00")
        minutes = [ledger_raw[k] for k in ("margin_minutes", "at_risk_spare_minutes", "admission_spare_minutes")]
        if any(type(v) is not int for v in minutes):
            raise ValueError("uptime-ledger minutes must be integers")
        ledger = UptimeLedgerPolicy(int(boundary[:2]), Decimal("0.50"), *minutes)
        shield.validate()
        ledger.validate()
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ConfigurationError("invalid v9 shield/uptime policy") from exc
    return shield, ledger, frozenset(v.strip().casefold() for v in excluded_raw)


def _parse_anchor(raw: object) -> AnchorPolicy:
    keys = {"quantity", "entry_contracts", "keep_contracts", "max_midpoint_distance"}
    if (not isinstance(raw, dict) or set(raw) != keys
            or any(isinstance(v, bool) or not isinstance(v, str) for v in raw.values())):
        raise ConfigurationError("invalid v9 anchor policy schema")
    try:
        anchor = AnchorPolicy(**{k: Decimal(v) for k, v in raw.items()})
        anchor.validate()
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ConfigurationError("invalid v9 anchor policy") from exc
    return anchor


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
