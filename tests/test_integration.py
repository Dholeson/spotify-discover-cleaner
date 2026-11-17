"""Cross-package integration regressions found while wiring the v2 packages together.

Runs against the isolated app copy from tests/platform_harness.py (no network, no real data).
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402


def _track(tid, *artists):
    return {"id": tid, "name": f"Song {tid}", "artists": ", ".join(artists),
            "artist_list": [{"id": None, "name": n} for n in artists]}


class LikeArtistCountsTest(unittest.TestCase):
    """A Like must not re-page the whole Liked Songs library inside the request."""

    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        self.app.invalidate_liked_artist_counts_cache()
        self.addCleanup(self.app.invalidate_liked_artist_counts_cache)

    def _warm(self, counts):
        a = self.app
        with a._LIKED_ARTIST_COUNT_LOCK:
            a._LIKED_ARTIST_COUNT_CACHE.update(counts=dict(counts), names={}, fetched_at=time.time())

    def test_like_counts_into_a_warm_cache(self):
        a = self.app
        self._warm({"woe, is me": 2})
        a.note_liked_track_in_artist_counts(_track("t1", "Woe, Is Me", "Guest"))
        counts = a._LIKED_ARTIST_COUNT_CACHE["counts"]
        self.assertEqual(counts["woe, is me"], 3)
        self.assertEqual(counts["guest"], 1)
        self.assertNotIn("woe", counts)
        self.assertGreater(a._LIKED_ARTIST_COUNT_CACHE["fetched_at"], 0)  # still warm

    def test_cold_cache_is_left_for_the_next_full_count(self):
        a = self.app
        a.note_liked_track_in_artist_counts(_track("t1", "Solo"))
        self.assertEqual(a._LIKED_ARTIST_COUNT_CACHE["counts"], {})

    def _run_check(self, track, history, sp):
        a = self.app
        events = mock.Mock()
        events.is_artist_liked.return_value = False
        with mock.patch.object(a, "_SQLITE_ENABLED", True), \
                mock.patch.object(a, "_events", events), \
                mock.patch.object(a, "current_auto_artist_like_threshold", return_value=3), \
                mock.patch.object(a, "is_favorite_artist_opted_out", return_value=False), \
                mock.patch.object(a, "add_favorite_artist") as add_fav, \
                mock.patch.dict(a.state, {"event_history": history}):
            newly = a._check_auto_artist_like(sp, track)
        return newly, events, add_fav

    def test_comma_named_band_is_one_artist(self):
        history = [dict(_track(f"h{i}", "Woe, Is Me"), type="liked") for i in range(3)]
        sp = mock.Mock()
        self._warm({"woe, is me": 1})
        newly, events, add_fav = self._run_check(_track("t9", "Woe, Is Me"), history, sp)
        self.assertEqual(newly, ["Woe, Is Me"])
        names = [c.kwargs["artist_name"] for c in events.upsert_artist_like.call_args_list]
        self.assertEqual(names, ["Woe, Is Me"])
        add_fav.assert_called_once_with("Woe, Is Me", source="auto")
        sp.current_user_saved_tracks.assert_not_called()

    def test_cold_cache_warms_in_background_not_in_the_request(self):
        a = self.app
        sp = mock.Mock()
        with mock.patch.object(a, "_warm_liked_artist_counts_async") as warm:
            newly, _, _ = self._run_check(_track("t9", "Someone"), [], sp)
        self.assertEqual(newly, [])
        warm.assert_called_once_with(sp)
        sp.current_user_saved_tracks.assert_not_called()

    def test_legacy_history_rows_without_credit_list_still_count(self):
        history = [{"type": "liked", "id": f"h{i}", "artists": "Solo Act"} for i in range(3)]
        self._warm({"solo act": 0})
        newly, _, _ = self._run_check(_track("t9", "Solo Act"), history, mock.Mock())
        self.assertEqual(newly, ["Solo Act"])


if __name__ == "__main__":
    unittest.main()
