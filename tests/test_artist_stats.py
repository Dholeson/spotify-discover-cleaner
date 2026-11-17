"""Artist page backend: personal stats over the plays ledger, legacy listen_stats, my_plays
annotation, hide-from-stats, history/search, discography heard state and the v2 routes.

Every test builds its own in-memory SQLite ledger; nothing touches the real DB or config.
"""

import json
import sqlite3
import sys
import types
import unittest
from datetime import datetime

from src.services import artist_stats as AS
from src.services import discography as D

PLAYS_DDL = """
CREATE TABLE IF NOT EXISTS plays (
  id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE, source TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  day_local TEXT NOT NULL, hour_local INTEGER NOT NULL, dow INTEGER NOT NULL, ym TEXT NOT NULL, year INTEGER NOT NULL,
  artist_name TEXT NOT NULL, artist_key TEXT NOT NULL, track_name TEXT NOT NULL, track_key TEXT NOT NULL,
  song_key TEXT NOT NULL, album_name TEXT, album_key TEXT, spotify_track_id TEXT, listened_s REAL,
  linked_live_event_id INTEGER, is_backlog INTEGER NOT NULL DEFAULT 0, ts_quality TEXT NOT NULL DEFAULT 'ok',
  superseded_by INTEGER);
CREATE INDEX IF NOT EXISTS plays_artist_ts ON plays(artist_key, ts_ms);
CREATE INDEX IF NOT EXISTS plays_song_ts ON plays(song_key, ts_ms);
CREATE TABLE IF NOT EXISTS artist_stats (artist_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, first_ts INTEGER,
  last_ts INTEGER, plays INTEGER NOT NULL DEFAULT 0, days_listened INTEGER, peak_year INTEGER,
  is_music INTEGER NOT NULL DEFAULT 1, spotify_artist_id TEXT, lfm_listeners INTEGER, lfm_fetched_at INTEGER);
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  day TEXT NOT NULL, track_id TEXT, artist_id TEXT, album_id TEXT, payload TEXT, source TEXT DEFAULT 'live');
"""

NOW_MS = int(datetime(2026, 9, 25, 12, 0).timestamp() * 1000)


def ms(y, m, d, h=12, mi=0, s=0):
    return int(datetime(y, m, d, h, mi, s).timestamp() * 1000)


_OPEN = []


class Base(unittest.TestCase):
    def tearDown(self):
        while _OPEN:
            _OPEN.pop().close()
        AS.invalidate_caches()
        # Once the isolated app copy is loaded (platform_harness), routes import ITS src package.
        live = sys.modules.get("src.services.artist_stats")
        if live is not None and live is not AS:
            live.invalidate_caches()


class Ledger:
    """Tiny plays-ledger builder (keys computed with the same helpers the app uses)."""

    def __init__(self, with_plays=True):
        self.conn = sqlite3.connect(":memory:")
        _OPEN.append(self.conn)
        if with_plays:
            self.conn.executescript(PLAYS_DDL)
        else:  # an app DB whose ledger has not been built yet
            self.conn.executescript("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                                    "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, event_type TEXT, "
                                    "ts_ms INTEGER, day TEXT, track_id TEXT, payload TEXT, source TEXT);")
        self.next_event = 1
        self.K = AS.keys()
        if with_plays and callable(getattr(self.K, "mark_ready", None)):
            self.K.mark_ready(self.conn)
        AS.invalidate_caches()

    def play(self, artist, title, ts, album=None, source="lastfm", sp_id=None, quality="ok", superseded=None,
             linked=None, n=1):
        for i in range(n):
            t = ts + i * 60_000
            dt = datetime.fromtimestamp(t / 1000)
            ak = self.K.artist_key(artist)
            self.conn.execute(
                "INSERT INTO plays (event_id, source, ts_ms, day_local, hour_local, dow, ym, year, artist_name, "
                "artist_key, track_name, track_key, song_key, album_name, album_key, spotify_track_id, "
                "ts_quality, superseded_by, linked_live_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.next_event, source, t, dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday(), dt.strftime("%Y-%m"),
                 dt.year, artist, ak, title, self.K.track_key(ak, title), self.K.song_key(ak, title), album,
                 self.K.album_key(ak, album) if album else None, sp_id, quality, superseded, linked))
            self.next_event += 1
        self.conn.commit()
        return self

    def event(self, event_type, artists, track_id, ts=NOW_MS):
        self.conn.execute("INSERT INTO events (event_type, ts_ms, day, track_id, payload, source) VALUES (?,?,?,?,?,?)",
                          (event_type, ts, "2026-09-25", track_id, json.dumps({"artists": artists}), "live"))
        self.conn.commit()
        return self


