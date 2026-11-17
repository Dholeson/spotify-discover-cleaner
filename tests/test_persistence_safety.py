"""Persistence safety regressions (from the migration safety review).

Temp databases only: migrations (one backup per run, index-independent Last.fm dedup), the
ledger rebuild's failure backoff, and the event writer's retry on a busy database.
"""

import contextlib
import importlib
import io
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = 1_746_700_000_000


def _mods():
    return (importlib.import_module("src.persistence.db"), importlib.import_module("src.persistence.plays"),
            importlib.import_module("src.persistence.events"))


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.db, self.plays, self.events = _mods()
        self._orig_path = self.db.DB_PATH
        self.tmp = tempfile.mkdtemp(prefix="persist_safety_")
        self.db.DB_PATH = Path(self.tmp) / "music_explorer.db"
        self.db.MIGRATION_STATUS.update(version=None, applied=[], error=None, backups=[])

    def tearDown(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
        self.db._local.conn = None
        self.db.DB_PATH = self._orig_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def legacy(self, rows, user_version=0, original_indexes=True):
        c = sqlite3.connect(str(self.db.DB_PATH))
        c.executescript(self.db._SCHEMA)
        if original_indexes:
            # The indexes the original app created; with them a correlated dedup query was O(n^2).
            c.execute("CREATE INDEX IF NOT EXISTS events_source ON events(source)")
            c.execute("CREATE INDEX IF NOT EXISTS events_type ON events(event_type)")
        for ts, payload in rows:
            c.execute("INSERT INTO events (event_type, ts_ms, day, payload, source) "
                      "VALUES ('SONG_PLAYED', ?, '2025-05-08', ?, 'lastfm')", (ts, json.dumps(payload)))
        c.execute(f"PRAGMA user_version = {int(user_version)}")
        c.commit()
        c.close()

    def init(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.db.init_db()
        return self.db.get_conn()


class MigrationSafetyTests(TempDbCase):
    def test_v2_dedup_keeps_the_oldest_row_and_never_merges_null_keys(self):
        a = {"artists": "A", "track_name": "T", "album": ""}
        rows = [(BASE, a), (BASE, a), (BASE, dict(a, album="x")),          # ids 1-3: one scrobble, 3 copies
                (BASE, {"artists": "A", "track_name": "U"}),              # 4: same second, other track
                (BASE + 1000, a),                                         # 5: other second
                (BASE, {"track_name": "T"}), (BASE, {"track_name": "T"})]  # 6, 7: no artist (never merged)
        self.legacy(rows)
        conn = self.init()
        self.assertEqual(self.db.MIGRATION_STATUS["error"], None)
        ids = [r[0] for r in conn.execute("SELECT id FROM events WHERE source = 'lastfm' ORDER BY id")]
        self.assertEqual(ids, [1, 4, 5, 6, 7])

    def test_v2_dedup_is_fast_with_the_original_indexes(self):
        payloads = [{"artists": f"Artist {i % 300}", "track_name": f"Song {i}"} for i in range(20000)]
        rows = [(BASE + i * 1000, p) for i, p in enumerate(payloads)] + [(BASE, payloads[0])]
        self.legacy(rows)
        t0 = time.perf_counter()
        conn = self.init()
        self.assertLess(time.perf_counter() - t0, 20)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE source = 'lastfm'").fetchone()[0], 20000)
        # the dedup step itself, run against a table that still has the old index, stays fast
        conn.execute("CREATE INDEX IF NOT EXISTS events_source ON events(source)")
        where = self._dup_where()
        t0 = time.perf_counter()
        conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}").fetchone()
        self.assertLess(time.perf_counter() - t0, 5)

    def _dup_where(self):
        captured = {}

        class Spy:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *a):
                if "NOT IN (SELECT MIN(id)" in sql and sql.startswith("SELECT COUNT(*)"):
                    captured["where"] = sql.split("WHERE", 1)[1]
                return self.conn.execute(sql, *a)
        mem = sqlite3.connect(":memory:")
        mem.executescript(self.db._SCHEMA)
        with contextlib.redirect_stdout(io.StringIO()):
            self.db._migrate_v2(Spy(mem))
        mem.close()
        return captured["where"]

    def test_one_backup_per_run(self):
        self.legacy([(BASE, {"artists": "A", "track_name": "T"})])
        self.init()
        self.assertEqual(self.db.list_backups(), ["music_explorer.db.bak-v2"])

    def test_schema_only_steps_take_no_backup(self):
        self.legacy([(BASE, {"artists": "A", "track_name": "T"})], user_version=2, original_indexes=False)
        conn = self.init()
        self.assertEqual(self.db.schema_version(conn), self.db.SCHEMA_VERSION)
        self.assertEqual(self.db.list_backups(), [])

    def test_connections_cap_the_wal_file(self):
        conn = self.init()
        self.assertEqual(conn.execute("PRAGMA journal_size_limit").fetchone()[0], 16 * 1024 * 1024)


