"""Local-server lockdown: Host allowlist, same-origin writes, JSON errors, /callback escaping
and OAuth state, masked + validated settings, live Last.fm settings.

Runs against an isolated copy of app.py (tests/platform_harness.py); never the real .env.
"""

import hashlib
import json
import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        a.app.config["SERVER_NAME"] = None
        platform_harness.reset_env_file(a)
        a.LASTFM_API_KEY = a.lastfm_api_key()
        a.LASTFM_USERNAME = a.lastfm_username()
        self.client = a.app.test_client()
        with a.state_lock:
            self._threshold = a.state["threshold_seconds"]

    def tearDown(self):
        a = self.app
        with a.state_lock:
            a.state["threshold_seconds"] = self._threshold
        platform_harness.reset_env_file(a)
        a.LASTFM_API_KEY = a.lastfm_api_key()
        a.LASTFM_USERNAME = a.lastfm_username()


class HostAndOriginTest(Base):
    def test_cross_site_text_plain_post_is_rejected(self):
        # WP-01 ACCEPT 1
        r = self.client.post("/api/threshold", data='{"seconds":20}',
                             headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"]["code"], "forbidden_origin")
        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["threshold_seconds"], self._threshold)

    def test_same_origin_post_is_allowed_on_any_port(self):
        for host in ("127.0.0.1:8901", "localhost:5000"):
            r = self.client.post("/api/threshold", json={"seconds": 21},
                                 headers={"Host": host, "Origin": f"http://{host}"})
            self.assertEqual(r.status_code, 200, host)
            self.assertEqual(r.get_json()["threshold_seconds"], 21)

    def test_origin_must_match_port(self):
        r = self.client.post("/api/threshold", json={"seconds": 22},
                             headers={"Host": "127.0.0.1:8901", "Origin": "http://127.0.0.1:8888"})
        self.assertEqual(r.status_code, 403)

    def test_null_origin_rejected(self):
        r = self.client.post("/api/threshold", json={"seconds": 22}, headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)

    def test_referer_fallback(self):
        ok = self.client.post("/api/threshold", json={"seconds": 23},
                              headers={"Host": "127.0.0.1:8901", "Referer": "http://127.0.0.1:8901/#/now"})
        self.assertEqual(ok.status_code, 200)
        bad = self.client.post("/api/threshold", json={"seconds": 23},
                               headers={"Referer": "http://evil.example/page"})
        self.assertEqual(bad.status_code, 403)

    def test_non_browser_clients_without_origin_are_allowed(self):
        r = self.client.post("/api/threshold", json={"seconds": 24})
        self.assertEqual(r.status_code, 200)

    def test_get_requests_are_not_origin_checked(self):
        r = self.client.get("/api/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 200)

    def test_foreign_host_is_rejected_everywhere(self):
        # WP-01 ACCEPT 2 (DNS rebinding)
        for path in ("/api/status", "/api/health", "/", "/api/settings"):
            r = self.client.get(path, headers={"Host": "evil.example:8901"})
            self.assertEqual(r.status_code, 403, path)
        r = self.client.get("/api/status", headers={"Host": "evil.example:8901"})
        self.assertEqual(r.get_json()["error"]["code"], "forbidden_host")
        for host in ("127.0.0.1:8901", "localhost", "[::1]:8901"):
            self.assertEqual(self.client.get("/api/health", headers={"Host": host}).status_code, 200, host)

    def test_other_state_changing_routes_are_protected(self):
        for path in ("/api/dev/clear-guard", "/api/settings", "/api/favorites", "/api/research"):
            r = self.client.post(path, json={}, headers={"Origin": "http://evil.example"})
            self.assertEqual(r.status_code, 403, path)


class JsonErrorsTest(Base):
    def test_unhandled_exception_is_json_500(self):
        r = self.client.get("/api/_test/boom")
        self.assertEqual(r.status_code, 500)
        self.assertTrue(r.is_json)
        self.assertEqual(r.get_json()["error"]["code"], "internal_error")
        errors = self.client.get("/api/dev/errors").get_json()["errors"]
        self.assertTrue(any("boom" in (e.get("message") or "") for e in errors))

    def test_http_errors_keep_status(self):
        r = self.client.get("/api/definitely-not-a-route")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "not_found")
        r = self.client.get("/api/threshold")  # POST-only
        self.assertEqual(r.status_code, 405)
        self.assertTrue(r.is_json)

    def test_non_api_404_stays_html(self):
        r = self.client.get("/no-such-page")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.is_json)

    def test_malformed_bodies_are_400_json(self):
        for body in ("{not json", "[1,2]", '"text"'):
            r = self.client.post("/api/threshold", data=body, headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.is_json)
        for payload in ({"seconds": "abc"}, {"seconds": None}, {"seconds": True}, {"seconds": "nan"}, {}):
            r = self.client.post("/api/threshold", json=payload)
            self.assertEqual(r.status_code, 400, payload)
            self.assertEqual(r.get_json()["error"]["field"], "seconds")
        r = self.client.post("/api/lyrics-offset", json={"offset_ms": "x"})
        self.assertEqual(r.status_code, 400)

    def test_seek_with_bad_position_is_json_not_html(self):
        # WP-01 ACCEPT 7: whatever the route does, the answer is a JSON error.
        a = self.app
        with mock.patch.object(a, "get_sp", return_value=object()):
            r = self.client.post("/api/seek", json={"position_ms": "abc"})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertTrue(r.is_json, r.get_data(as_text=True)[:200])
        self.assertIn("error", r.get_json())

    def test_threshold_and_lyrics_offset_persist(self):
        a = self.app
        r = self.client.post("/api/threshold", json={"seconds": 1000})
        self.assertEqual(r.get_json()["threshold_seconds"], 600)
        self.assertEqual(a.load_config()["threshold_seconds"], 600)
        r = self.client.post("/api/threshold", json={"threshold": 25})  # legacy field name
        self.assertEqual(r.get_json()["threshold_seconds"], 25)
        r = self.client.post("/api/lyrics-offset", json={"offset_ms": 1234.4})
        self.assertEqual(r.get_json()["lyrics_offset_ms"], 1234)
        self.assertEqual(a.load_config()["lyrics_offset_ms"], 1234)

    def test_favorites_and_research_validate_input(self):
        r = self.client.post("/api/favorites", json={"artist": 5})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/favorites", json={"artist": "X", "action": "explode"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/queue-genres", json={"tracks": "nope"})
        self.assertEqual(r.status_code, 400)
        self.app._SQLITE_ENABLED, saved = True, self.app._SQLITE_ENABLED
        try:
            r = self.client.post("/api/research", json={"entity_name": ["x"]})
            self.assertEqual(r.status_code, 400)
            r = self.client.post("/api/research", json={"entity_name": "X", "entity_type": "planet"})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["error"]["field"], "entity_type")
            r = self.client.post("/api/research", json={"entity_name": "X", "artist_name": {"a": 1}})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.get_json()["error"]["field"], "artist_name")
            r = self.client.patch("/api/research/1", json={"status": "gone"})
            self.assertEqual(r.status_code, 400)
        finally:
            self.app._SQLITE_ENABLED = saved


