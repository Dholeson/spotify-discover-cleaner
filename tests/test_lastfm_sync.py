"""Last.fm sync: forward cursor from the data, per-page retries, backfill flag, unique key,
local days, plays-ledger feed, and the data API routes. Last.fm itself is a fake that
serves user.getRecentTracks pages from an in-memory list; every test uses a temp database."""

import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src.persistence import db, plays
from src.services import importers


def _use_current_modules():
    """Rebind db/plays/events/importers to the modules runtime imports resolve to now.

    tests/platform_harness.py swaps every src.* module for a temp copy partway through a
    full run; code under test imports lazily (``from src.persistence import db``), so the
    test must point the same module objects at its temp database."""
    global db, plays, importers
    db = importlib.import_module("src.persistence.db")
    plays = importlib.import_module("src.persistence.plays")
    importers = importlib.import_module("src.services.importers")

BASE_S = int(datetime(2026, 5, 12, 21, 0, 0).timestamp())  # local evening: the UTC date is the next day west of UTC


class FakeLastfm:
    """user.getRecentTracks over a list of (uts, artist, title, album), newest first."""

    def __init__(self, scrobbles, page_size=3):
        self.scrobbles = list(scrobbles)
        self.page_size = page_size
        self.calls = []
        self.fail = {}            # page -> remaining failures (-1 = always)
        self.error_body = None    # e.g. {"error": 10, "message": "Invalid API key"}
        self.now_playing = None   # (artist, title) shown on page 1 without a date

    def __call__(self, method, user=None, limit=200, page=1, **params):
        assert method == "user.getRecentTracks"
        self.calls.append(dict(params, page=page, limit=limit, user=user))
        if self.error_body is not None:
            return dict(self.error_body)
        left = self.fail.get(page, 0)
        if left:
            self.fail[page] = left - 1 if left > 0 else -1
            return None
        lo = int(params.get("from", 0))
        hi = int(params.get("to", 2 ** 40))
        rows = sorted((s for s in self.scrobbles if lo <= s[0] <= hi), key=lambda s: -s[0])
        size = self.page_size  # the fake ignores `limit` so tests stay small
        total_pages = (len(rows) + size - 1) // size
        chunk = rows[(page - 1) * size: page * size]
        tracks = [{"artist": {"#text": a}, "name": t, "album": {"#text": al}, "date": {"uts": str(uts)}}
                  for uts, a, t, al in chunk]
        if self.now_playing and page == 1:
            tracks.insert(0, {"artist": {"#text": self.now_playing[0]}, "name": self.now_playing[1],
                              "album": {"#text": ""}, "@attr": {"nowplaying": "true"}})
        return {"recenttracks": {"track": tracks, "@attr": {
            "page": str(page), "perPage": str(size), "totalPages": str(total_pages), "total": str(len(rows))}}}


def scrobbles(start_s, n, step_s=240, artist="Band", prefix="Song"):
    return [(start_s + i * step_s, artist, f"{prefix} {i}", "Album") for i in range(n)]


class SyncCase(unittest.TestCase):
    def setUp(self):
        _use_current_modules()
        self._orig_path = db.DB_PATH
        self.tmp = tempfile.mkdtemp(prefix="lfm_sync_test_")
        db.DB_PATH = Path(self.tmp) / "music_explorer.db"
        with contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        self.conn = db.get_conn()
        plays.rebuild(self.conn)  # an empty, ready ledger
        self.sleeps = []
        # Never reach the real app module (a real lastfm_call / shared error state).
        self._core_patch = mock.patch.object(importers, "_core", lambda: None)
        self._core_patch.start()

    def tearDown(self):
        self._core_patch.stop()
        try:
            self.conn.close()
        except Exception:
            pass
        db._local.conn = None
        db.DB_PATH = self._orig_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sync(self, fake, mode="sync", **kw):
        return importers.run_lastfm_sync(self.conn, mode, user="tester", call=fake, sleep=self.sleeps.append,
                                         now_s=BASE_S + 10 ** 6, **kw)

    def lastfm_rows(self):
        return self.conn.execute(
            "SELECT ts_ms, day, json_extract(payload, '$.artists'), json_extract(payload, '$.track_name'), "
            "json_extract(payload, '$.source') FROM events WHERE source = 'lastfm' ORDER BY ts_ms").fetchall()

    def max_ts(self):
        return self.conn.execute("SELECT MAX(ts_ms) FROM events WHERE source = 'lastfm'").fetchone()[0]

    def mark_backfill_done(self):
        self.conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, '1')", (importers.META_BACKFILL_DONE,))
        self.conn.commit()


