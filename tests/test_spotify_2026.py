"""Spotify's 2026 Development Mode API (apps created after 2026-02-11) and the legacy API.

Covers the offline FakeSpotify in both modes (FAKE_API_MODE) and the app code that has to work
against both: playlist field renames (tracks -> items, track -> item), fields filters that may
be rejected, removed endpoints (403), the search limit cap of 10, and removed fields.

FakeSpotify runs on a tiny throwaway SQLite file; the app-level tests use the isolated copy of
app.py from platform_harness (network blocked, temp data dir). Nothing touches real data.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

import spotipy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_harness  # noqa: E402

PID = "4uLU6hMCjMI75M1A2tKUQC"   # FakeSpotify's stock demo playlist id (not a real user's)

# (track id, title, artists credit, album) in playlist-creation order. The tribute band comes
# first so a limit=1 artist search for "Dance Gavin Dance" would pick the wrong artist.
TRACKS = [
    ("trk0000000000000000001", "Dance Gavin Dance Medley", "Dance Gavin Dance Tribute Band", "Covers Vol. 1"),
    ("trk0000000000000000002", "Prisoner", "Dance Gavin Dance", "Mothership"),
    ("trk0000000000000000003", "Inspire The Liars", "Dance Gavin Dance", "Mothership"),
    ("trk0000000000000000004", "Prisoner", "Dance Gavin Dance", "Prisoner (Single)"),
    ("trk0000000000000000005", "Synergy", "Dance Gavin Dance, Bilmuri", "Jackpot Juicer"),
    ("trk0000000000000000006", "Ruthless", "Hail The Sun", "Divine Inner Tension"),
    ("trk0000000000000000007", "Black Serotonin", "Hail The Sun", "New Age Filth"),
]


def make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("create table tracks (id text primary key, name text, image_url text, duration_ms integer, "
                 "spotify_url text, album_id text, album text, created_at integer)")
    conn.execute("create table events (id integer primary key, track_id text, payload text, source text)")
    for i, (tid, name, artists, album) in enumerate(TRACKS):
        conn.execute("insert into tracks values (?,?,?,?,?,?,?,?)",
                     (tid, name, f"https://i.scdn.co/image/{i}", 200000 + i, None,
                      "alb" + "".join(c for c in album.lower() if c.isalnum())[:18], album, 1000 + i))
        conn.execute("insert into events (track_id, payload, source) values (?,?,?)",
                     (tid, '{"artists": "%s", "genres": ["post-hardcore"]}' % artists, "live"))
    conn.commit()
    conn.close()


class FakeDb:
    """One throwaway DB for the whole module."""
    _dir = None

    @classmethod
    def path(cls):
        if cls._dir is None:
            cls._dir = tempfile.TemporaryDirectory(prefix="dc_sp2026_")
            make_db(Path(cls._dir.name) / "fake.db")
        return Path(cls._dir.name) / "fake.db"


def new_fake(mode, pl_size=None):
    from src.devtools.fake_spotify import FakeSpotify
    env = {"FAKE_PL_SIZE": str(pl_size or 0)}
    with mock.patch.dict(os.environ, env):
        return FakeSpotify(FakeDb.path(), PID, api_mode=mode)


def status_of(exc_ctx):
    return getattr(exc_ctx.exception, "http_status", None)


# ---------------------------------------------------------------------------
# FakeSpotify: both modes
# ---------------------------------------------------------------------------

class FakeSpotifyLegacyModeTest(unittest.TestCase):
    def setUp(self):
        self.sp = new_fake(None)

    def test_default_mode_is_legacy(self):
        with mock.patch.dict(os.environ, {"FAKE_API_MODE": ""}):
            self.assertEqual(new_fake(None).api_mode, "legacy")
        self.assertEqual(new_fake("2026").api_mode, "2026")
        with mock.patch.dict(os.environ, {"FAKE_API_MODE": "2026"}):
            self.assertEqual(new_fake(None).api_mode, "2026")

    def test_legacy_shapes_unchanged(self):
        meta = self.sp.playlist(PID, fields="name,tracks.total")
        self.assertEqual(meta["tracks"]["total"], len(TRACKS))   # fields ignored, as before
        page = self.sp.playlist_items(PID, limit=3, offset=0)
        self.assertIn("track", page["items"][0])
        self.assertNotIn("item", page["items"][0])
        pls = self.sp.current_user_playlists()["items"]
        self.assertIn("tracks", pls[0])
        me = self.sp.current_user()
        self.assertEqual((me["country"], me["product"]), ("US", "premium"))

    def test_legacy_endpoints_and_fields_still_work(self):
        aid = self.sp.playlist_items(PID, limit=1)["items"][0]["track"]["artists"][0]["id"]
        self.assertEqual(len(self.sp.artists([aid])["artists"]), 1)
        self.assertIsInstance(self.sp.artist_top_tracks(aid)["tracks"], list)
        self.assertIn("popularity", self.sp.artist(aid))
        self.assertIn("followers", self.sp.artist(aid))
        self.assertIn("popularity", self.sp.playlist_items(PID, limit=1)["items"][0]["track"])
        self.assertIsInstance(self.sp.search(q="prisoner", limit=50)["tracks"]["items"], list)


class FakeSpotify2026ModeTest(unittest.TestCase):
    def setUp(self):
        self.sp = new_fake("2026", pl_size=250)

    def test_playlist_meta_uses_items(self):
        meta = self.sp.playlist(PID)
        self.assertNotIn("tracks", meta)
        self.assertEqual(meta["items"]["total"], 250)
        self.assertIn("item", meta["items"]["items"][0])
        self.assertEqual(self.sp.playlist(PID, fields="name,snapshot_id,items.total"),
                         {"name": meta["name"], "snapshot_id": meta["snapshot_id"], "items": {"total": 250}})

    def test_legacy_field_names_are_rejected(self):
        for fields in ("name,tracks.total", "name,snapshot_id,tracks.total,items.total"):
            with self.assertRaises(spotipy.SpotifyException) as cm:
                self.sp.playlist(PID, fields=fields)
            self.assertEqual(status_of(cm), 400)
        with self.assertRaises(spotipy.SpotifyException) as cm:
            self.sp.playlist_items(PID, fields="items(track(id)),next,total", limit=5)
        self.assertEqual(status_of(cm), 400)
        with self.assertRaises(spotipy.SpotifyException) as cm:
            self.sp.playlist(PID, fields="name,(")
        self.assertEqual(status_of(cm), 400)

    def test_items_pages_use_item_and_keep_fields_across_next(self):
        page = self.sp.playlist_items(PID, fields="items(added_at,item(id,name)),next,total", limit=100)
        self.assertEqual(page["total"], 250)
        self.assertEqual(set(page["items"][0]), {"added_at", "item"})
        self.assertEqual(set(page["items"][0]["item"]), {"id", "name"})
        page2 = self.sp.next(page)
        self.assertEqual(set(page2["items"][0]["item"]), {"id", "name"})
        page3 = self.sp.next(page2)
        self.assertEqual(len(page3["items"]), 50)
        self.assertIsNone(page3["next"])
        unfiltered = self.sp.playlist_items(PID, limit=2)
        self.assertNotIn("track", unfiltered["items"][0])
        self.assertIn("is_local", unfiltered["items"][0])

    def test_removed_endpoints_are_403(self):
        aid = self.sp.playlist_items(PID, limit=1)["items"][0]["item"]["artists"][0]["id"]
        for call in (lambda: self.sp.artists([aid]), lambda: self.sp.artist_top_tracks(aid),
                     lambda: self.sp.new_releases(), lambda: self.sp.user("someone"),
                     lambda: self.sp.available_markets()):
            with self.assertRaises(spotipy.SpotifyException) as cm:
                call()
            self.assertEqual(status_of(cm), 403)
            self.assertNotIn("not registered for this application", str(cm.exception))
        # What remains: single artist, albums, album, saved tracks, library writes.
        self.assertEqual(self.sp.artist(aid)["id"], aid)
        self.assertTrue(self.sp.artist_albums(aid, include_groups="album,single", limit=10)["items"])
        self.sp.current_user_saved_tracks_add(["spotify:track:trk0000000000000000006"])
        self.assertEqual(self.sp.current_user_saved_tracks_contains(["trk0000000000000000006"]), [True])

    def test_search_limit_cap(self):
        self.assertTrue(self.sp.search(q="prisoner", type="track", limit=10)["tracks"]["items"])
        with self.assertRaises(spotipy.SpotifyException) as cm:
            self.sp.search(q="prisoner", type="track", limit=11)
        self.assertEqual(status_of(cm), 400)
        self.assertIn("Invalid limit", str(cm.exception))

    def test_removed_fields_are_absent(self):
        t = self.sp.playlist_items(PID, limit=1)["items"][0]["item"]
        self.assertNotIn("popularity", t)
        art = self.sp.artist(t["artists"][0]["id"])
        self.assertNotIn("popularity", art)
        self.assertNotIn("followers", art)
        for a in self.sp.search(q="dance", type="artist", limit=5)["artists"]["items"]:
            self.assertNotIn("followers", a)
        me = self.sp.current_user()
        for gone in ("country", "product", "email", "explicit_content", "followers"):
            self.assertNotIn(gone, me)
        pls = self.sp.current_user_playlists()["items"]
        self.assertNotIn("tracks", pls[0])
        self.assertEqual(pls[0]["items"]["total"], 250)

    def test_writes_still_work(self):
        uri = self.sp.playlist_items(PID, limit=1)["items"][0]["item"]["uri"]
        self.sp.playlist_remove_all_occurrences_of_items(PID, [uri])
        self.assertEqual(self.sp.playlist(PID, fields="items.total")["items"]["total"], 249)
        self.sp.playlist_add_items(PID, [uri], position=0)
        self.assertEqual(self.sp.playlist_items(PID, limit=1)["items"][0]["item"]["uri"], uri)


# ---------------------------------------------------------------------------
# Backlog index worker against both modes
# ---------------------------------------------------------------------------

class BacklogIndexBothModesTest(unittest.TestCase):
    def sync(self, mode, size):
        from src.services import backlog_index as bi
        sp = new_fake(mode, pl_size=size)
        idx = bi.PlaylistIndex(store=None)
        idx.switch(PID)
        worker = bi.IndexSyncWorker(idx, get_sp=lambda: sp, sleep=lambda s: None)
        self.assertEqual(worker.sync(sp), "committed")
        return bi, sp, idx, worker

    def test_2026_backlog_loads_completely(self):
        bi, sp, idx, worker = self.sync("2026", 1200)
        st = idx.status()
        self.assertEqual((st["known"], st["total"], st["complete"]), (1200, 1200, True))
        self.assertIs(idx.contains(sp.pl[-1]["id"]), True)
        entry = idx.entry(sp.pl[0]["id"])
        self.assertTrue(entry["name"] and entry["artists"] and entry["image"])
        # Both-name filters were rejected once; the 2026-only filters are used from then on.
        self.assertEqual(worker.meta_fields, bi.META_FIELDS_2026)
        self.assertEqual(worker.item_fields, bi.ITEM_FIELDS_2026)
        before = len(sp.calls)
        self.assertEqual(worker.sync(sp), "unchanged")
        self.assertEqual([n for _, n in sp.calls[before:]], ["playlist"])  # one meta call, no 400 retry

    def test_legacy_backlog_loads_completely(self):
        bi, sp, idx, worker = self.sync(None, 1200)
        st = idx.status()
        self.assertEqual((st["known"], st["total"]), (1200, 1200))
        self.assertEqual(worker.meta_fields, bi.META_FIELDS)
        self.assertEqual(worker.item_fields, bi.ITEM_FIELDS)

    def test_fields_chain_reaches_unfiltered(self):
        from src.services import backlog_index as bi
        w = bi.IndexSyncWorker(bi.PlaylistIndex(store=None), get_sp=lambda: None)
        self.assertEqual(w._next_fields(bi.ITEM_FIELDS_CHAIN, bi.ITEM_FIELDS), bi.ITEM_FIELDS_2026)
        self.assertIsNone(w._next_fields(bi.ITEM_FIELDS_CHAIN, bi.ITEM_FIELDS_2026))
        self.assertIsNone(w._next_fields(bi.META_FIELDS_CHAIN, "something else"))


# ---------------------------------------------------------------------------
# app.py against both modes (isolated copy, network blocked)
# ---------------------------------------------------------------------------

class AppBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        self._saved = (a._SPOTIFY_MIN_INTERVAL_S, dict(a._SPOTIFY_LANE_MIN_INTERVAL_S), a._SP_SEARCH_MIN_INTERVAL,
                       a._SQLITE_ENABLED, dict(a._spotify_feature_flags))
        a._SPOTIFY_MIN_INTERVAL_S = 0.0
        for lane in a._SPOTIFY_LANE_MIN_INTERVAL_S:
            a._SPOTIFY_LANE_MIN_INTERVAL_S[lane] = 0.0
        a._SP_SEARCH_MIN_INTERVAL = 0.0
        a._SQLITE_ENABLED = False
        self.reset()

    def tearDown(self):
        a = self.app
        (a._SPOTIFY_MIN_INTERVAL_S, lanes, a._SP_SEARCH_MIN_INTERVAL, a._SQLITE_ENABLED, flags) = self._saved
        a._SPOTIFY_LANE_MIN_INTERVAL_S.clear()
        a._SPOTIFY_LANE_MIN_INTERVAL_S.update(lanes)
        with a._spotify_feature_lock:
            a._spotify_feature_flags.clear()
            a._spotify_feature_flags.update(flags)
        self.reset()

    def reset(self):
        a = self.app
        a._clear_guard_state(persist=False, include_search=True)
        a._SPOTIFY_APP_REG_BLOCK_UNTIL = 0.0
        a._SPOTIFY_GUARD_CACHE.clear()
        a._SPOTIFY_INFLIGHT.clear()
        a._SPOTIFY_GUARD_BUDGET_BUCKETS.clear()
        a.artist_genre_cache.clear()
        a._sp_artist_cache.clear()
        a._sp_track_cache.clear()
        a._SPOTIFY_FIELDS_REJECTED.clear()

    def guarded(self, mode, pl_size=None):
        fake = new_fake(mode, pl_size=pl_size)
        return fake, self.app.GuardedSpotify(fake)

    def names(self, fake):
        return [n for _, n in fake.calls]


class RecordingSearch:
    """A stub client that records search kwargs and answers with fixed artist candidates."""

    def __init__(self, artists=()):
        self.searches = []
        self.artists = list(artists)

    def search(self, *args, **kwargs):
        self.searches.append((args, kwargs))
        return {"artists": {"items": self.artists}, "tracks": {"items": []}}


class ArtistResolutionTest(AppBase):
    def test_picks_the_normalized_name_match_among_five(self):
        a = self.app
        stub = RecordingSearch([
            {"id": "tribute", "name": "Dance Gavin Dance Tribute Band", "images": []},
            {"id": "dgd", "name": "Dance Gavin Dance", "images": [{"url": "big"}, {"url": "small"}]},
        ])
        found, blocked = a._spotify_search_artist_ex(a.GuardedSpotify(stub), "dance gavin dance ")
        self.assertFalse(blocked)
        self.assertEqual((found["id"], found["name"], found["image"]), ("dgd", "Dance Gavin Dance", "small"))
        self.assertEqual(stub.searches[0][1]["limit"], 5)
        self.assertEqual(stub.searches[0][1]["type"], "artist")

    def test_falls_back_to_spotifys_first_result(self):
        a = self.app
        stub = RecordingSearch([{"id": "first", "name": "Somebody Else"}, {"id": "second", "name": "Another"}])
        self.assertEqual(a.spotify_search_artist(a.GuardedSpotify(stub), "Nobody")["id"], "first")

    def test_against_fake_2026(self):
        a = self.app
        fake, sp = self.guarded("2026")
        found, _ = a._spotify_search_artist_ex(sp, "Dance Gavin Dance")
        self.assertEqual(found["name"], "Dance Gavin Dance")


class SearchLimitCapTest(AppBase):
    def test_guard_never_sends_more_than_ten(self):
        a = self.app
        stub = RecordingSearch()
        sp = a.GuardedSpotify(stub)
        sp.search(q="a", type="track", limit=50)
        sp.search("b", 25, type="track")
        sp.search(q="c", type="track", limit=8)
        sp.search(q="d", type="track")
        self.assertEqual(stub.searches[0][1]["limit"], 10)
        self.assertEqual(stub.searches[1][0], ("b", 10))
        self.assertEqual(stub.searches[2][1]["limit"], 8)
        self.assertNotIn("limit", stub.searches[3][1])

    def test_fake_2026_search_through_guard(self):
        fake, sp = self.guarded("2026")
        self.assertTrue(sp.search(q="prisoner", type="track", limit=40)["tracks"]["items"])


class PlaylistMetaFallbackTest(AppBase):
    FIELDS = "name,owner.id,owner.display_name,collaborative,tracks.total,items.total,snapshot_id"

    def test_2026_rejects_legacy_names_then_remembers(self):
        a = self.app
        fake, sp = self.guarded("2026", pl_size=120)
        meta = a.spotify_playlist_meta(sp, PID, fields=self.FIELDS)
        self.assertEqual(meta["owner"]["id"], "demo_user")
        self.assertEqual(a.playlist_total_from_meta(meta), 120)
        self.assertIn(self.FIELDS, a._SPOTIFY_FIELDS_REJECTED)
        a._SPOTIFY_GUARD_CACHE.clear()
        before = len(fake.calls)
        a.spotify_playlist_meta(sp, PID, fields=self.FIELDS)
        self.assertEqual(self.names(fake)[before:], ["playlist"])   # no second 400

    def test_legacy_first_filter_works(self):
        a = self.app
        fake, sp = self.guarded(None)
        meta = a.spotify_playlist_meta(sp, PID, fields=self.FIELDS)
        self.assertEqual(a.playlist_total_from_meta(meta), len(TRACKS))
        self.assertEqual(self.names(fake), ["playlist"])
        self.assertEqual(a._SPOTIFY_FIELDS_REJECTED, set())

    def test_a_400_for_every_variant_is_raised_and_not_remembered(self):
        a = self.app

        class BadId:
            def playlist(self, pid, fields=None, **k):
                raise spotipy.SpotifyException(400, -1, "Invalid base62 id")

        with self.assertRaises(spotipy.SpotifyException) as cm:
            a.spotify_playlist_meta(a.GuardedSpotify(BadId()), "bad", fields=self.FIELDS)
        self.assertEqual(status_of(cm), 400)
        self.assertEqual(a._SPOTIFY_FIELDS_REJECTED, set())

    def test_variants(self):
        a = self.app
        self.assertEqual(a._spotify_playlist_fields_variants("name,tracks.total,items(item(id))"),
                         ["name,tracks.total,items(item(id))", "name,items(item(id))", None])
        self.assertEqual(a._spotify_playlist_fields_variants("name"), ["name", None])
        self.assertEqual(a._spotify_playlist_fields_variants(None), [None])

    def test_entry_and_total_helpers_read_both_shapes(self):
        a = self.app
        t = {"id": "x"}
        self.assertIs(a.playlist_entry_media({"track": t}), t)
        self.assertIs(a.playlist_entry_media({"item": t}), t)
        self.assertEqual(a.playlist_total_from_meta({"tracks": {"total": 3}}), 3)
        self.assertEqual(a.playlist_total_from_meta({"items": {"total": 4}}), 4)


class RemovedEndpointsTest(AppBase):
    def test_batch_artists_403_disables_it_and_single_lookups_fill_in(self):
        a = self.app
        fake, sp = self.guarded("2026")
        aids = list(dict.fromkeys(ar["id"] for t in fake.tracks for ar in t["artists"]))
        with a._spotify_feature_lock:
            a._spotify_feature_flags["artist_batch"] = True
        with a.state_lock:
            a.state["last_error"] = None
        genres = a.fetch_genres_for_artists(sp, aids[:2])
        self.assertEqual(genres, ["post-hardcore"])
        self.assertFalse(a.spotify_feature_enabled("artist_batch"))
        self.assertEqual(self.names(fake).count("artists"), 1)
        a.fetch_genres_for_artists(sp, aids)
        self.assertEqual(self.names(fake).count("artists"), 1)    # never again this session
        with a.state_lock:
            self.assertIsNone(a.state["last_error"])                # no user-facing error

    def test_single_lookups_leave_budget_headroom(self):
        a = self.app
        fake, sp = self.guarded("2026")
        limit, _ = a._spotify_guard_policy("artist")["budget"]
        a._SPOTIFY_GUARD_BUDGET_BUCKETS["artist"] = deque([time.monotonic()] * (limit - a._GENRE_LOOKUP_BUDGET_RESERVE - 2))
        a.ensure_artist_genre_cache(sp, [f"fakeartistx{i}" for i in range(10)])
        self.assertEqual(self.names(fake).count("artist"), 0)       # FakeSpotify.artist() is unrecorded
        self.assertGreaterEqual(a._spotify_budget_left("artist"), a._GENRE_LOOKUP_BUDGET_RESERVE)
        self.assertEqual(len([k for k in a.artist_genre_cache if k.startswith("fakeartistx")]), 2)

    def test_top_tracks_403_disables_and_search_fallback_fills_in(self):
        a = self.app
        fake, sp = self.guarded("2026")
        dgd = a.spotify_search_artist(sp, "Dance Gavin Dance")
        with a._spotify_feature_lock:
            a._spotify_feature_flags["artist_top_tracks"] = True
        with mock.patch.object(a, "lastfm_api_key", return_value=None), \
                mock.patch.object(a, "lastfm_artist_top_tracks", return_value=[]):
            info = a.fetch_artist_info(sp, dgd["id"], "Dance Gavin Dance", None, None, [])
            self.assertFalse(a.spotify_feature_enabled("artist_top_tracks"))
            self.assertEqual(self.names(fake).count("artist_top_tracks"), 1)
            self.assertFalse([n for n in info["notes"] if "top tracks" in n])
            titles = [t["name"] for t in info["top_tracks"]]
            self.assertEqual(sorted(titles), ["Inspire The Liars", "Prisoner", "Synergy"])   # one Prisoner
            self.assertTrue(all(t["id"] and t["uri"] and t["image"] for t in info["top_tracks"]))
            self.assertIn("spotify search (top tracks)", info["sources"])
            self.assertIn("search", self.names(fake))
            a.fetch_artist_info(sp, dgd["id"], "Dance Gavin Dance", None, None, [])
            self.assertEqual(self.names(fake).count("artist_top_tracks"), 1)

    def test_top_tracks_via_search_filters_by_credit(self):
        a = self.app
        fake, sp = self.guarded("2026")
        rows, blocked = a.spotify_artist_top_tracks_via_search(sp, None, "Hail The Sun")
        self.assertFalse(blocked)
        self.assertEqual(sorted(r["name"] for r in rows), ["Black Serotonin", "Ruthless"])
        limit, _ = a._spotify_guard_policy("search")["budget"]
        a._SPOTIFY_GUARD_BUDGET_BUCKETS["search"] = deque([time.monotonic()] * limit)
        a._SPOTIFY_GUARD_CACHE.clear()
        self.assertEqual(a.spotify_artist_top_tracks_via_search(sp, None, "Dance Gavin Dance"), ([], True))

    def test_removed_endpoint_detection(self):
        a = self.app
        self.assertTrue(a.spotify_feature_removed(spotipy.SpotifyException(403, -1, "Forbidden")))
        self.assertTrue(a.spotify_feature_removed(spotipy.SpotifyException(410, -1, "Gone")))
        self.assertFalse(a.spotify_feature_removed(spotipy.SpotifyException(
            403, -1, "Check settings on developer.spotify.com/dashboard, the user may not be registered for this application")))
        self.assertFalse(a.spotify_feature_removed(a.SpotifyAppRegistrationBlocked("blocked")))
        self.assertFalse(a.spotify_feature_removed(spotipy.SpotifyException(500, -1, "oops")))


class LikedHelpersBothShapesTest(AppBase):
    def test_saved_track_entries_with_item_key(self):
        a = self.app

        class Saved:
            def current_user_saved_tracks(self, limit=20, offset=0, **k):
                t = {"id": "s1", "name": "Song", "uri": "spotify:track:s1", "artists": [{"name": "Band"}],
                     "album": {"name": "LP", "images": [{"url": "img"}]}, "duration_ms": 1}
                return {"items": [{"added_at": "2026-03-01T00:00:00Z", "item": t}] if offset == 0 else [],
                        "next": None}

        sp = a.GuardedSpotify(Saved())
        with a._liked_songs_cache_lock:
            a._liked_songs_cache.update(tracks=[], fetched_at=0)
        rows = a.get_liked_songs_page(sp, force=True, limit=10)
        self.assertEqual([(r["id"], r["artists"]) for r in rows], [("s1", "Band")])
        a.invalidate_liked_artist_counts_cache()
        self.assertEqual(a.get_liked_artist_counts(sp, force=True), {"band": 1})
        a.invalidate_liked_artist_counts_cache()


class DiscographyBothModesTest(AppBase):
    def test_albums_page_through_artist_albums_with_limit_10(self):
        from src.services import discography as D
        for mode in (None, "2026"):
            self.reset()
            fake, sp = self.guarded(mode)
            dgd = self.app.spotify_search_artist(sp, "Dance Gavin Dance")
            calls = []
            real = fake.artist_albums

            def spy(artist_id, **kw):
                calls.append(kw)
                return real(artist_id, **kw)

            fake.artist_albums = spy
            albums = D.fetch_artist_albums(sp, dgd["id"])
            self.assertEqual(sorted(x["name"] for x in albums), ["Jackpot Juicer", "Mothership", "Prisoner (Single)"])
            self.assertTrue(calls and all(kw["limit"] <= 10 for kw in calls))
            data = D.build_discography_from_albums(albums, None, artist_name="Dance Gavin Dance", artist_id=dgd["id"])
            self.assertTrue(data)


if __name__ == "__main__":
    unittest.main()
