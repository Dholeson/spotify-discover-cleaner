"""Spotify rate-limit guard (GuardedSpotify and friends), search helpers, token handling and
the Last.fm client's failure semantics.

Scenarios ported from the audit's guard reproductions (notes/guardtest, notes/bv_core).
Runs against an isolated copy of app.py (tests/platform_harness.py) with a fake Spotify
client; no network, no real data.
"""

import os
import sys
import threading
import time
import unittest
from collections import deque
from unittest import mock

import spotipy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402


class FakeSpotify:
    """Records every raw call. Playlists page 100 items at a time, like Spotify."""

    def __init__(self, playlist_size=1200, liked_size=300):
        self.calls = []
        self.raise_429 = set()
        self.playlists = {"pid": [f"t{i:04d}" for i in range(playlist_size)], "otherpid": ["x1", "x2"]}
        self.liked_size = liked_size
        self._lock = threading.Lock()

    def _rec(self, name):
        with self._lock:
            self.calls.append(name)
        if name in self.raise_429:
            raise spotipy.SpotifyException(429, -1, "rate limited", headers={"Retry-After": "600"})

    def count(self, name):
        with self._lock:
            return self.calls.count(name)

    # playback
    def current_playback(self, *a, **k):
        self._rec("current_playback")
        return {"is_playing": True}

    def devices(self, *a, **k):
        self._rec("devices")
        return {"devices": [{"id": "d1", "is_active": True}]}

    def pause_playback(self, *a, **k):
        self._rec("pause_playback")

    # playlists
    def playlist(self, pid, fields=None, **k):
        self._rec("playlist")
        return {"name": "Backlog", "snapshot_id": f"s{len(self.calls)}",
                "tracks": {"total": len(self.playlists.get(pid, []))}}

    def _page(self, pid, offset, limit=100):
        ids = self.playlists.get(pid, [])
        items = [{"track": {"id": t, "type": "track"}} for t in ids[offset:offset + limit]]
        nxt = f"{pid}|{offset + limit}" if offset + limit < len(ids) else None
        return {"items": items, "next": nxt, "offset": offset, "limit": limit, "total": len(ids)}

    def playlist_items(self, pid, limit=100, offset=0, **k):
        self._rec("playlist_items")
        return self._page(pid, offset, limit)

    def next(self, page):
        self._rec("next")
        if not page or not page.get("next"):
            return None
        pid, off = page["next"].split("|")
        return self._page(pid, int(off), page.get("limit") or 100)

    def playlist_remove_all_occurrences_of_items(self, pid, items, snapshot_id=None):
        self._rec("playlist_remove_all_occurrences_of_items")
        self.playlists[pid] = [t for t in self.playlists.get(pid, []) if t not in items]
        return {"snapshot_id": "after-remove"}

    # metadata / search
    def artist(self, aid):
        self._rec("artist")
        return {"id": aid, "name": f"Artist {aid}", "genres": ["rock", "metal"]}

    def search(self, q="", type="track", limit=1, **k):
        self._rec("search")
        return {"artists": {"items": [{"id": "a-" + q[:10], "name": q, "external_urls": {}, "images": []}]},
                "tracks": {"items": [{"id": "trk1", "name": "Song", "uri": "spotify:track:trk1",
                                      "artists": [{"id": "ar1", "name": "Band"}],
                                      "album": {"id": "al1", "name": "LP", "images": []},
                                      "external_urls": {}, "duration_ms": 1000}]}}

    # library
    def current_user_saved_tracks(self, limit=20, offset=0, **k):
        self._rec("current_user_saved_tracks")
        n = max(0, min(limit, self.liked_size - offset))
        items = [{"track": {"id": f"liked{offset + i}", "name": "L", "artists": [{"name": f"Artist{(offset + i) % 7}"}],
                            "album": {"name": "A", "images": []}}, "added_at": "2026-01-01T00:00:00Z"}
                 for i in range(n)]
        return {"items": items, "next": "more" if offset + n < self.liked_size else None}

    def current_user_saved_tracks_contains(self, ids):
        self._rec("current_user_saved_tracks_contains")
        return [True for _ in ids]

    def current_user_saved_tracks_add(self, ids):
        self._rec("current_user_saved_tracks_add")

    def current_user(self):
        self._rec("current_user")
        return {"id": "user1", "display_name": "User"}


class GuardBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        self._saved = {
            "_SPOTIFY_MIN_INTERVAL_S": a._SPOTIFY_MIN_INTERVAL_S,
            "_SPOTIFY_LANE_MIN_INTERVAL_S": dict(a._SPOTIFY_LANE_MIN_INTERVAL_S),
            "_SP_SEARCH_MIN_INTERVAL": a._SP_SEARCH_MIN_INTERVAL,
            "_SQLITE_ENABLED": a._SQLITE_ENABLED,
            "_SPOTIFY_GUARD_CACHE_MAX": a._SPOTIFY_GUARD_CACHE_MAX,
        }
        # Pacing is tested separately; the rest must not wait 0.35-1.1 s per call.
        a._SPOTIFY_MIN_INTERVAL_S = 0.0
        for lane in a._SPOTIFY_LANE_MIN_INTERVAL_S:
            a._SPOTIFY_LANE_MIN_INTERVAL_S[lane] = 0.0
        a._SP_SEARCH_MIN_INTERVAL = 0.0
        a._SQLITE_ENABLED = False
        self.reset_guard()
        self.fake = FakeSpotify()
        self.sp = a.GuardedSpotify(self.fake)

    def tearDown(self):
        a = self.app
        a._SPOTIFY_MIN_INTERVAL_S = self._saved["_SPOTIFY_MIN_INTERVAL_S"]
        a._SPOTIFY_LANE_MIN_INTERVAL_S.clear()
        a._SPOTIFY_LANE_MIN_INTERVAL_S.update(self._saved["_SPOTIFY_LANE_MIN_INTERVAL_S"])
        a._SP_SEARCH_MIN_INTERVAL = self._saved["_SP_SEARCH_MIN_INTERVAL"]
        a._SQLITE_ENABLED = self._saved["_SQLITE_ENABLED"]
        a._SPOTIFY_GUARD_CACHE_MAX = self._saved["_SPOTIFY_GUARD_CACHE_MAX"]
        self.reset_guard()

    def reset_guard(self):
        a = self.app
        a._clear_guard_state(persist=False, include_search=True)
        a._SPOTIFY_APP_REG_BLOCK_UNTIL = 0.0
        a._SPOTIFY_GUARD_CACHE.clear()
        a._SPOTIFY_INFLIGHT.clear()
        a.artist_genre_cache.clear()
        a._sp_artist_cache.clear()
        a._sp_track_cache.clear()
        a._liked_track_cache.clear()
        a.invalidate_liked_artist_counts_cache()

    def fill_budget(self, method, age_s=0.0):
        a = self.app
        limit, _ = a._spotify_guard_policy(method)["budget"]
        now = time.monotonic()
        a._SPOTIFY_GUARD_BUDGET_BUCKETS[method] = deque([now - age_s] * limit)

    def wait_for(self, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()


class StaleWhileRevalidateTest(GuardBase):
    def _make_stale(self):
        a = self.app
        self.sp.playlist("pid", fields="name")
        key = a._spotify_guard_cache_key("playlist", ("pid",), {"fields": "name"})
        a._SPOTIFY_GUARD_CACHE[key]["fresh_until"] = time.monotonic() - 1
        return key

    def test_global_lock_stale_reads_make_no_raw_calls(self):
        # WP-02 ACCEPT 1: with a 600 s global lock, 4 stale reads make 0 raw calls (was 4).
        a = self.app
        self._make_stale()
        a._spotify_guard_note_rate_limit("current_playback", 600)
        before = self.fake.count("playlist")
        for _ in range(4):
            self.assertEqual(self.sp.playlist("pid", fields="name")["name"], "Backlog")
        time.sleep(0.3)
        self.assertEqual(self.fake.count("playlist") - before, 0)
        with self.assertRaises(a.SpotifyGuardBlocked) as cm:
            self.sp.playlist("never-cached", fields="name")
        self.assertTrue(cm.exception.local)
        self.assertEqual(cm.exception.kind, "cooldown")
        self.assertGreater(cm.exception.retry_after, 500)
        self.assertFalse(a.is_real_rate_limit(cm.exception))

    def test_protect_mode_stale_reads_make_no_raw_calls(self):
        a = self.app
        self._make_stale()
        a._SPOTIFY_GUARD_PROTECT_MODE_UNTIL = time.monotonic() + 300
        before = self.fake.count("playlist")
        for _ in range(3):
            self.sp.playlist("pid", fields="name")
        time.sleep(0.3)
        self.assertEqual(self.fake.count("playlist") - before, 0)

    def test_unblocked_stale_read_refreshes_once_and_uses_budget(self):
        a = self.app
        key = self._make_stale()
        used_before = len(a._SPOTIFY_GUARD_BUDGET_BUCKETS.get("playlist", ()))
        before = self.fake.count("playlist")
        for _ in range(4):
            self.sp.playlist("pid", fields="name")
        self.assertTrue(self.wait_for(lambda: self.fake.count("playlist") - before == 1))
        time.sleep(0.2)
        self.assertEqual(self.fake.count("playlist") - before, 1)
        self.assertEqual(len(a._SPOTIFY_GUARD_BUDGET_BUCKETS["playlist"]) - used_before, 1)
        self.assertTrue(self.wait_for(lambda: a._SPOTIFY_GUARD_CACHE[key]["fresh_until"] > time.monotonic()))

    def test_full_budget_blocks_the_refresh_but_serves_stale(self):
        a = self.app
        self._make_stale()
        self.fill_budget("playlist")
        before = self.fake.count("playlist")
        self.assertEqual(self.sp.playlist("pid", fields="name")["name"], "Backlog")
        time.sleep(0.3)
        self.assertEqual(self.fake.count("playlist") - before, 0)

    def test_worker_rechecks_locks_before_the_raw_call(self):
        a = self.app
        key = self._make_stale()
        a._spotify_guard_note_rate_limit("current_playback", 600)
        attr = self.fake.playlist
        a._SPOTIFY_GUARD_CACHE[key]["refreshing"] = True
        a._spotify_trigger_background_refresh(self.fake, "playlist", attr, ("pid",), {"fields": "name"}, key,
                                              a._spotify_guard_policy("playlist"))
        time.sleep(0.3)
        self.assertEqual(self.fake.count("playlist"), 1)  # only the initial populate
        self.assertFalse(a._SPOTIFY_GUARD_CACHE[key]["refreshing"])


class BudgetTest(GuardBase):
    def test_budgets_are_per_method(self):
        # WP-02 ACCEPT 1: 20 current_playback calls followed by devices: devices is allowed.
        for _ in range(20):
            self.sp.current_playback()
        self.assertEqual(self.sp.devices()["devices"][0]["id"], "d1")
        for _ in range(20):
            self.sp.current_playback()
        with self.assertRaises(self.app.SpotifyGuardBlocked) as cm:
            self.sp.current_playback()  # 41st in a minute
        self.assertEqual(cm.exception.kind, "budget")
        self.assertTrue(cm.exception.local)
        self.assertGreaterEqual(cm.exception.retry_after, 1)
        self.sp.devices()  # still unaffected

    def test_paging_a_1200_track_playlist_twice(self):
        # WP-02 ACCEPT 1: paging a fake 1,200-item playlist with next completes.
        for _ in range(2):
            ids = []
            page = self.sp.playlist_items("pid", limit=100, offset=0)
            while True:
                ids.extend(i["track"]["id"] for i in page["items"])
                if not page.get("next"):
                    break
                page = self.sp.next(page)
            self.assertEqual(len(ids), 1200)
            self.assertEqual(len(set(ids)), 1200)

    def test_policies(self):
        a = self.app
        for m in ("playlist_remove_all_occurrences_of_items", "playlist_add_items", "current_user_saved_tracks_add",
                  "current_user_saved_tracks_delete", "start_playback", "pause_playback", "next_track",
                  "previous_track", "seek_track", "add_to_queue"):
            self.assertTrue(a._spotify_guard_policy(m).get("essential"), m)
        self.assertTrue(a._spotify_guard_policy("playlist_reorder_items")["essential"])  # unknown write
        self.assertTrue(a._spotify_guard_policy("current_user_saved_albums_add")["essential"])
        self.assertFalse(a._spotify_guard_policy("album_whatever_read").get("essential"))
        budgets = {"current_playback": 40, "devices": 10, "queue": 6, "next": 40, "playlist_items": 40,
                   "playlist": 20, "current_user_saved_tracks": 40, "current_user_saved_tracks_contains": 20,
                   "search": 15, "artist": 20, "artist_albums": 20, "artist_top_tracks": 10, "album": 20,
                   "album_tracks": 20}
        for method, limit in budgets.items():
            self.assertEqual(a._spotify_guard_policy(method)["budget"][0], limit, method)

    def test_protect_mode_allows_essential_writes_only(self):
        # WP-02 ACCEPT 1: in protect mode, the playlist removal is allowed.
        a = self.app
        a._SPOTIFY_GUARD_PROTECT_MODE_UNTIL = time.monotonic() + 300
        self.sp.playlist_remove_all_occurrences_of_items("pid", ["t0001"])
        self.sp.current_playback()
        with self.assertRaises(a.SpotifyGuardBlocked) as cm:
            self.sp.search(q="x", type="artist", limit=1)
        self.assertEqual(cm.exception.kind, "protect")

    def test_real_429_locks_everything_including_writes(self):
        a = self.app
        self.fake.raise_429.add("current_playback")
        with self.assertRaises(spotipy.SpotifyException) as cm:
            self.sp.current_playback()
        self.assertTrue(a.is_real_rate_limit(cm.exception))
        with self.assertRaises(a.SpotifyGuardBlocked):
            self.sp.pause_playback()
        self.assertGreater(a._spotify_guard_remaining_s(), 500)


class CacheTest(GuardBase):
    def test_write_invalidates_that_playlist_only(self):
        # WP-02 ACCEPT 1: after the removal no cache key containing the pid remains.
        a = self.app
        self.sp.playlist("pid", fields="name")
        self.sp.playlist_items("pid", limit=100, offset=0)
        self.sp.playlist_items("pid", limit=100, offset=100)
        self.sp.playlist("otherpid", fields="name")
        self.assertTrue(any("pid" in k and "otherpid" not in k for k in a._SPOTIFY_GUARD_CACHE))
        self.sp.playlist_remove_all_occurrences_of_items("pid", ["t0005"])
        remaining = [k for k in a._SPOTIFY_GUARD_CACHE if '"pid"' in k]
        self.assertEqual(remaining, [])
        self.assertTrue(any("otherpid" in k for k in a._SPOTIFY_GUARD_CACHE))
        before = self.fake.count("playlist_items")
        page = self.sp.playlist_items("pid", limit=100, offset=0)
        self.assertEqual(self.fake.count("playlist_items"), before + 1)  # fresh read after the write
        self.assertNotIn("t0005", [i["track"]["id"] for i in page["items"]])

    def test_library_write_invalidates_saved_track_caches(self):
        a = self.app
        self.sp.current_user_saved_tracks_contains(["abc"])
        self.sp.current_user_saved_tracks(limit=50, offset=0)
        self.sp.current_user_saved_tracks_add(["abc"])
        self.assertEqual([k for k in a._SPOTIFY_GUARD_CACHE if k.startswith("current_user_saved_tracks")], [])

    def test_returned_values_are_copies(self):
        # WP-02 ACCEPT 1: mutating a returned artist dict does not change the cached value.
        a1 = self.sp.artist("A")
        a1["genres"].append("mutated")
        a1["name"] = "changed"
        a2 = self.sp.artist("A")
        self.assertEqual(a2["genres"], ["rock", "metal"])
        self.assertEqual(a2["name"], "Artist A")
        self.assertEqual(self.fake.count("artist"), 1)

    def test_cache_is_bounded_lru(self):
        a = self.app
        a._SPOTIFY_GUARD_CACHE_MAX = 5
        for i in range(8):
            self.sp.artist(f"A{i}")
        self.assertLessEqual(len(a._SPOTIFY_GUARD_CACHE), 5)
        self.assertFalse(any('"A0"' in k for k in a._SPOTIFY_GUARD_CACHE))
        self.assertTrue(any('"A7"' in k for k in a._SPOTIFY_GUARD_CACHE))

    def test_explicit_invalidation_helpers(self):
        a = self.app
        self.sp.playlist("pid", fields="name")
        self.sp.playlist("otherpid", fields="name")
        a.invalidate_playlist_cache("https://open.spotify.com/playlist/pid")  # not a real id: raw needle
        a.invalidate_playlist_cache("pid")
        self.assertFalse(any('"pid"' in k for k in a._SPOTIFY_GUARD_CACHE))
        self.assertEqual(a._spotify_cache_invalidate(("playlist",)), 1)


class LocalBlockSemanticsTest(GuardBase):
    def test_genre_lookups_over_budget_cache_nothing(self):
        # WP-02 ACCEPT 1: 7 genre lookups over budget cache no [] and leave last_error alone.
        a = self.app
        self.fill_budget("artist")
        with a.state_lock:
            a.state["last_error"] = "sentinel"
        try:
            for i in range(7):
                a.ensure_artist_genre_cache(self.sp, [f"art{i}"])
            a.ensure_artist_genre_cache(self.sp, [f"art{i}" for i in range(7)])
            self.assertEqual({k: v for k, v in a.artist_genre_cache.items() if k.startswith("art")}, {})
            with a.state_lock:
                self.assertEqual(a.state["last_error"], "sentinel")
            self.assertEqual(self.fake.count("artist"), 0)
            # Once the budget frees up, lookups work and are cached.
            a._SPOTIFY_GUARD_BUDGET_BUCKETS["artist"].clear()
            self.assertEqual(a.fetch_genres_for_artists(self.sp, ["art1"]), ["rock", "metal"])
        finally:
            with a.state_lock:
                a.state["last_error"] = None

    def test_genre_real_404_is_cached_as_empty(self):
        a = self.app

        def gone(aid):
            raise spotipy.SpotifyException(404, -1, "gone")

        self.fake.artist = gone
        a.ensure_artist_genre_cache(a.GuardedSpotify(self.fake), ["dead"])
        self.assertEqual(a.artist_genre_cache.get("dead"), [])

    def test_search_local_block_is_not_a_backoff(self):
        a = self.app
        self.fill_budget("search")
        self.assertIsNone(a.spotify_search_artist(self.sp, "Hail The Sun"))
        self.assertIsNone(a.spotify_search_track(self.sp, "Hail The Sun", "Song"))
        self.assertEqual(a._sp_search_backoff_remaining_s(), 0)
        self.assertNotIn("hail the sun", a._sp_artist_cache)
        a._SPOTIFY_GUARD_BUDGET_BUCKETS["search"].clear()
        self.assertEqual(a.spotify_search_artist(self.sp, "Hail The Sun")["name"], 'artist:"Hail The Sun"')

    def test_search_real_429_sets_backoff(self):
        a = self.app
        self.fake.raise_429.add("search")
        self.assertIsNone(a.spotify_search_artist(self.sp, "Someone"))
        self.assertGreater(a._sp_search_backoff_remaining_s(), 500)

    def test_is_track_liked_unknown_on_block(self):
        a = self.app
        self.fill_budget("current_user_saved_tracks_contains")
        self.assertIsNone(a.is_track_liked(self.sp, "trackX"))
        a._SPOTIFY_GUARD_BUDGET_BUCKETS["current_user_saved_tracks_contains"].clear()
        self.assertTrue(a.is_track_liked(self.sp, "trackX"))

    def test_liked_scan_waits_out_a_short_block(self):
        a = self.app
        self.fill_budget("current_user_saved_tracks", age_s=59.0)  # frees in ~1 s
        t0 = time.monotonic()
        counts = a.get_liked_artist_counts(self.sp, force=True)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(sum(counts.values()), 300)

    def test_liked_scan_gives_up_on_a_long_block(self):
        a = self.app
        a._SPOTIFY_GUARD_PROTECT_MODE_UNTIL = time.monotonic() + 3000
        t0 = time.monotonic()
        with self.assertRaises(a.SpotifyGuardBlocked):
            a.get_liked_artist_counts(self.sp, force=True)
        self.assertLess(time.monotonic() - t0, 2)

    def test_app_registration_block_is_local_and_does_not_disable_features(self):
        a = self.app
        a._SPOTIFY_APP_REG_BLOCK_UNTIL = time.monotonic() + 100
        a._SPOTIFY_APP_REG_BLOCK_MSG = "Check settings on developer.spotify.com/dashboard, the user may not be registered for this application"
        with self.assertRaises(a.SpotifyAppRegistrationBlocked) as cm:
            self.sp.current_playback()
        self.assertTrue(a.is_local_guard_block(cm.exception))
        self.assertFalse(a.spotify_feature_removed(cm.exception))


class ThrottleTest(GuardBase):
    def setUp(self):
        super().setUp()
        a = self.app
        a._SPOTIFY_MIN_INTERVAL_S = self._saved["_SPOTIFY_MIN_INTERVAL_S"]
        a._SPOTIFY_LANE_MIN_INTERVAL_S.update(self._saved["_SPOTIFY_LANE_MIN_INTERVAL_S"])
        a._SPOTIFY_LAST_CALL_TIME = 0.0
        a._SPOTIFY_LAST_CALL_TIME_BY_LANE.clear()

    def test_write_right_after_poll_waits_at_most_0_4s(self):
        # WP-02 ACCEPT 1
        a = self.app
        a._spotify_throttle("realtime")
        t0 = time.monotonic()
        a._spotify_throttle("write")
        self.assertLess(time.monotonic() - t0, 0.4)

    def test_realtime_is_not_stuck_behind_a_sleeping_background_call(self):
        a = self.app
        a._spotify_throttle("background")
        done = {}

        def bg():
            t = time.monotonic()
            a._spotify_throttle("background")  # sleeps out its 1.1 s lane gap
            done["bg"] = time.monotonic() - t

        th = threading.Thread(target=bg)
        th.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        a._spotify_throttle("realtime")
        self.assertLess(time.monotonic() - t0, 0.4)
        th.join()
        self.assertGreater(done["bg"], 0.8)

    def test_search_throttle_never_waits_out_a_backoff(self):
        a = self.app
        a._SP_SEARCH_MIN_INTERVAL = 0.4
        a._sp_search_backoff_until = time.monotonic() + 60
        t0 = time.monotonic()
        a._sp_search_throttle()
        a._sp_search_throttle()
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_http_retries_only_idempotent_methods(self):
        sess = self.app._make_sane_session()
        retry = sess.get_adapter("https://api.spotify.com").max_retries
        self.assertEqual(set(retry.allowed_methods), {"GET", "HEAD", "OPTIONS"})
        self.assertFalse(retry.respect_retry_after_header)


class SearchRouteTest(GuardBase):
    def test_search_route_answers_429_immediately_during_backoff(self):
        # WP-02 ACCEPT 3
        a = self.app
        client = a.app.test_client()
        with mock.patch.object(a, "get_sp", return_value=self.sp):
            ok = client.get("/api/search?q=song")
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.get_json()["tracks"][0]["id"], "trk1")
            a._sp_search_backoff_until = time.monotonic() + 120
            t0 = time.monotonic()
            r = client.get("/api/search?q=other")
            elapsed = time.monotonic() - t0
        self.assertEqual(r.status_code, 429)
        self.assertLess(elapsed, 0.2)
        err = r.get_json()["error"]
        self.assertEqual(err["code"], "rate_limited")
        self.assertGreater(err["retry_after"], 100)

    def test_search_route_local_budget_block_is_429_without_backoff(self):
        a = self.app
        self.fill_budget("search")
        client = a.app.test_client()
        with mock.patch.object(a, "get_sp", return_value=self.sp):
            r = client.get("/api/search?q=zzz")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.get_json()["error"]["local"])
        self.assertEqual(a._sp_search_backoff_remaining_s(), 0)

    def test_dev_hook_forces_backoff(self):
        a = self.app
        client = a.app.test_client()
        r = client.post("/api/dev/search-backoff", json={"seconds": 30})
        self.assertEqual(r.status_code, 200)
        self.assertGreater(a._sp_search_backoff_remaining_s(), 25)
        client.post("/api/dev/search-backoff", json={"seconds": 0})
        self.assertEqual(a._sp_search_backoff_remaining_s(), 0)


