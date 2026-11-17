"""POST /api/v2/backlog/play (src/web/now_api.py) against a stub `app` module."""

import sys
import threading
import types
import unittest


class FakeSpotifyError(Exception):
    def __init__(self, http_status, msg="", headers=None, reason=None):
        super().__init__(msg)
        self.http_status = http_status
        self.msg = msg
        self.headers = headers or {}
        self.reason = reason


class GuardBlocked(FakeSpotifyError):
    def __init__(self, retry_after=7):
        super().__init__(429, "blocked locally")
        self.retry_after = retry_after


class FakeSp:
    def __init__(self, devices=None, fail=None, shuffle_fails=False):
        self.calls = []
        self._devices = devices if devices is not None else [
            {"id": "phone", "is_active": False}, {"id": "desk", "is_active": True}]
        self._fail = fail
        self._shuffle_fails = shuffle_fails

    def devices(self):
        self.calls.append(("devices",))
        return {"devices": self._devices}

    def start_playback(self, **kw):
        self.calls.append(("start_playback", kw))
        if self._fail is not None:
            raise self._fail

    def shuffle(self, state, device_id=None):
        self.calls.append(("shuffle", state, device_id))
        if self._shuffle_fails:
            raise AttributeError("shuffle")


class FakeIndex:
    def __init__(self, pid="pl123", entries=None):
        self.pid = pid
        self.name = "To listen"
        self._entries = entries if entries is not None else [
            {"track_id": f"t{i}", "uri": f"spotify:track:t{i}", "name": f"Song {i}", "artists": "Band"}
            for i in range(5)]

    def items(self):
        return list(self._entries)


class BacklogPlayTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask

        self._saved_app = sys.modules.get("app")
        core = types.ModuleType("app")
        core.state_lock = threading.RLock()
        core.state = {"playlist_id": "pl123", "playlist_name": "To listen"}
        core.SpotifyGuardBlocked = GuardBlocked
        core.sp = FakeSp()
        core.index = FakeIndex()
        core.get_sp = lambda: core.sp
        core._backlog_index = lambda: core.index
        core.changed = []
        core._playback_changed = lambda **kw: core.changed.append(kw)
        core.errors = []
        core._log_app_error = lambda e, ctx="": core.errors.append((ctx, e))
        sys.modules["app"] = core
        self.core = core
        from src.web import now_api

        flask_app = Flask(__name__)
        flask_app.register_blueprint(now_api.bp)
        self.client = flask_app.test_client()

    def tearDown(self):
        if self._saved_app is not None:
            sys.modules["app"] = self._saved_app
        else:
            sys.modules.pop("app", None)

    def post(self, body=None):
        return self.client.post("/api/v2/backlog/play", json=body if body is not None else {})

    def start_call(self):
        calls = [c for c in self.core.sp.calls if c[0] == "start_playback"]
        self.assertEqual(len(calls), 1)
        return calls[0][1]

    def test_plays_backlog_context_from_the_top_on_the_active_device(self):
        r = self.post()
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["playing"])
        self.assertEqual(body["playlist_id"], "pl123")
        self.assertEqual(body["name"], "To listen")
        self.assertFalse(body["shuffle"])
        self.assertEqual(body["started_with"]["track_id"], "t0")
        kw = self.start_call()
        self.assertEqual(kw["context_uri"], "spotify:playlist:pl123")
        self.assertEqual(kw["offset"], {"uri": "spotify:track:t0"})
        self.assertEqual(kw["device_id"], "desk")
        self.assertFalse(any(c[0] == "shuffle" for c in self.core.sp.calls))
        self.assertEqual(self.core.changed, [{"is_playing": True, "progress_ms": 0}])

    def test_shuffle_starts_at_a_backlog_track_and_turns_shuffle_on(self):
        r = self.post({"shuffle": True})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["shuffle"])
        self.assertTrue(body["shuffle_state"])
        kw = self.start_call()
        self.assertIn(kw["offset"]["uri"], {f"spotify:track:t{i}" for i in range(5)})
        self.assertIn(("shuffle", True, "desk"), self.core.sp.calls)

    def test_shuffle_failure_is_not_fatal(self):
        self.core.sp = FakeSp(shuffle_fails=True)
        r = self.post({"shuffle": True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["shuffle_state"])

    def test_empty_index_plays_the_context_without_offset(self):
        self.core.index = FakeIndex(entries=[])
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("offset", self.start_call())
        self.assertIsNone(r.get_json()["started_with"])

    def test_no_devices_listed_lets_spotify_choose(self):
        self.core.sp = FakeSp(devices=[])
        self.assertEqual(self.post().status_code, 200)
        self.assertIsNone(self.start_call()["device_id"])

    def test_invalid_shuffle_is_rejected(self):
        r = self.post({"shuffle": "yes"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "shuffle")
        self.assertEqual(self.core.sp.calls, [])

    def test_non_object_body_is_rejected(self):
        r = self.client.post("/api/v2/backlog/play", json=[1, 2])
        self.assertEqual(r.status_code, 400)

    def test_signed_out(self):
        self.core.sp = None
        r = self.post()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"]["code"], "signed_out")

    def test_no_backlog_playlist(self):
        self.core.index = FakeIndex(pid=None)
        self.core.state["playlist_id"] = None
        r = self.post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "no_backlog")

    def test_guard_block_is_503_with_retry_after(self):
        self.core.sp = FakeSp(fail=GuardBlocked(retry_after=12))
        r = self.post()
        self.assertEqual(r.status_code, 503)
        err = r.get_json()["error"]
        self.assertEqual(err["code"], "spotify_busy")
        self.assertEqual(err["retry_after"], 12)
        self.assertEqual(self.core.changed, [])

    def test_spotify_429(self):
        self.core.sp = FakeSp(fail=FakeSpotifyError(429, "slow down", headers={"Retry-After": "40"}))
        r = self.post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["error"]["retry_after"], 40)

    def test_no_active_device(self):
        self.core.sp = FakeSp(fail=FakeSpotifyError(404, "Player command failed: No active device found",
                                                    reason="NO_ACTIVE_DEVICE"))
        r = self.post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "no_device")

    def test_other_failures_are_502_and_logged(self):
        self.core.sp = FakeSp(fail=FakeSpotifyError(500, "boom"))
        r = self.post()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"]["code"], "spotify_error")
        self.assertEqual(len(self.core.errors), 1)


if __name__ == "__main__":
    unittest.main()