def standard_ledger():
    L = Ledger()
    # history starts 2021-08-18 with another artist
    L.play("Someone Else", "First Ever", ms(2021, 8, 18), album="Start")
    # Dance Gavin Dance: variants of one song, two albums, live play superseded by a scrobble
    L.play("Dance Gavin Dance", "Purple Reign", ms(2021, 8, 20), album="Mothership", n=5)
    L.play("Dance Gavin Dance", "Purple Reign (Tree City Sessions)", ms(2023, 1, 5), album="Tree City Sessions", n=2)
    L.play("Dance Gavin Dance", "Purple Reign - Instrumental", ms(2023, 2, 5), album="Mothership (Instrumental)")
    L.play("Dance Gavin Dance", "Betrayed by the Game", ms(2025, 5, 1), album="Mothership (Deluxe)", n=3,
           sp_id="sp_betrayed")
    L.play("Dance Gavin Dance", "Betrayed by the Game", ms(2025, 5, 1, 13), source="live", superseded=999)
    L.play("Dance Gavin Dance", "Caviar", ms(2026, 9, 1), source="live", sp_id="sp_caviar")
    # bulk batch must not create streaks / peak month
    L.play("Dance Gavin Dance", "Bulk Song", ms(2021, 9, 6, 0, 0), quality="bulk", album="Mothership", n=6)
    # comma-named bands and a prefix trap
    L.play("Woe, Is Me", "Fame over Function", ms(2022, 3, 1), album="Number[s]", n=4)
    L.play("Time, The Valuator", "How to Hate", ms(2022, 4, 1), n=2)
    L.play("Harmontown", "Episode 1", ms(2022, 5, 1), n=12)
    return L


