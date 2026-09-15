from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .execution import OrderIntent


SCHEMA = """
CREATE TABLE IF NOT EXISTS order_intent (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    purpose TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    state TEXT NOT NULL,
    server_order_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_deadline (
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    quantity TEXT NOT NULL,
    PRIMARY KEY (symbol, outcome)
);
CREATE TABLE IF NOT EXISTS audit_event (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_position (
    symbol TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    signed_quantity TEXT NOT NULL,
    average_price TEXT NOT NULL,
    market_value TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_order (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    outcome TEXT NOT NULL,
    price TEXT NOT NULL,
    original_quantity TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_order_client ON account_order(client_order_id);
CREATE TABLE IF NOT EXISTS execution_fill (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    outcome TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    event_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_window_state (
    window_start TEXT PRIMARY KEY,
    window_end TEXT NOT NULL,
    opening_equity TEXT NOT NULL,
    loss_high_water TEXT NOT NULL,
    pending_order_cap INTEGER NOT NULL,
    classification_complete INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reward_uptime (
    pool_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    eligible_samples INTEGER NOT NULL,
    qualifying_samples INTEGER NOT NULL,
    last_sample_bucket TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (pool_id, window_start, window_end)
);
CREATE TABLE IF NOT EXISTS reward_uptime_v2 (
    event_ticker TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    eligible_samples INTEGER NOT NULL,
    qualifying_samples INTEGER NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_sample_bucket TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_ticker, pool_id, window_start, window_end)
);
CREATE INDEX IF NOT EXISTS idx_reward_uptime_v2_pool
ON reward_uptime_v2(pool_id, window_start, window_end);
"""


