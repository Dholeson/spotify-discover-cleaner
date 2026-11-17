"""Backend pieces the Settings page relies on: "restart to apply" flags on GET /api/settings,
the Last.fm connection check, and the history-source summary in /api/v2/data/health.

The settings tests run against the isolated app copy (tests/platform_harness.py); the data API
tests mount the blueprint on a bare Flask app with a stand-in app module and a temp database.
"""

import importlib
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402
import test_lastfm_sync as lfm  # noqa: E402


class SettingsRestartFlagsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        a.app.config["SERVER_NAME"] = None
        platform_harness.reset_env_file(a)
        self.client = a.app.test_client()

    def tearDown(self):
        platform_harness.reset_env_file(self.app)

    def _items(self, body=None):
        body = body or self.client.get("/api/settings").get_json()
        return {s["key"]: s for s in body["settings"]}, body

    def test_nothing_pending_on_a_fresh_start(self):
        items, body = self._items()
        self.assertEqual(body["restart_pending"], [])
        self.assertFalse(items["APP_DEBUG"]["pending_restart"])
        self.assertFalse(items["DEFAULT_THRESHOLD"]["pending_restart"])
        self.assertNotIn("pending_restart", items["POLL_INTERVAL"])  # live: applies at once
        self.assertNotIn("pending_restart", items["LASTFM_API_KEY"])

    def test_saved_startup_settings_are_flagged_until_restart(self):
        a = self.app
        r = self.client.post("/api/settings", json={"updates": {
            "APP_DEBUG": not a.DEBUG_MODE, "DEFAULT_THRESHOLD": a.DEFAULT_THRESHOLD + 5, "POLL_INTERVAL": "5"}})
        self.assertEqual(r.status_code, 200, r.get_json())
        items, _ = self._items({"settings": r.get_json()["settings"]})  # POST returns the flags too
        self.assertTrue(items["APP_DEBUG"]["pending_restart"])
        self.assertTrue(items["DEFAULT_THRESHOLD"]["pending_restart"])
        self.assertNotIn("pending_restart", items["POLL_INTERVAL"])
        _, body = self._items()
        self.assertEqual(sorted(body["restart_pending"]), ["APP_DEBUG", "DEFAULT_THRESHOLD"])
        # Saving the running value again clears the flag.
        r = self.client.post("/api/settings", json={"updates": {"APP_DEBUG": a.DEBUG_MODE}})
        self.assertEqual(r.status_code, 200)
        items, _ = self._items()
        self.assertFalse(items["APP_DEBUG"]["pending_restart"])

    def test_invalid_value_names_the_field(self):
        r = self.client.post("/api/settings", json={"updates": {"POLL_INTERVAL": "0.2"}})
        self.assertEqual(r.status_code, 400)
        err = r.get_json()["error"]
        self.assertEqual(err["field"], "POLL_INTERVAL")
        self.assertIn("between", err["message"])


class _DataApiCase(lfm.SyncCase):
    def setUp(self):
        super().setUp()
        from flask import Flask
        data_api = importlib.import_module("src.web.data_api")
        data_api._sources_cache.update(key=None, value=None)
        self.fake_core = types.ModuleType("app")
        self.fake_core._SQLITE_ENABLED = True
        self.fake_core.get_sp = lambda: None
        self.fake_core._LFM_MISSING = type("Missing", (), {"__bool__": lambda s: False})()
        self.fake_core._LFM_FAIL = type("Fail", (), {"__bool__": lambda s: False})()
        self.fake_core._lfm_last_error = {}
        self.fake_core.lastfm_api_key = lambda: os.environ.get("LASTFM_API_KEY") or None
        self.fake_core.lastfm_username = lambda: os.environ.get("LASTFM_USERNAME") or None
        self._mods = mock.patch.dict(sys.modules, {"app": self.fake_core})
        self._mods.start()
        # SyncCase hides the app module from the importers; these routes need the stand-in.
        self._core_patch.stop()
        imp = importlib.import_module("src.services.importers")
        self._core_patch = mock.patch.object(imp, "_core", lambda: self.fake_core)
        self._core_patch.start()
        self._env = mock.patch.dict("os.environ", {"LASTFM_API_KEY": "k", "LASTFM_USERNAME": "someone"})
        self._env.start()
        flask_app = Flask("settings_data_api_test")
        flask_app.register_blueprint(data_api.bp)
        self.client = flask_app.test_client()

    def tearDown(self):
        self._env.stop()
        self._mods.stop()
        super().tearDown()