class StatusSummaryTest(GuardBase):
    def test_states(self):
        a = self.app
        with mock.patch.object(a, "get_cached_token", return_value=None):
            self.assertEqual(a.spotify_status_summary()["state"], "signed_out")
        tok = {"access_token": "x", "expires_at": time.time() + 3600}
        with mock.patch.object(a, "get_cached_token", return_value=tok):
            s = a.spotify_status_summary()
            self.assertEqual((s["state"], s["message"]), ("ok", "Spotify OK"))
            a._sp_search_backoff_until = time.monotonic() + 90
            s = a.spotify_status_summary()
            self.assertEqual(s["state"], "pacing")
            self.assertIn("Spotify search", s["paused"])
            a._SPOTIFY_GUARD_PROTECT_MODE_UNTIL = time.monotonic() + 300
            s = a.spotify_status_summary()
            self.assertEqual(s["state"], "protect")
            self.assertIn("Auto-clear", s["working"])
            a._spotify_guard_note_rate_limit("search", 7860)
            s = a.spotify_status_summary()
            self.assertEqual(s["state"], "rate_limited")
            self.assertIn("2h 11m", s["message"])
            self.assertIn("Auto-clear", s["paused"])
            self.assertIn("History & stats", s["working"])
            self.assertGreater(s["until_ms"], time.time() * 1000 + 7000 * 1000)
            a._SPOTIFY_APP_REG_BLOCK_UNTIL = time.monotonic() + 100
            self.assertEqual(a.spotify_status_summary()["state"], "app_not_registered")

    def test_status_route_includes_summary(self):
        a = self.app
        status = a.app.test_client().get("/api/status").get_json()
        self.assertIn(status["spotify_status"]["state"],
                      {"ok", "pacing", "rate_limited", "protect", "app_not_registered", "signed_out"})
        for key in ("until_ms", "reason", "message", "paused", "working"):
            self.assertIn(key, status["spotify_status"])


