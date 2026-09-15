from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Protocol


class PermitStore(Protocol):
    def get_runtime_state(self, key: str) -> str | None: ...
    def set_runtime_state(self, key: str, value: str, *, updated_at: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class ActivationPermit:
    permit_id: str
    approved_at: datetime
    expires_at: datetime
    maximum_active_pools: int
    policy_sha256: str
    account_fingerprint: str
    build_sha256: str
    symbols: tuple[str, ...]


def policy_digest(policy_path: Path) -> str:
    return hashlib.sha256(policy_path.read_bytes()).hexdigest()

def build_digest() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    files = sorted(list((root / "src").rglob("*.py")) + list((root / "scripts").glob("*.py")))
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_activation_permit(
    permit_path: Path,
    *,
    policy_path: Path,
    store: PermitStore,
    now: datetime | None = None,
    account_fingerprint: str | None = None,
) -> ActivationPermit:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("permit validation time must be timezone-aware")
    try:
        raw = json.loads(permit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("activation permit is missing or unreadable") from exc
    required = {
        "permit_version", "permit_id", "approved_at", "expires_at",
        "maximum_active_pools", "policy_sha256",
        "account_fingerprint", "build_sha256", "symbols",
    }
    if set(raw) != required or raw["permit_version"] != 2:
        raise ValueError("activation permit schema/version mismatch")
    permit = ActivationPermit(
        permit_id=str(raw["permit_id"]),
        approved_at=datetime.fromisoformat(str(raw["approved_at"])),
        expires_at=datetime.fromisoformat(str(raw["expires_at"])),
        maximum_active_pools=int(raw["maximum_active_pools"]),
        policy_sha256=str(raw["policy_sha256"]),
        account_fingerprint=str(raw["account_fingerprint"]),
        build_sha256=str(raw["build_sha256"]),
        symbols=tuple(raw["symbols"]) if isinstance(raw["symbols"], list) else (),
    )
    if not permit.permit_id or permit.approved_at.tzinfo is None or permit.expires_at.tzinfo is None:
        raise ValueError("activation permit identity and timezone-aware timestamps are required")
    if permit.approved_at > current or current >= permit.expires_at:
        raise ValueError("activation permit is not currently valid")
    if (permit.expires_at - permit.approved_at).total_seconds() > 24 * 60 * 60:
        raise ValueError("activation permit validity cannot exceed 24 hours")
    if not 1 <= permit.maximum_active_pools <= 15:
        raise ValueError("activation permit pool breadth must be between 1 and 15")
    if permit.policy_sha256 != policy_digest(policy_path):
        raise ValueError("activation permit does not match the approved policy")
    if account_fingerprint is None or permit.account_fingerprint != account_fingerprint:
        raise ValueError("activation permit does not match the account")
    if permit.build_sha256 != build_digest():
        raise ValueError("activation permit does not match the reviewed build")
    if (not permit.symbols or len(permit.symbols) != len(set(permit.symbols))
            or len(permit.symbols) > permit.maximum_active_pools
            or any(not isinstance(symbol, str) or not symbol or symbol != symbol.upper() for symbol in permit.symbols)):
        raise ValueError("activation permit requires exact unique uppercase symbols within pool breadth")
    if store.get_runtime_state(f"consumed_permit:{permit.permit_id}") is not None:
        raise ValueError("activation permit has already been consumed")
    return permit


def consume_activation_permit(
    permit: ActivationPermit,
    store: PermitStore,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    store.set_runtime_state(
        f"consumed_permit:{permit.permit_id}",
        current.isoformat(),
        updated_at=current,
    )
