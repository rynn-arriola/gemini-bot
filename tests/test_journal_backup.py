"""Offline backup failures/races; all file mutation is confined to temporary dirs."""
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from gemini_bot.journal_backup import create_backup, verify_journal, JournalBackups
from gemini_bot.storage import SQLiteJournal
import gemini_bot.journal_backup as backups


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "production.sqlite"
        self.writer = sqlite3.connect(self.path, isolation_level=None)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, value TEXT)")
        self.writer.execute("INSERT INTO history VALUES (1, 'committed in WAL')")

    def tearDown(self):
        self.writer.close()
        self.tmp.cleanup()

    def snapshots(self):
        return backups._managed(self.path.parent / "backups")

    def test_snapshot_contains_committed_wal_and_is_standalone(self):
        result = create_backup(self.path)
        snapshot, = self.snapshots()
        with closing(sqlite3.connect(snapshot)) as copy:
            self.assertEqual(copy.execute("SELECT * FROM history").fetchall(), [(1, "committed in WAL")])
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone(), ("delete",))
        self.assertEqual(result["sha256"], hashlib.sha256(snapshot.read_bytes()).hexdigest())
        self.assertEqual(result["snapshot_bytes"], snapshot.stat().st_size)
        self.assertEqual(result["removed_snapshots"], 0)
        self.assertFalse(list(snapshot.parent.glob("*.partial")))

    def test_uncommitted_writes_excluded_without_blocking_writer(self):
        self.writer.execute("BEGIN IMMEDIATE")
        self.writer.execute("INSERT INTO history VALUES (2, 'uncommitted')")
        create_backup(self.path)
        with closing(sqlite3.connect(self.snapshots()[0])) as copy:
            self.assertEqual(copy.execute("SELECT count(*) FROM history").fetchone()[0], 1)
        self.writer.execute("COMMIT")
        self.assertEqual(self.writer.execute("SELECT count(*) FROM history").fetchone()[0], 2)

    def test_all_production_tables_preserved_including_unknown_intents_and_hard(self):
        journal_path = Path(self.tmp.name) / "ledger.sqlite"
        journal = SQLiteJournal(journal_path)
        try:
            journal.connection.execute("INSERT INTO runtime_state VALUES ('permit','consumed','now')")
            journal.connection.execute("INSERT INTO order_intent VALUES ('c','X','YES','BUY','entry','.4','15','sent_unknown',NULL,'now')")
            journal.connection.execute("INSERT INTO realized_risk_window_state VALUES ('start','end','-30','30',0,1,'now')")
            create_backup(journal_path)
            with closing(sqlite3.connect(self.snapshots()[0])) as copy:
                tables = [r[0] for r in journal.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    self.assertEqual(journal.connection.execute(f'SELECT * FROM "{table}"').fetchall(),
                                     copy.execute(f'SELECT * FROM "{table}"').fetchall())
        finally:
            journal.close()

    def test_rotation_only_removes_managed_files_after_verified_replacement(self):
        with patch.object(backups, "RETAIN", 2):
            create_backup(self.path)
            first = self.snapshots()[0]
            manual = first.parent / "manual-incident.sqlite"
            manual.write_bytes(b"retain evidence")
            create_backup(self.path)
            result = create_backup(self.path)
            self.assertEqual(len(self.snapshots()), 2)
            self.assertFalse(first.exists())
            self.assertEqual(manual.read_bytes(), b"retain evidence")
            self.assertEqual(result["removed_snapshots"], 1)

    def test_failed_verification_never_rotates_or_publishes(self):
        create_backup(self.path)
        previous = self.snapshots()
        with patch.object(backups, "_check", side_effect=RuntimeError("secret fixture detail")):
            with self.assertRaises(RuntimeError):
                create_backup(self.path)
        self.assertEqual(self.snapshots(), previous)
        self.assertFalse(list(previous[0].parent.glob("*.partial")))

    def test_rename_failure_preserves_prior_good_snapshot(self):
        create_backup(self.path)
        previous = self.snapshots()
        with patch.object(Path, "rename", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                create_backup(self.path)
        self.assertEqual(self.snapshots(), previous)
        self.assertFalse(list(previous[0].parent.glob("*.partial")))

    def test_low_space_does_not_delete_backups_to_make_room(self):
        create_backup(self.path)
        previous = self.snapshots()
        with patch.object(backups.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError):
                create_backup(self.path)
        self.assertEqual(self.snapshots(), previous)

    def test_size_limit_preserves_source(self):
        with patch.object(backups, "MAX_BYTES", 1):
            with self.assertRaisesRegex(RuntimeError, "size budget"):
                create_backup(self.path)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.writer.execute("SELECT count(*) FROM history").fetchone()[0], 1)

    def test_missing_source_not_created(self):
        missing = self.path.parent / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            create_backup(missing)
        with self.assertRaises(sqlite3.OperationalError):
            verify_journal(missing)
        self.assertFalse(missing.exists())

    def test_malformed_source_preserved_no_restore(self):
        bad = self.path.parent / "bad.sqlite"
        bad.write_bytes(b"not a sqlite database")
        before = bad.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            verify_journal(bad)
        with self.assertRaises(sqlite3.DatabaseError):
            create_backup(bad)
        self.assertEqual(bad.read_bytes(), before)
        self.assertEqual(self.snapshots(), [])

    def test_cooperative_stop_cleans_its_temporary(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(TimeoutError):
            create_backup(self.path, stop)
        self.assertEqual(list((self.path.parent / "backups").iterdir()), [])

    def test_timeout_does_not_publish(self):
        with patch.object(backups, "TIMEOUT_SECONDS", 0):
            with self.assertRaises(TimeoutError):
                create_backup(self.path)
        self.assertEqual(self.snapshots(), [])

    def test_interrupted_file_budget_refuses_unbounded_growth(self):
        directory = self.path.parent / "backups"
        directory.mkdir()
        for i in range(backups.RETAIN + 5):
            (directory / f"interrupted-{i}.partial").touch()
        with self.assertRaisesRegex(RuntimeError, "retention review"):
            create_backup(self.path)
        self.assertEqual(len(list(directory.iterdir())), backups.RETAIN + 5)

    def test_readonly_integrity_check_leaves_data_unchanged(self):
        before = self.writer.execute("SELECT * FROM history").fetchall()
        verify_journal(self.path)
        self.assertEqual(self.writer.execute("SELECT * FROM history").fetchall(), before)

    def test_empty_existing_authority_does_not_become_a_blank_new_ledger(self):
        empty = self.path.parent / "empty.sqlite"
        empty.touch()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            verify_journal(empty, required_tables=frozenset({"order_intent", "execution_fill"}))
        self.assertEqual(empty.stat().st_size, 0)

    def test_rotation_failure_reported_and_stops_unbounded_growth(self):
        with patch.object(backups, "RETAIN", 1):
            create_backup(self.path)
            old = self.snapshots()[0]
            original = Path.unlink
            def refuse_old(path, *args, **kwargs):
                if path == old:
                    raise PermissionError("private")
                return original(path, *args, **kwargs)
            with patch.object(Path, "unlink", refuse_old):
                report = create_backup(self.path)
            self.assertEqual(report["rotation_error_type"], "PermissionError")
            self.assertEqual(len(self.snapshots()), 2)

    def test_clock_rollback_never_rotates_the_new_verified_snapshot(self):
        with patch.object(backups, "RETAIN", 1):
            create_backup(self.path)
            with patch.object(backups, "datetime") as clock:
                clock.now.return_value.strftime.return_value = "20000101T000000000000Z"
                clock.now.return_value.isoformat.return_value = "2000-01-01T00:00:00+00:00"
                create_backup(self.path)
            self.assertEqual(len(self.snapshots()), 1)
            self.assertIn("20000101", self.snapshots()[0].name)


class BackupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_offloop_io_and_close_joins_worker(self):
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        entered = threading.Event()
        finished = threading.Event()
        def worker(path, stop):
            entered.set()
            stop.wait(2)
            time.sleep(.02)
            finished.set()
            raise TimeoutError("private detail")
        service = JournalBackups(Path("unused"))
        with patch.object(backups, "create_backup", side_effect=worker):
            service.start()
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            self.assertTrue(service.report()["in_progress"])
            await service.close()
            self.assertTrue(finished.is_set())
            await service.close()  # Idempotent, no additional worker.
            self.assertEqual(errors, [])

    async def test_redacted_failure_retries_without_mutation_authority(self):
        service = JournalBackups(Path("unused"))
        with patch.object(backups, "INTERVAL_SECONDS", .01), patch.object(
                backups, "create_backup", side_effect=[PermissionError("PRIVATE"), {"last_success_at": "now"}]) as call:
            service.start()
            for _ in range(100):
                if service.state["error_type"]:
                    break
                await asyncio.sleep(.001)
            self.assertEqual(service.report()["error_type"], "PermissionError")
            self.assertNotIn("PRIVATE", json.dumps(service.report()))
            for _ in range(100):
                if service.state["last_success_at"]:
                    break
                await asyncio.sleep(.001)
            await service.close()
            self.assertGreaterEqual(call.call_count, 2)
            self.assertIsNone(service.report()["error_type"])
            self.assertFalse(service.report()["automatic_restore"])

    async def test_duplicate_start_and_start_after_close_rejected(self):
        service = JournalBackups(Path("unused"))
        with patch.object(backups, "create_backup", return_value={}):
            service.start()
            with self.assertRaises(RuntimeError):
                service.start()
            await service.close()
            with self.assertRaises(RuntimeError):
                service.start()


class StartupIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_authority_blocks_before_network_schema_or_credential_writes(self):
        from gemini_bot.config import LiveConfig, load_policy
        from gemini_bot.orchestrator import ProductionOrchestrator
        from gemini_bot.storage import WindowsOwnerLock
        with tempfile.TemporaryDirectory() as tmp:
            strategy, risk = load_policy()
            runtime = ProductionOrchestrator(
                LiveConfig("fake", "fake", "", True, strategy, risk), Path(tmp) / "run", read_only=True)
            control = Path(tmp) / "control"
            control.mkdir()
            source = control / "production.sqlite"
            source.write_bytes(b"malformed authority must be preserved")
            runtime.control_directory = control
            runtime.lock = WindowsOwnerLock(control / "mutation-owner.lock")
            runtime.rest.terms_status = AsyncMock()
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    await runtime.prepare()
                runtime.rest.terms_status.assert_not_called()
                self.assertIsNone(runtime._journal_backups)
                self.assertEqual(source.read_bytes(), b"malformed authority must be preserved")
            finally:
                await runtime.close()
            with WindowsOwnerLock(control / "mutation-owner.lock"):
                pass  # Failed startup must not strand process ownership.