class RestoredLockoutTest(GuardBase):
    def test_first_live_success_clears_restored_marker(self):
        a = self.app
        a._SPOTIFY_GUARD_RESTORED_PENDING = True
        a._SPOTIFY_GUARD_RESTORED_UNTIL_WALL = time.time() + 40000
        a._SPOTIFY_GUARD_REVALIDATING = True  # keep get_sp()'s re-check out of this test
        try:
            self.sp.current_playback()
            self.assertFalse(a._SPOTIFY_GUARD_RESTORED_PENDING)
            self.assertEqual(a._spotify_guard_restored_remaining_s(), 0)
        finally:
            a._SPOTIFY_GUARD_REVALIDATING = False

    def test_get_sp_revalidates_once_signed_in(self):
        a = self.app
        a._SPOTIFY_GUARD_RESTORED_PENDING = True
        a._SPOTIFY_GUARD_RESTORED_UNTIL_WALL = time.time() + 40000
        tok = {"access_token": "tok-restored", "expires_at": time.time() + 3600, "scope": a.SCOPE}
        fake = self.fake
        with mock.patch.object(a, "get_cached_token", return_value=tok), \
                mock.patch.object(a.spotipy, "Spotify", return_value=fake):
            a._sp_client_cache.update(access_token=None, client=None)
            client = a.get_sp()
            self.assertIsNotNone(client)
            self.assertTrue(self.wait_for(lambda: not a._SPOTIFY_GUARD_RESTORED_PENDING))
        self.assertIn("current_user", fake.calls)
        a._sp_client_cache.update(access_token=None, client=None)