class ProfileTests(Base):
    def setUp(self):
        self.L = standard_ledger()
        self.c = self.L.conn

    def test_counts_canonical_plays_only(self):
        p = AS.artist_profile(self.c, "Dance Gavin Dance", now_ms=NOW_MS)
        # 5 + 2 + 1 + 3 + 1 (live, not superseded) + 6 bulk = 18; the superseded live row is excluded
        self.assertEqual(p["plays"], 18)
        self.assertTrue(p["found"])
        self.assertEqual(p["name"], "Dance Gavin Dance")
        self.assertEqual(p["split"]["lastfm"], 17)
        self.assertEqual(p["split"]["app"], 1)

    def test_rank_share_and_history_start(self):
        p = AS.artist_profile(self.c, "dance gavin dance", now_ms=NOW_MS)
        self.assertEqual(p["rank"], 1)
        total = 1 + 18 + 4 + 2 + 12
        self.assertAlmostEqual(p["share_pct"], round(100 * 18 / total, 2))
        self.assertTrue(p["first_is_history_start"])
        w = AS.artist_profile(self.c, "Woe, Is Me", now_ms=NOW_MS)
        self.assertEqual(w["rank"], 3)  # Harmontown 12 > Woe 4
        self.assertFalse(w["first_is_history_start"])

    def test_comma_names_are_whole_artists(self):
        self.assertEqual(AS.artist_profile(self.c, "Woe, Is Me", now_ms=NOW_MS)["plays"], 4)
        self.assertEqual(AS.artist_profile(self.c, "Woe", now_ms=NOW_MS)["plays"], 0)
        t = AS.artist_profile(self.c, "Time", now_ms=NOW_MS)
        self.assertEqual(t["plays"], 0)
        self.assertFalse(t["found"])
        self.assertIsNone(t["rank"])
        self.assertEqual(t["plays_by_year"], [])
        self.assertEqual(len(t["plays_by_month"]), 24)
        self.assertEqual(AS.artist_profile(self.c, "Time, The Valuator", now_ms=NOW_MS)["plays"], 2)

    def test_top_tracks_merge_versions_by_song(self):
        p = AS.artist_profile(self.c, "Dance Gavin Dance", now_ms=NOW_MS)
        top = p["top_tracks"][0]
        self.assertEqual(top["name"], "Purple Reign")  # most-played variant names the song
        self.assertEqual(top["plays"], 8)
        self.assertEqual(top["variants"], 3)
        self.assertEqual(p["songs"], 4)       # purple reign, betrayed, caviar, bulk song
        self.assertEqual(p["recordings"], 6)  # + TCS and instrumental recordings
        betrayed = next(t for t in p["top_tracks"] if t["name"] == "Betrayed by the Game")
        self.assertEqual(betrayed["spotify_track_id"], "sp_betrayed")
        self.assertEqual(betrayed["plays"], 3)

    def test_top_albums_and_year_series(self):
        p = AS.artist_profile(self.c, "Dance Gavin Dance", now_ms=NOW_MS)
        mothership = p["top_albums"][0]
        self.assertEqual(mothership["name"], "Mothership")
        # Mothership + Mothership (Deluxe) share an album key; bulk plays count toward totals
        self.assertEqual(mothership["plays"], 5 + 3 + 6)
        self.assertEqual(mothership["tracks_heard"], 3)
        years = {r["year"]: r["plays"] for r in p["plays_by_year"]}
        self.assertEqual(list(years), [2021, 2022, 2023, 2024, 2025, 2026])  # zero-filled through this year
        self.assertEqual(years[2021], 11)
        self.assertEqual(years[2022], 0)
        self.assertEqual(sum(years.values()), p["plays"])
        months = p["plays_by_month"]
        self.assertEqual(len(months), 24)
        self.assertEqual(months[-1], {"ym": "2026-09", "plays": 1})

    def test_streak_and_peak_month_ignore_bulk(self):
        p = AS.artist_profile(self.c, "Dance Gavin Dance", now_ms=NOW_MS)
        self.assertEqual(p["peak_month"], {"ym": "2021-08", "plays": 5})  # not the bulk 2021-09 batch
        self.assertEqual(p["longest_streak"]["days"], 1)
        self.assertNotIn("2021-09-06", {p["longest_streak"]["start"]})
        self.assertEqual(p["days_listened"], 5)  # distinct non-bulk days

    def test_streak_counts_consecutive_days(self):
        L = Ledger()
        for d in (1, 2, 3, 5, 6):
            L.play("A", "x", ms(2024, 1, d))
        L.play("A", "x", ms(2024, 1, 4), quality="bulk")
        s = AS.artist_profile(L.conn, "A", now_ms=NOW_MS)["longest_streak"]
        self.assertEqual(s, {"days": 3, "start": "2024-01-01", "end": "2024-01-03"})

    def test_backlog_cleared_count_from_removal_events(self):
        self.L.event("TRACK_REMOVED_FROM_QUEUE", "Woe, Is Me, Guest", "t1")
        self.L.event("TRACK_REMOVED_FROM_QUEUE", "Woe, Is Me, Guest", "t1")  # duplicate removal of one track
        self.L.event("TRACK_REMOVED_FROM_QUEUE", "Woe, Is Me", "t2")
        self.L.event("TRACK_REMOVED_FROM_QUEUE", "Woe", "t3")
        self.L.event("TRACK_LIKED", "Woe, Is Me", "t2")
        p = AS.artist_profile(self.c, "Woe, Is Me", now_ms=NOW_MS)
        self.assertEqual(p["backlog"]["cleared_count"], 2)
        self.assertEqual(p["liked_count"], 1)

    def test_profile_is_cached_until_the_ledger_changes(self):
        a = AS.artist_profile(self.c, "Woe, Is Me", now_ms=NOW_MS)
        self.L.play("Woe, Is Me", "New Song", ms(2026, 9, 20))
        b = AS.artist_profile(self.c, "Woe, Is Me", now_ms=NOW_MS)
        self.assertEqual(b["plays"], a["plays"] + 1)

    def test_warm_caches_precomputes_the_biggest_profiles(self):
        AS.invalidate_caches()
        AS.warm_caches(self.c)
        top = max(AS.artist_totals(self.c)["items"].values(), key=lambda a: a["plays"])
        with AS._profile_lock:
            warmed = {k[1] for k in AS._profile_cache}
        self.assertIn(top["name"], warmed)

    def test_legacy_shape_matches_profile(self):
        p = AS.artist_profile(self.c, "Dance Gavin Dance", now_ms=NOW_MS)
        legacy = AS.legacy_from_profile(p)
        self.assertEqual(legacy["listen_count"], p["plays"])
        self.assertEqual(legacy["imported_listens"] + legacy["app_listens"], p["plays"])
        self.assertEqual(legacy["distinct_tracks"], p["songs"])
        self.assertEqual(legacy["distinct_albums"], p["albums"])
        self.assertEqual(legacy["top_tracks"][0], {"name": "Purple Reign", "count": 8,
                                                   "song_key": p["top_tracks"][0]["song_key"],
                                                   "spotify_track_id": None})
        self.assertLessEqual(len(legacy["top_tracks"]), 5)
        for k in ("listen_count", "imported_listens", "app_listens", "liked_count", "removed_count",
                  "distinct_tracks", "distinct_albums", "distinct_days", "revisit_days", "first_heard_at",
                  "last_heard_at", "top_tracks", "top_albums"):
            self.assertIn(k, legacy)

    def test_public_profile_hides_internal_sets(self):
        p = AS.artist_profile(self.c, "Dance Gavin Dance", now_ms=NOW_MS)
        self.assertIn("heard", p)
        out = AS.public_profile(p)
        self.assertNotIn("heard", out)
        json.dumps(out)


class NotReadyTests(Base):
    def test_missing_ledger_raises(self):
        L = Ledger(with_plays=False)
        self.assertFalse(AS.ledger_ready(L.conn))
        with self.assertRaises(AS.LedgerNotReady):
            AS.artist_profile(L.conn, "Anyone")
        self.assertFalse(AS.annotate_artist_info(L.conn, {"top_tracks": [{"name": "x"}]}, "Anyone"))