class CallbackTest(Base):
    def test_error_text_is_escaped(self):
        # WP-01 ACCEPT 3
        r = self.client.get("/callback?error=" + urllib.parse.quote("<script>alert(1)</script>"))
        body = r.get_data(as_text=True)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)

    def test_login_issues_state_and_callback_requires_it(self):
        a = self.app
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 302)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query)["state"][0]
        self.assertGreaterEqual(len(state), 20)
        with mock.patch.object(a._oauth, "get_access_token") as exchange:
            bad = self.client.get("/callback?code=abc&state=forged")
            self.assertEqual(bad.status_code, 400)
            missing = self.client.get("/callback?code=abc")
            self.assertEqual(missing.status_code, 400)
            exchange.assert_not_called()
            ok = self.client.get(f"/callback?code=abc&state={state}")
            self.assertEqual(ok.status_code, 302)
            exchange.assert_called_once()
            replay = self.client.get(f"/callback?code=abc&state={state}")  # single use
            self.assertEqual(replay.status_code, 400)
        with a.state_lock:
            a.state["auth_ready"] = False

    def test_exchange_failure_is_escaped(self):
        a = self.app
        state = a._new_oauth_state()
        with mock.patch.object(a._oauth, "get_access_token", side_effect=RuntimeError("<b>bad</b>")):
            r = self.client.get(f"/callback?code=abc&state={state}")
        self.assertEqual(r.status_code, 500)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", r.get_data(as_text=True))