class ForwardSyncTests(SyncCase):
    def test_forward_sync_sends_from_max_plus_one(self):
        old = scrobbles(BASE_S - 3600, 4, prefix="Old")
        fake = FakeLastfm(old)
        self.sync(fake)
        self.assertEqual(len(self.lastfm_rows()), 4)
        newest = self.max_ts()
        self.mark_backfill_done()

        fake.scrobbles += scrobbles(BASE_S, 5, prefix="New")
        fake.calls.clear()
        st = self.sync(fake)
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(st["inserted"], 5)
        self.assertEqual(fake.calls[0]["from"], newest // 1000 + 1)
        self.assertTrue(all(c.get("from") == newest // 1000 + 1 for c in fake.calls))
        self.assertEqual(self.max_ts(), (BASE_S + 4 * 240) * 1000)
        # backfill is finished, so no call walked older history
        self.assertFalse(any("from" not in c for c in fake.calls))

    def test_pages_are_stored_oldest_first(self):
        fake = FakeLastfm(scrobbles(BASE_S, 7))
        self.mark_backfill_done()
        self.sync(fake)
        self.assertEqual([c["page"] for c in fake.calls], [1, 3, 2])  # page 1 = probe, then 3 -> 1

    def test_rows_use_local_day_and_keep_source_marker(self):
        fake = FakeLastfm([(BASE_S, "Woe, Is Me", "Vengeance", "Number[s]")])
        self.sync(fake)
        ts_ms, day, artist, track, src = self.lastfm_rows()[0]
        self.assertEqual(day, datetime.fromtimestamp(BASE_S).strftime("%Y-%m-%d"))
        self.assertEqual((artist, track, src), ("Woe, Is Me", "Vengeance", "lastfm_import"))

    def test_now_playing_item_is_skipped(self):
        fake = FakeLastfm(scrobbles(BASE_S, 2))
        fake.now_playing = ("Band", "Playing Now")
        st = self.sync(fake)
        self.assertEqual(st["inserted"], 2)
        self.assertEqual(st["failed"], 0)

    def test_same_second_distinct_scrobbles_are_both_inserted(self):
        fake = FakeLastfm([(BASE_S, "A", "One", ""), (BASE_S, "A", "Two", ""), (BASE_S, "B", "One", "")])
        st = self.sync(fake)
        self.assertEqual(st["inserted"], 3)
        self.assertEqual(len(self.lastfm_rows()), 3)

    def test_rerun_inserts_nothing(self):
        fake = FakeLastfm(scrobbles(BASE_S, 8))
        first = self.sync(fake)
        self.assertEqual(first["inserted"], 8)
        again = self.sync(fake, mode="full")        # re-reads everything
        self.assertEqual((again["inserted"], again["duplicates"]), (0, 8))
        overlap = self.sync(fake, overlap_s=3600)   # a sync that re-reads the last hour
        self.assertEqual(overlap["inserted"], 0)
        self.assertGreater(overlap["duplicates"], 0)
        self.assertEqual(len(self.lastfm_rows()), 8)


class RetryTests(SyncCase):
    def test_failing_page_is_retried(self):
        fake = FakeLastfm(scrobbles(BASE_S, 7))
        self.mark_backfill_done()
        fake.fail[2] = 2
        st = self.sync(fake)
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(st["inserted"], 7)
        self.assertEqual([c["page"] for c in fake.calls].count(2), 3)
        self.assertEqual([s for s in self.sleeps if s >= 1], [1, 2])

    def test_page_that_keeps_failing_stops_without_advancing(self):
        data = scrobbles(BASE_S, 7)             # pages: 1 = newest 3, 2 = middle 3, 3 = oldest 1
        fake = FakeLastfm(data)
        self.mark_backfill_done()
        fake.fail[2] = -1
        st = self.sync(fake)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["failed_page"], 2)
        self.assertIn("page 2", st["error"])
        self.assertEqual([c["page"] for c in fake.calls].count(2), len(importers.LASTFM_RETRY_DELAYS) + 1)
        self.assertEqual([s for s in self.sleeps if s >= 1], list(importers.LASTFM_RETRY_DELAYS))
        # only the oldest page was stored: nothing newer than the failed page, so no gap
        self.assertEqual(self.max_ts(), data[0][0] * 1000)
        self.assertEqual(len(self.lastfm_rows()), 1)
        # the next run resumes right after the last stored scrobble and fills everything
        fake.fail.clear()
        fake.calls.clear()
        st = self.sync(fake)
        self.assertEqual(st["status"], "done")
        self.assertEqual(fake.calls[0]["from"], data[0][0] + 1)
        self.assertEqual(len(self.lastfm_rows()), 7)

    def test_permanent_error_is_not_retried(self):
        fake = FakeLastfm(scrobbles(BASE_S, 2))
        fake.error_body = {"error": 10, "message": "Invalid API key"}
        st = self.sync(fake)
        self.assertEqual(st["status"], "error")
        self.assertIn("API key", st["error"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_transport_exception_is_retried(self):
        calls = {"n": 0}
        inner = FakeLastfm(scrobbles(BASE_S, 2))

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("boom")
            return inner(*a, **k)
        self.mark_backfill_done()
        st = self.sync(flaky)
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(st["inserted"], 2)

    def test_missing_username_is_an_error(self):
        st = importers.run_lastfm_sync(self.conn, "sync", user="", call=FakeLastfm([]), sleep=self.sleeps.append)
        self.assertEqual(st["status"], "error")
        self.assertIn("username", st["error"])


class BackfillTests(SyncCase):
    def test_backfill_runs_until_done_then_never_again(self):
        old = scrobbles(BASE_S - 10 * 86400, 5, prefix="Old")
        recent = scrobbles(BASE_S, 2, prefix="Recent")
        fake = FakeLastfm(recent)
        self.sync(fake)                 # first run sees only the recent part (e.g. an old cursor)
        self.assertEqual(len(self.lastfm_rows()), 2)
        # one empty backfill answer may be a Last.fm glitch: only a second one finishes backfill
        self.assertIsNone(importers._meta_get(self.conn, importers.META_BACKFILL_DONE))
        self.sync(fake)
        self.assertIsNotNone(importers._meta_get(self.conn, importers.META_BACKFILL_DONE))

        # a fresh DB state where older history is still missing and the flag is unset
        self.conn.execute("DELETE FROM app_meta WHERE key = ?", (importers.META_BACKFILL_DONE,))
        self.conn.commit()
        fake.scrobbles = old + recent
        fake.calls.clear()
        st = self.sync(fake)
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(st["inserted"], 5)
        oldest_recent = recent[0][0]
        self.assertTrue(any(c.get("to") == oldest_recent - 1 and "from" not in c for c in fake.calls))
        self.assertIsNotNone(importers._meta_get(self.conn, importers.META_BACKFILL_DONE))

        fake.calls.clear()
        self.sync(fake)
        self.assertFalse(any("from" not in c for c in fake.calls))  # no backfill request any more

    def test_backfill_failure_keeps_flag_unset(self):
        fake = FakeLastfm(scrobbles(BASE_S - 86400, 7, prefix="Old") + scrobbles(BASE_S, 1))
        self.sync(fake, mode="sync")    # imports everything, sets the flag
        self.conn.execute("DELETE FROM app_meta WHERE key = ?", (importers.META_BACKFILL_DONE,))
        self.conn.execute("DELETE FROM events WHERE source = 'lastfm' AND ts_ms < ?", ((BASE_S - 86400 + 5 * 240) * 1000,))
        self.conn.commit()
        fake.fail[2] = -1
        st = self.sync(fake, mode="backfill")
        self.assertEqual(st["status"], "error")
        self.assertIsNone(importers._meta_get(self.conn, importers.META_BACKFILL_DONE))


class SafetyReviewTests(SyncCase):
    def test_sync_is_paused_while_the_unique_key_is_missing(self):
        # a database whose v2 migration didn't apply: INSERT OR IGNORE would ignore nothing
        self.conn.execute("DROP INDEX events_lastfm_uniq")
        self.conn.execute("PRAGMA user_version = 0")
        self.conn.commit()
        fake = FakeLastfm(scrobbles(BASE_S, 3))
        for _ in range(2):              # the overlap re-read that used to duplicate everything
            st = self.sync(fake, overlap_s=3 * 86400)
            self.assertEqual(st["status"], "error")
            self.assertIn("upgrade", st["error"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.lastfm_rows(), [])
        with mock.patch.object(importers, "lastfm_credentials", lambda: ("k", "u")):
            self.assertEqual(importers.start_lastfm_sync("sync"), (False, "upgrade_pending"))

    def test_corrected_scrobble_is_not_stored_twice(self):
        fake = FakeLastfm([(BASE_S, "dance gavin dance", "Purple Reign", "Mothership"),
                           (BASE_S + 300, "A", "One", ""), (BASE_S + 600, "A", "Two", "")])
        self.sync(fake)
        # Last.fm later corrects an artist and the user edits a title; a full re-read must not
        # store second copies, while a genuinely new scrobble in an old second still lands.
        fake.scrobbles = [(BASE_S, "Dance Gavin Dance", "Purple Reign", "Mothership"),
                          (BASE_S + 300, "A", "One (feat. B)", ""), (BASE_S + 600, "A", "Two", ""),
                          (BASE_S + 600, "C", "Three", "")]
        st = self.sync(fake, mode="full")
        self.assertEqual((st["inserted"], st["corrected"]), (1, 2))
        self.assertEqual(len(self.lastfm_rows()), 4)

    def test_transient_empty_backfill_answer_does_not_finish_backfill(self):
        recent = scrobbles(BASE_S, 2, prefix="Recent")
        old = scrobbles(BASE_S - 10 * 86400, 4, prefix="Old")
        fake = FakeLastfm(recent)       # Last.fm "forgets" the older history once
        self.sync(fake)
        self.assertIsNone(importers._meta_get(self.conn, importers.META_BACKFILL_DONE))
        fake.scrobbles = old + recent   # ... and serves it on the next run
        st = self.sync(fake)
        self.assertEqual(st["inserted"], 4)
        self.assertIsNotNone(importers._meta_get(self.conn, importers.META_BACKFILL_DONE))
        self.assertEqual(len(self.lastfm_rows()), 6)


class LedgerFeedTests(SyncCase):
    def test_imported_scrobbles_reach_the_plays_ledger_and_merge_live_plays(self):
        events = importlib.import_module("src.persistence.events")
        # a live play logged by the app before its scrobble was imported
        start_ms = BASE_S * 1000
        events.upsert_track("sp1", "Purple Reign", album="Mothership")
        live_id = events.write_event(events.SONG_PLAYED, start_ms + 200_000, plays.local_day(start_ms + 200_000),
                                     track_id="sp1", payload={"artists": "Dance Gavin Dance", "listened_s": 195,
                                                              "track_name": "Purple Reign", "was_in_playlist": True})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plays WHERE superseded_by IS NULL").fetchone()[0], 1)
        fake = FakeLastfm([(BASE_S, "Dance Gavin Dance", "Purple Reign", "Mothership"),
                           (BASE_S + 600, "Dance Gavin Dance", "Caviar", "Mothership")])
        st = self.sync(fake)
        self.assertEqual(st["inserted"], 2)
        rows = self.conn.execute("SELECT source, track_name, linked_live_event_id, spotify_track_id FROM plays "
                                 "WHERE superseded_by IS NULL ORDER BY ts_ms").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [("lastfm", "Purple Reign"), ("lastfm", "Caviar")])
        self.assertEqual((rows[0][2], rows[0][3]), (live_id, "sp1"))
        dgd = self.conn.execute("SELECT plays FROM artist_stats WHERE artist_key = 'dance gavin dance'").fetchone()[0]
        self.assertEqual(dgd, 2)


class StartAndStatusTests(SyncCase):
    def test_not_configured_does_not_start(self):
        with mock.patch.object(importers, "lastfm_credentials", lambda: (None, None)):
            self.assertEqual(importers.start_lastfm_sync("sync"), (False, "not_configured"))
        with self.assertRaises(ValueError):
            importers.start_lastfm_sync("nope")

    def test_status_has_contract_fields(self):
        fake = FakeLastfm(scrobbles(BASE_S, 2))
        self.sync(fake)
        with mock.patch.dict("os.environ", {"LASTFM_API_KEY": "k", "LASTFM_USERNAME": "u"}):
            st = importers.lastfm_status(self.conn)
        for k in ("running", "status", "mode", "pages_done", "pages_total", "inserted", "duplicates", "failed",
                  "error", "newest_scrobble_ts", "last_sync_at", "auto_sync_minutes", "configured",
                  "page", "total_pages", "imported", "total"):
            self.assertIn(k, st, k)
        self.assertEqual(st["newest_scrobble_ts"], (BASE_S + 240) * 1000)
        self.assertTrue(st["configured"])
        self.assertIsNotNone(st["last_sync_at"])


class DataApiRouteTests(SyncCase):
    """The blueprint on a bare Flask app with a stand-in for the app module."""

    def setUp(self):
        super().setUp()
        from flask import Flask
        data_api = importlib.import_module("src.web.data_api")
        self.fake_core = types.ModuleType("app")
        self.fake_core._SQLITE_ENABLED = True
        self.fake_core.get_sp = lambda: None
        self._mods = mock.patch.dict(sys.modules, {"app": self.fake_core})
        self._mods.start()
        self._env = mock.patch.dict("os.environ", {"LASTFM_API_KEY": "", "LASTFM_USERNAME": ""})
        self._env.start()
        flask_app = Flask("data_api_test")
        flask_app.register_blueprint(data_api.bp)
        self.client = flask_app.test_client()

    def tearDown(self):
        self._env.stop()
        self._mods.stop()
        super().tearDown()

    def test_lastfm_import_get_and_post_validation(self):
        r = self.client.get("/api/lastfm/import")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["configured"])
        r = self.client.post("/api/lastfm/import", json={"mode": "bogus"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "invalid_mode")
        r = self.client.post("/api/lastfm/import", json={"mode": "sync"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["code"], "lastfm_not_configured")
        r = self.client.post("/api/lastfm/import", data="[1]", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_lastfm_import_refused_while_upgrade_pending(self):
        self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()
        with mock.patch.object(importers, "lastfm_credentials", lambda: ("k", "u")):
            r = self.client.post("/api/lastfm/import", json={"mode": "sync"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "upgrade_pending")

    def test_spotify_import_requires_sign_in_and_bool_seed(self):
        self.assertEqual(self.client.get("/api/spotify/import").status_code, 200)
        r = self.client.post("/api/spotify/import", json={"seed_artist_likes": "yes"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/spotify/import", json={})
        self.assertEqual(r.status_code, 401)

    def test_data_health_shape(self):
        self.sync(FakeLastfm(scrobbles(BASE_S, 3)))
        r = self.client.get("/api/v2/data/health")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        for k in ("plays_total", "plays_ready", "plays_rules_version", "newest_play_ts", "newest_lastfm_ts",
                  "live_since_ts", "lastfm_configured", "last_sync_at", "sync_running", "schema_version",
                  "db_size_bytes", "backups"):
            self.assertIn(k, d, k)
        self.assertEqual(d["plays_total"], 3)
        self.assertTrue(d["plays_ready"])
        self.assertEqual(d["schema_version"], db.SCHEMA_VERSION)
        self.assertNotIn("api_key", json.dumps(d).lower())


if __name__ == "__main__":
    unittest.main()
