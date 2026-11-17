"""Plays ledger: normalization, live/scrobble merge (both directions), bulk flags, local days,
migrations and the live-event metadata contract. Every test uses a temp database."""

import contextlib
import importlib
import io
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src.persistence import db, events, plays, sessions


def _use_current_modules():
    """Rebind the persistence modules to the ones runtime imports resolve to now
    (tests/platform_harness.py swaps src.* for a temp copy partway through a full run)."""
    global db, events, plays, sessions
    db = importlib.import_module("src.persistence.db")
    events = importlib.import_module("src.persistence.events")
    plays = importlib.import_module("src.persistence.plays")
    sessions = importlib.import_module("src.persistence.sessions")

MIN = 60_000
BASE = int(datetime(2026, 5, 8, 12, 0, 0).timestamp() * 1000)  # local noon


class LedgerCase(unittest.TestCase):
    def setUp(self):
        _use_current_modules()
        self._orig_path = db.DB_PATH
        self.tmp = tempfile.mkdtemp(prefix="plays_test_")
        db.DB_PATH = Path(self.tmp) / "music_explorer.db"
        with contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        self.conn = db.get_conn()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        db._local.conn = None
        db.DB_PATH = self._orig_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------
    def scrobble(self, ts_ms, artist, track, album=""):
        payload = {"artists": artist, "track_name": track, "album": album, "source": "lastfm_import"}
        cur = self.conn.execute(
            "INSERT INTO events (event_type, ts_ms, day, payload, source) VALUES ('SONG_PLAYED', ?, ?, ?, 'lastfm')",
            (ts_ms, plays.local_day(ts_ms), json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()
        return cur.lastrowid

    def live(self, end_ms, track_id, name, artists, listened_s, album="Album", in_backlog=True,
             artist_names=None, started_ms=None):
        events.upsert_track(track_id, name, album=album)
        payload = {"artists": artists, "listened_s": listened_s, "source": "playback",
                   "was_in_playlist": in_backlog}
        if artist_names is not None:
            payload["artist_names"] = artist_names
        if started_ms is not None:
            payload["started_ms"] = started_ms
        return events.write_event(events.SONG_PLAYED, end_ms, plays.local_day(end_ms), track_id=track_id,
                                  payload=payload)

    def canonical(self, where="1", params=()):
        return self.conn.execute(
            f"SELECT * FROM plays WHERE superseded_by IS NULL AND ({where}) ORDER BY ts_ms", params).fetchall()


class NormalizationTests(unittest.TestCase):
    def test_artist_key_folds_unicode_case_quotes_and_space(self):
        self.assertEqual(plays.artist_key("Woe, Is Me"), "woe, is me")
        self.assertEqual(plays.artist_key("Andrés"), plays.artist_key("Andrés"))
        self.assertEqual(plays.artist_key("  Dance   Gavin Dance "), "dance gavin dance")
        self.assertEqual(plays.artist_key("Don’t"), "don't")

    def test_track_key_strips_recording_noise_only(self):
        ak = plays.artist_key("Dance Gavin Dance")
        base = plays.track_key(ak, "Purple Reign")
        self.assertEqual(base, "dance gavin dance|purple reign")
        for variant in ("Purple Reign (feat. Someone)", "Purple Reign - Remastered 2011",
                        "Purple Reign (Explicit)", "Purple Reign - Radio Edit", "PURPLE  REIGN"):
            self.assertEqual(plays.track_key(ak, variant), base, variant)
        # a live / session recording is a different recording of the same song
        self.assertNotEqual(plays.track_key(ak, "Purple Reign - Live"), base)
        self.assertNotEqual(plays.track_key(ak, "Purple Reign (Tree City Sessions)"), base)

    def test_song_key_merges_performances(self):
        ak = "dance gavin dance"
        base = plays.song_key(ak, "Man of the Year")
        for variant in ("Man of the Year - Live", "Man of the Year (Tree City Sessions)",
                        "Man of the Year (Acoustic)", "Man of the Year (Demo)", "Man of the Year (feat. X)"):
            self.assertEqual(plays.song_key(ak, variant), base, variant)

    def test_album_key(self):
        ak = "dance gavin dance"
        self.assertEqual(plays.album_key(ak, "Mothership (Deluxe Edition)"), "dance gavin dance|mothership")
        self.assertEqual(plays.album_key(ak, "Mothership - EP"), "dance gavin dance|mothership")
        self.assertIsNone(plays.album_key(ak, ""))
        self.assertIsNone(plays.album_key(ak, None))

    def test_primary_artist_keeps_comma_names(self):
        known = {"woe, is me", "dance gavin dance", "tyler, the creator", "nothing,nowhere."}.__contains__
        self.assertEqual(plays.primary_artist("Woe, Is Me", known), "Woe, Is Me")
        self.assertEqual(plays.primary_artist("Dance Gavin Dance, Chino Moreno", known), "Dance Gavin Dance")
        self.assertEqual(plays.primary_artist("Tyler, The Creator, Kali Uchis", known), "Tyler, The Creator")
        self.assertEqual(plays.primary_artist("nothing,nowhere., Foo", known), "nothing,nowhere.")
        self.assertEqual(plays.primary_artist("Now, Now", known), "Now")  # unknown: first token
        self.assertEqual(plays.primary_artist("Solo Artist", known), "Solo Artist")

    def test_local_parts_use_os_local_time(self):
        ts = int(datetime(2025, 3, 9, 23, 30).timestamp() * 1000)  # late evening local
        day, hour, dow, ym, year = plays.local_parts(ts)
        dt = datetime.fromtimestamp(ts / 1000)
        self.assertEqual((day, hour, dow, ym, year),
                         (dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday(), dt.strftime("%Y-%m"), dt.year))
        self.assertEqual(day, "2025-03-09")


class RebuildTests(LedgerCase):
    def test_rebuild_merges_links_drops_and_keeps_comma_artists(self):
        s1 = self.scrobble(BASE, "Dance Gavin Dance", "Purple Reign", "Mothership")
        self.scrobble(BASE + 10 * MIN, "Tyler, The Creator", "EARFQUAKE", "IGOR")
        self.scrobble(BASE + 20 * MIN, "Woe, Is Me", "Vengeance", "Number[s]")
        # live copy of the first scrobble: logged at the END, 200 s later, credited to two artists
        l1 = self.live(BASE + 200_000, "sp1", "Purple Reign", "Dance Gavin Dance, Chino Moreno", 190)
        # a long live play with no scrobble -> provisional play; a short one -> not a play
        l2 = self.live(BASE + 60 * MIN, "sp2", "Brand New Song", "Woe, Is Me, Guest", 120)
        l3 = self.live(BASE + 70 * MIN, "sp3", "Skipped Song", "Somebody", 12)
        stats = plays.rebuild(self.conn)
        self.assertTrue(plays.ensure_plays_ready(self.conn, start_build=False))
        self.assertEqual(stats["merged"], 1)
        self.assertEqual(stats["provisional"], 1)
        self.assertEqual(stats["dropped_short"], 1)

        rows = self.canonical()
        self.assertEqual(len(rows), 4)
        merged = self.conn.execute("SELECT * FROM plays WHERE event_id = ?", (s1,)).fetchone()
        self.assertEqual(merged["linked_live_event_id"], l1)
        self.assertEqual(merged["spotify_track_id"], "sp1")
        self.assertEqual(merged["listened_s"], 190)
        self.assertEqual(merged["is_backlog"], 1)
        prov = self.conn.execute("SELECT * FROM plays WHERE event_id = ?", (l2,)).fetchone()
        self.assertEqual(prov["source"], "live")
        self.assertEqual(prov["artist_key"], "woe, is me")          # longest known comma prefix
        self.assertEqual(prov["ts_ms"], BASE + 60 * MIN - 120_000)  # START = end - listened
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM plays_skipped WHERE event_id = ?", (l3,)).fetchone())
        keys = {r["artist_key"] for r in rows}
        self.assertIn("tyler, the creator", keys)                   # scrobble artists are never split
        self.assertNotIn("tyler", keys)
        # rollups
        a = self.conn.execute("SELECT * FROM artist_stats WHERE artist_key = 'woe, is me'").fetchone()
        self.assertEqual(a["plays"], 2)
        self.assertEqual(a["display_name"], "Woe, Is Me")

    def test_merge_needs_a_compatible_artist(self):
        # same title, different artist, inside the window: two different listens
        s_other = self.scrobble(BASE, "Other Band", "Intro")
        l_intro = self.live(BASE + 90_000, "spI", "Intro", "My Band", 80)
        # accents/punctuation differ between the services: still the same listen
        s_acc = self.scrobble(BASE + 30 * MIN, "Andrés", "Canción")
        l_acc = self.live(BASE + 30 * MIN + 200_000, "spA", "Canción", "Andres", 190)
        # the scrobble credits the featured artist of a comma-joined live credit
        s_feat = self.scrobble(BASE + 60 * MIN, "Chino Moreno", "Duet")
        l_feat = self.live(BASE + 60 * MIN + 200_000, "spD", "Duet", "Somebody, Chino Moreno", 190)
        for incremental in (False, True):
            if incremental:
                self.conn.execute("DELETE FROM plays")
                self.conn.execute("DELETE FROM app_meta WHERE key = ?", (plays.META_WATERMARK,))
                self.conn.commit()
                plays.catch_up(self.conn)
            else:
                plays.rebuild(self.conn)
            link = dict(self.conn.execute("SELECT event_id, linked_live_event_id FROM plays WHERE source='lastfm'").fetchall())
            self.assertIsNone(link[s_other], incremental)
            self.assertEqual(link[s_acc], l_acc, incremental)
            self.assertEqual(link[s_feat], l_feat, incremental)
            live_rows = [r[0] for r in self.conn.execute("SELECT event_id FROM plays WHERE source='live'")]
            self.assertEqual(live_rows, [l_intro], incremental)

    def test_rebuild_is_repeatable_and_preserves_hidden_flag(self):
        self.scrobble(BASE, "Harmontown", "Episode 1")
        self.scrobble(BASE + 30 * MIN, "Band", "Song")
        plays.rebuild(self.conn)
        first_ids = [r["id"] for r in self.canonical()]
        self.conn.execute("UPDATE artist_stats SET is_music = 0 WHERE artist_key = 'harmontown'")
        self.conn.commit()
        plays.rebuild(self.conn)
        rows = self.canonical()
        self.assertEqual(len(rows), 2)
        self.assertGreater(min(r["id"] for r in rows), max(first_ids))  # ids only grow
        hidden = self.conn.execute("SELECT is_music, plays FROM artist_stats WHERE artist_key = 'harmontown'").fetchone()
        self.assertEqual((hidden["is_music"], hidden["plays"]), (0, 1))

    def test_bulk_runs_flagged(self):
        t = BASE
        for i in range(6):          # an offline batch: 6 plays 1 s apart
            self.scrobble(t + i * 1000, "Batch", f"Song {i}")
        for i in range(4):          # 4 quick plays are not enough for a run
            self.scrobble(t + 3_600_000 + i * 2000, "Quick", f"Song {i}")
        self.scrobble(t + 7_200_000, "Normal", "Song")
        stats = plays.rebuild(self.conn)
        self.assertEqual(stats["bulk"], 6)
        q = {r["artist_key"]: r["ts_quality"] for r in self.canonical()}
        self.assertEqual(q["batch"], "bulk")
        self.assertEqual(q["quick"], "ok")
        self.assertEqual(q["normal"], "ok")

    def _album_row_for_albumless_scrobble(self):
        self.scrobble(BASE, "DGD", "Caviar", "Mothership")
        self.scrobble(BASE + 5 * MIN, "DGD", "Caviar", "Mothership")
        e = self.scrobble(BASE + 10 * MIN, "DGD", "Caviar", "")
        plays.rebuild(self.conn)
        row = self.conn.execute("SELECT album_name, album_key, album_filled FROM plays WHERE event_id = ?", (e,)).fetchone()
        return (row["album_name"], row["album_key"], row["album_filled"])

    def test_missing_album_stays_missing_like_lastfm(self):
        # Default: album-less scrobbles are not attributed to an album, so album counts match Last.fm.
        self.assertEqual(self._album_row_for_albumless_scrobble(), (None, None, 0))

    def test_missing_album_is_filled_and_flagged_when_enabled(self):
        with mock.patch.object(plays, "FILL_MISSING_ALBUMS", True):
            self.assertEqual(self._album_row_for_albumless_scrobble(), ("Mothership", "dgd|mothership", 1))


class IncrementalTests(LedgerCase):
    def setUp(self):
        super().setUp()
        plays.rebuild(self.conn)  # empty but ready ledger

    def test_live_after_scrobble_links_immediately(self):
        s = self.scrobble(BASE, "Sleep Token", "Gethsemane", "Even In Arcadia")
        plays.catch_up(self.conn)
        l = self.live(BASE + 400_000, "spG", "Gethsemane", "Sleep Token", 380, in_backlog=False)
        rows = self.canonical()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], s)
        self.assertEqual(rows[0]["linked_live_event_id"], l)
        self.assertEqual(rows[0]["spotify_track_id"], "spG")
        self.assertEqual(rows[0]["is_backlog"], 0)

    def test_scrobble_after_live_supersedes_the_provisional_play(self):
        l = self.live(BASE + 240_000, "spX", "Caviar", "Dance Gavin Dance", 230)
        rows = self.canonical()
        self.assertEqual([r["source"] for r in rows], ["live"])
        self.assertEqual(self.conn.execute("SELECT plays FROM artist_stats WHERE artist_key='dance gavin dance'").fetchone()[0], 1)
        s = self.scrobble(BASE + 5_000, "Dance Gavin Dance", "Caviar", "Mothership")
        self.assertEqual(plays.ingest_event(self.conn, {"id": s, "event_type": "SONG_PLAYED", "ts_ms": BASE + 5_000,
                                                        "source": "lastfm", "payload": {"artists": "Dance Gavin Dance",
                                                        "track_name": "Caviar", "album": "Mothership"}}),
                         "inserted_linked")
        rows = self.canonical()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "lastfm")
        self.assertEqual(rows[0]["linked_live_event_id"], l)
        self.assertEqual(rows[0]["spotify_track_id"], "spX")
        old = self.conn.execute("SELECT superseded_by FROM plays WHERE event_id = ?", (l,)).fetchone()
        self.assertEqual(old[0], s)
        self.assertEqual(self.conn.execute("SELECT plays FROM artist_stats WHERE artist_key='dance gavin dance'").fetchone()[0], 1)
        # a second ingest of either event changes nothing
        self.assertEqual(plays.catch_up(self.conn, full_check=True), 0)

    def test_short_live_play_is_skipped_but_still_links_to_a_later_scrobble(self):
        l = self.live(BASE + 25_000, "spS", "Short One", "Band", 20)
        self.assertEqual(len(self.canonical()), 0)
        self.scrobble(BASE, "Band", "Short One")
        plays.catch_up(self.conn)
        rows = self.canonical()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["linked_live_event_id"], l)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM plays_skipped WHERE event_id = ?", (l,)).fetchone())

    def test_started_ms_and_artist_names_from_new_payloads(self):
        start = BASE - 100_000
        self.live(BASE, "spN", "Now Song", "Now, Now", 95, artist_names=["Now, Now"], started_ms=start)
        row = self.canonical()[0]
        self.assertEqual(row["artist_key"], "now, now")
        self.assertEqual(row["ts_ms"], start)

    def test_incremental_bulk_flagging(self):
        for i in range(5):
            self.scrobble(BASE + i * 2000, "Batch", f"Song {i}")
            plays.catch_up(self.conn)
        self.assertEqual({r["ts_quality"] for r in self.canonical()}, {"bulk"})

    def test_ingest_is_fast(self):
        for i in range(300):
            self.scrobble(BASE - 400 * MIN + i * 4 * MIN, f"Artist {i % 30}", f"Song {i % 50}", f"Album {i % 7}")
        plays.rebuild(self.conn)
        n = 100
        t0 = time.perf_counter()
        for i in range(n):
            self.live(BASE + (i + 1) * 5 * MIN, f"sp{i}", f"Song {i % 50}", f"Artist {i % 30}", 200)
        per_event_ms = (time.perf_counter() - t0) * 1000 / n
        # write_event + ingest; the ledger part alone is ~1 ms (the sandbox check measures it)
        self.assertLess(per_event_ms, 25)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plays WHERE source='live'").fetchone()[0], n)

    def test_readiness_follows_rules_version(self):
        self.assertTrue(plays.ensure_plays_ready(self.conn, start_build=False))
        self.conn.execute("UPDATE app_meta SET value = 'old' WHERE key = ?", (plays.META_RULES,))
        self.conn.commit()
        self.assertFalse(plays.ensure_plays_ready(self.conn, start_build=False))
        # ingest while not ready is deferred, and the rebuild picks the event up
        e = self.scrobble(BASE, "Deferred", "Song")
        self.assertEqual(plays.ingest_event(self.conn, {"id": e, "event_type": "SONG_PLAYED", "ts_ms": BASE,
                                                        "source": "lastfm", "payload": {"artists": "Deferred",
                                                        "track_name": "Song"}}), "deferred")
        plays.rebuild(self.conn)
        self.assertEqual(len(self.canonical("artist_key = 'deferred'")), 1)