class TokenTest(GuardBase):
    def test_refresh_is_single_flight_and_kept_in_memory(self):
        a = self.app
        expired = {"access_token": "old", "refresh_token": "r", "scope": a.SCOPE, "expires_at": time.time() - 10}
        refreshed = {"access_token": "new", "refresh_token": "r", "scope": a.SCOPE, "expires_at": time.time() + 3600}
        calls = {"read": 0, "refresh": 0}

        class Handler:
            def get_cached_token(self_inner):
                calls["read"] += 1
                return dict(expired)

        class FakeOAuth:
            cache_handler = Handler()

            def is_token_expired(self_inner, info):
                return info["expires_at"] - time.time() < 60

            def refresh_access_token(self_inner, refresh_token):
                calls["refresh"] += 1
                time.sleep(0.2)
                return dict(refreshed)

        saved = a._oauth
        a._oauth = FakeOAuth()
        a.reset_token_memory()
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(a.get_cached_token())) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(calls["refresh"], 1)
            self.assertTrue(all(r and r["access_token"] == "new" for r in results))
            reads = calls["read"]
            a.get_cached_token()
            self.assertEqual(calls["read"], reads)  # served from memory
        finally:
            a._oauth = saved
            a.reset_token_memory()

    def test_transient_refresh_failure_keeps_a_still_valid_token(self):
        a = self.app
        almost = {"access_token": "almost", "refresh_token": "r", "scope": a.SCOPE, "expires_at": time.time() + 30}

        class FakeOAuth:
            class cache_handler:
                @staticmethod
                def get_cached_token():
                    return dict(almost)

            @staticmethod
            def is_token_expired(info):
                return True

            @staticmethod
            def refresh_access_token(refresh_token):
                raise ConnectionError("offline")

        saved = a._oauth
        a._oauth = FakeOAuth()
        a.reset_token_memory()
        a._token_mem = dict(almost)
        try:
            self.assertEqual(a.get_cached_token()["access_token"], "almost")
        finally:
            a._oauth = saved
            a.reset_token_memory()


