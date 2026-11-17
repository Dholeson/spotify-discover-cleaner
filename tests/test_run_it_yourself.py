"""Running it yourself: the data folder, first-run setup, the offline demo and its snapshot.

- Setup API (GET/POST /api/v2/setup) and the CLI run against the isolated app copy
  (tests/platform_harness.py).
- A fresh install without credentials, DISCOVER_DATA_DIR and the --demo mode each start a
  separate Python process on a throwaway copy of the app, so their import-time decisions never
  touch this process (or the developer's real files). Network access is blocked in all of them.
- Demo seeding and dev/make_snapshot.py run on temp folders; the committed snapshot is only read.
"""

import contextlib
import gzip
import hashlib
import io
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
SNAPSHOT = PROJECT / "demo" / "snapshot"
HEX_A = "0123456789abcdef0123456789abcdef"
HEX_B = "fedcba9876543210fedcba9876543210"
HEX_C = "aaaabbbbccccddddeeeeffff00001111"


def _sha(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _copy_app(dest, with_demo=False):
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copy2(PROJECT / "app.py", dest / "app.py")
    shutil.copytree(PROJECT / "src", dest / "src", ignore=ignore)
    shutil.copytree(PROJECT / "templates", dest / "templates", ignore=ignore)
    if with_demo:
        shutil.copytree(SNAPSHOT, dest / "demo" / "snapshot")


_CHILD_PRELUDE = textwrap.dedent("""
    import json, os, sys
    import requests
    _send = requests.Session.send
    def _blocked(self, req, **kw):
        raise requests.exceptions.ConnectionError("network blocked in tests: " + str(req.url))
    requests.Session.send = _blocked
    sys.path.insert(0, os.getcwd())
    import app as a
    a.app.testing = True
    c = a.app.test_client()
    out = {}
""")


def _run_child(app_dir, body, env_extra=None, timeout=180):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SPOTIPY_", "LASTFM_", "DISCOVER_")) and k not in ("PLAYLIST_ID", "WERKZEUG_RUN_MAIN")}
    env.update(env_extra or {})
    env["PYTHONIOENCODING"] = "utf-8"
    code = _CHILD_PRELUDE + textwrap.dedent(body) + "\nprint('RESULT=' + json.dumps(out))\n"
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(app_dir), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}")
    line = next(l for l in reversed(proc.stdout.splitlines()) if l.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


# --------------------------------------------------------------------------- data folder
class DataDirTest(unittest.TestCase):
    def test_resolve_data_dir(self):
        db = _load_module("dc_db_for_datadir_test", PROJECT / "src" / "persistence" / "db.py")
        self.assertEqual(db.resolve_data_dir({}), db.APP_DIR)
        self.assertEqual(db.resolve_data_dir({"DISCOVER_DATA_DIR": "   "}), db.APP_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "data"
            got = db.resolve_data_dir({"DISCOVER_DATA_DIR": str(target)})
            self.assertEqual(got, target)
            self.assertTrue(target.is_dir(), "the data folder is created")


class FreshInstallTest(unittest.TestCase):
    """No .env, no credentials: the server starts and asks for setup instead of exiting."""

    def test_no_credentials_starts_with_setup_and_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "app"
            data_dir = Path(tmp) / "data"
            app_dir.mkdir()
            _copy_app(app_dir)
            out = _run_child(app_dir, """
                from pathlib import Path
                out["oauth_none"] = a._oauth is None
                out["token"] = a.get_cached_token()
                out["paths"] = {"config": str(Path(a.CONFIG_PATH).parent), "cache": str(Path(a.CACHE_PATH).parent),
                                "db": str(Path(a._db.DB_PATH).parent), "env": str(Path(a.ENV_PATH).parent),
                                "migrate": str(Path(a._migrate.CONFIG_PATH).parent)}
                a._db.init_db()
                out["index"] = c.get("/").status_code
                out["setup"] = c.get("/api/v2/setup").get_json()
                login = c.get("/login")
                out["login"] = [login.status_code, login.headers.get("Location")]
                out["callback"] = c.get("/callback?code=x&state=y").status_code
                st = c.get("/api/status")
                out["status"] = [st.status_code, (st.get_json() or {}).get("setup")]
                out["config_has_demo"] = '"demo": false' in c.get("/").get_data(as_text=True)
            """, {"DISCOVER_DATA_DIR": str(data_dir)})
            self.assertTrue(out["oauth_none"])
            self.assertIsNone(out["token"])
            for key in ("config", "cache", "db", "migrate"):
                self.assertEqual(Path(out["paths"][key]), data_dir, key)
            self.assertEqual(Path(out["paths"]["env"]), app_dir, ".env stays in the app folder")
            self.assertTrue((data_dir / "music_explorer.db").exists())
            self.assertFalse((app_dir / "music_explorer.db").exists())
            self.assertEqual(out["index"], 200)
            s = out["setup"]
            self.assertTrue(s["needs_credentials"])
            self.assertFalse(s["demo"])
            self.assertEqual(s["redirect_uri"], "http://127.0.0.1:8888/callback")
            self.assertFalse(s["client_id_set"] or s["client_secret_set"])
            self.assertEqual(out["login"][0], 302)
            self.assertTrue(out["login"][1].endswith("/#/setup"))
            self.assertEqual(out["callback"], 400)
            self.assertEqual(out["status"][0], 200)
            self.assertEqual(out["status"][1], {"demo": False, "needs_credentials": True, "needs_playlist": False})
            self.assertTrue(out["config_has_demo"])


# --------------------------------------------------------------------------- setup API
class SetupApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        platform_harness.reset_env_file(a)
        a._oauth = a.build_oauth_manager()
        with a.state_lock:
            self.saved_state = {k: a.state.get(k) for k in ("auth_ready", "spotify_user_id", "last_error", "playlist_id")}
        self.client = a.app.test_client()

    def tearDown(self):
        a = self.app
        platform_harness.reset_env_file(a)
        a._oauth = a.build_oauth_manager()
        a.DEMO_MODE = False
        with a.state_lock:
            a.state.update(self.saved_state)

    def _post(self, body, **headers):
        return self.client.post("/api/v2/setup", json=body, headers=headers)

    def test_get_never_returns_secrets(self):
        body = self.client.get("/api/v2/setup").get_json()
        self.assertFalse(body["needs_credentials"])
        self.assertTrue(body["client_secret_set"])
        text = json.dumps(body)
        self.assertNotIn("testsecretABCD", text)
        self.assertNotIn("testlastfmkeyWXYZ", text)
        self.assertNotIn("testclientid", text)

    def test_invalid_input_is_rejected_and_env_untouched(self):
        a = self.app
        before = _sha(a.ENV_PATH)
        cases = [
            {"client_id": "not-hex", "client_secret": HEX_B},
            {"client_id": HEX_A, "client_secret": HEX_B[:-1]},
            {"client_id": HEX_A, "client_secret": HEX_A},
            {"client_id": HEX_A, "client_secret": HEX_B, "lastfm_api_key": "short"},
            {"client_id": HEX_A, "client_secret": HEX_B, "lastfm_username": "9starts-with-digit"},
            {"client_id": HEX_A + "\nSPOTIPY_REDIRECT_URI=http://evil", "client_secret": HEX_B},
            {"client_id": 123, "client_secret": HEX_B},
        ]
        for body in cases:
            r = self._post(body)
            self.assertEqual(r.status_code, 400, body)
            err = r.get_json()["error"]
            self.assertEqual(err["code"], "invalid_setup")
            self.assertIn(err["field"], err["fields"])
            self.assertEqual(_sha(a.ENV_PATH), before, body)

    def test_cross_site_post_is_refused(self):
        a = self.app
        before = _sha(a.ENV_PATH)
        r = self._post({"client_id": HEX_A, "client_secret": HEX_B}, Origin="http://evil.example")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"]["code"], "forbidden_origin")
        self.assertEqual(_sha(a.ENV_PATH), before)

    def test_save_writes_env_keeps_other_lines_and_applies_live(self):
        a = self.app
        r = self._post({"client_id": HEX_A.upper(), "client_secret": HEX_B, "lastfm_api_key": HEX_C,
                        "lastfm_username": "new_user"}, Origin="http://localhost")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["next"], "/login")
        self.assertEqual(body["saved"], ["LASTFM_API_KEY", "LASTFM_USERNAME", "SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET"])
        text = Path(a.ENV_PATH).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# test .env\n"), "comments are kept")
        self.assertIn(f"SPOTIPY_CLIENT_ID={HEX_A.upper()}\n", text)
        self.assertIn(f"SPOTIPY_CLIENT_SECRET={HEX_B}\n", text)
        self.assertIn("SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback\n", text, "other keys are kept")
        self.assertIn("LASTFM_USERNAME=new_user\n", text)
        self.assertEqual(text.count("SPOTIPY_CLIENT_ID="), 1)
        self.assertEqual(os.environ["SPOTIPY_CLIENT_SECRET"], HEX_B)
        self.assertEqual(a._oauth.client_id, HEX_A.upper(), "sign-in uses the new app at once")
        self.assertFalse(Path(a.ENV_PATH).with_name(".env.tmp").exists())
        self.assertNotIn(HEX_B, json.dumps(body), "the secret is never echoed")
        # Saving the same keys again changes nothing (and doesn't sign the user out).
        r2 = self._post({"client_id": HEX_A.upper(), "client_secret": HEX_B}, Origin="http://localhost")
        self.assertEqual(r2.get_json()["saved"], [])

    def test_empty_secret_keeps_the_saved_one(self):
        r = self._post({"client_id": "", "client_secret": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["saved"], [])

    def test_missing_env_file_is_created_with_a_header(self):
        a = self.app
        Path(a.ENV_PATH).unlink()
        for k in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "LASTFM_API_KEY", "LASTFM_USERNAME"):
            os.environ.pop(k, None)
        self.assertTrue(self.client.get("/api/v2/setup").get_json()["needs_credentials"])
        r = self._post({"client_id": HEX_A})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["field"], "client_secret")
        self.assertFalse(Path(a.ENV_PATH).exists())
        r = self._post({"client_id": HEX_A, "client_secret": HEX_B})
        self.assertEqual(r.status_code, 200)
        text = Path(a.ENV_PATH).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Discover Cleaner settings"))
        self.assertIn(f"SPOTIPY_CLIENT_ID={HEX_A}\n", text)
        self.assertNotIn("REDIRECT", text, "the default Redirect URI follows --port, so it isn't pinned")
        self.assertFalse(r.get_json()["setup"]["needs_credentials"])

    def test_status_extension_and_playlist_step(self):
        a = self.app
        with a.state_lock:
            a.state["auth_ready"] = True
            a.state["playlist_id"] = ""
        s = self.client.get("/api/status").get_json()["setup"]
        self.assertEqual(s, {"demo": False, "needs_credentials": False, "needs_playlist": True})
        with a.state_lock:
            a.state["playlist_id"] = "5bxDiscoverBacklogDemo"
        self.assertFalse(self.client.get("/api/status").get_json()["setup"]["needs_playlist"])

    def test_no_backlog_playlist_is_tolerated(self):
        """A fresh install has no backlog playlist: nothing errors and nothing is 'in' the backlog."""
        a = self.app
        index = a._backlog_index()
        old_pid = index.pid
        with a.state_lock:
            a.state["playlist_id"] = ""
        index.switch("", notify=False)
        try:
            self.assertIs(index.contains("anytrack"), False)
            a._db.init_db()
            for url in ("/api/status", "/api/v2/backlog", "/api/v2/backlog/tracks", "/api/v2/backlog/insights",
                        "/api/playlist-tracks", "/api/v2/setup"):
                r = self.client.get(url)
                self.assertLess(r.status_code, 500, f"{url}: {r.get_data(as_text=True)[:300]}")
            backlog = self.client.get("/api/status").get_json()["backlog"]
            self.assertEqual(backlog["playlist_id"], "")
            self.assertFalse(backlog["loading"])
        finally:
            index.switch(old_pid, notify=False)

    def test_demo_mode_refuses_setup_and_credentials(self):
        a = self.app
        before = _sha(a.ENV_PATH)
        a.DEMO_MODE = True
        r = self._post({"client_id": HEX_A, "client_secret": HEX_B})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"]["code"], "demo_mode")
        r = self.client.post("/api/settings", json={"updates": {"SPOTIPY_CLIENT_ID": HEX_A}})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/login").headers["Location"], "/#/now")
        self.assertEqual(_sha(a.ENV_PATH), before)

    def test_premium_403_has_a_plain_explanation(self):
        a = self.app
        import spotipy
        exc = spotipy.SpotifyException(403, -1, "Active premium subscription required for the owner of the app.")
        self.assertTrue(a.is_spotify_app_registration_error(exc))
        self.assertIn("Premium", a.spotify_app_access_message(str(exc)))
        self.assertIn("User Management", a.spotify_app_access_message("user may not be registered"))

    def test_cli(self):
        a = self.app
        args = a._parse_cli(["--demo", "--reset", "--port", "9000"])
        self.assertTrue(args.demo and args.reset)
        self.assertEqual(args.port, 9000)
        self.assertIsNone(a._parse_cli([]).port)
        for bad in (["--reset"], ["--port", "80"], ["--port", "x"]):
            with self.assertRaises(SystemExit, msg=bad), contextlib.redirect_stderr(io.StringIO()):
                a._parse_cli(bad)