class AnnotateTests(Base):
    def test_my_plays_and_unheard(self):
        L = standard_ledger()
        info = {
            "name": "Dance Gavin Dance",
            "top_tracks": [
                {"id": "x1", "name": "Purple Reign - Remastered 2011", "artists": "Dance Gavin Dance"},
                {"id": "sp_betrayed", "name": "Betrayed By The Game (Live)", "artists": "Dance Gavin Dance"},
                {"id": None, "name": "Never Played", "artists": "Dance Gavin Dance"},
                {"id": "x2", "name": "Fame over Function", "artists": "Woe, Is Me",
                 "artist_list": [{"id": "w", "name": "Woe, Is Me"}]},
            ],
            "related": [{"id": None, "name": "Woe, Is Me"}, {"id": "a", "name": "Nobody Heard"}],
            "recommendations": [{"id": None, "name": "How to Hate", "artists": "Time, The Valuator",
                                 "lastfm_artist": "Time, The Valuator"}],
        }
        self.assertTrue(AS.annotate_artist_info(L.conn, info, "Dance Gavin Dance"))
        plays = [t["my_plays"] for t in info["top_tracks"]]
        self.assertEqual(plays, [8, 3, 0, 4])
        self.assertEqual([t["name"] for t in info["unheard_top_tracks"]], ["Never Played"])
        self.assertTrue(all(t["my_plays"] == 0 for t in info["unheard_top_tracks"]))
        self.assertEqual([r["my_plays"] for r in info["related"]], [4, 0])
        self.assertEqual(info["recommendations"][0]["my_plays"], 2)

    def test_spotify_id_counts_as_heard(self):
        L = Ledger()
        L.play("Band", "Totally Different Title", ms(2024, 1, 1), sp_id="abc")
        counts = AS.song_play_counts(L.conn, [{"artist": "Band", "title": "Other", "id": "abc"}])
        self.assertEqual(counts, [1])


class HiddenTests(Base):
    def test_hide_excludes_from_rank_and_is_durable(self):
        L = standard_ledger()
        c = L.conn
        self.assertEqual(AS.artist_profile(c, "Woe, Is Me", now_ms=NOW_MS)["rank"], 3)
        self.assertTrue(AS.set_hidden(c, "harmontown", True))
        h = AS.artist_profile(c, "Harmontown", now_ms=NOW_MS)
        self.assertTrue(h["hidden"])
        self.assertEqual(h["plays"], 12)  # the artist page still shows the numbers
        self.assertIsNone(h["rank"])  # ... but a hidden artist holds no rank (as in the index)
        self.assertEqual(AS.artist_profile(c, "Woe, Is Me", now_ms=NOW_MS)["rank"], 2)
        listing = AS.list_artists(c, sort="plays")
        row = next(i for i in listing["items"] if i["key"] == "harmontown")
        self.assertTrue(row["hidden"])
        self.assertIsNone(row["rank"])
        self.assertEqual(c.execute("SELECT display_name, is_music FROM artist_stats WHERE artist_key='harmontown'")
                         .fetchone(), ("Harmontown", 0))
        # a rollup rebuild that forgets the flag is repaired from the durable app_meta copy
        c.execute("UPDATE artist_stats SET is_music=1")
        c.commit()
        AS.invalidate_caches()
        self.assertIn("harmontown", AS.hidden_keys(c))
        self.assertEqual(c.execute("SELECT is_music FROM artist_stats WHERE artist_key='harmontown'").fetchone()[0], 0)
        AS.set_hidden(c, "Harmontown", False)
        self.assertNotIn("harmontown", AS.hidden_keys(c))
        self.assertFalse(AS.artist_profile(c, "Harmontown", now_ms=NOW_MS)["hidden"])