class SQLiteJournal:
    """Durable mutation/deadline journal; values are stored as exact strings."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(order_intent)")
        }
        if "purpose" not in columns:
            self.connection.execute(
                "ALTER TABLE order_intent ADD COLUMN purpose TEXT NOT NULL DEFAULT 'legacy_unknown'"
            )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def pending_exit(self, symbol: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM order_intent WHERE symbol=? AND purpose='exit' "
            "AND state IN ('sent_unknown','acknowledged') LIMIT 1", (symbol,)
        ).fetchone() is not None

    def record_intent(self, intent: OrderIntent) -> None:
        self.connection.execute(
            """INSERT INTO order_intent
               (client_order_id,symbol,outcome,side,purpose,price,quantity,state,server_order_id,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (
                intent.client_order_id,
                intent.leg.symbol,
                intent.leg.outcome.value,
                intent.leg.side.value,
                intent.purpose.value,
                str(intent.leg.price),
                str(intent.leg.quantity),
                intent.state.value,
                intent.server_order_id,
            ),
        )

    def update_intent(self, intent: OrderIntent) -> None:
        changed = self.connection.execute(
            """UPDATE order_intent SET state=?,server_order_id=?,updated_at=datetime('now')
               WHERE client_order_id=?""",
            (intent.state.value, intent.server_order_id, intent.client_order_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("intent update did not affect exactly one durable row")

    def unresolved_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT client_order_id FROM order_intent WHERE state='sent_unknown' ORDER BY client_order_id"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def nonterminal_sent_ids(self) -> tuple[str, ...]:
        return tuple(str(row[0]) for row in self.connection.execute(
            "SELECT client_order_id FROM order_intent WHERE state IN ('sent_unknown','acknowledged')"
        ))

    def reconcile_working_intent(self, client_order_id: str, order_id: str) -> None:
        if not order_id:
            raise ValueError("working reconciliation requires order identity")
        self.connection.execute(
            "UPDATE order_intent SET state='acknowledged',server_order_id=?,updated_at=datetime('now') WHERE client_order_id=? AND state IN ('sent_unknown','acknowledged')",
            (order_id, client_order_id),
        )

    def knows_client_order_id(self, client_order_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM order_intent WHERE client_order_id=?", (client_order_id,)
        ).fetchone() is not None

    def resolve_intent_terminal(self, client_order_id: str, server_order_id: str | None) -> None:
        changed = self.connection.execute(
            """UPDATE order_intent SET state='terminal',server_order_id=COALESCE(?,server_order_id),
                      updated_at=datetime('now')
               WHERE client_order_id=? AND state IN ('sent_unknown','acknowledged')""",
            (server_order_id, client_order_id),
        ).rowcount
        if changed not in {0, 1}:
            raise RuntimeError("intent reconciliation affected multiple rows")

    def persist_deadline(
        self,
        symbol: str,
        outcome: str,
        opened_at: datetime,
        deadline: datetime,
        quantity: Decimal,
    ) -> None:
        existing = self.connection.execute(
            "SELECT deadline FROM position_deadline WHERE symbol=? AND outcome=?",
            (symbol, outcome),
        ).fetchone()
        effective = deadline
        if existing is not None:
            old = datetime.fromisoformat(str(existing[0]))
            effective = min(old, deadline)
        self.connection.execute(
            """INSERT INTO position_deadline(symbol,outcome,opened_at,deadline,quantity)
               VALUES(?,?,?,?,?) ON CONFLICT(symbol,outcome) DO UPDATE SET
               opened_at=MIN(position_deadline.opened_at,excluded.opened_at),
               deadline=excluded.deadline,quantity=excluded.quantity""",
            (symbol, outcome, opened_at.isoformat(), effective.isoformat(), str(quantity)),
        )

    def get_deadline(self, symbol: str, outcome: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT deadline FROM position_deadline WHERE symbol=? AND outcome=?",
            (symbol, outcome),
        ).fetchone()
        return None if row is None else datetime.fromisoformat(str(row[0]))

    def clear_deadline(self, symbol: str, outcome: str) -> None:
        self.connection.execute(
            "DELETE FROM position_deadline WHERE symbol=? AND outcome=?", (symbol, outcome)
        )

    def audit(self, kind: str, details: dict[str, object]) -> None:
        self.connection.execute(
            "INSERT INTO audit_event(occurred_at,kind,details_json) VALUES(datetime('now'),?,?)",
            (kind, json.dumps(details, separators=(",", ":"), sort_keys=True)),
        )

    def replace_account_snapshot(
        self,
        positions: list[dict[str, str]],
        orders: list[dict[str, str]],
        *,
        observed_at: datetime,
    ) -> None:
        """Atomically replace reconciled account truth."""
        if observed_at.tzinfo is None:
            raise ValueError("snapshot timestamp must be timezone-aware")
        with self.transaction():
            self.connection.execute("DELETE FROM account_position")
            self.connection.execute("DELETE FROM account_order")
            self.connection.executemany(
                """INSERT INTO account_position
                   (symbol,outcome,signed_quantity,average_price,market_value,observed_at)
                   VALUES(?,?,?,?,?,?)""",
                [
                    (
                        row["symbol"], row["outcome"], row["signed_quantity"],
                        row["average_price"], row["market_value"], observed_at.isoformat(),
                    )
                    for row in positions
                ],
            )
            self.connection.executemany(
                """INSERT INTO account_order
                   (order_id,client_order_id,symbol,side,outcome,price,original_quantity,
                    remaining_quantity,status,observed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        row["order_id"], row["client_order_id"], row["symbol"], row["side"],
                        row["outcome"], row["price"], row["original_quantity"],
                        row["remaining_quantity"], row["status"], observed_at.isoformat(),
                    )
                    for row in orders
                ],
            )

    def load_position_quantities(self) -> dict[str, Decimal]:
        return {
            str(row[0]): Decimal(str(row[1]))
            for row in self.connection.execute(
                "SELECT symbol,signed_quantity FROM account_position ORDER BY symbol"
            )
        }

    def record_fill(
        self,
        *,
        trade_id: str,
        order_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        outcome: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        event_at: datetime,
    ) -> bool:
        if not trade_id or event_at.tzinfo is None:
            raise ValueError("fill identity and timezone-aware timestamp are required")
        changed = self.connection.execute(
            """INSERT OR IGNORE INTO execution_fill
               (trade_id,order_id,client_order_id,symbol,side,outcome,quantity,price,fee,event_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id, order_id, client_order_id, symbol, side, outcome,
                str(quantity), str(price), str(fee), event_at.isoformat(),
            ),
        ).rowcount
        return changed == 1

    def load_risk_window(self, window_start: datetime) -> dict[str, object] | None:
        row = self.connection.execute(
            """SELECT window_end,opening_equity,loss_high_water,pending_order_cap,
                      classification_complete,updated_at
               FROM risk_window_state WHERE window_start=?""",
            (window_start.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        return {
            "window_start": window_start,
            "window_end": datetime.fromisoformat(str(row[0])),
            "opening_equity": Decimal(str(row[1])),
            "loss_high_water": Decimal(str(row[2])),
            "pending_order_cap": int(row[3]),
            "classification_complete": bool(row[4]),
            "updated_at": datetime.fromisoformat(str(row[5])),
        }

    def upsert_risk_window(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        opening_equity: Decimal,
        loss_high_water: Decimal,
        pending_order_cap: int,
        classification_complete: bool,
        updated_at: datetime,
    ) -> None:
        if any(value.tzinfo is None for value in (window_start, window_end, updated_at)):
            raise ValueError("risk-window timestamps must be timezone-aware")
        self.connection.execute(
            """INSERT INTO risk_window_state
               (window_start,window_end,opening_equity,loss_high_water,pending_order_cap,
                classification_complete,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(window_start) DO UPDATE SET
                 loss_high_water=excluded.loss_high_water,
                 pending_order_cap=excluded.pending_order_cap,
                 classification_complete=excluded.classification_complete,
                 updated_at=excluded.updated_at""",
            (
                window_start.isoformat(), window_end.isoformat(), str(opening_equity),
                str(loss_high_water), pending_order_cap, int(classification_complete),
                updated_at.isoformat(),
            ),
        )

    def set_runtime_state(self, key: str, value: str, *, updated_at: datetime) -> None:
        if not key or updated_at.tzinfo is None:
            raise ValueError("runtime state requires a key and timezone-aware timestamp")
        self.connection.execute(
            """INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, value, updated_at.isoformat()),
        )

    def get_runtime_state(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM runtime_state WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def sample_reward_uptime(
        self,
        *,
        event_ticker: str,
        pool_id: str,
        window_start: datetime,
        window_end: datetime,
        sample_at: datetime,
        eligible: bool,
        qualifying: bool,
    ) -> None:
        if not event_ticker or not pool_id or any(value.tzinfo is None for value in (window_start, window_end, sample_at)):
            raise ValueError("uptime sample identity and aware timestamps are required")
        if qualifying and not eligible:
            raise ValueError("a qualifying sample must be eligible")
        bucket = sample_at.replace(second=0, microsecond=0).isoformat()
        with self.transaction():
            row = self.connection.execute(
                """SELECT eligible_samples,qualifying_samples,first_observed_at,last_sample_bucket
                   FROM reward_uptime_v2
                   WHERE event_ticker=? AND pool_id=? AND window_start=? AND window_end=?""",
                (event_ticker, pool_id, window_start.isoformat(), window_end.isoformat()),
            ).fetchone()
            if row is not None and str(row[3]) >= bucket:
                return
            # Unknown/unobserved minutes never improve this conservative proxy.
            elapsed = max(0, int((min(sample_at, window_end) - window_start).total_seconds() // 60) + 1)
            eligible_count = max(int(row[0]) if row else 0, elapsed)
            qualifying_count = (int(row[1]) if row else 0) + int(qualifying)
            first_observed_at = str(row[2]) if row else sample_at.isoformat()
            self.connection.execute(
                """INSERT INTO reward_uptime_v2
                   (event_ticker,pool_id,window_start,window_end,eligible_samples,qualifying_samples,
                    first_observed_at,last_sample_bucket,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_ticker,pool_id,window_start,window_end) DO UPDATE SET
                     eligible_samples=excluded.eligible_samples,
                     qualifying_samples=excluded.qualifying_samples,
                     last_sample_bucket=excluded.last_sample_bucket,
                     updated_at=excluded.updated_at""",
                (
                    event_ticker, pool_id, window_start.isoformat(), window_end.isoformat(),
                    eligible_count, qualifying_count, first_observed_at,
                    bucket, sample_at.isoformat(),
                ),
            )

    def load_reward_uptime(
        self, event_ticker: str, pool_id: str, window_start: datetime, window_end: datetime
    ) -> tuple[int, int, datetime | None]:
        row = self.connection.execute(
            """SELECT eligible_samples,qualifying_samples,first_observed_at
               FROM reward_uptime_v2
               WHERE event_ticker=? AND pool_id=? AND window_start=? AND window_end=?""",
            (event_ticker, pool_id, window_start.isoformat(), window_end.isoformat()),
        ).fetchone()
        return (0, 0, None) if row is None else (
            int(row[0]), int(row[1]), datetime.fromisoformat(str(row[2])),
        )

    def quick_check(self) -> str:
        return str(self.connection.execute("PRAGMA quick_check").fetchone()[0])


def shared_control_directory() -> Path:
    """Host-wide authority, independent of output folder and API-key rotation."""
    return Path(os.environ["LOCALAPPDATA"]) / "GeminiRewardBot" / "control"


class WindowsOwnerLock(AbstractContextManager["WindowsOwnerLock"]):
    """Non-blocking one-process lock using native Windows byte locking."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("owner lock already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise RuntimeError("another Gemini mutation process owns the lock") from exc
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        import msvcrt

        self._file.seek(0)
        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None

    def __enter__(self) -> WindowsOwnerLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