# --------------------------------------------------------------------------- offline demo
@unittest.skipUnless((SNAPSHOT / "meta.json").exists(), "demo/snapshot not built")
class DemoSeedTest(unittest.TestCase):
    def setUp(self):
        self.demo = _load_module("dc_demo_for_test", PROJECT / "src" / "devtools" / "demo.py")
        self.meta = json.loads((SNAPSHOT / "meta.json").read_text(encoding="utf-8"))

    def test_shift_days_are_whole_weeks(self):
        frozen = date.fromisoformat(self.meta["frozen_on"])
        self.assertEqual(self.demo.shift_days_for(frozen.isoformat(), frozen), 0)
        self.assertEqual(self.demo.shift_days_for(frozen.isoformat(), frozen + timedelta(days=13)), 7)
        self.assertEqual(self.demo.shift_days_for(frozen.isoformat(), frozen + timedelta(days=14)), 14)
        self.assertEqual(self.demo.shift_days_for(frozen.isoformat(), frozen - timedelta(days=30)), 0)

    def test_seed_reset_and_safety(self):
        frozen = date.fromisoformat(self.meta["frozen_on"])
        today = frozen + timedelta(days=45)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / ".demo-data"
            summary = self.demo.seed(data, SNAPSHOT, today=today)
            self.assertEqual(summary["shift_days"], 42)
            self.assertEqual(summary["rows"], {k: v for k, v in self.meta["rows"].items()})
            conn = sqlite3.connect(str(data / "music_explorer.db"))
            try:
                max_ts, n = conn.execute("SELECT MAX(ts_ms), COUNT(*) FROM events").fetchone()
                last_day = date.fromtimestamp(max_ts / 1000)
                self.assertGreaterEqual(last_day, frozen + timedelta(days=41))
                self.assertLessEqual(last_day, frozen + timedelta(days=43))
                self.assertEqual(n, self.meta["rows"]["events"])
                bad_day = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE day != date(ts_ms / 1000, 'unixepoch', 'localtime')").fetchone()[0]
                self.assertEqual(bad_day, 0, "local days follow the shifted timestamps")
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM app_meta WHERE key='migrated_config_history_v1'").fetchone())
            finally:
                conn.close()
            cfg = json.loads((data / ".config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["playlist_id"], self.meta["playlist_id"])
            last = max(h["day"] for h in cfg["backlog_history"])
            self.assertGreater(last, (frozen + timedelta(days=30)).isoformat())
            self.assertFalse((data / "music_explorer.db.seed-tmp").exists())

            keep = data / "my-notes.txt"
            keep.write_text("not demo data", encoding="utf-8")
            self.demo.reset_data(data)
            self.assertFalse((data / "music_explorer.db").exists())
            self.assertFalse((data / ".config.json").exists())
            self.assertTrue(keep.exists(), "reset removes only the demo's own files")
            with self.assertRaises(self.demo.DemoSetupError):
                self.demo.reset_data(self.demo.APP_DIR)

    def test_snapshot_has_no_account_details(self):
        blob = []
        for p in SNAPSHOT.glob("*.gz"):
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                blob.append(fh.read())
        text = "\n".join(blob) + (SNAPSHOT / "meta.json").read_text(encoding="utf-8")
        # Every playlist id left in the data is one of the demo's (the payloads are JSON inside JSON).
        pids = set(re.findall(r'playlist_id\\*"\s*:\s*\\*"([A-Za-z0-9]+)', text))
        self.assertTrue(pids, "the snapshot names the demo playlist")
        self.assertLessEqual(pids, {self.meta["playlist_id"], "5bxGraveyardSandbox002"})
        with gzip.open(SNAPSHOT / "interests.jsonl.gz", "rt", encoding="utf-8") as fh:
            cols = json.loads(fh.readline())["columns"]
            notes = [row[cols.index("note")] for row in map(json.loads, fh)]
        self.assertTrue(all(n is None for n in notes), "watchlist notes are dropped")
        self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
        self.assertNotRegex(text, r"[A-Za-z]:\\\\Users|/Users/|/home/")
        total = sum(p.stat().st_size for p in SNAPSHOT.iterdir() if p.is_file())
        self.assertLessEqual(total, 6 * 1024 * 1024)


@unittest.skipUnless((SNAPSHOT / "meta.json").exists(), "demo/snapshot not built")
class DemoModeProcessTest(unittest.TestCase):
    """`DISCOVER_DEMO=1` (what --demo sets) in a separate process on a copy of the app."""

    def test_demo_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "app"
            app_dir.mkdir()
            _copy_app(app_dir, with_demo=True)
            (app_dir / ".env").write_text(f"SPOTIPY_CLIENT_ID={HEX_A}\nSPOTIPY_CLIENT_SECRET={HEX_B}\n", encoding="utf-8")
            out = _run_child(app_dir, """
                from pathlib import Path
                out["demo"] = a.DEMO_MODE
                out["data"] = str(Path(a._db.DB_PATH).parent)
                out["env_path"] = str(Path(a.ENV_PATH).parent)
                out["client_id"] = os.environ.get("SPOTIPY_CLIENT_ID")
                out["oauth_none"] = a._oauth is None
                sp = a.get_sp()
                out["sp"] = type(sp).__name__
                out["user"] = sp.current_user()["display_name"]
                out["playlist"] = a.state["playlist_id"]
                pl = sp.current_user_playlists(limit=50)["items"]
                out["playlists"] = [p["name"] for p in pl]
                # The demo wraps the test's own block: its refusal (not the test's) must answer first.
                try:
                    requests.get("https://api.spotify.com/v1/me", timeout=2)
                    out["spotify_blocked"] = False
                except requests.exceptions.ConnectionError as e:
                    out["spotify_blocked"] = "offline demo" in str(e)
                out["setup_post"] = c.post("/api/v2/setup", json={"client_id": "0" * 32, "client_secret": "1" * 32}).status_code
                out["login"] = c.get("/login").headers.get("Location")
                out["status_setup"] = c.get("/api/status").get_json()["setup"]
                out["index_demo"] = '"demo": true' in c.get("/").get_data(as_text=True)
            """, {"DISCOVER_DEMO": "1", "DISCOVER_DEMO_DIR": str(Path(tmp) / "demo-data")})
            self.assertTrue(out["demo"])
            self.assertEqual(Path(out["data"]), Path(tmp) / "demo-data")
            self.assertEqual(Path(out["env_path"]), Path(tmp) / "demo-data", "the demo keeps its own .env")
            self.assertIsNone(out["client_id"], "the real .env is not loaded in the demo")
            self.assertTrue(out["oauth_none"])
            self.assertEqual(out["sp"], "GuardedSpotify")
            self.assertEqual(out["user"], "Dholeson")
            self.assertEqual(out["playlist"], "5bxDiscoverBacklogDemo")
            self.assertIn("Discover Backlog (demo)", out["playlists"])
            self.assertTrue(out["spotify_blocked"])
            self.assertEqual(out["setup_post"], 403)
            self.assertEqual(out["login"], "/#/now")
            self.assertEqual(out["status_setup"], {"demo": True, "needs_credentials": False, "needs_playlist": False})
            self.assertTrue(out["index_demo"])
            self.assertTrue((Path(tmp) / "demo-data" / ".demo-seed.json").exists())
            self.assertFalse((app_dir / "music_explorer.db").exists())


# --------------------------------------------------------------------------- snapshot builder
class MakeSnapshotTest(unittest.TestCase):
    REAL_PID = "RealPlaylistId00000001"

    def _source(self, root, track_name="Song"):
        src = root / "src"
        src.mkdir()
        conn = sqlite3.connect(str(src / "music_explorer.db"))
        conn.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, ts_ms INTEGER, day TEXT, track_id TEXT,
                                 artist_id TEXT, album_id TEXT, payload TEXT, source TEXT);
            CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT, album TEXT, album_id TEXT, image_url TEXT,
                                 spotify_url TEXT, duration_ms INTEGER, created_at INTEGER);
            CREATE TABLE interests (id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT, entity_name TEXT,
                                    status TEXT, note TEXT, created_at INTEGER, updated_at INTEGER);
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE backlog_index (pid TEXT PRIMARY KEY, snapshot_id TEXT, payload_json TEXT, synced_at_ms INTEGER);
        """)
        p1 = json.dumps({"artists": "A", "track_name": track_name, "source": "lastfm_import"})
        p2 = json.dumps({"listened_s": 15.1, "playlist_id": self.REAL_PID, "source": "auto-threshold",
                         "note": "private thought", "device_name": "Living room"})
        conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", [
            (1, "SONG_PLAYED", 1759500000000, "2025-10-03", None, None, None, p1, "lastfm"),
            (2, "THRESHOLD_REACHED", 1759600000000, "2025-10-04", "t1", None, None, p2, "live"),
        ])
        conn.executemany("INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?)", [
            (f"t{i}", f"Track {i}", "Album", "al", "", "", 200000, 1759500000000 + i) for i in range(3)])
        conn.execute("INSERT INTO interests VALUES (1,'track','t1','Track 1','active','my secret note',1,1)")
        conn.executemany("INSERT INTO app_meta VALUES (?,?)", [
            ("migrated_config_history_v1", "{}"), ("spotify_guard_global_reason", "playlist"), ("plays_built_at", "1")])
        conn.execute("INSERT INTO backlog_index VALUES (?,?,?,?)", (self.REAL_PID, "s", "[]", 1))
        conn.commit()
        conn.close()
        cfg = {"playlist_id": self.REAL_PID, "threshold_seconds": 15, "history": [{"type": "listen"}],
               "spotify_user": "someone", "favorite_artists": ["a"],
               "backlog_history": [{"day": "2025-10-01", "ts_ms": 1, "total": 10, "playlist_id": self.REAL_PID},
                                   {"day": "2025-10-02", "ts_ms": 2, "total": 8, "playlist_id": self.REAL_PID}]}
        (src / ".config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return src

    def test_build_scrubs_and_offsets(self):
        ms = _load_module("dc_make_snapshot", PROJECT / "dev" / "make_snapshot.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._source(root)
            out = root / "out"
            meta, total = ms.build(src, out)
            self.assertEqual(meta["frozen_on"], date.fromtimestamp(1759600000000 / 1000).isoformat())
            self.assertEqual(meta["rows"]["tracks"], 3)
            self.assertEqual(meta["backlog_size_offset"], 8 - 3)
            text = ""
            for p in out.glob("*.gz"):
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    text += fh.read()
            self.assertNotIn(self.REAL_PID, text)
            for secret in ("private thought", "Living room", "my secret note", "someone", "spotify_guard", "plays_built_at"):
                self.assertNotIn(secret, text)
            self.assertIn(ms.DEMO_PLAYLIST_ID, text)
            with gzip.open(out / "config.json.gz", "rt", encoding="utf-8") as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg["playlist_id"], ms.DEMO_PLAYLIST_ID)
            self.assertNotIn("history", cfg)
            self.assertEqual([h["total"] for h in cfg["backlog_history"]], [5, 3])
            first = (out / "events.jsonl.gz").read_bytes()
            ms.build(src, out)
            self.assertEqual((out / "events.jsonl.gz").read_bytes(), first, "byte-reproducible")

    def test_build_refuses_leaks(self):
        ms = _load_module("dc_make_snapshot_leak", PROJECT / "dev" / "make_snapshot.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._source(root, track_name="mail me at someone@example.com")
            with self.assertRaises(SystemExit):
                ms.build(src, root / "out")
            self.assertFalse((root / "out" / "meta.json").exists())


if __name__ == "__main__":
    unittest.main()
