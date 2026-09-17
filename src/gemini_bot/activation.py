from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Protocol
from .capacity import MAX_ACTIVE_MARKETS


class PermitStore(Protocol):
    def get_runtime_state(self, key: str) -> str | None: ...
    def set_runtime_state(self, key: str, value: str, *, updated_at: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class ActivationPermit:
    permit_id: str
    approved_at: datetime
    expires_at: datetime | None
    maximum_active_pools: int
    policy_sha256: str
    account_fingerprint: str
    build_sha256: str
    symbols: tuple[str, ...]
    uptime_override_until: datetime | None = None
    universe_scope: str | None = None
    permit_version: int = 2
    supervised_restart_limit: int = 0


def policy_digest(policy_path: Path) -> str:
    return hashlib.sha256(policy_path.read_bytes()).hexdigest()

def build_digest() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    files = sorted(
        list((root / "src").rglob("*.py"))
        + list((root / "scripts").glob("*.py"))
        + list((root / "scripts").glob("*.ps1"))
    )
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
    version = raw.get("permit_version")
    schemas = {
        2: required,
        3: required | {"uptime_override_until"},
        4: (required - {"symbols"}) | {"uptime_override_until", "universe_scope"},
        5: (required - {"symbols"}) | {
            "uptime_override_until", "universe_scope", "supervised_restart_limit",
        },
        6: (required - {"symbols"}) | {
            "uptime_override_until", "universe_scope", "supervised_restart_limit",
        },
        7: (required - {"symbols"}) | {
            "uptime_override_until", "universe_scope", "supervised_restart_limit",
        },
    }
    if version not in schemas or set(raw) != schemas[version]:
        raise ValueError("activation permit schema/version mismatch")
    override = (
        datetime.fromisoformat(str(raw["uptime_override_until"]))
        if version in {3, 4, 5, 6, 7} and not (version in {6, 7} and raw["uptime_override_until"] is None) else None
    )
    permit = ActivationPermit(
        permit_id=str(raw["permit_id"]),
        approved_at=datetime.fromisoformat(str(raw["approved_at"])),
        expires_at=(None if version == 7 and raw["expires_at"] is None
                    else datetime.fromisoformat(str(raw["expires_at"]))),
        maximum_active_pools=int(raw["maximum_active_pools"]),
        policy_sha256=str(raw["policy_sha256"]),
        account_fingerprint=str(raw["account_fingerprint"]),
        build_sha256=str(raw["build_sha256"]),
        symbols=tuple(raw["symbols"]) if isinstance(raw.get("symbols"), list) else (),
        uptime_override_until=override,
        universe_scope=str(raw["universe_scope"]) if version in {4, 5, 6, 7} else None,
        permit_version=int(version),
        supervised_restart_limit=int(raw.get("supervised_restart_limit", 0)),
    )
    if (not permit.permit_id or permit.approved_at.tzinfo is None
            or (permit.expires_at is not None and permit.expires_at.tzinfo is None)):
        raise ValueError("activation permit identity and timezone-aware timestamps are required")
    if permit.approved_at > current or (permit.expires_at is not None and current >= permit.expires_at):
        raise ValueError("activation permit is not currently valid")
    if version == 7 and permit.expires_at is not None:
        raise ValueError("continuous-service permit requires explicit null expiry")
    if permit.expires_at is not None and (permit.expires_at - permit.approved_at).total_seconds() > 24 * 60 * 60:
        raise ValueError("activation permit validity cannot exceed 24 hours")
    if permit.uptime_override_until is not None and (
        permit.uptime_override_until.tzinfo is None
        or (permit.permit_version not in {6, 7} and permit.uptime_override_until <= current)
        or (permit.permit_version in {6, 7} and permit.uptime_override_until < permit.approved_at)
        or (permit.expires_at is not None and permit.uptime_override_until > permit.expires_at)
        or (version == 7 and (permit.uptime_override_until - permit.approved_at).total_seconds() > 86400)
    ):
        raise ValueError("uptime override must be timezone-aware, active, and within permit validity")
    maximum = MAX_ACTIVE_MARKETS if permit.permit_version == 7 else 15
    if not 1 <= permit.maximum_active_pools <= maximum:
        raise ValueError("activation permit exceeds approved market breadth")
    if permit.policy_sha256 != policy_digest(policy_path):
        raise ValueError("activation permit does not match the approved policy")
    if account_fingerprint is None or permit.account_fingerprint != account_fingerprint:
        raise ValueError("activation permit does not match the account")
    if permit.build_sha256 != build_digest():
        raise ValueError("activation permit does not match the reviewed build")
    if permit.universe_scope is None:
        if (not permit.symbols or len(permit.symbols) != len(set(permit.symbols))
                or len(permit.symbols) > permit.maximum_active_pools
                or any(not isinstance(symbol, str) or not symbol or symbol != symbol.upper() for symbol in permit.symbols)):
            raise ValueError("activation permit requires exact unique uppercase symbols within pool breadth")
    elif permit.universe_scope != "reward_catalog":
        raise ValueError("activation permit universe scope is not approved")
    if permit.permit_version in {5, 6, 7} and not 1 <= permit.supervised_restart_limit <= 2:
        raise ValueError("supervised permit restart limit must be between one and two")
    consumed = store.get_runtime_state(f"consumed_permit:{permit.permit_id}")
    if consumed is not None:
        if permit.permit_version not in {5, 6, 7}:
            raise ValueError("activation permit has already been consumed")
        try:
            session = json.loads(consumed)
            valid_session = (
                isinstance(session, dict)
                and session.get("status") == "recoverable_flat"
                and isinstance(session.get("session_id"), str)
                and bool(session["session_id"])
                and (version == 7 or int(session.get("activations", 0)) < 1 + permit.supervised_restart_limit)
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            valid_session = False
        if not valid_session:
            raise ValueError("supervised activation session is not recoverable")
    return permit


def consume_activation_permit(
    permit: ActivationPermit,
    store: PermitStore,
    *,
    now: datetime | None = None,
    supervisor_session_id: str | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    if permit.permit_version in {5, 6, 7}:
        if not supervisor_session_id:
            raise ValueError("supervised permit requires a supervisor session identity")
        key = f"consumed_permit:{permit.permit_id}"
        prior = store.get_runtime_state(key)
        activations = 1
        if prior is not None:
            try:
                state = json.loads(prior)
            except json.JSONDecodeError as exc:
                raise ValueError("supervised activation state is unreadable") from exc
            if (
                state.get("session_id") != supervisor_session_id
                or state.get("status") != "recoverable_flat"
            ):
                raise ValueError("supervised activation session cannot be resumed")
            activations = int(state.get("activations", 0)) + 1
        if permit.permit_version != 7 and activations > 1 + permit.supervised_restart_limit:
            raise ValueError("supervised activation restart limit is exhausted")
        store.set_runtime_state(
            key,
            json.dumps({
                "session_id": supervisor_session_id,
                "activations": activations,
                "status": "active",
            }, separators=(",", ":"), sort_keys=True),
            updated_at=current,
        )
        return
    store.set_runtime_state(
        f"consumed_permit:{permit.permit_id}",
        current.isoformat(),
        updated_at=current,
    )


def finish_supervised_activation(
    permit: ActivationPermit,
    store: PermitStore,
    *,
    supervisor_session_id: str | None,
    outcome: str,
    now: datetime | None = None,
) -> None:
    """Close or release a supervised session only from its active runner."""
    if permit.permit_version not in {5, 6, 7}:
        return
    if outcome not in {"recoverable_flat", "closed", "attention"}:
        raise ValueError("unknown supervised activation outcome")
    if not supervisor_session_id:
        raise ValueError("supervised permit requires a supervisor session identity")
    key = f"consumed_permit:{permit.permit_id}"
    raw = store.get_runtime_state(key)
    if raw is None:
        raise RuntimeError("supervised activation state is missing")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("supervised activation state is unreadable") from exc
    if state.get("session_id") != supervisor_session_id or state.get("status") != "active":
        raise RuntimeError("supervised activation state does not match the active runner")
    state["status"] = outcome
    current = now or datetime.now(timezone.utc)
    store.set_runtime_state(
        key, json.dumps(state, separators=(",", ":"), sort_keys=True),
        updated_at=current,
    )