class LibraryTests(Base):
    def setUp(self):
        self.L = standard_ledger()
        self.c = self.L.conn

    def test_list_artists_sorts_and_filters(self):
        out = AS.list_artists(self.c, sort="plays", limit=2)
        self.assertEqual(out["total"], 5)
        self.assertEqual([i["name"] for i in out["items"]], ["Dance Gavin Dance", "Harmontown"])
        self.assertEqual(out["items"][0]["rank"], 1)
        self.assertEqual([i["name"] for i in AS.list_artists(self.c, sort="name")["items"]][:2],
                         ["Dance Gavin Dance", "Harmontown"])
        self.assertEqual(AS.list_artists(self.c, sort="recent")["items"][0]["name"], "Dance Gavin Dance")
        self.assertEqual(AS.list_artists(self.c, q="woe")["items"][0]["name"], "Woe, Is Me")

    def test_list_artists_hidden_filter_and_scale(self):
        AS.set_hidden(self.c, "Harmontown", True)
        every = AS.list_artists(self.c, sort="plays")
        self.assertEqual(every["total"], 5)
        self.assertEqual(every["hidden_total"], 1)
        self.assertEqual(every["max_plays"], every["items"][0]["plays"])
        music = AS.list_artists(self.c, sort="plays", hidden_filter="exclude")
        self.assertEqual(music["total"], 4)
        self.assertNotIn("harmontown", [i["key"] for i in music["items"]])
        self.assertEqual([i["rank"] for i in music["items"]][:2], [1, 2])  # hidden takes no rank slot
        only = AS.list_artists(self.c, hidden_filter="only")
        self.assertEqual([i["key"] for i in only["items"]], ["harmontown"])
        self.assertTrue(only["items"][0]["hidden"])
        self.assertIsNone(only["items"][0]["rank"])

    def test_history_pages_without_losing_same_second_plays(self):
        L = Ledger()
        base = ms(2025, 1, 1)
        for i in range(3):
            L.play("A", f"t{i}", base + i * 1000)
        for i in range(4):  # four scrobbles in the same second (real Last.fm data has these)
            L.play("B", f"same{i}", base + 10_000)
        for i in range(3):
            L.play("C", f"u{i}", base + 20_000 + i * 1000)
        seen = []
        before = None
        for _ in range(10):
            page = AS.history_page(L.conn, before_ms=before, limit=4)
            seen += [(i["artist"], i["track"]) for i in page["items"]]
            before = page["next_before_ms"]
            if before is None:
                break
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 10)
        self.assertEqual(seen[0], ("C", "u2"))

    def test_history_filters_and_excludes_superseded(self):
        page = AS.history_page(self.c, limit=50, artist="Dance Gavin Dance")
        self.assertEqual(len(page["items"]), 18)
        self.assertIsNone(page["next_before_ms"])
        self.assertTrue(all(i["artist_key"] == "dance gavin dance" for i in page["items"]))
        q = AS.history_page(self.c, limit=50, q="reign")
        self.assertEqual(len(q["items"]), 8)
        keys = set(q["items"][0])
        for k in ("ts_ms", "day", "source", "artist", "artist_key", "track", "album", "spotify_track_id",
                  "listened_s", "is_backlog"):
            self.assertIn(k, keys)

    def test_local_search(self):
        out = AS.search_local(self.c, "purple")
        self.assertEqual(out["tracks"][0]["name"], "Purple Reign")
        self.assertEqual(out["tracks"][0]["plays"], 8)
        self.assertEqual(out["tracks"][0]["artist"], "Dance Gavin Dance")
        out = AS.search_local(self.c, "woe")
        self.assertEqual(out["artists"][0], {"name": "Woe, Is Me", "key": "woe, is me", "plays": 4})
        out = AS.search_local(self.c, "mothership")
        self.assertEqual(out["albums"][0]["name"], "Mothership")
        self.assertEqual(AS.search_local(self.c, "  "), {"artists": [], "tracks": [], "albums": []})


# ---------------------------------------------------------------------------
# Discography
# ---------------------------------------------------------------------------

def rel(rid, name, atype="album", total=10, date="2016-01-01"):
    return {"id": rid, "name": name, "album_type": atype, "total_tracks": total, "release_date": date,
            "artists": [{"id": "dgd", "name": "Dance Gavin Dance"}]}


class FakeSp:
    def __init__(self, albums, tracklists):
        self.albums = albums
        self.tracklists = tracklists

    def _get(self, url, **kw):
        return {"items": self.albums[kw.get("offset", 0): kw.get("offset", 0) + kw.get("limit", 20)], "next": None}

    def album(self, album_id):
        names = self.tracklists[album_id]
        return {"id": album_id, "name": album_id, "artists": [{"id": "dgd", "name": "Dance Gavin Dance"}],
                "tracks": {"items": [{"id": f"{album_id}-{i}", "name": n, "artists": [{"name": "Dance Gavin Dance"}]}
                                     for i, n in enumerate(names)], "next": None}}

    def next(self, page):
        return None