class LastfmCheckTest(_DataApiCase):
    def _answer(self, result, error=None):
        def call(method, **params):
            self.assertEqual(method, "user.getinfo")
            self.assertEqual(params.get("user"), "someone")
            if error is not None:
                self.fake_core._lfm_last_error.update(error, ts=time.time())
            return result
        self.fake_core.lastfm_call = call

    def test_success_returns_profile(self):
        self._answer({"user": {"name": "someone", "playcount": "52340", "realname": "",
                               "registered": {"unixtime": "1629266501", "#text": 1629266501},
                               "url": "https://www.last.fm/user/someone",
                               "image": [{"#text": "https://img/s.png", "size": "small"},
                                         {"#text": "https://img/xl.png", "size": "extralarge"},
                                         {"#text": "javascript:alert(1)", "size": "mega"}]}})
        r = self.client.get("/api/v2/data/lastfm/check")
        self.assertEqual(r.status_code, 200)
        u = r.get_json()["user"]
        self.assertEqual((u["name"], u["playcount"], u["registered_ts"]), ("someone", 52340, 1629266501000))
        self.assertEqual(u["image"], "https://img/xl.png")
        self.assertIsNone(u["realname"])
        self.assertNotIn("k", r.get_json()["user"].values())

    def test_unknown_user_points_at_the_username(self):
        self._answer(self.fake_core._LFM_MISSING)
        r = self.client.get("/api/v2/data/lastfm/check")
        self.assertEqual(r.status_code, 400)
        err = r.get_json()["error"]
        self.assertEqual((err["code"], err["field"]), ("lastfm_user_not_found", "LASTFM_USERNAME"))

    def test_bad_key_points_at_the_key(self):
        self._answer(self.fake_core._LFM_FAIL, {"message": "user.getinfo: Last.fm error 10: Invalid API key", "code": 10})
        r = self.client.get("/api/v2/data/lastfm/check")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "LASTFM_API_KEY")

    def test_rate_limit_and_network_failures(self):
        self._answer(self.fake_core._LFM_FAIL, {"message": "rate limit", "code": 29})
        self.assertEqual(self.client.get("/api/v2/data/lastfm/check").status_code, 429)
        # A stale error from another thread must not be blamed on this call.
        self.fake_core._lfm_last_error.update(message="old", code=10, ts=time.time() - 60)
        self.fake_core.lastfm_call = lambda method, **p: self.fake_core._LFM_FAIL
        r = self.client.get("/api/v2/data/lastfm/check")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"]["code"], "lastfm_unreachable")

    def test_post_tests_typed_values_without_saving(self):
        seen = {}

        def call(method, **params):
            seen.update(params)
            return {"user": {"name": params["user"], "playcount": "3"}}
        self.fake_core.lastfm_call = call
        r = self.client.post("/api/v2/data/lastfm/check", json={"username": "typed_user", "api_key": "newkey"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen, {"user": "typed_user", "api_key": "newkey"})  # typed key overrides the saved one
        self.assertEqual(os.environ["LASTFM_USERNAME"], "someone")            # nothing was saved
        r = self.client.post("/api/v2/data/lastfm/check", json={"username": "has space"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "LASTFM_USERNAME")
        r = self.client.post("/api/v2/data/lastfm/check", data="[1]", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_first_setup_without_a_saved_key_calls_last_fm_directly(self):
        self.fake_core.lastfm_call = lambda *a, **k: self.fail("app.lastfm_call needs a saved key")
        resp = mock.Mock()
        resp.json.return_value = {"error": 10, "message": "Invalid API key"}
        with mock.patch.dict("os.environ", {"LASTFM_API_KEY": ""}),                 mock.patch("requests.get", return_value=resp) as get:
            r = self.client.post("/api/v2/data/lastfm/check", json={"api_key": "typedkey"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "LASTFM_API_KEY")
        self.assertEqual(get.call_args.kwargs["params"]["api_key"], "typedkey")
        self.assertNotIn("typedkey", r.get_data(as_text=True))

    def test_not_configured(self):
        with mock.patch.dict("os.environ", {"LASTFM_API_KEY": "", "LASTFM_USERNAME": "someone"}):
            r = self.client.get("/api/v2/data/lastfm/check")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "LASTFM_API_KEY")


class DataHealthSourcesTest(_DataApiCase):
    def test_sources_and_oldest_play(self):
        self.sync(lfm.FakeLastfm(lfm.scrobbles(lfm.BASE_S, 4)))
        d = self.client.get("/api/v2/data/health").get_json()
        self.assertEqual([s["source"] for s in d["sources"]], ["lastfm"])
        src = d["sources"][0]
        self.assertEqual(src["plays"], 4)
        self.assertEqual(d["oldest_play_ts"], src["first_ts"])
        self.assertLessEqual(src["first_ts"], src["last_ts"])
        self.assertEqual(sum(s["plays"] for s in d["sources"]), d["plays_total"])
        self.assertIsInstance(d["backup_files"], list)


if __name__ == "__main__":
    unittest.main()
