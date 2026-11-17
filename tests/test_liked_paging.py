"""GET /api/liked-songs: the classic response stays as it was; limit/offset page through every like.

Runs against an isolated copy of app.py (tests/platform_harness.py) with a fake Spotify client;
nothing here touches real data or the network.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402


def _track(i):
    return {"id": f"t{i:03d}", "name": f"Song {i}", "uri": f"spotify:track:t{i:03d}", "duration_ms": 1000,
            "artists": [{"name": "Band"}, {"name": "Guest"}],
            "album": {"name": "Album", "images": [{"url": "https://i.scdn.co/big"}, {"url": "https://i.scdn.co/small"}]}}


class FakeSp:
    """Saved tracks in either shape: legacy ``track`` entries or 2026 ``item`` entries."""

    def __init__(self, n, key="track"):
        self.likes = [{"added_at": "2026-09-01T12:00:00Z", key: _track(i)} for i in range(n)]
        self.calls = []

    def current_user_saved_tracks(self, limit=20, offset=0, **kw):
        self.calls.append((limit, offset))
        page = self.likes[offset: offset + limit]
        return {"items": page, "total": len(self.likes), "limit": limit, "offset": offset,
                "next": "more" if offset + limit < len(self.likes) else None}


class LikedPagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        a.app.config["SERVER_NAME"] = None
        self.client = a.app.test_client()
        with a._liked_songs_cache_lock:
            a._liked_songs_cache.update(tracks=[], fetched_at=0.0)

    def get(self, sp, url):
        with mock.patch.object(self.app, "get_sp", return_value=sp), \
                mock.patch.object(self.app, "get_cached_track_genres", return_value=[]):
            return self.client.get(url)

    def test_pages_through_every_like(self):
        sp = FakeSp(120)
        seen, offset = [], 0
        while offset is not None:
            body = self.get(sp, f"/api/liked-songs?limit=50&offset={offset}").get_json()
            self.assertEqual(body["total"], 120)
            self.assertEqual(body["offset"], offset)
            seen += [t["id"] for t in body["tracks"]]
            offset = body["next_offset"]
        self.assertEqual(len(seen), 120)
        self.assertEqual(len(set(seen)), 120)
        self.assertEqual(sp.calls, [(50, 0), (50, 50), (50, 100)])
        row = self.get(sp, "/api/liked-songs?limit=1&offset=7").get_json()["tracks"][0]
        self.assertEqual((row["id"], row["artist"], row["artists"]), ("t007", "Band", "Band, Guest"))
        self.assertEqual((row["image"], row["liked_at"]), ("https://i.scdn.co/small", "2026-09-01T12:00:00Z"))

    def test_2026_item_shape(self):
        body = self.get(FakeSp(3, key="item"), "/api/liked-songs?offset=0").get_json()
        self.assertEqual([t["id"] for t in body["tracks"]], ["t000", "t001", "t002"])
        self.assertIsNone(body["next_offset"])

    def test_default_response_is_unchanged(self):
        sp = FakeSp(130)
        body = self.get(sp, "/api/liked-songs").get_json()
        self.assertEqual(set(body), {"tracks", "total"})
        self.assertEqual(len(body["tracks"]), 120)       # the classic UI's most recent likes
        self.assertEqual(body["total"], 120)

    def test_bad_paging_is_400(self):
        for q in ("limit=0", "limit=51", "offset=-1", "limit=x"):
            r = self.get(FakeSp(1), f"/api/liked-songs?{q}")
            self.assertEqual(r.status_code, 400, q)


if __name__ == "__main__":
    unittest.main()