class DiscographyTests(Base):
    def test_classification(self):
        self.assertEqual(D._classify(rel("a", "Four Songs", "single", 4)), "ep")
        self.assertEqual(D._classify(rel("a", "Three Songs", "single", 3)), "single")
        self.assertEqual(D._classify(rel("a", "Short Album", "album", 5)), "album")
        self.assertEqual(D._classify(rel("a", "Thing - EP", "single", 2)), "ep")
        self.assertEqual(D._classify(rel("a", "Live Forever", "album", 10)), "album")
        self.assertEqual(D._classify(rel("a", "Alive", "album", 10)), "album")
        for name in ("Mothership (Live)", "Live at Red Rocks", "Live From Chicago", "In Concert", "Thing - Live",
                     "Songs (Live in Tokyo)"):
            self.assertEqual(D._classify(rel("a", name, "album", 10)), "live", name)

    def test_heard_state_from_plays_is_capped_and_owned_by_the_album(self):
        L = standard_ledger()
        albums = [
            rel("moth", "Mothership", total=2),                        # 3 recordings heard > 2 tracks
            rel("moth_single", "Mothership", "single", 1, "2015-06-01"),  # same-named single
            rel("moth_dlx", "Mothership (Deluxe Edition)", total=14, date="2017-01-01"),
            rel("tcs", "Tree City Sessions", total=12, date="2018-01-01"),
            rel("purple", "Purple Reign", "single", 1, "2016-02-01"),
            rel("other", "Artificial Selection", total=13, date="2018-06-01"),
        ]
        data = D.build_discography_from_albums(albums, L.conn, artist_id="dgd")
        self.assertEqual(data["artist_name"], "Dance Gavin Dance")
        self.assertTrue(data["history_ready"])
        by = {a["id"]: a for a in data["albums"]}
        self.assertEqual(by["moth"]["heard_tracks"], 2)
        self.assertEqual(by["moth"]["exploration_pct"], 100)
        self.assertEqual(by["moth_single"]["heard_tracks"], 0)   # plays go to the album, not the single
        self.assertTrue(by["moth_dlx"]["is_variant"])
        self.assertEqual(by["moth_dlx"]["heard_tracks"], 3)
        self.assertEqual(by["tcs"]["heard_tracks"], 1)
        self.assertEqual(by["purple"]["heard_tracks"], 1)        # title song heard elsewhere
        self.assertEqual(by["other"]["heard_tracks"], 0)
        for a in data["albums"]:
            self.assertLessEqual(a["heard_tracks"], a["total_tracks"])
            self.assertLessEqual(a["exploration_pct"], 100)
        self.assertLessEqual(data["catalog_coverage_pct"], 100)

    def test_other_artists_albums_do_not_count(self):
        L = Ledger()
        L.play("Somebody Else", "Song", ms(2024, 1, 1), album="Mothership", n=3)
        data = D.build_discography_from_albums([rel("moth", "Mothership")], L.conn, artist_name="Dance Gavin Dance")
        self.assertEqual(data["albums"][0]["heard_tracks"], 0)

    def test_tracklist_marks_scrobbled_songs(self):
        L = standard_ledger()
        sp = FakeSp([], {"moth": ["Purple Reign", "Betrayed By The Game - Remastered", "Unheard Song"]})
        tl = D.build_album_tracklist(sp, "moth", L.conn)
        self.assertEqual([t["heard"] for t in tl["tracks"]], [True, True, False])
        self.assertEqual([t["my_plays"] for t in tl["tracks"]], [8, 3, 0])
        self.assertEqual(tl["heard_tracks"], 2)
        self.assertEqual(tl["total_tracks"], 3)

    def test_fetch_and_build(self):
        L = standard_ledger()
        sp = FakeSp([rel("moth", "Mothership", total=13)], {})
        data = D.build_discography(sp, "dgd", L.conn)
        self.assertEqual(data["albums"][0]["heard_tracks"], 3)

    def test_ledger_missing_means_no_heard_but_still_metadata(self):
        L = Ledger(with_plays=False)
        data = D.build_discography_from_albums([rel("moth", "Mothership")], L.conn, artist_name="Dance Gavin Dance")
        self.assertFalse(data["history_ready"])
        self.assertEqual(data["albums"][0]["heard_tracks"], 0)


# ---------------------------------------------------------------------------
# v2 routes (a stub `app` module stands in for app.py, so the real app never loads)
# ---------------------------------------------------------------------------