class RebuildBackoffTests(TempDbCase):
    def setUp(self):
        super().setUp()
        self.conn = self.init()
        self.addCleanup(self.plays._note_rebuild_success)

    def test_failed_rebuild_is_not_retried_by_every_request(self):
        plays = self.plays
        with mock.patch.object(plays, "_rebuild", side_effect=RuntimeError("bad row")):
            with self.assertRaises(RuntimeError):
                plays.rebuild(self.conn)
        self.assertGreater(plays.rebuild_backoff_remaining_s(), 4 * 60)
        with mock.patch.object(plays.threading, "Thread") as thread:
            self.assertFalse(plays.start_background_rebuild())
            self.assertFalse(plays.ensure_plays_ready(self.conn))
            thread.assert_not_called()
        st = plays.status(self.conn)
        self.assertEqual(st["rebuild_error"], "bad row")
        self.assertIsNotNone(st["rebuild_retry_at"])
        # the backoff doubles, capped at an hour
        with mock.patch.object(plays, "_rebuild", side_effect=RuntimeError("again")):
            with self.assertRaises(RuntimeError):
                plays.rebuild(self.conn)
        self.assertGreater(plays.rebuild_backoff_remaining_s(), 9 * 60)
        # a successful rebuild clears it
        plays.rebuild(self.conn)
        self.assertEqual(plays.rebuild_backoff_remaining_s(), 0)
        self.assertIsNone(plays.status(self.conn)["rebuild_error"])

    def test_checkpoint_does_not_wait_the_full_busy_timeout(self):
        plays = self.plays
        self.conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        self.conn.commit()
        reader = sqlite3.connect(str(self.db.DB_PATH))
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM events").fetchone()     # an open read snapshot
        self.conn.execute("INSERT INTO t VALUES (1)")
        self.conn.commit()
        t0 = time.perf_counter()
        plays._checkpoint_quietly(self.conn)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        reader.rollback()


class EventWriteRetryTests(unittest.TestCase):
    def test_busy_database_is_retried_once(self):
        _, _, events = _mods()
        calls = []

        class Conn:
            def execute(self, sql):
                calls.append(sql)
                if len(calls) == 1:
                    raise sqlite3.OperationalError("database is locked")
        events._begin_immediate(Conn())
        self.assertEqual(calls, ["BEGIN IMMEDIATE", "BEGIN IMMEDIATE"])

    def test_other_errors_and_a_second_lock_are_raised(self):
        _, _, events = _mods()

        class Locked:
            def execute(self, sql):
                raise sqlite3.OperationalError("database is locked")

        class Broken:
            def execute(self, sql):
                raise sqlite3.OperationalError("disk I/O error")
        with self.assertRaises(sqlite3.OperationalError):
            events._begin_immediate(Locked())
        with self.assertRaises(sqlite3.OperationalError):
            events._begin_immediate(Broken())


if __name__ == "__main__":
    unittest.main()
