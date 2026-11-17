"""Isolated copy of app.py for the platform tests (config IO, request security, Spotify guard).

load_app() copies app.py, src/ and templates/ into a temp directory with its own fake .env,
.config.json, token cache and SQLite file, blocks all network access, and imports that copy
once per test process. CONFIG_PATH, ENV_PATH, CACHE_PATH and the database therefore all
live in the temp directory: these tests can never read or write the user's real data.

Not a test module itself (no test_ prefix); test modules import it after putting this
directory on sys.path.
"""

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

ENV_TEXT = (
    "# test .env\n"
    "SPOTIPY_CLIENT_ID=testclientid\n"
    "SPOTIPY_CLIENT_SECRET=testsecretABCD\n"
    "SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback\n"
    "LASTFM_API_KEY=testlastfmkeyWXYZ\n"
    "LASTFM_USERNAME=olduser\n"
)

# Anything the developer's shell may export must not leak into the copy's settings.
_ENV_KEYS = (
    "SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI", "LASTFM_API_KEY",
    "LASTFM_USERNAME", "POLL_INTERVAL", "POLL_INTERVAL_PAUSED", "DEFAULT_THRESHOLD",
    "DISCOVER_WEEKLY_INFLOW", "AUTO_ARTIST_LIKE_THRESHOLD", "APP_DEBUG", "APP_USE_RELOADER",
    "SPOTIFY_EXTENDED_ENDPOINTS", "PLAYLIST_ID", "WERKZEUG_RUN_MAIN",
    "DISCOVER_DATA_DIR", "DISCOVER_DEMO", "DISCOVER_DEMO_DIR",
)

_LOCK = threading.Lock()
_APP = None


def _block_network():
    import requests

    def _blocked(self, req, **kw):
        raise requests.exceptions.ConnectionError("network blocked in tests: " + str(req.url))

    requests.Session.send = _blocked


def load_app():
    """Import (once) and return the isolated app module."""
    global _APP
    with _LOCK:
        if _APP is not None:
            return _APP
        tmp = Path(tempfile.mkdtemp(prefix="dc_platform_test_"))
        atexit.register(shutil.rmtree, str(tmp), True)
        shutil.copy2(PROJECT / "app.py", tmp / "app.py")
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(PROJECT / "src", tmp / "src", ignore=ignore)
        if (PROJECT / "templates").is_dir():
            shutil.copytree(PROJECT / "templates", tmp / "templates", ignore=ignore)
        (tmp / ".env").write_text(ENV_TEXT, encoding="utf-8")
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        _block_network()
        # The copy must import ITS src/ package (whose DB_PATH points into tmp), never the
        # real project's, so drop any src.* modules already imported by other tests.
        for name in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
            del sys.modules[name]
        sys.path.insert(0, str(tmp))
        spec = importlib.util.spec_from_file_location("dc_platform_app", tmp / "app.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["dc_platform_app"] = mod
        spec.loader.exec_module(mod)
        assert Path(mod.CONFIG_PATH).parent == tmp, mod.CONFIG_PATH
        assert Path(mod.ENV_PATH).parent == tmp, mod.ENV_PATH
        assert Path(mod.CACHE_PATH).parent == tmp, mod.CACHE_PATH
        assert str(mod._db.DB_PATH).startswith(str(tmp)), mod._db.DB_PATH
        mod.app.testing = True
        mod.TEST_DIR = tmp

        # A route that fails on purpose, for the JSON error handler test. Routes must be
        # added before the app serves its first request.
        @mod.app.route("/api/_test/boom")
        def _test_boom():  # pragma: no cover - exercised through the test client
            raise RuntimeError("boom for the error handler test")

        _APP = mod
        return mod


def reset_env_file(mod, text=ENV_TEXT):
    """Restore the copy's .env and the matching os.environ values."""
    Path(mod.ENV_PATH).write_text(text, encoding="utf-8")
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