class RouteTests(Base):
    def setUp(self):
        from flask import Flask

        self.L = standard_ledger()
        conn = self.L.conn
        self._saved_app = sys.modules.get("app")
        core = types.ModuleType("app")
        core._SQLITE_ENABLED = True
        core._db = types.SimpleNamespace(get_conn=lambda: conn)
        core.errors = []
        core._log_app_error = lambda e, ctx="": core.errors.append((ctx, e))
        core.get_sp = lambda: None

        class Blocked(Exception):
            pass

        core.SpotifyGuardBlocked = Blocked
        core._sp_search_rate_limited = lambda: False
        core._sp_search_backoff_remaining_s = lambda: 0
        sys.modules["app"] = core
        self.core = core
        from src.web import artist_api, library_api

        flask_app = Flask(__name__)
        flask_app.register_blueprint(artist_api.bp)
        flask_app.register_blueprint(library_api.bp)
        self.client = flask_app.test_client()

    def tearDown(self):
        super().tearDown()
        if self._saved_app is not None:
            sys.modules["app"] = self._saved_app
        else:
            sys.modules.pop("app", None)

    def test_stats_contract(self):
        r = self.client.get("/api/v2/artist/Woe,%20Is%20Me/stats")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        for k in ("key", "name", "plays", "rank", "total_artists", "share_pct", "first_ts", "last_ts",
                  "first_is_history_start", "days_listened", "songs", "recordings", "albums", "plays_by_year",
                  "plays_by_month", "top_tracks", "top_albums", "longest_streak", "peak_month", "split", "backlog",
                  "hidden", "found"):
            self.assertIn(k, body)
        self.assertEqual(body["plays"], 4)
        self.assertNotIn("heard", body)
        self.assertEqual(self.client.get("/api/v2/artist/AC%2FDC/stats").get_json()["found"], False)
        unknown = self.client.get("/api/v2/artist/Nobody%20At%20All/stats")
        self.assertEqual(unknown.status_code, 200)
        self.assertFalse(unknown.get_json()["found"])

    def test_not_ready_is_503(self):
        empty = Ledger(with_plays=False).conn
        self.core._db = types.SimpleNamespace(get_conn=lambda: empty)
        for url in ("/api/v2/artist/X/stats", "/api/v2/artists", "/api/v2/history", "/api/v2/search/local?q=x"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 503, url)
            self.assertEqual(r.get_json()["error"]["code"], "history_not_ready")

    def test_validation(self):
        r = self.client.get("/api/v2/artists?sort=bogus")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "sort")
        self.assertEqual(self.client.get("/api/v2/artists?limit=0").status_code, 400)
        self.assertEqual(self.client.get("/api/v2/history?before_ms=abc").status_code, 400)
        r = self.client.post("/api/v2/artist/Harmontown/hide", json={"hidden": "yes"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "hidden")
        self.assertEqual(self.client.get("/api/v2/resolve").status_code, 400)

    def test_hide_roundtrip_and_listing(self):
        r = self.client.post("/api/v2/artist/Harmontown/hide", json={"hidden": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["hidden"], True)
        items = self.client.get("/api/v2/artists?sort=plays").get_json()["items"]
        self.assertTrue(next(i for i in items if i["key"] == "harmontown")["hidden"])
        self.assertTrue(self.client.get("/api/v2/artist/Harmontown/stats").get_json()["hidden"])

    def test_history_and_search(self):
        body = self.client.get("/api/v2/history?limit=5").get_json()
        self.assertEqual(len(body["items"]), 5)
        self.assertIsNotNone(body["next_before_ms"])
        s = self.client.get("/api/v2/search/local?q=purple").get_json()
        self.assertEqual(s["tracks"][0]["name"], "Purple Reign")
        self.assertEqual(self.client.get("/api/v2/search/local?q=").get_json(), {"artists": [], "tracks": [], "albums": []})

    def test_resolve_needs_auth_and_reports_rate_limit(self):
        self.assertEqual(self.client.get("/api/v2/resolve?artist=Woe,%20Is%20Me").status_code, 401)
        sp_calls = []

        class Sp:
            def search(self, **kw):
                sp_calls.append(kw)
                if kw["type"] == "track":
                    return {"tracks": {"items": [
                        {"id": "wrong", "name": "Fame over Function", "artists": [{"id": "x", "name": "Woe"}],
                         "album": {"images": []}},
                        {"id": "right", "name": "Fame over Function", "uri": "spotify:track:right",
                         "artists": [{"id": "w", "name": "Woe, Is Me"}], "album": {"images": [{"url": "big"}, {"url": "small"}]}},
                    ]}}
                return {"artists": {"items": [{"id": "w", "name": "Woe, Is Me", "images": []}]}}

        self.core.get_sp = lambda: Sp()
        body = self.client.get("/api/v2/resolve?artist=Woe,%20Is%20Me&track=Fame%20over%20Function").get_json()
        self.assertEqual(body["track"]["id"], "right")
        self.assertEqual(body["track"]["image"], "small")
        self.assertEqual(body["artist"]["id"], "w")
        self.assertEqual(len(sp_calls), 1)  # the artist came from the track's credits: no second search
        self.core._sp_search_rate_limited = lambda: True
        self.core._sp_search_backoff_remaining_s = lambda: 42
        r = self.client.get("/api/v2/resolve?artist=Woe,%20Is%20Me")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["error"]["retry_after"], 42)

    def test_artists_hidden_param(self):
        self.client.post("/api/v2/artist/Harmontown/hide", json={"hidden": True})
        body = self.client.get("/api/v2/artists?hidden=exclude").get_json()
        self.assertNotIn("harmontown", [i["key"] for i in body["items"]])
        self.assertEqual(body["hidden_total"], 1)
        only = self.client.get("/api/v2/artists?hidden=only").get_json()
        self.assertEqual([i["key"] for i in only["items"]], ["harmontown"])
        r = self.client.get("/api/v2/artists?hidden=maybe")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "hidden")