class EventContractTests(LedgerCase):
    def test_write_from_legacy_keeps_names_ids_and_extras(self):
        plays.rebuild(self.conn)
        entry = {
            "type": "listen", "ts_ms": BASE, "day": plays.local_day(BASE), "track_id": "t1",
            "track_name": "Vengeance", "artists": "Woe, Is Me", "artist_ids": ["a1"], "artist_names": ["Woe, Is Me"],
            "album": "Number[s]", "album_id": "al1", "duration_ms": 200_000, "listened_s": 190.0,
            "source": "playback", "was_in_playlist": True, "started_ms": BASE - 195_000,
            "started_in_backlog": True, "auto_removed": True, "genres": ["metalcore"], "image": "x",
        }
        events.write_from_legacy(entry)
        ev = self.conn.execute("SELECT * FROM events WHERE event_type = 'SONG_PLAYED'").fetchone()
        p = json.loads(ev["payload"])
        for k in ("track_name", "album", "album_id", "artist_ids", "artist_names", "duration_ms", "started_ms",
                  "started_in_backlog", "auto_removed"):
            self.assertIn(k, p, k)
        self.assertEqual(ev["artist_id"], "a1")
        self.assertIn("Woe, Is Me", ev["payload"])  # stored as UTF-8 text, not \\u escapes
        tr = self.conn.execute("SELECT duration_ms, album_id FROM tracks WHERE id='t1'").fetchone()
        self.assertEqual((tr[0], tr[1]), (200_000, "al1"))
        tl = self.conn.execute("SELECT completed FROM track_listens").fetchall()
        self.assertEqual([r[0] for r in tl], [1])  # 190 s of 200 s >= 80 %
        row = self.canonical()[0]
        self.assertEqual((row["artist_key"], row["ts_ms"], row["is_backlog"]), ("woe, is me", BASE - 195_000, 1))

    def test_unknown_legacy_types_are_not_plays_and_threshold_is_not_a_listen(self):
        events.write_from_legacy({"type": "mystery", "ts_ms": BASE, "track_id": "t2", "track_name": "X"})
        events.record_action(events.THRESHOLD_REACHED, track_id="t2", track_name="X", listened_s=20)
        events.record_action(events.TRACK_COMPLETED, track_id="t2", track_name="X", listened_s=20)
        types = [r[0] for r in self.conn.execute("SELECT event_type FROM events ORDER BY id")]
        self.assertEqual(types, ["MYSTERY", "THRESHOLD_REACHED", "TRACK_COMPLETED"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM track_listens").fetchone()[0], 0)

    def test_upsert_artist_like_keeps_liked_at_and_manual_flag(self):
        events.upsert_artist_like("x", "X", auto_liked=False, track_count=1)
        first = self.conn.execute("SELECT liked_at FROM artist_likes").fetchone()[0]
        time.sleep(0.01)
        events.upsert_artist_like("x", "X", auto_liked=True, track_count=5)
        row = self.conn.execute("SELECT liked_at, auto_liked, track_count FROM artist_likes").fetchone()
        self.assertEqual((row[0], row[1], row[2]), (first, 0, 5))

    def test_session_counts_tracks_not_polls(self):
        t = BASE
        for i in range(10):
            sessions.heartbeat("a", t + i * 3000)
        for i in range(10):
            sessions.heartbeat("b", t + 40_000 + i * 3000)
        sessions.heartbeat("a", t + 80_000)
        sid = sessions.current_session_id()
        count = self.conn.execute("SELECT track_count FROM listen_sessions WHERE id = ?", (sid,)).fetchone()[0]
        self.assertEqual(count, 3)
        sessions._current_session_id = None
        sessions._last_heartbeat_ms = 0
        sessions._last_track_id = None


class MigrationTests(unittest.TestCase):
    def setUp(self):
        _use_current_modules()
        self._orig_path = db.DB_PATH
        self.tmp = tempfile.mkdtemp(prefix="migr_test_")
        db.DB_PATH = Path(self.tmp) / "music_explorer.db"

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.conn = None
        db.DB_PATH = self._orig_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _legacy_db(self):
        """A v0 database shaped like the user's: UTC days on scrobbles, a removal loop."""
        c = sqlite3.connect(str(db.DB_PATH))
        c.executescript(db._SCHEMA)
        c.execute("ALTER TABLE events ADD COLUMN source2 TEXT")  # harmless extra column
        c.execute("CREATE INDEX events_type ON events(event_type)")
        late = int(datetime(2025, 6, 1, 22, 30).timestamp() * 1000)  # local evening = next UTC day west of UTC
        utc_day = datetime.fromtimestamp(late / 1000, timezone.utc).strftime("%Y-%m-%d")
        c.execute("INSERT INTO events (event_type, ts_ms, day, payload, source) VALUES ('SONG_PLAYED', ?, ?, ?, 'lastfm')",
                  (late, utc_day, json.dumps({"artists": "A", "track_name": "T", "album": ""})))
        c.execute("INSERT INTO tracks (id, name, created_at) VALUES ('t', 'T', 0)")
        for i in range(3):
            c.execute("INSERT INTO events (event_type, ts_ms, day, track_id, source) VALUES "
                      "('TRACK_REMOVED_FROM_QUEUE', ?, '2026-05-13', 't', 'live')", (BASE + i,))
            c.execute("INSERT INTO events (event_type, ts_ms, day, track_id, source) VALUES "
                      "('TRACK_COMPLETED', ?, '2026-05-13', 't', 'live')", (BASE + 10 + i,))
            c.execute("INSERT INTO track_listens (track_id, ts_ms, day, completed) VALUES ('t', ?, '2026-05-13', 1)",
                      (BASE + 10 + i,))
        c.commit()
        c.close()
        return late

    def test_v2_v3_migrate_backup_and_are_idempotent(self):
        late = self._legacy_db()
        with contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        conn = db.get_conn()
        self.assertEqual(db.schema_version(conn), 3)
        self.assertTrue(db.backup_path(2).exists())
        self.assertFalse(db.backup_path(3).exists())   # one backup per run (v3 is schema-only)
        day = conn.execute("SELECT day FROM events WHERE source='lastfm'").fetchone()[0]
        self.assertEqual(day, datetime.fromtimestamp(late / 1000).strftime("%Y-%m-%d"))
        counts = dict(conn.execute("SELECT event_type, COUNT(*) FROM events WHERE source='live' GROUP BY 1").fetchall())
        self.assertEqual(counts, {"TRACK_REMOVED_FROM_QUEUE": 1, "TRACK_COMPLETED": 1})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_listens").fetchone()[0], 0)
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("events_lastfm_uniq", idx)
        self.assertIn("events_type_ts", idx)
        self.assertNotIn("events_type", idx)
        self.assertTrue({"plays", "artist_stats", "track_stats", "album_stats"} <=
                        {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertFalse(plays.ensure_plays_ready(conn, start_build=False))  # built later, in the background
        # the backup is the pre-migration database
        b = sqlite3.connect(str(db.backup_path(2)))
        self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(b.execute("SELECT COUNT(*) FROM events").fetchone()[0], 7)
        b.close()
        # a second start changes nothing
        before = conn.execute("SELECT COUNT(*), SUM(ts_ms) FROM events").fetchone()
        with contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        self.assertEqual(tuple(conn.execute("SELECT COUNT(*), SUM(ts_ms) FROM events").fetchone()), tuple(before))
        self.assertEqual(db.schema_version(conn), 3)

    def test_unique_key_allows_same_second_distinct_scrobbles(self):
        with contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        conn = db.get_conn()
        ins = ("INSERT OR IGNORE INTO events (event_type, ts_ms, day, payload, source) "
               "VALUES ('SONG_PLAYED', ?, '2025-06-06', ?, 'lastfm')")
        a = json.dumps({"artists": "A", "track_name": "One"})
        b = json.dumps({"artists": "A", "track_name": "Two"})
        self.assertEqual(conn.execute(ins, (BASE, a)).rowcount, 1)
        self.assertEqual(conn.execute(ins, (BASE, b)).rowcount, 1)
        self.assertEqual(conn.execute(ins, (BASE, a)).rowcount, 0)
        conn.commit()


if __name__ == "__main__":
    unittest.main()