class SettingsTest(Base):
    def _post(self, updates):
        return self.client.post("/api/settings", json={"updates": updates})

    def _items(self):
        return {s["key"]: s for s in self.client.get("/api/settings").get_json()["settings"]}

    def test_secrets_are_masked(self):
        # WP-01 ACCEPT 4
        items = self._items()
        sec = items["SPOTIPY_CLIENT_SECRET"]
        self.assertEqual(sec["value"], "")
        self.assertTrue(sec["has_value"])
        self.assertEqual(sec["hint"], "••••ABCD")
        key = items["LASTFM_API_KEY"]
        self.assertEqual(key["value"], "")
        self.assertEqual(key["hint"], "••••WXYZ")
        raw = self.client.get("/api/settings").get_data(as_text=True)
        self.assertNotIn("testsecretABCD", raw)
        self.assertNotIn("testlastfmkeyWXYZ", raw)

    def test_bounds_and_effective_values(self):
        items = self._items()
        p = items["POLL_INTERVAL"]
        self.assertEqual((p["min"], p["max"], p["num_type"]), (1.5, 30, "float"))
        self.assertEqual(items["DEFAULT_THRESHOLD"]["num_type"], "int")
        self.assertEqual(items["APP_DEBUG"]["value"], "0")  # effective value, not missing
        self.assertEqual(items["POLL_INTERVAL"]["value"], "3")
        self.assertEqual(items["SPOTIPY_REDIRECT_URI"]["value"], "http://127.0.0.1:8888/callback")

    def test_invalid_values_are_rejected_without_touching_env(self):
        # WP-01 ACCEPT 5
        a = self.app
        before = _sha(a.ENV_PATH)
        cases = [
            {"LASTFM_USERNAME": "newuser\nSPOTIPY_REDIRECT_URI=http://attacker.example/cb"},
            {"LASTFM_USERNAME": "a\rb"},
            {"LASTFM_API_KEY": "abc\x00def"},
            {"DEFAULT_THRESHOLD": "12.5"},
            {"DEFAULT_THRESHOLD": 12.5},
            {"POLL_INTERVAL": "nan"},
            {"POLL_INTERVAL": "inf"},
            {"POLL_INTERVAL": "-3"},
            {"POLL_INTERVAL_PAUSED": "500"},
            {"AUTO_ARTIST_LIKE_THRESHOLD": "abc"},
            {"APP_DEBUG": "maybe"},
            {"PLAYLIST_ID": "not a playlist"},
            {"LASTFM_USERNAME": ["list"]},
            {"POLL_INTERVAL": "4", "DEFAULT_THRESHOLD": "12.5"},  # all-or-nothing
        ]
        for updates in cases:
            r = self._post(updates)
            self.assertEqual(r.status_code, 400, updates)
            err = r.get_json()["error"]
            self.assertEqual(err["code"], "invalid_setting")
            self.assertIn(err["field"], updates)
            self.assertEqual(_sha(a.ENV_PATH), before, updates)
        r = self.client.post("/api/settings", json={"updates": []})
        self.assertEqual(r.status_code, 400)

    def test_empty_secret_keeps_saved_value_and_does_not_sign_out(self):
        a = self.app
        with a.state_lock:
            a.state["auth_ready"] = True
        before = _sha(a.ENV_PATH)
        r = self._post({"SPOTIPY_CLIENT_SECRET": "", "SPOTIPY_CLIENT_ID": "testclientid", "LASTFM_API_KEY": "••••WXYZ"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["saved"], [])
        self.assertEqual(_sha(a.ENV_PATH), before)
        with a.state_lock:
            self.assertTrue(a.state["auth_ready"])
            a.state["auth_ready"] = False

    def test_valid_update_is_written_and_applied_live(self):
        a = self.app
        old_poll = a.POLL_INTERVAL
        try:
            r = self._post({"POLL_INTERVAL": "4.5", "DEFAULT_THRESHOLD": 40, "APP_DEBUG": True})
            self.assertEqual(r.status_code, 200, r.get_json())
            body = r.get_json()
            self.assertEqual(sorted(body["saved"]), ["APP_DEBUG", "DEFAULT_THRESHOLD", "POLL_INTERVAL"])
            env = a.read_env_file()
            self.assertEqual((env["POLL_INTERVAL"], env["DEFAULT_THRESHOLD"], env["APP_DEBUG"]), ("4.5", "40", "1"))
            self.assertEqual(a.POLL_INTERVAL, 4.5)
            self.assertIn("POLL_INTERVAL", body["live_applied"])
            self.assertIn("DEFAULT_THRESHOLD", body["restart_required"])
            self.assertIn("settings", body)
        finally:
            a.POLL_INTERVAL = old_poll

    def test_credential_change_clears_auth(self):
        a = self.app
        a.CACHE_PATH and Path(a.CACHE_PATH).write_text("{}", encoding="utf-8")
        r = self._post({"SPOTIPY_CLIENT_SECRET": "brandnewsecret9999"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["cleared_spotify_cache"])
        self.assertFalse(Path(a.CACHE_PATH).exists())
        self.assertEqual(a.read_env_file()["SPOTIPY_CLIENT_SECRET"], "brandnewsecret9999")
        self.assertEqual(self._items()["SPOTIPY_CLIENT_SECRET"]["hint"], "••••9999")
        a._oauth = a.build_oauth_manager()

    def test_lastfm_username_applies_without_restart(self):
        # WP-01 ACCEPT 8
        a = self.app
        a._lfm_cache_set("artistinfo:someone", {"name": "x"})
        r = self._post({"LASTFM_USERNAME": "newuser"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("LASTFM_USERNAME", r.get_json()["live_applied"])
        self.assertEqual(a.lastfm_username(), "newuser")
        self.assertEqual(a.LASTFM_USERNAME, "newuser")  # legacy readers see it too
        self.assertIsNone(a._lfm_cache_get("artistinfo:someone", 3600))
        self.assertTrue(a.lastfm_configured())
        r = self.client.get("/api/lastfm/import")
        if r.status_code == 200 and "configured" in (r.get_json() or {}):
            self.assertTrue(r.get_json()["configured"])


if __name__ == "__main__":
    unittest.main()