class LegacyArtistRouteTests(unittest.TestCase):
    """app.py artist routes (/api/artist-info, /api/artist/discography) on the isolated app copy."""

    @classmethod
    def setUpClass(cls):
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import platform_harness
        cls.app = platform_harness.load_app()
        cls.client = cls.app.app.test_client()

    def setUp(self):
        from unittest import mock
        self.mock = mock
        a = self.app
        with a._artist_discography_lock:
            a._artist_discography_cache.clear()
        self.fetched = []
        import importlib
        # The route imports discography lazily from whichever src package the app copy uses.
        disco = importlib.import_module("src.services.discography")
        fetched = self.fetched

        def fake_fetch(sp, artist_id, include_groups="album,single,compilation"):
            fetched.append(artist_id)
            return [{"id": "r1", "name": "Mothership", "album_type": "album", "total_tracks": 15,
                     "release_date": "2016-10-07", "artists": [{"id": artist_id, "name": "Dance Gavin Dance"}]}]

        p = mock.patch.object(disco, "fetch_artist_albums", fake_fetch)
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("get_sp", lambda: object()), ("_SQLITE_ENABLED", False)):
            p = mock.patch.object(a, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_discography_needs_an_id_or_a_name(self):
        r = self.client.get("/api/artist/discography")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["field"], "artist_id")

    def test_discography_resolves_the_name_when_no_id(self):
        with self.mock.patch.object(self.app, "_spotify_search_artist_ex",
                                    return_value=({"id": "dgd", "name": "Dance Gavin Dance"}, False)) as search:
            r = self.client.get("/api/artist/discography?artist_name=Dance%20Gavin%20Dance")
        self.assertEqual(r.status_code, 200)
        search.assert_called_once()
        self.assertEqual(self.fetched, ["dgd"])
        body = r.get_json()
        self.assertEqual(body["artist_id"], "dgd")
        self.assertEqual([a["name"] for a in body["albums"]], ["Mothership"])

    def test_discography_name_lookup_rate_limited_and_not_found(self):
        with self.mock.patch.object(self.app, "_spotify_search_artist_ex", return_value=(None, True)), \
                self.mock.patch.object(self.app, "_sp_search_backoff_remaining_s", return_value=42):
            r = self.client.get("/api/artist/discography?artist_name=Anyone")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["code"], "rate_limited")
        self.assertEqual(r.get_json()["retry_after"], 42)
        with self.mock.patch.object(self.app, "_spotify_search_artist_ex", return_value=(None, False)):
            r = self.client.get("/api/artist/discography?artist_name=Nobody%20At%20All")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["code"], "artist_not_found")
        self.assertEqual(self.fetched, [])

    def test_artist_info_uses_the_artist_photo_and_flags_search_cooldown(self):
        mock = self.mock
        sp = mock.Mock()
        sp.artist.return_value = {"id": "dgd", "name": "Dance Gavin Dance", "genres": ["post-hardcore"],
                                  "followers": {"total": 1234},
                                  "images": [{"url": "https://i/big"}, {"url": "https://i/small"}]}
        sp.current_user_saved_tracks_contains.return_value = []
        info = {"name": "Dance Gavin Dance", "artist_image": "https://i/album-cover", "top_tracks": [],
                "related": [], "lastfm_tags": []}
        with mock.patch.object(self.app, "get_sp", return_value=sp), \
                mock.patch.object(self.app, "_spotify_search_artist_ex", return_value=({"id": "dgd"}, False)), \
                mock.patch.object(self.app, "fetch_artist_info", return_value=dict(info)), \
                mock.patch.object(self.app, "_artist_page_liked_counts", return_value={}), \
                mock.patch.object(self.app, "_sp_search_rate_limited", return_value=False):
            body = self.client.get("/api/artist-info?artist_name=Dance%20Gavin%20Dance").get_json()
        self.assertEqual(body["artist_image"], "https://i/big")
        self.assertEqual(body["artist_image_kind"], "artist")
        self.assertEqual(body["spotify_followers"], 1234)
        self.assertFalse(body["spotify_limited"])
        self.assertEqual(body["artist_id"], "dgd")
        with mock.patch.object(self.app, "get_sp", return_value=sp), \
                mock.patch.object(self.app, "_spotify_search_artist_ex", return_value=(None, True)), \
                mock.patch.object(self.app, "fetch_artist_info", return_value=dict(info)), \
                mock.patch.object(self.app, "_artist_page_liked_counts", return_value={}):
            body = self.client.get("/api/artist-info?artist_name=Dance%20Gavin%20Dance").get_json()
        self.assertTrue(body["spotify_limited"])
        self.assertEqual(body["artist_image"], "https://i/album-cover")
        self.assertEqual(body["artist_image_kind"], "album")

    def test_artist_info_says_when_lastfm_is_failing(self):
        mock = self.mock
        sp = mock.Mock()
        sp.artist.return_value = {"images": []}
        sp.current_user_saved_tracks_contains.return_value = []
        bare = {"name": "X", "top_tracks": [], "related": [], "lastfm_tags": [], "bio": None}

        def get(recent_error):
            import time as _t
            with mock.patch.object(self.app, "get_sp", return_value=sp), \
                    mock.patch.object(self.app, "_spotify_search_artist_ex", return_value=({"id": "x"}, False)), \
                    mock.patch.object(self.app, "fetch_artist_info", return_value=dict(bare)), \
                    mock.patch.object(self.app, "_artist_page_liked_counts", return_value={}), \
                    mock.patch.object(self.app, "lastfm_api_key", return_value="key"), \
                    mock.patch.dict(self.app._lfm_last_error, {"ts": _t.time() if recent_error else 0.0}), \
                    mock.patch.dict(self.app._lfm_breaker, {"open_until": 0.0}):
                return self.client.get("/api/artist-info?artist_name=X").get_json()

        self.assertTrue(get(recent_error=True)["lastfm_unavailable"])
        self.assertFalse(get(recent_error=False)["lastfm_unavailable"])


if __name__ == "__main__":
    unittest.main()
