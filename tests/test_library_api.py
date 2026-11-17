"""Library view backend (src/web/library_api.py): album art on history rows, the enriched
watchlist and play counts for Spotify lists.

A stub `app` module stands in for app.py and every test uses an in-memory SQLite DB, so the real
app, DB and config are never touched.
"""

import sqlite3
import sys
import types
import unittest
from unittest import mock

from src.services import artist_stats as AS
from tests.test_artist_stats import Ledger, _OPEN, ms

EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, name TEXT NOT NULL, album TEXT, album_id TEXT,
  image_url TEXT, spotify_url TEXT, duration_ms INTEGER, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS interests (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
  entity_id TEXT, entity_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', note TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS interest_reasons (id INTEGER PRIMARY KEY AUTOINCREMENT, interest_id INTEGER NOT NULL,
  tag TEXT NOT NULL);
"""

SAVED_AT = ms(2025, 1, 1)


def library_ledger():
    L = Ledger()
    L.conn.row_factory = sqlite3.Row  # like the app's connection
    L.conn.executescript(EXTRA_DDL)
    # Clever: 2 plays before saving, 3 after (one live with an id, one scrobble of a live version)
    L.play("Beartooth", "Clever", ms(2024, 6, 1), album="Disease", n=2)
    L.play("Beartooth", "Clever", ms(2025, 3, 1), album="Disease", source="live", sp_id="sp_clever")
    L.play("Beartooth", "Clever - Live", ms(2025, 4, 1), album="Live in Cologne", n=2)
    L.play("Sleep Token", "Gethsemane", ms(2024, 2, 1), album="Even In Arcadia", n=4)
    L.play("Sleep Token", "Gethsemane", ms(2025, 6, 1), album="Even In Arcadia", n=1)
    L.conn.execute("INSERT INTO tracks (id, name, album, album_id, image_url, spotify_url, created_at) "
                   "VALUES ('sp_clever', 'Clever', 'Disease', 'al_disease', 'https://i.scdn.co/image/clever', "
                   "'https://open.spotify.com/track/sp_clever', 1)")
    L.conn.execute("INSERT INTO tracks (id, name, album, album_id, image_url, created_at) "
                   "VALUES ('sp_nothing', 'Unplayed', 'Some Album', 'al_x', NULL, 1)")
    L.conn.commit()
    return L


class StubAppMixin:
    def install_app(self, conn, backlog=None):
        from flask import Flask

        self._saved_app = sys.modules.get("app")
        core = types.ModuleType("app")
        core._SQLITE_ENABLED = True
        core._db = types.SimpleNamespace(get_conn=lambda: conn)
        core.errors = []
        core._log_app_error = lambda e, ctx="": core.errors.append((ctx, e))
        core.get_sp = lambda: None
        if backlog is not None:
            core._backlog_index = lambda: backlog
        sys.modules["app"] = core
        self.core = core
        from src.persistence import research
        self._research_patch = mock.patch.object(research, "get_conn", lambda: conn)
        self._research_patch.start()
        from src.web import library_api

        flask_app = Flask(__name__)
        flask_app.register_blueprint(library_api.bp)
        self.client = flask_app.test_client()

    def uninstall_app(self):
        self._research_patch.stop()
        if self._saved_app is not None:
            sys.modules["app"] = self._saved_app
        else:
            sys.modules.pop("app", None)
        while _OPEN:
            _OPEN.pop().close()
        AS.invalidate_caches()


class FakeBacklog:
    def __init__(self, entries):
        self.entries = {e["track_id"]: e for e in entries}

    def entry(self, tid):
        e = self.entries.get(tid)
        return dict(e) if e else None

    def contains(self, tid):
        return tid in self.entries


class HistoryArtTests(StubAppMixin, unittest.TestCase):
    def setUp(self):
        self.L = library_ledger()
        self.install_app(self.L.conn)

    def tearDown(self):
        self.uninstall_app()

    def test_rows_get_own_art_or_same_song_art(self):
        r = self.client.get("/api/v2/history?artist=Beartooth&limit=10")
        self.assertEqual(r.status_code, 200)
        items = r.get_json()["items"]
        self.assertEqual(len(items), 5)
        live = [i for i in items if i["spotify_track_id"] == "sp_clever"][0]
        self.assertEqual(live["image"], "https://i.scdn.co/image/clever")
        # Scrobbles of the same song (including the live recording) borrow that art.
        self.assertTrue(all(i["image"] == "https://i.scdn.co/image/clever" for i in items))

    def test_rows_without_any_match_have_null_image(self):
        items = self.client.get("/api/v2/history?artist=Sleep%20Token").get_json()["items"]
        self.assertTrue(items)
        self.assertTrue(all("image" in i and i["image"] is None for i in items))

    def test_missing_tracks_table_is_harmless(self):
        from src.web.library_api import attach_art
        conn = sqlite3.connect(":memory:")
        _OPEN.append(conn)
        rows = [{"spotify_track_id": "x", "song_key": "a|b"}]
        attach_art(conn, rows)
        self.assertIsNone(rows[0]["image"])


class WatchlistTests(StubAppMixin, unittest.TestCase):
    def setUp(self):
        self.L = library_ledger()
        c = self.L.conn
        add = ("INSERT INTO interests (entity_type, entity_id, entity_name, status, note, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?)")
        c.execute(add, ("track", "sp_clever", "Clever", "active", "that riff", SAVED_AT, SAVED_AT + 5))
        c.execute(add, ("artist", None, "Sleep Token", "active", None, SAVED_AT, SAVED_AT + 4))
        c.execute(add, ("album", "al_disease", "Disease", "promoted", None, SAVED_AT, SAVED_AT + 3))
        c.execute(add, ("track", "sp_backlog", "Brand New", "archived", None, SAVED_AT, SAVED_AT + 2))
        c.execute("INSERT INTO interest_reasons (interest_id, tag) VALUES (1, 'guitar tone'), (1, 'lyrics')")
        c.commit()
        backlog = FakeBacklog([{"track_id": "sp_backlog", "name": "Brand New", "album": "Fresh",
                                "image": "https://i.scdn.co/image/fresh",
                                "artist_list": [{"id": "a1", "name": "Woe, Is Me"}, {"id": "a2", "name": "Guest"}]}])
        self.install_app(c, backlog=backlog)

    def tearDown(self):
        self.uninstall_app()

    def by_name(self, status):
        r = self.client.get(f"/api/v2/watchlist?status={status}")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        return body, {i["entity_name"]: i for i in body["items"]}

    def test_active_items_are_enriched(self):
        body, items = self.by_name("active")
        self.assertEqual(body["counts"], {"active": 2, "promoted": 1, "archived": 1})
        self.assertTrue(body["stats_ready"])
        self.assertIn("guitar tone", body["seed_tags"])
        self.assertEqual(set(items), {"Clever", "Sleep Token"})
        clever = items["Clever"]
        self.assertEqual(clever["artist"], "Beartooth")
        self.assertEqual(clever["album"], "Disease")
        self.assertEqual(clever["image"], "https://i.scdn.co/image/clever")
        self.assertEqual(clever["uri"], "spotify:track:sp_clever")
        self.assertEqual(clever["reasons"], ["guitar tone", "lyrics"])
        self.assertEqual(clever["note"], "that riff")
        # song-level: 2 + 1 + 2 (live version) plays; 3 of them after saving
        self.assertEqual(clever["plays"], 5)
        self.assertEqual(clever["plays_since_saved"], 3)
        self.assertEqual(clever["last_played_ts"], ms(2025, 4, 1) + 60_000)
        self.assertFalse(clever["in_backlog"])
        st = items["Sleep Token"]
        self.assertEqual((st["artist"], st["plays"], st["plays_since_saved"]), ("Sleep Token", 5, 1))
        self.assertIsNone(st["uri"])

    def test_album_and_backlog_track(self):
        _, done = self.by_name("promoted")
        album = done["Disease"]
        self.assertEqual(album["artist"], "Beartooth")
        self.assertEqual(album["plays"], 3)
        self.assertEqual(album["uri"], "spotify:album:al_disease")
        self.assertEqual(album["image"], "https://i.scdn.co/image/clever")
        _, archived = self.by_name("archived")
        fresh = archived["Brand New"]
        self.assertTrue(fresh["in_backlog"])
        self.assertEqual(fresh["artist"], "Woe, Is Me")
        self.assertEqual([a["name"] for a in fresh["artist_list"]], ["Woe, Is Me", "Guest"])
        self.assertEqual(fresh["image"], "https://i.scdn.co/image/fresh")
        self.assertEqual(fresh["plays"], 0)

    def test_all_and_validation(self):
        body, _ = self.by_name("all")
        self.assertEqual(len(body["items"]), 4)
        r = self.client.get("/api/v2/watchlist?status=done")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "status")

    def test_unavailable_without_sqlite(self):
        self.core._SQLITE_ENABLED = False
        r = self.client.get("/api/v2/watchlist")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["error"]["code"], "watchlist_unavailable")


class PlayCountTests(StubAppMixin, unittest.TestCase):
    def setUp(self):
        self.L = library_ledger()
        self.install_app(self.L.conn)

    def tearDown(self):
        self.uninstall_app()

    def test_counts_for_tracks_and_artists(self):
        r = self.client.post("/api/v2/library/play-counts", json={
            "tracks": [{"name": "Clever", "artist": "Beartooth"},
                       {"name": "Something Else", "artist": "Nobody", "id": "sp_clever"},
                       {"name": "Unknown", "artist": "Nobody"}],
            "artists": ["Sleep Token", "Nobody"],
        })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["tracks"][0], 5)
        self.assertGreaterEqual(body["tracks"][1], 1)  # matched by Spotify id
        self.assertEqual(body["tracks"][2], 0)
        self.assertEqual(body["artists"], {"Sleep Token": 5, "Nobody": 0})

    def test_validation(self):
        for payload, field in (({"tracks": "x"}, "tracks"), ({"tracks": [{"name": 3}]}, "tracks"),
                               ({"artists": [1]}, "artists"),
                               ({"tracks": [{"name": "a", "artist": "b"}] * 501}, "tracks")):
            r = self.client.post("/api/v2/library/play-counts", json=payload)
            self.assertEqual(r.status_code, 400, payload if len(str(payload)) < 200 else field)
            self.assertEqual(r.get_json()["error"]["field"], field)
        r = self.client.post("/api/v2/library/play-counts", data="nope", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_empty_request(self):
        r = self.client.post("/api/v2/library/play-counts", json={})
        self.assertEqual(r.get_json(), {"tracks": [], "artists": {}})


if __name__ == "__main__":
    unittest.main()
