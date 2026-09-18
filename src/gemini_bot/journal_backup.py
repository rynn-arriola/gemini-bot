"""Verified, bounded local snapshots. Never restore or modify source authority."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid


MAX_BYTES = 128 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
RETAIN = 24
INTERVAL_SECONDS = 3600
TIMEOUT_SECONDS = 15
_NAME = re.compile(r"auto-v1-\d{8}T\d{12}Z-[0-9a-f]{32}\.sqlite\Z")


def _read(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                           timeout=0.1, isolation_level=None)


def _check(connection, deadline: float, stop: threading.Event) -> None:
    connection.set_progress_handler(
        lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("journal integrity verification failed")
    finally:
        connection.set_progress_handler(None, 0)
    if stop.is_set() or time.monotonic() >= deadline:
        raise TimeoutError("journal verification interrupted")


def verify_journal(path: Path, *, required_tables: frozenset[str] = frozenset()) -> None:
    """Check existing authority read-only before any schema/account write."""
    with closing(_read(path)) as source:
        _check(source, time.monotonic() + TIMEOUT_SECONDS, threading.Event())
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required_tables <= tables:
            raise RuntimeError("existing control journal is incomplete; explicit recovery required")


def _managed(directory: Path) -> list[Path]:
    # No globs outside this dedicated directory; no links/junction traversal.
    result = []
    for path in directory.iterdir():
        if (_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink()
                and path.resolve().parent == directory):
            result.append(path)
    return sorted(result)


def create_backup(source_path: Path, stop: threading.Event | None = None) -> dict:
    """Caller holds the production owner lock. One private read connection only.

    SQLite's backup API includes committed WAL data. A live snapshot may contain
    nonterminal intents and is NOT permission to restore, replay, or activate.
    """
    stop = stop or threading.Event()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    source_path = source_path.resolve(strict=True)
    directory = source_path.parent / "backups"
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve() != directory:
        raise RuntimeError("backup directory must not be redirected")
    directory = directory.resolve()
    # Failed deletions/interrupted runs must not accumulate unlimited new files.
    if len(list(directory.iterdir())) > RETAIN + 4:
        raise RuntimeError("backup directory needs retention review")
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES + MAX_BYTES:
        raise OSError("insufficient backup headroom")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"auto-v1-{stamp}-{uuid.uuid4().hex}.sqlite"
    temporary = final.with_suffix(".partial")
    temporary.touch(exist_ok=False)
    try:
        with closing(_read(source_path)) as source, closing(sqlite3.connect(temporary)) as target:
            page_size = source.execute("PRAGMA page_size").fetchone()[0]
            if source.execute("PRAGMA page_count").fetchone()[0] * page_size > MAX_BYTES:
                raise RuntimeError("backup size budget exceeded")

            def progress(status, remaining, total):
                if stop.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("backup interrupted")
                if total * page_size > MAX_BYTES:
                    raise RuntimeError("backup size budget exceeded")

            source.backup(target, pages=128, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
            _check(target, deadline, stop)
        # Reopen independently after closing all writer handles (required on Windows).
        with closing(_read(temporary)) as verification:
            _check(verification, deadline, stop)
        digest = hashlib.sha256()
        with temporary.open("rb+") as handle:
            while block := handle.read(1024 * 1024):
                if stop.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("backup interrupted")
                digest.update(block)
            os.fsync(handle.fileno())
        if stop.is_set():
            raise TimeoutError("backup interrupted")
        # Unique target; Windows rename refuses an existing file.
        temporary.rename(final)
        removed = 0
        rotation_error = None
        try:
            previous = [p for p in _managed(directory) if p != final]
            for old in previous[:max(0, len(previous) - RETAIN + 1)]:
                # Resolve again immediately before deletion; never recurse.
                if old.resolve().parent != directory or old.is_symlink():
                    raise RuntimeError("unsafe backup retention target")
                old.unlink()
                removed += 1
        except Exception as exc:
            rotation_error = type(exc).__name__
        return {"last_success_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_bytes": final.stat().st_size, "sha256": digest.hexdigest(),
                "removed_snapshots": removed, "rotation_error_type": rotation_error}
    finally:
        # Only our new temporary, never existing backups or production companions.
        temporary.unlink(missing_ok=True)


def _attempt_backup(source: Path, stop: threading.Event) -> dict:
    # Catch inside the thread: Python's shield may log a late exception after
    # cancellation, including its raw text, even if close subsequently joins it.
    try:
        return {**create_backup(source, stop), "error_type": None}
    except Exception as exc:
        return {"error_type": type(exc).__name__}


class JournalBackups:
    """One bounded I/O worker, hourly; no API/risk/account/mutation dependencies."""
    def __init__(self, source: Path):
        self.source = source
        self.stop = threading.Event()
        self.task: asyncio.Task | None = None
        self.worker: asyncio.Task | None = None
        self.state: dict = {"last_success_at": None, "error_type": None}

    def start(self) -> None:
        if self.task is not None or self.stop.is_set():
            raise RuntimeError("backup service already started or closed")
        self.task = asyncio.create_task(self._run(), name="gemini-journal-backups")

    async def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self.worker = asyncio.create_task(asyncio.to_thread(_attempt_backup, self.source, self.stop))
                result = await asyncio.shield(self.worker)
                self.state = {**self.state, **result}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redacted health only. Backup failure never grants/clears trading permission.
                self.state = {**self.state, "error_type": type(exc).__name__}
            await asyncio.sleep(INTERVAL_SECONDS)

    def report(self) -> dict:
        return {**self.state, "interval_seconds": INTERVAL_SECONDS,
                "retained_limit": RETAIN, "automatic_restore": False,
                "in_progress": self.worker is not None and not self.worker.done()}

    async def close(self) -> None:
        self.stop.set()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # Canceling to_thread does not stop its thread. Join the cooperative worker
        # before the orchestrator releases its owner lock or closes authority.
        if self.worker is not None:
            await asyncio.gather(self.worker, return_exceptions=True)