class LastfmClientTest(unittest.TestCase):
    """WP-07 (5): failures are never cached for long; genuine not-found is."""

    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        platform_harness.reset_env_file(self.app)
        self.app.lastfm_reset_caches()

    def tearDown(self):
        self.app.lastfm_reset_caches()

    def test_failure_is_not_cached_and_next_call_fetches_again(self):
        # WP-07 ACCEPT 4
        a = self.app
        ok = {"similarartists": {"artist": [{"name": "Sister Band", "match": "0.9", "url": "u"}]}}
        responses = [a._LFM_FAIL, ok]
        calls = []

        def fake_call(method, **params):
            calls.append(method)
            return responses.pop(0)

        with mock.patch.object(a, "lastfm_call", side_effect=fake_call):
            self.assertEqual(a.lastfm_similar_artists("Band"), [])
            got = a.lastfm_similar_artists("Band")
            self.assertEqual(got[0]["name"], "Sister Band")
            self.assertEqual(len(calls), 2)
            a.lastfm_similar_artists("Band")
            self.assertEqual(len(calls), 2)  # success is cached

    def test_artist_info_failure_retried_and_not_found_cached_as_missing(self):
        a = self.app
        calls = []

        def fake_call(method, **params):
            calls.append(params.get("artist"))
            return a._LFM_MISSING if params.get("artist") == "Nobody" else a._LFM_FAIL

        with mock.patch.object(a, "lastfm_call", side_effect=fake_call):
            self.assertIsNone(a.lastfm_artist_info("Nobody"))
            self.assertIsNone(a.lastfm_artist_info("Nobody"))
            self.assertEqual(calls.count("Nobody"), 1)
            self.assertEqual(a._lfm_cache_get("artistinfo:nobody", 3600), {"missing": True})
            self.assertIsNone(a.lastfm_artist_info("Flaky"))
            self.assertIsNone(a.lastfm_artist_info("Flaky"))
            self.assertEqual(calls.count("Flaky"), 2)

    def test_list_not_found_is_cached_as_empty_list(self):
        a = self.app
        with mock.patch.object(a, "lastfm_call", return_value=a._LFM_MISSING) as call:
            self.assertEqual(a.lastfm_artist_top_tags("Nobody"), [])
            self.assertEqual(a.lastfm_artist_top_tags("Nobody"), [])
            self.assertEqual(call.call_count, 1)
        self.assertEqual(a._lfm_cache_get("artist_top_tags:nobody:6", 3600), [])

    def test_breaker_fails_fast_after_repeated_failures(self):
        a = self.app
        with mock.patch.object(a, "lastfm_call", return_value=a._LFM_FAIL) as call:
            for i in range(5):
                a.lastfm_similar_artists(f"A{i}")
            self.assertEqual(call.call_count, 5)
            a.lastfm_similar_artists("A99")
            self.assertEqual(call.call_count, 5)
        a.lastfm_reset_caches()

    def test_user_top_artists_keeps_previous_list_on_failure(self):
        a = self.app
        a._lastfm_user_top_artists.update(data=["old"], fetched_at=0)
        with mock.patch.object(a, "lastfm_call", return_value=a._LFM_FAIL):
            self.assertEqual(a.lastfm_user_top_artists(), ["old"])
        self.assertEqual(a._lastfm_user_top_artists["fetched_at"], 0)

    def test_lastfm_call_classifies_responses(self):
        a = self.app

        class Resp:
            def __init__(self, status, body):
                self.status_code, self._body = status, body

            def json(self):
                if isinstance(self._body, Exception):
                    raise self._body
                return self._body

        cases = [
            (Resp(200, {"artist": {"name": "X"}}), dict),
            (Resp(200, {"error": 6, "message": "The artist you supplied could not be found"}), a._LFM_MISSING),
            (Resp(404, {"error": 6, "message": "not found"}), a._LFM_MISSING),
            (Resp(200, {"error": 29, "message": "Rate limit exceeded"}), a._LFM_FAIL),
            (Resp(500, ValueError("no json")), a._LFM_FAIL),
            (Resp(200, ["not", "a", "dict"]), a._LFM_FAIL),
        ]
        for resp, want in cases:
            with mock.patch.object(a.requests, "get", return_value=resp):
                got = a.lastfm_call("artist.getInfo", artist="X")
            if want is dict:
                self.assertIsInstance(got, dict)
            else:
                self.assertIs(got, want)
                self.assertFalse(got)
                self.assertNotIsInstance(got, dict)
        with mock.patch.object(a.requests, "get", side_effect=ConnectionError("offline")):
            self.assertIs(a.lastfm_call("artist.getInfo", artist="X"), a._LFM_FAIL)
        os.environ.pop("LASTFM_API_KEY", None)
        self.assertIs(a.lastfm_call("artist.getInfo", artist="X"), a._LFM_FAIL)

    def test_every_request_has_a_bounded_timeout(self):
        a = self.app
        with mock.patch.object(a.requests, "get", side_effect=ConnectionError("offline")) as get:
            a.lastfm_call("artist.getInfo", artist="X")
        timeout = get.call_args.kwargs.get("timeout")
        parts = timeout if isinstance(timeout, tuple) else (timeout,)
        self.assertTrue(parts and all(isinstance(t, (int, float)) and 0 < t <= 15 for t in parts), timeout)

    def test_unreachable_lastfm_fails_fast_after_one_attempt(self):
        # An artist page makes several Last.fm calls: when Last.fm can't be reached, only the first
        # may wait for the network; the rest must fail at once (no stacked timeouts).
        a = self.app
        import requests as _requests
        with mock.patch.object(a.requests, "get", side_effect=_requests.exceptions.ConnectTimeout("timed out")) as get:
            self.assertEqual(a.lastfm_similar_artists("Band A"), [])
            self.assertEqual(a.lastfm_artist_top_tags("Band B"), [])
            self.assertIsNone(a.lastfm_artist_info("Band C"))
        self.assertEqual(get.call_count, 1)
        a.lastfm_reset_caches()
        ok = {"similarartists": {"artist": [{"name": "Sister Band", "match": "0.9", "url": "u"}]}}

        class Resp:
            status_code = 200

            def json(self):
                return ok

        with mock.patch.object(a.requests, "get", return_value=Resp()):
            self.assertEqual(a.lastfm_similar_artists("Band A")[0]["name"], "Sister Band")


if __name__ == "__main__":
    unittest.main()
