"""
Spotify Discover Cleaner
------------------------
Watches Spotify playback. When you've listened to a track from your target
playlist for at least N seconds (adjustable in the UI), it removes that track
from the playlist.

Run: python app.py
Then open http://127.0.0.1:8888
"""

import os
import re
import sys
import time
import threading
import traceback as _traceback
import inspect
from collections import deque as _deque

# `python app.py` runs this file as __main__. Alias it as `app` so modules under src/ can
# `import app as core` (lazily, inside functions) and get THIS live module instead of
# executing a second copy with its own state.
sys.modules.setdefault("app", sys.modules[__name__])

_app_error_log: _deque = _deque(maxlen=40)

def _append_dev_event(event_type: str, message: str, context: str = "", tb: str = "") -> None:
    import time as _t
    _app_error_log.appendleft({
        "ts": int(_t.time() * 1000),
        "context": context,
        "type": event_type,
        "message": message,
        "traceback": tb,
    })

def _log_app_error(exc: Exception, context: str = "") -> None:
    """Record an exception into the dev error log."""
    _append_dev_event(type(exc).__name__, str(exc), context=context, tb=_traceback.format_exc())

import urllib.parse
from collections import Counter, deque, defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, render_template_string, redirect, send_file

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ---------- Config ----------
APP_DIR = Path(os.path.abspath(__file__)).parent
# `python app.py --demo` (or DISCOVER_DEMO=1): the offline demo. Simulated Spotify, the bundled
# listening history, and its own data folder (.demo-data/, or DISCOVER_DEMO_DIR). Decided before
# anything reads a path or a setting, so the demo can never touch a real install's files.
DEMO_MODE = (__name__ == "__main__" and "--demo" in sys.argv[1:]) or os.environ.get("DISCOVER_DEMO") == "1"
if DEMO_MODE:
    os.environ["DISCOVER_DEMO"] = "1"  # inherited by the reloader's child process
    os.environ["DISCOVER_DATA_DIR"] = os.environ.get("DISCOVER_DEMO_DIR") or str(APP_DIR / ".demo-data")
    for _k in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI", "PLAYLIST_ID", "LASTFM_USERNAME"):
        os.environ.pop(_k, None)
    os.environ["LASTFM_AUTO_SYNC"] = "0"
    Path(os.environ["DISCOVER_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    load_dotenv(Path(os.environ["DISCOVER_DATA_DIR"]) / ".env")  # the demo's own settings, if any
else:
    load_dotenv(APP_DIR / ".env")

import json

# ---------- Persistence (SQLite) ----------
try:
    from src.persistence import db as _db
    from src.persistence import events as _events
    from src.persistence import migrate as _migrate
    from src.persistence import sessions as _sessions
    _SQLITE_ENABLED = True
except Exception as _sqlite_import_err:  # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "SQLite persistence unavailable: %s", _sqlite_import_err
    )
    _SQLITE_ENABLED = False

# Database, .config.json, the Spotify token cache and backups live in DATA_DIR
# (DISCOVER_DATA_DIR; default: this folder). .env stays next to app.py (the demo keeps its own).
DATA_DIR = _db.DATA_DIR if _SQLITE_ENABLED else APP_DIR
if DEMO_MODE:
    from src.devtools import demo as _demo
    # Blocks *.spotify.com for this process and seeds DATA_DIR from demo/snapshot/ when it is
    # empty (or on --demo --reset), before .config.json is read below.
    _demo.prepare(DATA_DIR, reset=("--reset" in sys.argv[1:] and os.environ.get("WERKZEUG_RUN_MAIN") != "true"))

import copy
import html
import itertools
import math
import secrets
from collections import OrderedDict


def _env_num(name, default, cast=float, lo=None, hi=None):
    """Parse a numeric setting from the environment without ever failing startup.

    A hand-edited or UI-saved .env used to brick the app (int("12.5") at import) or kill
    the poll thread (POLL_INTERVAL=nan). Unparseable or non-finite values fall back to the
    default; out-of-range values are clamped. Both are logged to the dev error log.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
        if not math.isfinite(value):
            raise ValueError("not a finite number")
    except (TypeError, ValueError):
        _append_dev_event("ConfigWarning", f"{name}={raw!r} is not a valid number; using {default}.",
                          context="env")
        return default
    if cast is int and not value.is_integer():
        _append_dev_event("ConfigWarning", f"{name}={raw!r} is not a whole number; using {round(value)}.",
                          context="env")
        value = float(round(value))
    clamped = value
    if lo is not None and clamped < lo:
        clamped = lo
    if hi is not None and clamped > hi:
        clamped = hi
    if clamped != value:
        _append_dev_event("ConfigWarning", f"{name}={raw!r} is outside {lo}..{hi}; using {clamped}.",
                          context="env")
    return cast(clamped)


DEFAULT_PLAYLIST_ID = (os.environ.get("PLAYLIST_ID") or "").strip()  # empty until one is chosen in the app
POLL_INTERVAL = _env_num("POLL_INTERVAL", 3.0, float, 1.5, 30)
POLL_INTERVAL_PAUSED = _env_num("POLL_INTERVAL_PAUSED", 10.0, float, 2, 120)
DEFAULT_THRESHOLD = _env_num("DEFAULT_THRESHOLD", 30, int, 3, 600)
AUTO_ARTIST_LIKE_THRESHOLD = _env_num("AUTO_ARTIST_LIKE_THRESHOLD", 5, int, 0, 100)
DISCOVER_WEEKLY_INFLOW = _env_num("DISCOVER_WEEKLY_INFLOW", 30, int, 0, 500)
LISTEN_EVENT_MIN_SECONDS = _env_num("LISTEN_EVENT_MIN_SECONDS", 10.0, float, 0, 600)
MAX_EVENT_HISTORY = _env_num("MAX_EVENT_HISTORY", 3000, int, 100, 100000)
MAX_BACKLOG_HISTORY = _env_num("MAX_BACKLOG_HISTORY", 180, int, 10, 10000)
LIKED_LIBRARY_SAMPLE_LIMIT = _env_num("LIKED_LIBRARY_SAMPLE_LIMIT", 0, int, 0, 100000)
LIKED_LIBRARY_PROFILE_TTL_S = _env_num("LIKED_LIBRARY_PROFILE_TTL_S", 12 * 3600, int, 60, 30 * 86400)
ANALYTICS_WINDOW_DAYS = _env_num("ANALYTICS_WINDOW_DAYS", 14, int, 1, 365)
CONFIG_PATH = DATA_DIR / ".config.json"
ENV_PATH = (DATA_DIR if DEMO_MODE else APP_DIR) / ".env"

# Settings exposed in the UI for editing. Updates write to .env and require a
# restart for most fields (since module-level constants only read on startup).
SETTINGS_SCHEMA = [
    # section, key, label, type, secret, hint
    ("spotify", "SPOTIPY_CLIENT_ID", "Spotify App ID", "text", False,
     "Found in your app at developer.spotify.com/dashboard"),
    ("spotify", "SPOTIPY_CLIENT_SECRET", "Spotify App Secret", "password", True,
     "Keep this private — never share or commit it"),
    ("spotify", "SPOTIPY_REDIRECT_URI", "Spotify Login Redirect URL", "text", False,
     "Read-only here. Must match exactly what's in your Spotify Dashboard."),
    ("lastfm", "LASTFM_API_KEY", "API Key", "password", True, "From last.fm/api/account/create"),
    ("lastfm", "LASTFM_USERNAME", "Username", "text", False, "For personalization & weekly tops"),
    ("polling", "POLL_INTERVAL", "Poll interval, playing (s)", "number", False, "How often to check Spotify"),
    ("polling", "POLL_INTERVAL_PAUSED", "Poll interval, paused (s)", "number", False, "Slower while paused"),
    ("polling", "DEFAULT_THRESHOLD", "Default threshold (s)", "number", False, "Listen-time before removal"),
    ("playlist", "PLAYLIST_ID", "Default playlist", "text", False, "Used on first run only"),
    ("playlist", "DISCOVER_WEEKLY_INFLOW", "Discover Weekly inflow / week", "number", False, "Used for burndown analytics"),
    ("behavior", "AUTO_ARTIST_LIKE_THRESHOLD", "Auto-like artist threshold", "number", False,
     "Liked songs needed to auto-like that artist (0 = disabled)"),
    ("behavior", "APP_DEBUG", "Flask debug mode", "bool", False, ""),
    ("behavior", "APP_USE_RELOADER", "Flask auto-reload", "bool", False, "Off — duplicates poll loop if on"),
    ("behavior", "SPOTIFY_EXTENDED_ENDPOINTS", "Extended Spotify endpoints", "bool", False, "Only on for older dev apps"),
]
_SETTINGS_KEY_TO_SPEC = {s[1]: s for s in SETTINGS_SCHEMA}
READONLY_SETTINGS = {"SPOTIPY_REDIRECT_URI"}
SPOTIFY_AUTH_SETTINGS = {"SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET"}
# POLL_INTERVAL* are live because poll_loop reads the module globals on every cycle and the
# settings route updates them; the others are read through os.environ or getters.
LIVE_APPLY_SETTINGS = {"DISCOVER_WEEKLY_INFLOW", "AUTO_ARTIST_LIKE_THRESHOLD", "LASTFM_API_KEY", "LASTFM_USERNAME",
                       "POLL_INTERVAL", "POLL_INTERVAL_PAUSED"}
# Typed bounds for numeric settings: returned by GET /api/settings and enforced by POST, and
# the same ranges _env_num() clamps to at startup (kept in sync by hand; tests cover both).
SETTINGS_BOUNDS = {
    "POLL_INTERVAL": {"type": "float", "min": 1.5, "max": 30, "step": 0.5},
    "POLL_INTERVAL_PAUSED": {"type": "float", "min": 2, "max": 120, "step": 1},
    "DEFAULT_THRESHOLD": {"type": "int", "min": 3, "max": 600, "step": 1},
    "DISCOVER_WEEKLY_INFLOW": {"type": "int", "min": 0, "max": 500, "step": 1},
    "AUTO_ARTIST_LIKE_THRESHOLD": {"type": "int", "min": 0, "max": 100, "step": 1},
}
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_FORBIDDEN_CHARS_RE = re.compile(r"[\r\n\x00]")

def read_env_file():
    """Returns dict of KEY -> value. Simple parser, no shell-style substitutions."""
    if not ENV_PATH.exists():
        return {}
    out = {}
    # utf-8-sig: tolerate the BOM Notepad adds when the user edits .env by hand.
    for line in ENV_PATH.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip()
        # Strip matching surrounding quotes
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            out[k] = v
    return out

def _replace_file_atomic(tmp_path, final_path, attempts=8):
    """os.replace with a short retry: on Windows an antivirus scan or a reader holding the
    target open makes the rename fail with PermissionError for a few milliseconds."""
    for i in range(attempts):
        try:
            os.replace(tmp_path, final_path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


def _write_text_atomic(path, text):
    """Write text to path via a temp file + fsync + rename, so a crash never leaves a
    half-written file behind."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    _replace_file_atomic(tmp, path)


def write_env_updates(updates):
    """Update .env in place (comments and ordering preserved, new keys appended), atomically.

    Values containing CR, LF or NUL are refused: a newline would let one setting inject
    arbitrary extra lines (e.g. a new SPOTIPY_REDIRECT_URI) into the file.
    """
    for k, v in updates.items():
        if not _ENV_KEY_RE.fullmatch(str(k)):
            raise ValueError(f"invalid setting name: {k!r}")
        if _ENV_FORBIDDEN_CHARS_RE.search(str(v)):
            raise ValueError(f"{k} must not contain line breaks or NUL characters")
    existing = ENV_PATH.read_text(encoding="utf-8-sig").splitlines() if ENV_PATH.exists() else []
    seen = set()
    out_lines = []
    for line in existing:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out_lines.append(line)
            continue
        k, _, _ = s.partition("=")
        k = k.strip()
        if k in updates:
            out_lines.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            out_lines.append(f"{k}={v}")
    _write_text_atomic(ENV_PATH, "\n".join(out_lines).rstrip() + "\n")

def env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

# Off unless explicitly enabled: the Werkzeug debugger allows code execution from the browser.
DEBUG_MODE = env_flag("APP_DEBUG", False)
USE_RELOADER = env_flag("APP_USE_RELOADER", False)
SPOTIFY_EXTENDED_ENDPOINTS = env_flag("SPOTIFY_EXTENDED_ENDPOINTS", False)

# ---------- .config.json IO ----------
# Every read-modify-write goes through update_config() under one lock, and every write is
# atomic (temp file + fsync + rename; the previous version is rotated to .config.json.bak).
# The old load_config() turned a parse error (or a read racing a non-atomic write) into {},
# and the next save wrote that {} over ~1 MB of history, stats and caches.
# Lock order: _CONFIG_IO_LOCK may be taken while state_lock is held, never the reverse.
_CONFIG_IO_LOCK = threading.RLock()
CONFIG_UNREADABLE_MESSAGE = "Config file unreadable, not saving to avoid data loss"
_config_corrupt = None          # set while .config.json and its .bak are both unreadable
_config_main_corrupt = False    # main file unreadable (we loaded .bak): never rotate it over .bak


class ConfigCorrupt(RuntimeError):
    """.config.json (and its .bak) exist but cannot be parsed; saving is disabled."""


def _config_bak_path():
    return CONFIG_PATH.with_name(CONFIG_PATH.name + ".bak")


def _read_config_file(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}  # an empty file holds nothing worth protecting
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{Path(path).name}: top-level JSON value is not an object")
    return data


def load_config_strict():
    """Read .config.json: missing -> {}; unparseable -> the .bak copy (logged); both
    unparseable -> ConfigCorrupt, and every save is a no-op until a later load succeeds."""
    global _config_corrupt, _config_main_corrupt
    with _CONFIG_IO_LOCK:
        main_err = bak_err = None
        if CONFIG_PATH.exists():
            try:
                data = _read_config_file(CONFIG_PATH)
            except (OSError, ValueError) as e:  # JSONDecodeError/UnicodeDecodeError are ValueErrors
                main_err = e
            else:
                _config_corrupt = None
                _config_main_corrupt = False
                return data
        bak = _config_bak_path()
        if bak.exists():
            try:
                data = _read_config_file(bak)
            except (OSError, ValueError) as e:
                bak_err = e
            else:
                # Main missing (crash between the two renames of a save) or corrupt.
                if main_err is not None and not _config_main_corrupt:
                    _append_dev_event("ConfigCorrupt", f".config.json unreadable ({main_err}); loaded .config.json.bak",
                                      context="config")
                _config_main_corrupt = main_err is not None
                _config_corrupt = None
                return data
        if main_err is None and bak_err is None:
            _config_corrupt = None
            return {}  # first run: nothing saved yet
        if _config_corrupt is None:
            _append_dev_event("ConfigCorrupt", f"{CONFIG_UNREADABLE_MESSAGE}: {main_err or bak_err}", context="config")
        _config_corrupt = CONFIG_UNREADABLE_MESSAGE
        raise ConfigCorrupt(CONFIG_UNREADABLE_MESSAGE)


def _write_config_locked(cfg):
    """Serialize cfg and atomically replace .config.json. Caller holds _CONFIG_IO_LOCK."""
    global _config_main_corrupt
    text = json.dumps(cfg, indent=2)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if CONFIG_PATH.exists():
        if _config_main_corrupt:
            # Keep the unreadable file for inspection instead of rotating it over the good .bak.
            _replace_file_atomic(CONFIG_PATH, CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupt-{int(time.time())}"))
        else:
            _replace_file_atomic(CONFIG_PATH, _config_bak_path())
    _replace_file_atomic(tmp, CONFIG_PATH)
    _config_main_corrupt = False


def load_config():
    """Lenient read for bootstrap/display code: {} when missing or unreadable.
    Never write back what this returns; use update_config() for read-modify-write."""
    try:
        return load_config_strict()
    except ConfigCorrupt:
        return {}


def save_config(cfg):
    """Atomically replace .config.json with cfg. A no-op (returns False) while the config is
    unreadable. Prefer update_config(), which cannot lose concurrent updates."""
    if not isinstance(cfg, dict):
        return False
    with _CONFIG_IO_LOCK:
        if _config_corrupt:
            return False
        try:
            _write_config_locked(cfg)
            return True
        except Exception as e:
            _log_app_error(e, context="save_config")
            return False


def update_config(mutator):
    """Read-modify-write .config.json atomically under the config lock.

    mutator(cfg) edits the dict in place (or returns a replacement dict). Returns the saved
    dict, or None when nothing was written (config unreadable, or the write failed). Never
    call it with a mutator that takes state_lock: copy what you need first.
    """
    with _CONFIG_IO_LOCK:
        try:
            cfg = load_config_strict()
        except ConfigCorrupt:
            return None
        result = mutator(cfg)
        if isinstance(result, dict):
            cfg = result
        try:
            _write_config_locked(cfg)
        except Exception as e:
            _log_app_error(e, context="update_config")
            return None
        return cfg


def config_error_message():
    """Persistent status error while the config cannot be read (None when healthy)."""
    return _config_corrupt

_PLAYLIST_URI_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]+/)?playlist/|spotify:playlist:)"
    r"([A-Za-z0-9]{22})"
)

def parse_playlist_id(input_str):
    """Accept a playlist ID, share URL, or spotify URI. Returns the 22-char ID or None."""
    if not input_str:
        return None
    s = input_str.strip()
    # Bare 22-char ID
    if re.fullmatch(r"[A-Za-z0-9]{22}", s):
        return s
    m = _PLAYLIST_URI_RE.search(s)
    return m.group(1) if m else None

SCOPE = (
    "user-read-currently-playing "
    "user-read-playback-state "
    "user-read-recently-played "
    "user-modify-playback-state "
    "user-library-read "
    "user-library-modify "
    "user-top-read "
    "playlist-modify-public "
    "playlist-modify-private "
    "playlist-read-private "
    "playlist-read-collaborative"
)

CACHE_PATH = str(DATA_DIR / ".spotify_cache")
LRCLIB_BASE = "https://lrclib.net/api/get"
LRCLIB_UA = "SpotifyDiscoverCleaner/0.3 (https://github.com/Dholeson/spotify-discover-cleaner)"
SERVER_PORT = 8888  # main() sets the port actually served (--port)


def spotify_credentials_configured():
    """True once a Spotify app's Client ID and Secret are set (.env or environment)."""
    return bool((os.environ.get("SPOTIPY_CLIENT_ID") or "").strip()
                and (os.environ.get("SPOTIPY_CLIENT_SECRET") or "").strip())


def spotify_redirect_uri():
    """The Redirect URI the Spotify app must list: SPOTIPY_REDIRECT_URI, else this server's."""
    return (os.environ.get("SPOTIPY_REDIRECT_URI") or "").strip() or f"http://127.0.0.1:{SERVER_PORT}/callback"


def clear_spotify_cache_file():
    try:
        Path(CACHE_PATH).unlink(missing_ok=True)
        return True, None
    except Exception as e:
        return False, str(e)

def build_oauth_manager():
    """Build a Spotify OAuth manager from the current process environment.

    None until a Client ID and Secret are configured (first run shows the Setup screen
    instead) and always in the offline demo, which never signs in to Spotify.
    """
    if DEMO_MODE or not spotify_credentials_configured():
        return None
    from spotipy.cache_handler import CacheFileHandler
    return SpotifyOAuth(
        client_id=os.environ.get("SPOTIPY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=spotify_redirect_uri(),
        scope=SCOPE,
        cache_handler=CacheFileHandler(cache_path=CACHE_PATH),
        open_browser=False,
    )

# Import-time snapshots kept for legacy readers; the settings route refreshes them when
# the values change. New code should call lastfm_api_key() / lastfm_username().
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_USERNAME = os.environ.get("LASTFM_USERNAME")
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def lastfm_api_key():
    """Current Last.fm API key (live: reflects Settings changes without a restart)."""
    return (os.environ.get("LASTFM_API_KEY") or "").strip() or None


def lastfm_username():
    """Current Last.fm username (live: reflects Settings changes without a restart)."""
    return (os.environ.get("LASTFM_USERNAME") or "").strip() or None


def lastfm_configured():
    return bool(lastfm_api_key() and lastfm_username())

# ---------- Shared state ----------
_initial_cfg = load_config()
_initial_playlist_id = _initial_cfg.get("playlist_id") or DEFAULT_PLAYLIST_ID

_initial_stats = _initial_cfg.get("stats") or {}
_today_str = datetime.now().strftime("%Y-%m-%d")
APP_START_MS = int(time.time() * 1000)
_initial_history = (_initial_cfg.get("history") or [])[-MAX_EVENT_HISTORY:]
_initial_backlog_history = (_initial_cfg.get("backlog_history") or [])[-MAX_BACKLOG_HISTORY:]
_initial_liked_library_profile = _initial_cfg.get("liked_library_profile") or {}
_initial_track_genre_cache = _initial_cfg.get("track_genre_cache") or {}

state = {
    "playlist_id": _initial_playlist_id,
    "threshold_seconds": _initial_cfg.get("threshold_seconds", DEFAULT_THRESHOLD),
    "lyrics_offset_ms": int(_initial_cfg.get("lyrics_offset_ms", 2500)),
    "favorite_artists": set(
        (a or "").strip().lower()
        for a in (_initial_cfg.get("favorite_artists") or [])
        if a
    ),
    "favorite_artist_opt_outs": set(
        (a or "").strip().lower()
        for a in (_initial_cfg.get("favorite_artist_opt_outs") or [])
        if a
    ),
    "stats": {
        "removed_total": int(_initial_stats.get("removed_total", 0)),
        "removed_today": int(_initial_stats.get("removed_today", 0)) if _initial_stats.get("day") == _today_str else 0,
        "liked_total": int(_initial_stats.get("liked_total", 0)),
        "liked_today": int(_initial_stats.get("liked_today", 0)) if _initial_stats.get("day") == _today_str else 0,
        "added_total": int(_initial_stats.get("added_total", 0)),
        "added_today": int(_initial_stats.get("added_today", 0)) if _initial_stats.get("day") == _today_str else 0,
        "day": _today_str,
    },
    "weekly_top": [],
    "weekly_top_fetched_at": 0,
    "newly_liked": [],
    "newly_liked_fetched_at": 0,
    "scrobbles_today": None,
    "scrobbles_today_fetched_at": 0,
    "current_track": None,
    "current_track_id": None,
    "current_listened": 0.0,
    "current_in_playlist": False,
    "current_is_liked": False,
    "current_artist_info": None,    # {top_tracks, related, recommendations}
    "current_lyrics": None,         # {synced: bool, lines: [{ts_ms, text}], plain: str|None, source}
    "is_playing": False,
    "progress_ms": 0,
    "duration_ms": 0,
    "recently_played": deque(maxlen=25),
    "recently_removed": deque(maxlen=25),
    "playlist_track_ids": set(),
    "playlist_track_ids_loaded": False,
    "playlist_name": "",
    "playlist_total": 0,
    "playlist_duration_ms": 0,
    "spotify_queue_count": 0,
    "playlist_snapshot_id": None,
    "event_history": list(_initial_history),
    "backlog_history": list(_initial_backlog_history),
    "liked_library_profile": _initial_liked_library_profile.get("data"),
    "liked_library_profile_fetched_at": float(_initial_liked_library_profile.get("fetched_at") or 0),
    "liked_library_profile_refreshing": False,
    "user_top_track_ids": [],       # for personalized recs
    "last_error": None,
    "last_poll": None,
    "auth_ready": False,
    "spotify_user_id": None,
    "client_visible": True,
    "client_focused": True,
    "client_state_seen_at": time.time(),
    "auto_remove_attempted_track_id": None,
}
state_lock = threading.RLock()  # reentrant — same thread can acquire twice safely

# Caches
artist_genre_cache = {}
artist_genre_lock = threading.Lock()
TRACK_GENRE_CACHE_MAX = 5000
_track_genre_cache_lock = threading.RLock()
_track_genre_cache = {}
def _bootstrap_genre_list(values, limit=4):
    out = []
    seen = set()
    for raw in (values or []):
        g = str(raw or "").strip().lower()
        if not g or g in seen:
            continue
        seen.add(g)
        out.append(g)
        if len(out) >= limit:
            break
    return out
if isinstance(_initial_track_genre_cache, dict):
    for _tid, _entry in _initial_track_genre_cache.items():
        if not _tid:
            continue
        _genres = []
        _artist = ""
        _ts = 0
        if isinstance(_entry, dict):
            _genres = _bootstrap_genre_list(_entry.get("genres") or [], limit=4)
            _artist = (_entry.get("artist") or "").strip()
            _ts = int(_entry.get("ts") or 0)
        elif isinstance(_entry, list):
            _genres = _bootstrap_genre_list(_entry, limit=4)
        if _genres:
            _track_genre_cache[str(_tid)] = {"genres": _genres, "artist": _artist, "ts": _ts}
_spotify_queue_cache_lock = threading.Lock()
_spotify_queue_cache = {"count": 0, "fetched_at": 0.0}
_liked_songs_cache_lock = threading.Lock()
_liked_songs_cache = {"tracks": [], "fetched_at": 0.0}

# ---------- OAuth: Flask-native ----------
# Single shared OAuth manager. open_browser=False so spotipy never spins up its
# own local receiver — Flask's /callback handles it.
_oauth = build_oauth_manager()

# The token is kept in memory and refreshed single-flight: every poll and request used to
# re-read .spotify_cache, and two threads refreshing at once could each see the other's
# half-written cache file and briefly log the user out.
_TOKEN_LOCK = threading.Lock()
_token_mem = None


def _token_usable(token_info, margin_s=60):
    try:
        return bool(token_info) and float(token_info.get("expires_at") or 0) - time.time() >= margin_s
    except (TypeError, ValueError):
        return False


def reset_token_memory():
    """Forget the in-memory token (after sign-in, sign-out or credential changes)."""
    global _token_mem
    with _TOKEN_LOCK:
        _token_mem = None


def get_cached_token():
    """Return a valid token dict (refreshing if needed), or None."""
    global _token_mem
    tok = _token_mem
    if _token_usable(tok):
        return tok
    with _TOKEN_LOCK:
        tok = _token_mem
        if _token_usable(tok):
            return tok  # another thread refreshed while we waited
        if _oauth is None:  # no Spotify app configured yet (first run)
            _token_mem = None
            return None
        try:
            token_info = _oauth.cache_handler.get_cached_token()
            if not token_info:
                _token_mem = None
                return None
            # Validate scope match — token cache might be stale if we added scopes
            cached_scopes = set((token_info.get("scope") or "").split())
            needed_scopes = set(SCOPE.split())
            if not needed_scopes.issubset(cached_scopes):
                _token_mem = None
                return None
            if _oauth.is_token_expired(token_info):
                token_info = _oauth.refresh_access_token(token_info["refresh_token"])
            _token_mem = token_info
            return token_info
        except Exception as e:
            _log_app_error(e, context="get_cached_token")
            # A transient refresh failure must not log the user out while the old token lives.
            if _token_usable(tok, margin_s=0):
                return tok
            return None

def _make_sane_session():
    """A requests session that does NOT respect Retry-After headers.

    Spotify can send Retry-After values of many hours (we've seen 44949s ≈ 12.5h).
    urllib3's default Retry honors that header EVEN when the status code isn't in
    status_forcelist, which causes the entire poll loop to hang for hours.
    Building our own session with respect_retry_after_header=False is the only
    way to disable that behavior. We handle 429 manually in poll_loop with a cap.
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    sess = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        status_forcelist=(500, 502, 503, 504),
        # Only idempotent reads are retried: a retried POST (next track, playlist add,
        # queue) could run twice when the first attempt actually reached Spotify.
        allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
        backoff_factor=0.5,
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


class SpotifyGuardBlocked(spotipy.SpotifyException):
    """Raised locally when the in-app Spotify guard blocks a request preflight.

    Subclasses SpotifyException (http_status 429) for old callers, but it is NOT a Spotify
    rate limit: no request was sent. `.local` is True; `.retry_after` is seconds until the
    call may be allowed; `.kind` is budget | protect | cooldown | inflight. Use
    is_real_rate_limit(e) to tell a genuine 429 apart.
    """

    local = True

    def __init__(self, message, retry_after=None, scope="spotify", kind="cooldown"):
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        super().__init__(429, -1, message, reason="local guard", headers=headers)
        self.scope = scope
        self.kind = kind
        self.retry_after = retry_after


class SpotifyAppRegistrationBlocked(spotipy.SpotifyException):
    """Raised locally when Spotify says this user isn't registered for the dev app."""

    local = True

    def __init__(self, message, retry_after=None):
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        super().__init__(403, -1, message, reason="app user not registered", headers=headers)
        self.retry_after = retry_after


def is_local_guard_block(exc):
    """True for exceptions raised by this app's guard without contacting Spotify."""
    return bool(getattr(exc, "local", False))


def is_real_rate_limit(exc):
    """True only for a 429 that Spotify actually returned (never for a local guard block)."""
    return (isinstance(exc, spotipy.SpotifyException)
            and getattr(exc, "http_status", None) == 429
            and not is_local_guard_block(exc))


def spotify_retry_after_from(exc, default=30):
    """Retry-After seconds carried by a SpotifyException (or our local block)."""
    ra = getattr(exc, "retry_after", None)
    if ra is not None:
        try:
            return max(0, int(ra))
        except (TypeError, ValueError):
            pass
    try:
        headers = getattr(exc, "headers", None) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            return max(0, int(float(raw)))
    except (TypeError, ValueError):
        pass
    return default


_SPOTIFY_GUARD_LOCK = threading.RLock()
_SPOTIFY_GUARD_CALL_LOG = deque(maxlen=400)
# LRU response cache (method:args -> entry); bounded, expired entries pruned on insert.
_SPOTIFY_GUARD_CACHE = OrderedDict()
_SPOTIFY_GUARD_CACHE_MAX = 2000
_SPOTIFY_GUARD_CATEGORY_LOCKS = {}
_SPOTIFY_GUARD_METHOD_LOCKS = {}
_SPOTIFY_GUARD_BUDGET_BUCKETS = {}
_SPOTIFY_GUARD_GLOBAL_UNTIL = 0.0
_SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S = None
_SPOTIFY_GUARD_GLOBAL_REASON = None
_SPOTIFY_GUARD_PROTECT_MODE_UNTIL = 0.0
_SPOTIFY_GUARD_PROTECT_REASON = None
_SPOTIFY_GUARD_RESTORED_UNTIL_WALL = 0.0
_SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL = 0.0
_SPOTIFY_GUARD_RESTORED_REASON = None
_SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S = None
_SPOTIFY_GUARD_RESTORED_PENDING = False
_SPOTIFY_GUARD_REVALIDATING = False
_SPOTIFY_THROTTLE_LOCK = threading.Lock()
_SPOTIFY_LAST_CALL_TIME = 0.0                       # when the most recent live call was admitted
_SPOTIFY_LAST_CALL_TIME_BY_LANE = defaultdict(float)  # ... per lane
_SPOTIFY_MIN_INTERVAL_S = 0.35   # 350 ms floor between any two live Spotify calls (~170 req/min max)
# Minimum gap between two calls of the SAME lane. A user's click (write) must not wait
# behind background paging, so lanes only pace themselves plus the global floor above.
_SPOTIFY_LANE_MIN_INTERVAL_S = {
    "realtime": 0.35,
    "interactive": 0.65,
    "background": 1.10,
    "write": 0.35,
}
_SPOTIFY_CALL_SEQ = itertools.count(1)
_SPOTIFY_APP_REG_BLOCK_UNTIL = 0.0
_SPOTIFY_APP_REG_BLOCK_MSG = None
_SPOTIFY_SCHEDULER_LOCK = threading.RLock()
_SPOTIFY_WRITE_LOCK = threading.Lock()
_SPOTIFY_INFLIGHT = {}

_SPOTIFY_CONTEXT_SKIP_FUNCS = frozenset({
    "_spotify_call_context", "_spotify_guard_log", "_spotify_guard_note_rate_limit",
    "_spotify_guard_preflight", "_spotify_guard_emit_logs", "guarded", "__getattr__",
    "_record_spotify_429_diagnostic", "_note_spotify_app_registration_error", "_worker",
})
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _spotify_call_context(with_stack=True):
    with state_lock:
        current = dict(state.get("current_track") or {})
        playlist_id = state.get("playlist_id")
        listened = round(float(state.get("current_listened") or 0), 1)
    stack = []
    if with_stack:
        # A cheap frame walk (inspect.stack() stats every source file): callers in app.py
        # and src/ only, guard internals skipped.
        try:
            frame = sys._getframe(1)
            depth = 0
            while frame is not None and depth < 25 and len(stack) < 6:
                code = frame.f_code
                filename = code.co_filename or ""
                if (filename.startswith(_PROJECT_DIR) and "site-packages" not in filename
                        and code.co_name not in _SPOTIFY_CONTEXT_SKIP_FUNCS):
                    stack.append(f"{code.co_name}:{frame.f_lineno}")
                frame = frame.f_back
                depth += 1
        except Exception:
            stack = []
    return {
        "track_id": current.get("id"),
        "track_name": current.get("name"),
        "artists": current.get("artists"),
        "playlist_id": playlist_id,
        "listened_s": listened,
        "stack": stack,
        "thread": threading.current_thread().name,
    }

def _spotify_guard_snapshot(limit=30):
    # Its own small lock: iterating a deque while another thread appends raises.
    with _SPOTIFY_GUARD_LOG_LOCK:
        return list(itertools.islice(_SPOTIFY_GUARD_CALL_LOG, 0, limit))

def is_spotify_app_registration_error(exc):
    if not isinstance(exc, spotipy.SpotifyException):
        return False
    if getattr(exc, "http_status", None) != 403:
        return False
    msg = str(exc).lower()
    # Spotify has worded this as "...not registered for this application" and as "Check settings
    # on developer.spotify.com/dashboard, the user may not be registered." (Development Mode
    # allowlist). A removed endpoint's 403 says neither. Since 2026 Spotify also answers 403
    # "Active premium subscription required for the owner of the app" when the account that
    # created the developer app has no Premium: every call fails the same way, so it is paused alike.
    return ("not registered for this application" in msg or "may not be registered" in msg
            or "premium subscription required" in msg)


def spotify_app_access_message(detail):
    """Plain-language reason for an app-level 403 (see is_spotify_app_registration_error)."""
    if "premium subscription required" in str(detail or "").lower():
        return ("Spotify requires the account that created this Spotify app to have Premium. "
                "Upgrade that account (or create the app with a Premium account and save its keys "
                "in Settings), then sign in again.")
    return ("Spotify rejected this account for the app. Add it under User Management "
            "in the Spotify developer dashboard, then sign in again.")

def _spotify_app_reg_block_remaining_s():
    with _SPOTIFY_GUARD_LOCK:
        return max(0, int((_SPOTIFY_APP_REG_BLOCK_UNTIL - time.monotonic()) + 0.999))

def _spotify_app_reg_block_until_ms():
    with _SPOTIFY_GUARD_LOCK:
        remaining_s = max(0.0, _SPOTIFY_APP_REG_BLOCK_UNTIL - time.monotonic())
        if remaining_s <= 0:
            return None
        return int(time.time() * 1000 + (remaining_s * 1000))

def _clear_spotify_app_registration_block():
    global _SPOTIFY_APP_REG_BLOCK_UNTIL, _SPOTIFY_APP_REG_BLOCK_MSG
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_APP_REG_BLOCK_UNTIL = 0.0
        _SPOTIFY_APP_REG_BLOCK_MSG = None

def _note_spotify_app_registration_error(method_name, exc, quiet_s=300):
    global _SPOTIFY_APP_REG_BLOCK_UNTIL, _SPOTIFY_APP_REG_BLOCK_MSG
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_APP_REG_BLOCK_UNTIL = max(_SPOTIFY_APP_REG_BLOCK_UNTIL, time.monotonic() + max(60, int(quiet_s)))
        _SPOTIFY_APP_REG_BLOCK_MSG = str(exc)
    _append_dev_event(
        "Spotify403",
        f"Spotify app access rejected on {method_name}: {exc}",
        context=f"spotify_403_{method_name}",
        tb="\n".join((_spotify_call_context().get("stack") or [])),
    )


def _spotify_throttle(lane="interactive"):
    """Enforce paced Spotify access across request lanes.

    Any two live calls are at least _SPOTIFY_MIN_INTERVAL_S apart, and calls in the same
    lane at least that lane's gap apart. The slot is checked under the lock but the wait
    happens outside it (then re-checked), so a background call sleeping out its 1.1 s lane
    gap never holds up a realtime poll or a click: those wait at most ~0.35 s.
    """
    global _SPOTIFY_LAST_CALL_TIME
    lane = lane if lane in _SPOTIFY_LANE_MIN_INTERVAL_S else "interactive"
    lane_min = float(_SPOTIFY_LANE_MIN_INTERVAL_S.get(lane) or _SPOTIFY_MIN_INTERVAL_S)
    while True:
        with _SPOTIFY_THROTTLE_LOCK:
            now = time.monotonic()
            ready_at = max(
                _SPOTIFY_LAST_CALL_TIME + _SPOTIFY_MIN_INTERVAL_S,
                _SPOTIFY_LAST_CALL_TIME_BY_LANE.get(lane, 0.0) + lane_min,
            )
            if ready_at <= now:
                _SPOTIFY_LAST_CALL_TIME = now
                _SPOTIFY_LAST_CALL_TIME_BY_LANE[lane] = now
                return
            wait = ready_at - now
        time.sleep(wait)


_LIKED_ARTIST_COUNT_CACHE = {"fetched_at": 0.0, "counts": {}, "names": {}}
_LIKED_ARTIST_COUNT_LOCK = threading.Lock()
_FAVORITE_RELEASE_CACHE = {"fetched_at": 0.0, "data": None}
_FAVORITE_RELEASE_LOCK = threading.Lock()

# Budgets are per METHOD ([calls, window_s]; the bucket key is the method name), so polling
# can't starve devices, and search can't starve recommendations. They are conservative
# local caps on top of Spotify's real (much higher) limit; real 429s still lock app-wide.
# "essential" methods (playback + the writes the user asked for) are exempt from protect
# mode but still wait out a real 429 lock. Paging a 1,200-track backlog needs 12 pages of
# playlist_items/next per sync, well inside 40/min.
_SPOTIFY_GUARD_POLICIES = {
    "search": {"category": "search", "budget": [15, 60], "cache_ttl_s": 86400, "stale_ttl_s": 604800, "lane": "interactive", "single_flight": True},
    "artist": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "artists": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "artist_top_tracks": {"category": "metadata", "budget": [10, 60], "cache_ttl_s": 86400, "stale_ttl_s": 604800, "lane": "interactive", "single_flight": True},
    "artist_related_artists": {"category": "metadata", "budget": [5, 60], "cache_ttl_s": 86400, "stale_ttl_s": 604800, "lane": "interactive", "single_flight": True},
    "recommendations": {"category": "recommendations", "budget": [5, 60], "cache_ttl_s": 3600, "stale_ttl_s": 21600, "lane": "background", "single_flight": True},
    "artist_albums": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "album": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "albums": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "album_tracks": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "track": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "tracks": {"category": "metadata", "budget": [20, 60], "cache_ttl_s": 172800, "stale_ttl_s": 1209600, "lane": "interactive", "single_flight": True},
    "playlist": {"category": "playlist_read", "budget": [20, 60], "cache_ttl_s": 300, "stale_ttl_s": 3600, "lane": "background", "single_flight": True},
    "playlist_items": {"category": "playlist_read", "budget": [40, 60], "cache_ttl_s": 300, "stale_ttl_s": 3600, "lane": "background", "single_flight": True},
    "playlist_tracks": {"category": "playlist_read", "budget": [40, 60], "cache_ttl_s": 300, "stale_ttl_s": 3600, "lane": "background", "single_flight": True},
    "current_user_playlists": {"category": "playlist_read", "budget": [10, 60], "cache_ttl_s": 300, "stale_ttl_s": 3600, "lane": "background", "single_flight": True},
    "current_user_saved_tracks": {"category": "library", "budget": [40, 60], "cache_ttl_s": 300, "stale_ttl_s": 1800, "lane": "background", "single_flight": True},
    "current_user_saved_tracks_contains": {"category": "library", "budget": [20, 60], "cache_ttl_s": 21600, "stale_ttl_s": 86400, "lane": "interactive", "single_flight": True},
    "current_user_top_tracks": {"category": "user_read", "budget": [5, 60], "cache_ttl_s": 3600, "stale_ttl_s": 21600, "lane": "background", "single_flight": True},
    "current_user_top_artists": {"category": "user_read", "budget": [5, 60], "cache_ttl_s": 3600, "stale_ttl_s": 21600, "lane": "background", "single_flight": True},
    "current_user_recently_played": {"category": "user_read", "budget": [6, 60], "cache_ttl_s": 30, "stale_ttl_s": 300, "lane": "background", "single_flight": True},
    "current_playback": {"category": "playback_read", "budget": [40, 60], "cache_ttl_s": 0, "lane": "realtime", "essential": True},
    "currently_playing": {"category": "playback_read", "budget": [20, 60], "cache_ttl_s": 0, "lane": "realtime", "essential": True},
    "devices": {"category": "playback_read", "budget": [10, 60], "cache_ttl_s": 20, "stale_ttl_s": 120, "lane": "interactive", "single_flight": True, "essential": True},
    "queue": {"category": "playback_read", "budget": [6, 60], "cache_ttl_s": 0, "lane": "interactive"},
    "me": {"category": "user_read", "budget": [5, 60], "cache_ttl_s": 3600, "stale_ttl_s": 21600, "lane": "interactive", "single_flight": True},
    "current_user": {"category": "user_read", "budget": [5, 60], "cache_ttl_s": 3600, "stale_ttl_s": 21600, "lane": "interactive", "single_flight": True},
    "start_playback": {"category": "playback_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "pause_playback": {"category": "playback_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "next_track": {"category": "playback_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "previous_track": {"category": "playback_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "seek_track": {"category": "playback_write", "budget": [40, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "add_to_queue": {"category": "playback_write", "budget": [20, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "transfer_playback": {"category": "playback_write", "budget": [10, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "playlist_add_items": {"category": "playlist_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "playlist_remove_all_occurrences_of_items": {"category": "playlist_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "current_user_saved_tracks_add": {"category": "library_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "current_user_saved_tracks_delete": {"category": "library_write", "budget": [30, 60], "cache_ttl_s": 0, "lane": "write", "essential": True},
    "next": {"category": "pagination", "budget": [40, 60], "cache_ttl_s": 0, "lane": "background", "single_flight": False},
}

_SPOTIFY_DEFAULT_READ_POLICY = {
    "category": "default", "budget": (10, 60), "cache_ttl_s": 0, "stale_ttl_s": 0,
    "essential": False, "lane": "interactive", "single_flight": False,
}
# Unknown methods that change something the user asked for default to essential writes.
_SPOTIFY_DEFAULT_WRITE_POLICY = {
    "category": "write", "budget": (30, 60), "cache_ttl_s": 0, "stale_ttl_s": 0,
    "essential": True, "lane": "write", "single_flight": False,
}
_SPOTIFY_WRITE_PREFIXES = (
    "playlist_add", "playlist_remove", "playlist_replace", "playlist_reorder", "playlist_change",
    "playlist_upload", "user_playlist_create", "user_playlist_add", "user_playlist_remove",
    "user_playlist_replace", "user_playlist_reorder", "user_playlist_change",
    "start_playback", "pause_playback", "next_track", "previous_track", "seek_track",
    "add_to_queue", "transfer_playback", "volume", "repeat", "shuffle",
    "current_user_follow", "current_user_unfollow", "user_follow", "user_unfollow",
)
_SPOTIFY_WRITE_SUFFIXES = ("_add", "_delete", "_remove")


def _spotify_method_is_write(method_name):
    name = str(method_name or "")
    return name.startswith(_SPOTIFY_WRITE_PREFIXES) or name.endswith(_SPOTIFY_WRITE_SUFFIXES)


def _spotify_guard_policy(method_name):
    policy = _SPOTIFY_GUARD_POLICIES.get(method_name)
    if policy is not None:
        return policy
    if _spotify_method_is_write(method_name):
        return _SPOTIFY_DEFAULT_WRITE_POLICY
    return _SPOTIFY_DEFAULT_READ_POLICY


def _spotify_guard_cache_key(method_name, args, kwargs):
    try:
        payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    except Exception:
        payload = repr((args, kwargs))
    return f"{method_name}:{payload}"


def _spotify_copy(value):
    """Callers get their own copy: mutating a returned dict must not change the cache."""
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    return value


def _spotify_cache_get(cache_key, now=None, allow_stale=False):
    """Raw cache lookup (caller holds the guard lock). The returned value is the cached
    object itself; copy it with _spotify_copy() before handing it out."""
    if not cache_key:
        return None
    now = time.monotonic() if now is None else now
    entry = _SPOTIFY_GUARD_CACHE.get(cache_key)
    if not entry:
        return None
    if isinstance(entry, tuple):
        fresh_until, value = entry
        if fresh_until > now:
            return {"value": value, "state": "fresh", "fresh_until": fresh_until, "stale_until": fresh_until}
        return None
    fresh_until = float(entry.get("fresh_until") or 0.0)
    stale_until = float(entry.get("stale_until") or fresh_until or 0.0)
    value = entry.get("value")
    if fresh_until > now:
        _SPOTIFY_GUARD_CACHE.move_to_end(cache_key)
        return {"value": value, "state": "fresh", "fresh_until": fresh_until, "stale_until": stale_until}
    if allow_stale and stale_until > now:
        _SPOTIFY_GUARD_CACHE.move_to_end(cache_key)
        return {"value": value, "state": "stale", "fresh_until": fresh_until, "stale_until": stale_until}
    return None


def _spotify_cache_prune(now=None):
    """Drop expired entries, then least-recently-used ones beyond the cap (lock held)."""
    now = time.monotonic() if now is None else now
    if len(_SPOTIFY_GUARD_CACHE) <= _SPOTIFY_GUARD_CACHE_MAX:
        return
    for key in [k for k, e in _SPOTIFY_GUARD_CACHE.items()
                if isinstance(e, dict) and float(e.get("stale_until") or 0) <= now and not e.get("refreshing")]:
        _SPOTIFY_GUARD_CACHE.pop(key, None)
    while len(_SPOTIFY_GUARD_CACHE) > _SPOTIFY_GUARD_CACHE_MAX:
        _SPOTIFY_GUARD_CACHE.popitem(last=False)


def _spotify_cache_store(cache_key, value, fresh_ttl_s, stale_ttl_s=0):
    if not cache_key:
        return
    now = time.monotonic()
    fresh_ttl_s = max(0, int(fresh_ttl_s or 0))
    stale_ttl_s = max(fresh_ttl_s, int(stale_ttl_s or 0))
    _SPOTIFY_GUARD_CACHE[cache_key] = {
        "value": _spotify_copy(value),
        "fresh_until": now + fresh_ttl_s,
        "stale_until": now + stale_ttl_s,
        "refreshing": False,
    }
    _SPOTIFY_GUARD_CACHE.move_to_end(cache_key)
    _spotify_cache_prune(now)


# Bumped by every invalidation: a live read that started before a write must not store its
# (now outdated) result after the write invalidated the cache.
_SPOTIFY_CACHE_GEN = 0
_SPOTIFY_PLAYLIST_CACHE_METHODS = ("playlist", "playlist_items", "playlist_tracks",
                                   "user_playlist", "user_playlist_tracks")
_SPOTIFY_LIBRARY_CACHE_METHODS = ("current_user_saved_tracks", "current_user_saved_tracks_contains")


def _spotify_cache_invalidate(methods, needle=None):
    """Remove cached responses of the given method names (optionally only keys containing
    needle, e.g. a playlist id). Returns how many entries were dropped."""
    global _SPOTIFY_CACHE_GEN
    methods = {methods} if isinstance(methods, str) else set(methods or ())
    # Match the needle as a whole token: a plain substring test would let id "abc" also
    # evict "xabc"'s pages. Spotify ids are alphanumeric, so non-alnum neighbours delimit them
    # whether the key holds a bare id, a spotify: URI or a share URL.
    needle_re = (re.compile(r"(?<![A-Za-z0-9])" + re.escape(str(needle)) + r"(?![A-Za-z0-9])")
                 if needle else None)
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_CACHE_GEN += 1
        doomed = [k for k in _SPOTIFY_GUARD_CACHE
                  if k.split(":", 1)[0] in methods and (needle_re is None or needle_re.search(k))]
        for k in doomed:
            _SPOTIFY_GUARD_CACHE.pop(k, None)
    return len(doomed)


def invalidate_playlist_cache(playlist_id=None):
    """Forget cached reads of one playlist (all playlists when None) after it changed."""
    pid = None
    if playlist_id:
        pid = parse_playlist_id(str(playlist_id)) or str(playlist_id)
    n = _spotify_cache_invalidate(_SPOTIFY_PLAYLIST_CACHE_METHODS, needle=pid)
    # Track counts in the playlist list changed too.
    n += _spotify_cache_invalidate(("current_user_playlists",))
    return n


def invalidate_library_cache():
    """Forget cached Liked Songs pages and saved-state lookups after a like/unlike."""
    return _spotify_cache_invalidate(_SPOTIFY_LIBRARY_CACHE_METHODS)


def _spotify_invalidate_after_write(method_name, args, kwargs):
    if not _spotify_method_is_write(method_name):
        return
    if method_name.startswith("playlist_"):
        pid = kwargs.get("playlist_id") or (args[0] if args else None)
        invalidate_playlist_cache(pid if isinstance(pid, str) else None)
    elif method_name.startswith("user_playlist"):
        pid = kwargs.get("playlist_id") or (args[1] if len(args) > 1 else None)
        invalidate_playlist_cache(pid if isinstance(pid, str) else None)
    elif method_name.startswith("current_user_saved_tracks"):
        invalidate_library_cache()


def _spotify_cache_set_refreshing(cache_key, refreshing):
    if not cache_key:
        return False
    entry = _SPOTIFY_GUARD_CACHE.get(cache_key)
    if not isinstance(entry, dict):
        return False
    if refreshing and entry.get("refreshing"):
        return False
    entry["refreshing"] = bool(refreshing)
    return True


def _spotify_inflight_wait(cache_key, timeout_s=20):
    if not cache_key:
        return None
    with _SPOTIFY_SCHEDULER_LOCK:
        token = _SPOTIFY_INFLIGHT.get(cache_key)
        if token:
            token["waiters"] = int(token.get("waiters") or 0) + 1
            return token
    return None


def _spotify_inflight_start(cache_key):
    if not cache_key:
        return None, False
    with _SPOTIFY_SCHEDULER_LOCK:
        token = _SPOTIFY_INFLIGHT.get(cache_key)
        if token:
            token["waiters"] = int(token.get("waiters") or 0) + 1
            return token, False
        token = {
            "event": threading.Event(),
            "result": None,
            "error": None,
            "started": time.monotonic(),
            "waiters": 0,
        }
        _SPOTIFY_INFLIGHT[cache_key] = token
        return token, True


def _spotify_inflight_finish(cache_key, token, result=None, error=None):
    if token is None:
        return
    token["result"] = result
    token["error"] = error
    token["event"].set()
    if cache_key:
        with _SPOTIFY_SCHEDULER_LOCK:
            if _SPOTIFY_INFLIGHT.get(cache_key) is token:
                _SPOTIFY_INFLIGHT.pop(cache_key, None)


def _spotify_trigger_background_refresh(client, method_name, attr, args, kwargs, cache_key, policy,
                                        already_marked=True):
    """Refresh a stale cache entry in a daemon thread.

    Preflight only returns "stale-refresh" when nothing blocks a live call; it has already
    marked the entry as refreshing and reserved a budget slot. The worker re-checks locks and
    protect mode right before the raw call, because a 429 may have arrived meanwhile.
    """
    if not cache_key:
        return False
    if not already_marked:
        with _SPOTIFY_GUARD_LOCK:
            if not _spotify_cache_set_refreshing(cache_key, True):
                return False
    gen = _SPOTIFY_CACHE_GEN

    def _worker():
        try:
            started = time.monotonic()
            _spotify_throttle(policy.get("lane") or "background")
            with _SPOTIFY_GUARD_LOCK:
                now = time.monotonic()
                if _SPOTIFY_APP_REG_BLOCK_UNTIL > now:
                    blocked = {"kind": "app-registration"}
                else:
                    blocked = _spotify_guard_is_blocked(method_name, policy, now, check_budget=False)
            if blocked:
                _spotify_guard_log(method_name, "hydrate-skipped", blocked.get("kind"))
                return
            result = attr(*args, **kwargs)
            cache_ttl_s = int(policy.get("cache_ttl_s") or 0)
            stale_ttl_s = int(policy.get("stale_ttl_s") or cache_ttl_s or 0)
            if cache_ttl_s > 0:
                with _SPOTIFY_GUARD_LOCK:
                    if gen == _SPOTIFY_CACHE_GEN:
                        _spotify_cache_store(cache_key, result, cache_ttl_s, stale_ttl_s)
            _spotify_guard_log(
                method_name,
                "hydrated",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            _spotify_note_live_success()
        except SpotifyGuardBlocked as e:
            _spotify_guard_log(method_name, "hydrate-blocked", str(e))
        except SpotifyAppRegistrationBlocked as e:
            _spotify_guard_log(method_name, "hydrate-403-blocked", str(e))
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                retry_after = spotify_retry_after_from(e, default=30)
                _spotify_guard_note_rate_limit(method_name, retry_after)
                _spotify_guard_log(method_name, "hydrate-429", f"retry-after {retry_after}s", http_status=429, retry_after_s=retry_after)
                _record_spotify_429_diagnostic(method_name, retry_after, e)
            elif is_spotify_app_registration_error(e):
                _note_spotify_app_registration_error(method_name, e)
                _spotify_guard_log(method_name, "hydrate-403", "user not registered for this application", http_status=403)
            else:
                _spotify_guard_log(method_name, "hydrate-http", str(e), http_status=e.http_status)
        except Exception as e:
            _spotify_guard_log(method_name, "hydrate-error", str(e))
        finally:
            with _SPOTIFY_GUARD_LOCK:
                _spotify_cache_set_refreshing(cache_key, False)

    threading.Thread(target=_worker, daemon=True, name=f"sp-hydrate-{method_name}").start()
    return True


# Outcomes common enough that capturing the caller stack for them is wasted work.
_SPOTIFY_LOG_NO_STACK = frozenset({"ok", "cached", "stale", "hydrated"})
_SPOTIFY_GUARD_LOG_LOCK = threading.Lock()


def _spotify_guard_emit_logs(entries):
    for method_name, outcome, detail, extra in entries or ():
        try:
            _spotify_guard_log(method_name, outcome, detail, **(extra or {}))
        except Exception:
            pass


def _spotify_guard_log(method_name, outcome, detail=None, **extra):
    """Append to the dev call log. Never call this while holding _SPOTIFY_GUARD_LOCK: it
    takes state_lock, and a state_lock holder may be waiting for the guard lock."""
    ctx = _spotify_call_context(with_stack=outcome not in _SPOTIFY_LOG_NO_STACK)
    entry = {
        "seq": next(_SPOTIFY_CALL_SEQ),
        "ts_ms": current_ts_ms() if "current_ts_ms" in globals() else int(time.time() * 1000),
        "method": method_name,
        "category": _spotify_guard_policy(method_name).get("category"),
        "outcome": outcome,
        "detail": detail,
        "track_id": ctx.get("track_id"),
        "track_name": ctx.get("track_name"),
        "artists": ctx.get("artists"),
        "playlist_id": ctx.get("playlist_id"),
        "listened_s": ctx.get("listened_s"),
        "thread": ctx.get("thread"),
        "stack": ctx.get("stack"),
    }
    entry.update(extra)
    with _SPOTIFY_GUARD_LOG_LOCK:
        _SPOTIFY_GUARD_CALL_LOG.appendleft(entry)


def _spotify_guard_remaining_s():
    with _SPOTIFY_GUARD_LOCK:
        return max(0, int((_SPOTIFY_GUARD_GLOBAL_UNTIL - time.monotonic()) + 0.999))


def _spotify_guard_protect_remaining_s():
    with _SPOTIFY_GUARD_LOCK:
        return max(0, int((_SPOTIFY_GUARD_PROTECT_MODE_UNTIL - time.monotonic()) + 0.999))


def _spotify_guard_until_ms():
    with _SPOTIFY_GUARD_LOCK:
        remaining_s = max(0.0, _SPOTIFY_GUARD_GLOBAL_UNTIL - time.monotonic())
        if remaining_s <= 0:
            return None
        return int(time.time() * 1000 + (remaining_s * 1000))


def _spotify_guard_protect_until_ms():
    with _SPOTIFY_GUARD_LOCK:
        remaining_s = max(0.0, _SPOTIFY_GUARD_PROTECT_MODE_UNTIL - time.monotonic())
        if remaining_s <= 0:
            return None
        return int(time.time() * 1000 + (remaining_s * 1000))


def _spotify_guard_restored_remaining_s():
    with _SPOTIFY_GUARD_LOCK:
        if not _SPOTIFY_GUARD_RESTORED_PENDING:
            return 0
        return max(0, int((_SPOTIFY_GUARD_RESTORED_UNTIL_WALL - time.time()) + 0.999))


def _spotify_guard_restored_until_ms():
    with _SPOTIFY_GUARD_LOCK:
        if not _SPOTIFY_GUARD_RESTORED_PENDING or _SPOTIFY_GUARD_RESTORED_UNTIL_WALL <= time.time():
            return None
        return int(_SPOTIFY_GUARD_RESTORED_UNTIL_WALL * 1000)


def _clear_guard_state(persist=True, include_search=True):
    global _SPOTIFY_GUARD_GLOBAL_UNTIL, _SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S, _SPOTIFY_GUARD_GLOBAL_REASON
    global _SPOTIFY_GUARD_PROTECT_MODE_UNTIL, _SPOTIFY_GUARD_PROTECT_REASON
    global _SPOTIFY_GUARD_RESTORED_UNTIL_WALL, _SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL
    global _SPOTIFY_GUARD_RESTORED_REASON, _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S
    global _SPOTIFY_GUARD_RESTORED_PENDING, _SPOTIFY_GUARD_REVALIDATING
    global _sp_search_backoff_until, _sp_search_retry_after_s
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_GUARD_GLOBAL_UNTIL = 0.0
        _SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S = None
        _SPOTIFY_GUARD_GLOBAL_REASON = None
        _SPOTIFY_GUARD_PROTECT_MODE_UNTIL = 0.0
        _SPOTIFY_GUARD_PROTECT_REASON = None
        _SPOTIFY_GUARD_RESTORED_UNTIL_WALL = 0.0
        _SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL = 0.0
        _SPOTIFY_GUARD_RESTORED_REASON = None
        _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S = None
        _SPOTIFY_GUARD_RESTORED_PENDING = False
        _SPOTIFY_GUARD_REVALIDATING = False
        _SPOTIFY_GUARD_CATEGORY_LOCKS.clear()
        _SPOTIFY_GUARD_METHOD_LOCKS.clear()
        _SPOTIFY_GUARD_BUDGET_BUCKETS.clear()
    if include_search:
        _sp_search_backoff_until = 0.0
        _sp_search_retry_after_s = None
    if persist and _SQLITE_ENABLED:
        try:
            conn = _db.get_conn()
            conn.execute("DELETE FROM app_meta WHERE key IN (?,?,?,?,?)", (
                "spotify_guard_global_until_wall",
                "spotify_guard_protect_until_wall",
                "spotify_guard_global_reason",
                "spotify_guard_global_retry_after_s",
                "spotify_guard_protect_reason",
            ))
            conn.commit()
        except Exception:
            pass


def _spotify_guard_wait_out_lock(step_s=30):
    """Sleep until the current global Spotify guard lock expires.

    We wait in chunks so the loop can remain interruptible and keep status text
    reasonably fresh, but we do not make any more Spotify calls meanwhile.
    """
    while True:
        remaining = _spotify_guard_remaining_s()
        if remaining <= 0:
            return
        time.sleep(min(step_s, remaining))


def _spotify_guard_is_blocked(method_name, policy, now, check_budget=True):
    """Why a live call to method_name must not be made right now, or None.

    Caller holds _SPOTIFY_GUARD_LOCK. Returns {kind, scope, retry_after, message} with kind
    protect (non-essential call during protect mode), cooldown (a real 429 lock) or budget
    (this method's local per-minute cap is used up).
    """
    if _SPOTIFY_GUARD_PROTECT_MODE_UNTIL > now and not policy.get("essential", False):
        retry_after = max(0, int((_SPOTIFY_GUARD_PROTECT_MODE_UNTIL - now) + 0.999))
        return {"kind": "protect", "scope": "protect-mode", "retry_after": retry_after,
                "message": f"Spotify protect mode is active for {retry_after}s. Non-essential {method_name} blocked."}
    lock_until = max(
        _SPOTIFY_GUARD_GLOBAL_UNTIL,
        _SPOTIFY_GUARD_CATEGORY_LOCKS.get(policy["category"], 0.0),
        _SPOTIFY_GUARD_METHOD_LOCKS.get(method_name, 0.0),
    )
    if lock_until > now:
        retry_after = max(0, int((lock_until - now) + 0.999))
        return {"kind": "cooldown", "scope": policy["category"], "retry_after": retry_after,
                "message": f"Spotify guard blocked {method_name}. Cooling down for {retry_after}s."}
    if check_budget:
        limit, window_s = policy.get("budget") or (0, 0)
        if limit and window_s:
            bucket = _SPOTIFY_GUARD_BUDGET_BUCKETS.setdefault(method_name, deque())
            cutoff = now - window_s
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int((bucket[0] + window_s - now) + 0.999))
                return {"kind": "budget", "scope": method_name, "retry_after": retry_after,
                        "message": f"Spotify guard budget reached for {method_name}. Retry in {retry_after}s."}
    return None


def _spotify_guard_consume_budget(method_name, policy, now):
    limit, window_s = policy.get("budget") or (0, 0)
    if limit and window_s:
        _SPOTIFY_GUARD_BUDGET_BUCKETS.setdefault(method_name, deque()).append(now)


def _spotify_guard_release_budget(method_name):
    """Give back the slot reserved by preflight when the call was never made."""
    with _SPOTIFY_GUARD_LOCK:
        bucket = _SPOTIFY_GUARD_BUDGET_BUCKETS.get(method_name)
        if bucket:
            bucket.pop()


def _spotify_guard_preflight(method_name, args, kwargs):
    """Decide how a guarded call proceeds; returns (cache_state, value, cache_key, policy).

    cache_state: "fresh" (serve value), "stale" (serve value; a refresh is blocked or already
    running), "stale-refresh" (serve value and refresh in the background: nothing blocks a
    live call and a budget slot is reserved), or None (make the live call; slot reserved).
    Raises SpotifyGuardBlocked / SpotifyAppRegistrationBlocked when a live call is needed
    but not allowed. Cached values are always served, even during a lock: stale data beats
    no data, and serving it costs Spotify nothing.
    """
    policy = _spotify_guard_policy(method_name)
    cache_ttl_s = int(policy.get("cache_ttl_s") or 0)
    stale_ttl_s = int(policy.get("stale_ttl_s") or 0)
    cache_key = _spotify_guard_cache_key(method_name, args, kwargs) if cache_ttl_s > 0 else None
    now = time.monotonic()
    logs = []  # emitted after the lock is released (logging takes state_lock)
    try:
        with _SPOTIFY_GUARD_LOCK:
            if cache_key:
                cached = _spotify_cache_get(cache_key, now=now, allow_stale=stale_ttl_s > cache_ttl_s)
                if cached and cached.get("state") == "fresh":
                    logs.append((method_name, "cached", None, None))
                    return "fresh", _spotify_copy(cached["value"]), cache_key, policy
                if cached and cached.get("state") == "stale" and not policy.get("essential", False):
                    if _SPOTIFY_APP_REG_BLOCK_UNTIL > now:
                        blocked = {"kind": "app-registration"}
                    else:
                        blocked = _spotify_guard_is_blocked(method_name, policy, now)
                    entry = _SPOTIFY_GUARD_CACHE.get(cache_key)
                    value = _spotify_copy(cached["value"])
                    if blocked is None and isinstance(entry, dict) and not entry.get("refreshing"):
                        entry["refreshing"] = True
                        _spotify_guard_consume_budget(method_name, policy, now)
                        logs.append((method_name, "stale", "refreshing", None))
                        return "stale-refresh", value, cache_key, policy
                    logs.append((method_name, "stale", (blocked or {}).get("kind") or "refresh-in-flight", None))
                    return "stale", value, cache_key, policy

            if _SPOTIFY_APP_REG_BLOCK_UNTIL > now:
                retry_after = max(0, int((_SPOTIFY_APP_REG_BLOCK_UNTIL - now) + 0.999))
                logs.append((method_name, "blocked", f"app-registration {retry_after}s", None))
                raise SpotifyAppRegistrationBlocked(
                    _SPOTIFY_APP_REG_BLOCK_MSG or "Spotify says this user is not registered for the current application.",
                    retry_after=retry_after,
                )
            blocked = _spotify_guard_is_blocked(method_name, policy, now)
            if blocked:
                logs.append((method_name, "blocked", f"{blocked['kind']} {blocked['retry_after']}s", None))
                raise SpotifyGuardBlocked(
                    blocked["message"],
                    retry_after=blocked["retry_after"],
                    scope=blocked["scope"],
                    kind=blocked["kind"],
                )
            _spotify_guard_consume_budget(method_name, policy, now)
        return None, None, cache_key, policy
    finally:
        _spotify_guard_emit_logs(logs)


def _spotify_guard_note_rate_limit(method_name, retry_after_s):
    global _SPOTIFY_GUARD_GLOBAL_UNTIL, _SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S, _SPOTIFY_GUARD_GLOBAL_REASON
    global _SPOTIFY_GUARD_PROTECT_MODE_UNTIL, _SPOTIFY_GUARD_PROTECT_REASON
    policy = _spotify_guard_policy(method_name)
    now = time.monotonic()
    until = now + max(0, retry_after_s)
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_GUARD_GLOBAL_UNTIL = max(_SPOTIFY_GUARD_GLOBAL_UNTIL, until)
        _SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S = retry_after_s
        _SPOTIFY_GUARD_GLOBAL_REASON = method_name
        _SPOTIFY_GUARD_CATEGORY_LOCKS[policy["category"]] = max(
            _SPOTIFY_GUARD_CATEGORY_LOCKS.get(policy["category"], 0.0), until
        )
        _SPOTIFY_GUARD_METHOD_LOCKS[method_name] = max(
            _SPOTIFY_GUARD_METHOD_LOCKS.get(method_name, 0.0), until
        )
        if not policy.get("essential", False):
            protect_until = now + max(300, retry_after_s)
            _SPOTIFY_GUARD_PROTECT_MODE_UNTIL = max(_SPOTIFY_GUARD_PROTECT_MODE_UNTIL, protect_until)
            _SPOTIFY_GUARD_PROTECT_REASON = method_name
    if policy["category"] == "search" or method_name == "search":
        global _sp_search_backoff_until, _sp_search_retry_after_s
        _sp_search_retry_after_s = retry_after_s
        _sp_search_backoff_until = max(_sp_search_backoff_until, now + max(0, retry_after_s))
    # Persist guard expiry so it survives a server restart
    _persist_guard_state()

def _record_spotify_429_diagnostic(method_name, retry_after_s, exc):
    try:
        ctx = _spotify_call_context()
        recent = _spotify_guard_snapshot(limit=25)
        lines = [
            f"method={method_name}",
            f"retry_after_s={retry_after_s}",
            f"thread={ctx.get('thread')}",
            f"track={ctx.get('track_name') or '-'}",
            f"artists={ctx.get('artists') or '-'}",
            f"playlist_id={ctx.get('playlist_id') or '-'}",
            f"listened_s={ctx.get('listened_s')}",
            "caller_stack:",
        ]
        for item in (ctx.get("stack") or []):
            lines.append(f"  - {item}")
        lines.append("recent_spotify_calls:")
        for item in recent:
            lines.append(
                f"  #{item.get('seq')} {item.get('method')} [{item.get('category')}] "
                f"{item.get('outcome')} detail={item.get('detail') or '-'} "
                f"elapsed_ms={item.get('elapsed_ms', '-')}"
            )
        _append_dev_event(
            "Spotify429",
            f"Spotify 429 on {method_name} (Retry-After {retry_after_s}s): {exc}",
            context=f"spotify_429_{method_name}",
            tb="\n".join(lines),
        )
    except Exception:
        pass


def _persist_guard_state():
    """Write guard expiry timestamps to app_meta as wall-clock Unix times."""
    try:
        if not _SQLITE_ENABLED:
            return
        import time as _t
        now_wall = _t.time()
        now_mono = _t.monotonic()
        with _SPOTIFY_GUARD_LOCK:
            g_mono = _SPOTIFY_GUARD_GLOBAL_UNTIL
            p_mono = _SPOTIFY_GUARD_PROTECT_MODE_UNTIL
            g_reason = _SPOTIFY_GUARD_GLOBAL_REASON or ""
            p_reason = _SPOTIFY_GUARD_PROTECT_REASON or ""
            g_retry = _SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S or 0
        g_wall = now_wall + (g_mono - now_mono)
        p_wall = now_wall + (p_mono - now_mono)
        conn = _db.get_conn()
        conn.execute("INSERT OR REPLACE INTO app_meta (key,value) VALUES (?,?)",
                     ("spotify_guard_global_until_wall", str(g_wall)))
        conn.execute("INSERT OR REPLACE INTO app_meta (key,value) VALUES (?,?)",
                     ("spotify_guard_protect_until_wall", str(p_wall)))
        conn.execute("INSERT OR REPLACE INTO app_meta (key,value) VALUES (?,?)",
                     ("spotify_guard_global_reason", g_reason))
        conn.execute("INSERT OR REPLACE INTO app_meta (key,value) VALUES (?,?)",
                     ("spotify_guard_global_retry_after_s", str(g_retry)))
        conn.execute("INSERT OR REPLACE INTO app_meta (key,value) VALUES (?,?)",
                     ("spotify_guard_protect_reason", p_reason))
        conn.commit()
    except Exception:
        pass


def _restore_guard_state():
    """On startup, mark any saved guard windows as restored/unverified.

    We no longer blindly hard-restore long lockouts into active guard state.
    Instead, we surface them in the UI and revalidate once with a lightweight
    Spotify call.
    """
    global _SPOTIFY_GUARD_RESTORED_UNTIL_WALL, _SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL
    global _SPOTIFY_GUARD_RESTORED_REASON, _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S
    global _SPOTIFY_GUARD_RESTORED_PENDING
    try:
        if not _SQLITE_ENABLED:
            return
        import time as _t
        now_wall = _t.time()
        conn = _db.get_conn()
        row_g = conn.execute("SELECT value FROM app_meta WHERE key=?", ("spotify_guard_global_until_wall",)).fetchone()
        row_p = conn.execute("SELECT value FROM app_meta WHERE key=?", ("spotify_guard_protect_until_wall",)).fetchone()
        row_r = conn.execute("SELECT value FROM app_meta WHERE key=?", ("spotify_guard_global_reason",)).fetchone()
        row_ra = conn.execute("SELECT value FROM app_meta WHERE key=?", ("spotify_guard_global_retry_after_s",)).fetchone()
        g_wall = float(row_g["value"]) if row_g and row_g["value"] else 0.0
        p_wall = float(row_p["value"]) if row_p and row_p["value"] else 0.0
        if g_wall > now_wall + 5 or p_wall > now_wall + 5:
            with _SPOTIFY_GUARD_LOCK:
                _SPOTIFY_GUARD_RESTORED_UNTIL_WALL = g_wall
                _SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL = p_wall
                _SPOTIFY_GUARD_RESTORED_REASON = (row_r["value"] if row_r and row_r["value"] else None)
                try:
                    _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S = int(float(row_ra["value"])) if row_ra and row_ra["value"] else None
                except Exception:
                    _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S = None
                _SPOTIFY_GUARD_RESTORED_PENDING = True
    except Exception:
        pass


def _clear_restored_guard_markers():
    """The lockout restored from the last session is over (a live call succeeded): drop the
    markers and, unless a new live lock exists, the persisted copy. Live locks are kept."""
    global _SPOTIFY_GUARD_RESTORED_UNTIL_WALL, _SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL
    global _SPOTIFY_GUARD_RESTORED_REASON, _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S
    global _SPOTIFY_GUARD_RESTORED_PENDING
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_GUARD_RESTORED_UNTIL_WALL = 0.0
        _SPOTIFY_GUARD_RESTORED_PROTECT_UNTIL_WALL = 0.0
        _SPOTIFY_GUARD_RESTORED_REASON = None
        _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S = None
        _SPOTIFY_GUARD_RESTORED_PENDING = False
        now = time.monotonic()
        live_lock = _SPOTIFY_GUARD_GLOBAL_UNTIL > now or _SPOTIFY_GUARD_PROTECT_MODE_UNTIL > now
    if not live_lock and _SQLITE_ENABLED:
        try:
            conn = _db.get_conn()
            conn.execute("DELETE FROM app_meta WHERE key IN (?,?,?,?,?)", (
                "spotify_guard_global_until_wall",
                "spotify_guard_protect_until_wall",
                "spotify_guard_global_reason",
                "spotify_guard_global_retry_after_s",
                "spotify_guard_protect_reason",
            ))
            conn.commit()
        except Exception:
            pass


def _spotify_note_live_success():
    """Called after every successful live call: a restored (unverified) lockout from the
    previous session is evidently over once Spotify answers."""
    if _SPOTIFY_GUARD_RESTORED_PENDING:
        _clear_restored_guard_markers()


def _revalidate_restored_guard_state():
    global _SPOTIFY_GUARD_REVALIDATING, _SPOTIFY_GUARD_RESTORED_PENDING
    with _SPOTIFY_GUARD_LOCK:
        if not _SPOTIFY_GUARD_RESTORED_PENDING or _SPOTIFY_GUARD_REVALIDATING:
            return
        _SPOTIFY_GUARD_REVALIDATING = True

    def _worker():
        global _SPOTIFY_GUARD_REVALIDATING, _SPOTIFY_GUARD_RESTORED_PENDING
        try:
            sp = get_sp()
            if not sp:
                # Not signed in yet: get_sp() re-triggers this once a token exists.
                return
            try:
                sp.current_user()
                _clear_restored_guard_markers()
            except SpotifyGuardBlocked:
                # A new live guard took over; drop stale-restored marker.
                with _SPOTIFY_GUARD_LOCK:
                    _SPOTIFY_GUARD_RESTORED_PENDING = False
            except spotipy.SpotifyException as e:
                if e.http_status == 429:
                    retry_after = spotify_retry_after_from(e, default=30)
                    _spotify_guard_note_rate_limit("current_user", retry_after)
                    with _SPOTIFY_GUARD_LOCK:
                        _SPOTIFY_GUARD_RESTORED_PENDING = False
                else:
                    # Non-rate-limit failures shouldn't trap the app in stale state forever.
                    with _SPOTIFY_GUARD_LOCK:
                        _SPOTIFY_GUARD_RESTORED_PENDING = False
            except Exception:
                with _SPOTIFY_GUARD_LOCK:
                    _SPOTIFY_GUARD_RESTORED_PENDING = False
        finally:
            with _SPOTIFY_GUARD_LOCK:
                _SPOTIFY_GUARD_REVALIDATING = False

    threading.Thread(target=_worker, daemon=True).start()


SPOTIFY_SEARCH_MAX_LIMIT = 10  # Development Mode apps created after 2026-02-11: 400 "Invalid limit" above


def _spotify_clamp_search_limit(args, kwargs):
    """search(q, limit=...) with limit capped at SPOTIFY_SEARCH_MAX_LIMIT (keyword or positional)."""
    try:
        if "limit" in kwargs:
            if kwargs["limit"] is not None and int(kwargs["limit"]) > SPOTIFY_SEARCH_MAX_LIMIT:
                kwargs = dict(kwargs, limit=SPOTIFY_SEARCH_MAX_LIMIT)
        elif len(args) > 1 and args[1] is not None and int(args[1]) > SPOTIFY_SEARCH_MAX_LIMIT:
            args = (args[0], SPOTIFY_SEARCH_MAX_LIMIT) + tuple(args[2:])
    except (TypeError, ValueError):
        pass
    return args, kwargs


class GuardedSpotify:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):
            if name == "search":
                args, kwargs = _spotify_clamp_search_limit(args, kwargs)
            cache_state, cached_value, cache_key, policy = _spotify_guard_preflight(name, args, kwargs)
            if cache_state == "fresh":
                return cached_value
            if cache_state in ("stale", "stale-refresh"):
                if cache_state == "stale-refresh":
                    _spotify_trigger_background_refresh(self._client, name, attr, args, kwargs, cache_key, policy)
                return cached_value

            token = None
            is_owner = True
            if policy.get("single_flight", False) and cache_key:
                token, is_owner = _spotify_inflight_start(cache_key)
                if not is_owner:
                    _spotify_guard_release_budget(name)  # we piggyback on the owner's call
                    token["event"].wait(timeout=20)
                    if token.get("error") is not None:
                        raise token["error"]
                    if token.get("result") is not None:
                        return _spotify_copy(token["result"])
                    with _SPOTIFY_GUARD_LOCK:
                        cached = _spotify_cache_get(cache_key, allow_stale=True)
                        cached_value = _spotify_copy(cached.get("value")) if cached else None
                    if cached:
                        return cached_value
                    raise SpotifyGuardBlocked(
                        f"Spotify request for {name} was already in flight and did not complete cleanly.",
                        retry_after=5,
                        scope=policy["category"],
                        kind="inflight",
                    )

            lane = policy.get("lane") or "interactive"
            throttle_and_call_lock = _SPOTIFY_WRITE_LOCK if lane == "write" else nullcontext()
            started = time.monotonic()
            gen = _SPOTIFY_CACHE_GEN
            try:
                with throttle_and_call_lock:
                    _spotify_throttle(lane)
                    result = attr(*args, **kwargs)
                cache_ttl_s = int(policy.get("cache_ttl_s") or 0)
                stale_ttl_s = int(policy.get("stale_ttl_s") or cache_ttl_s or 0)
                if cache_key and cache_ttl_s > 0:
                    with _SPOTIFY_GUARD_LOCK:
                        if gen == _SPOTIFY_CACHE_GEN:  # no write invalidated the cache meanwhile
                            _spotify_cache_store(cache_key, result, cache_ttl_s, stale_ttl_s)
                _spotify_guard_log(name, "ok", elapsed_ms=int((time.monotonic() - started) * 1000))
                _spotify_note_live_success()
                _spotify_inflight_finish(cache_key, token, result=result)
                return result
            except spotipy.SpotifyException as e:
                _spotify_inflight_finish(cache_key, token, error=e)
                if e.http_status == 429 and not is_local_guard_block(e):
                    retry_after = spotify_retry_after_from(e, default=30)
                    _spotify_guard_note_rate_limit(name, retry_after)
                    _spotify_guard_log(
                        name,
                        "429",
                        f"retry-after {retry_after}s",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        http_status=429,
                        retry_after_s=retry_after,
                    )
                    _record_spotify_429_diagnostic(name, retry_after, e)
                elif is_spotify_app_registration_error(e):
                    _note_spotify_app_registration_error(name, e)
                    _spotify_guard_log(
                        name,
                        "403",
                        "user not registered for this application",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        http_status=403,
                    )
                else:
                    _spotify_guard_log(
                        name,
                        f"http-{e.http_status}",
                        str(e),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        http_status=e.http_status,
                    )
                raise
            except Exception as e:
                _spotify_inflight_finish(cache_key, token, error=e)
                _spotify_guard_log(
                    name,
                    "error",
                    str(e),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            finally:
                if token is not None and token.get("event") and not token["event"].is_set():
                    _spotify_inflight_finish(cache_key, token, error=RuntimeError(f"Spotify {name} call aborted"))
                # Whatever the outcome (a timeout may still have reached Spotify), cached
                # reads of what this write touched are no longer trustworthy.
                _spotify_invalidate_after_write(name, args, kwargs)

        return guarded


# One HTTP session and one client per access token: get_sp() runs on every poll and used to
# build a fresh requests.Session (and connection pool) each time.
_SP_CLIENT_LOCK = threading.Lock()
_sp_client_cache = {"access_token": None, "client": None}
_sp_http_session = None


def get_sp():
    """Return an authenticated Spotify client, or None if no valid token."""
    global _sp_http_session
    token = get_cached_token()
    if not token:
        return None
    access_token = token.get("access_token")
    with _SP_CLIENT_LOCK:
        client = _sp_client_cache.get("client")
        if client is None or _sp_client_cache.get("access_token") != access_token:
            if _sp_http_session is None:
                _sp_http_session = _make_sane_session()
            client = GuardedSpotify(spotipy.Spotify(
                auth=access_token,
                requests_timeout=15,
                requests_session=_sp_http_session,
            ))
            _sp_client_cache.update(access_token=access_token, client=client)
    # A lockout restored from the last session is re-checked as soon as we can sign in
    # (at startup there may have been no token yet).
    if _SPOTIFY_GUARD_RESTORED_PENDING and not _SPOTIFY_GUARD_REVALIDATING:
        _revalidate_restored_guard_state()
    return client

# ---------- Stats / counters ----------
def bump_stat(key, n=1):
    """Increment (or, with n<0 for an undo, decrement) an in-app counter and its today
    variant, persisting to config. Streaks are derived from SQLite events, not stored here."""
    today = datetime.now().strftime("%Y-%m-%d")
    with state_lock:
        s = state["stats"]
        if s.get("day") != today:
            s["day"] = today
            for k in ("removed_today", "liked_today", "added_today"):
                s[k] = 0
        if key in s:
            s[key] = max(0, int(s.get(key, 0)) + n)
        today_key = key.replace("_total", "_today")
        if today_key != key and today_key in s:
            s[today_key] = max(0, int(s.get(today_key, 0)) + n)
        s.pop("removal_dates", None)
        snapshot = dict(s)
    _backlog_update_config(lambda cfg: cfg.__setitem__("stats", snapshot))


def _backlog_update_config(mutator):
    """Persist through the locked, atomic update_config; plain load/save only as a fallback."""
    fn = globals().get("update_config")
    if callable(fn):
        fn(mutator)
        return
    cfg = load_config()
    if isinstance(cfg, dict):
        mutator(cfg)
        save_config(cfg)

def save_favorites():
    with state_lock:
        favs = sorted(state["favorite_artists"])
        opt_outs = sorted(state.get("favorite_artist_opt_outs") or [])
    update_config(lambda cfg: cfg.update(favorite_artists=favs, favorite_artist_opt_outs=opt_outs))

def current_day_str():
    return datetime.now().strftime("%Y-%m-%d")

def current_ts_ms():
    return int(time.time() * 1000)

def normalize_genres(genres, limit=8):
    out = []
    seen = set()
    for raw in genres or []:
        value = str(raw or "").strip().lower()
        if not value or value in seen:
            continue
        out.append(value)
        seen.add(value)
        if len(out) >= limit:
            break
    return out


def _split_artist_names(raw_artists):
    if not raw_artists:
        return []
    if isinstance(raw_artists, list):
        vals = raw_artists
    else:
        vals = str(raw_artists).split(",")
    out = []
    seen = set()
    for val in vals:
        name = str(val or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        out.append(name)
        seen.add(key)
    return out


def _artist_name_matches(raw_artists, target_name):
    target = (target_name or "").strip().lower()
    if not target:
        return False
    return target in {name.lower() for name in _split_artist_names(raw_artists)}


def _artist_stats_from_sqlite(conn, artist_id, artist_name, profile=None):
    """Legacy ``listen_stats`` shape, now a thin view of artist_stats.artist_profile.

    Counts come from the canonical plays ledger keyed by the normalized artist name, so live
    plays already covered by a scrobble are not counted twice and comma-named bands ('Woe, Is
    Me') match whole. Returns None without a name or while the ledger is not built (callers
    fall back to the in-memory history). artist_id is kept for compatibility: plays carry no
    Spotify artist ids.
    """
    from src.services import artist_stats as _artist_stats_svc

    if profile is None:
        target_name = (artist_name or "").strip()
        if not target_name or conn is None:
            return None
        try:
            profile = _artist_stats_svc.artist_profile(conn, target_name)
        except _artist_stats_svc.LedgerNotReady:
            return None
    return _artist_stats_svc.legacy_from_profile(profile)


def compact_track_for_history(track):
    if not track:
        return {}
    # artist_list keeps the credits as a list: the joined "artists" string cannot be split
    # back safely ('Woe, Is Me', 'Tyler, The Creator').
    artist_list = [{"id": a.get("id"), "name": a.get("name")}
                   for a in (track.get("artist_list") or [])
                   if isinstance(a, dict) and a.get("name")]
    return {
        "id": track.get("id"),
        "name": track.get("name") or "",
        "artists": track.get("artists") or "",
        "artist_list": artist_list,
        "album": track.get("album") or "",
        "album_id": track.get("album_id"),
        "image": track.get("image"),
        "url": track.get("url"),
        "duration_ms": track.get("duration_ms") or None,
        "genres": normalize_genres(track.get("genres"), limit=8),
    }

def save_analytics_state():
    with state_lock:
        history = list(state["event_history"])
        backlog_history = list(state["backlog_history"])
        liked_library_profile = state.get("liked_library_profile")
        liked_library_profile_fetched_at = float(state.get("liked_library_profile_fetched_at") or 0)

    def _apply(cfg):
        if history:
            cfg["history"] = history[-MAX_EVENT_HISTORY:]
        else:
            cfg.pop("history", None)
        if backlog_history:
            cfg["backlog_history"] = backlog_history[-MAX_BACKLOG_HISTORY:]
        else:
            cfg.pop("backlog_history", None)
        if liked_library_profile:
            cfg["liked_library_profile"] = {
                "data": liked_library_profile,
                "fetched_at": liked_library_profile_fetched_at,
            }
        else:
            cfg.pop("liked_library_profile", None)

    update_config(_apply)

def record_history_event(event_type, track=None, listened_s=None, reason=None, source=None,
                         target=None, extra=None):
    """Append an event to the in-memory/config history and persist it to SQLite.

    extra passes play-accounting fields straight through (started_ms, started_in_backlog,
    was_in_playlist, auto_removed, duration_ms); the SQLite payload keeps them.
    """
    payload = compact_track_for_history(track)
    artist_list = payload.get("artist_list") or []
    event = {
        "type": event_type,
        "ts_ms": current_ts_ms(),
        "day": current_day_str(),
        "track_id": payload.get("id"),
        "track_name": payload.get("name", ""),
        "artists": payload.get("artists", ""),
        "artist_ids": [a["id"] for a in artist_list if a.get("id")],
        "artist_names": [a["name"] for a in artist_list],
        "album": payload.get("album", ""),
        "album_id": payload.get("album_id"),
        "duration_ms": payload.get("duration_ms"),
        "image": payload.get("image"),
        "url": payload.get("url"),
        "genres": payload.get("genres", []),
        "listened_s": round(float(listened_s), 1) if listened_s is not None else None,
        "reason": reason,
        "source": source,
        "target": target,
    }
    if extra:
        # A None extra never erases what the track itself knew (e.g. duration_ms).
        event.update({k: v for k, v in extra.items() if v is not None or event.get(k) is None})
    with state_lock:
        history = state["event_history"]
        history.append(event)
        if len(history) > MAX_EVENT_HISTORY:
            del history[:-MAX_EVENT_HISTORY]
    save_analytics_state()
    # Dual-write to SQLite (best-effort; never blocks the caller). A SONG_PLAYED also lands
    # in the plays ledger here (events.write_event -> plays.ingest_event).
    if _SQLITE_ENABLED:
        try:
            _events.write_from_legacy(event)
        except Exception as exc:
            _log_app_error(exc, "record_history_event: SQLite write")
    return event

def snapshot_backlog(total, force=False, playlist_id=None):
    """Record today's size of one playlist's backlog: one entry per (day, playlist).

    Written by the backlog index on every commit and applied removal. Entries carry
    playlist_id so switching playlists never overwrites another playlist's day.
    """
    if total is None:
        return
    total = int(total)
    changed = False
    with state_lock:
        pid = playlist_id or state["playlist_id"]
        entry = {
            "day": current_day_str(),
            "ts_ms": current_ts_ms(),
            "total": total,
            "playlist_id": pid,
        }
        history = state["backlog_history"]
        slot = None
        for i in range(len(history) - 1, -1, -1):
            h = history[i]
            if h.get("day") != entry["day"]:
                break
            # Pre-v2 entries have no playlist_id; they were written for the startup playlist.
            if h.get("playlist_id") == pid or (h.get("playlist_id") is None and pid == _initial_playlist_id):
                slot = i
                break
        if slot is not None:
            prev = history[slot]
            if force or prev.get("total") != total or prev.get("playlist_id") != pid:
                history[slot] = entry
                changed = True
        else:
            history.append(entry)
            if len(history) > MAX_BACKLOG_HISTORY:
                del history[:-MAX_BACKLOG_HISTORY]
            changed = True
    if changed:
        save_analytics_state()

def note_recent_removal(track_info, listened_s, reason, source, extra=None):
    """Log one backlog removal: the legacy recently_removed list plus a 'removed' history event
    (dual-written to SQLite as TRACK_REMOVED_FROM_QUEUE). The clearing engine calls this exactly
    once per successful clear, so there is no per-day dedupe (it hid re-clears after an undo).
    Returns True when logged."""
    listened_value = None if listened_s in (None, "—") else round(float(listened_s), 1)
    with state_lock:
        state["recently_removed"].appendleft({
            "track": track_info,
            "listened": listened_value if listened_value is not None else "—",
            "reason": reason,
            "ts": datetime.now().strftime("%H:%M:%S"),
        })
        playlist_total = state["playlist_total"]
    payload_extra = {"playlist_total": playlist_total}
    if extra:
        payload_extra.update(extra)
    record_history_event(
        "removed",
        track=track_info,
        listened_s=listened_value,
        reason=reason,
        source=source,
        extra=payload_extra,
    )
    return True

def is_favorite_artist(name):
    if not name:
        return False
    return name.strip().lower() in state["favorite_artists"]

def is_favorite_artist_opted_out(name):
    if not name:
        return False
    return name.strip().lower() in state.get("favorite_artist_opt_outs", set())

def add_favorite_artist(name, source="manual"):
    key = (name or "").strip().lower()
    if not key:
        return False
    changed = False
    with state_lock:
        if key not in state["favorite_artists"]:
            state["favorite_artists"].add(key)
            changed = True
        state.get("favorite_artist_opt_outs", set()).discard(key)
    if changed or source == "manual":
        save_favorites()
    return changed

def remove_favorite_artist(name, opt_out=False):
    key = (name or "").strip().lower()
    if not key:
        return False
    changed = False
    with state_lock:
        if key in state["favorite_artists"]:
            state["favorite_artists"].discard(key)
            changed = True
        if opt_out:
            state.get("favorite_artist_opt_outs", set()).add(key)
    save_favorites()
    return changed

def current_auto_artist_like_threshold() -> int:
    try:
        return max(0, int(os.environ.get("AUTO_ARTIST_LIKE_THRESHOLD", str(AUTO_ARTIST_LIKE_THRESHOLD)) or AUTO_ARTIST_LIKE_THRESHOLD))
    except Exception:
        return max(0, int(AUTO_ARTIST_LIKE_THRESHOLD))

def current_discover_weekly_inflow() -> int:
    try:
        return max(0, int(os.environ.get("DISCOVER_WEEKLY_INFLOW", str(DISCOVER_WEEKLY_INFLOW)) or DISCOVER_WEEKLY_INFLOW))
    except Exception:
        return max(0, int(DISCOVER_WEEKLY_INFLOW))


def _credited_artist_names(track):
    """Artist names of a track/history entry; the credit list when present, because the joined
    'artists' string cannot be split back safely ('Woe, Is Me', 'Tyler, The Creator')."""
    names = [str(a.get("name")).strip() for a in (track.get("artist_list") or [])
             if isinstance(a, dict) and str(a.get("name") or "").strip()]
    return names or _split_artist_names(track.get("artists") or "")


def _cached_liked_artist_counts(sp):
    """Liked-track counts per artist without paging the whole library inside a request: a cold
    or expired cache is refreshed in the background and the cached copy (maybe empty) used now."""
    with _LIKED_ARTIST_COUNT_LOCK:
        counts = dict(_LIKED_ARTIST_COUNT_CACHE.get("counts") or {})
        fetched_at = float(_LIKED_ARTIST_COUNT_CACHE.get("fetched_at") or 0)
    if sp and (not counts or time.time() - fetched_at >= 900):
        _warm_liked_artist_counts_async(sp)
    return counts


def note_liked_track_in_artist_counts(track):
    """Count a newly liked track into a warm liked-artist cache instead of discarding the cache
    (a rebuild pages the entire Liked Songs library through Spotify)."""
    with _LIKED_ARTIST_COUNT_LOCK:
        counts = _LIKED_ARTIST_COUNT_CACHE.get("counts")
        if not counts:
            return  # cold cache: the next full count includes this like
        names = _LIKED_ARTIST_COUNT_CACHE.setdefault("names", {})
        for name in _credited_artist_names(track or {}):
            counts[name.lower()] = int(counts.get(name.lower(), 0)) + 1
            names.setdefault(name.lower(), name)


def _check_auto_artist_like(sp, track) -> list:
    """
    Called after a track is liked.  Checks each credited artist; if their liked-track count
    has reached AUTO_ARTIST_LIKE_THRESHOLD, upserts them into artist_likes and returns the
    list of newly-liked names.
    """
    if not _SQLITE_ENABLED:
        return []
    threshold = current_auto_artist_like_threshold()
    if threshold <= 0:
        return []
    names = _credited_artist_names(track or {})
    if not names:
        return []
    with state_lock:
        history = list(state["event_history"])
    try:
        spotify_counts = _cached_liked_artist_counts(sp)
    except Exception:
        spotify_counts = {}
    newly = []
    for name in names:
        try:
            if is_favorite_artist_opted_out(name):
                continue
            if _events.is_artist_liked(name):
                add_favorite_artist(name, source="auto")
                continue
            key = name.lower()
            history_count = sum(
                1 for e in history
                if e.get("type") == "liked"
                and key in {n.lower() for n in _credited_artist_names(e)}
            )
            spotify_count = int(spotify_counts.get(name.lower(), 0))
            count = max(history_count, spotify_count)
            if count >= threshold:
                _events.upsert_artist_like(
                    artist_id=name,
                    artist_name=name,
                    auto_liked=True,
                    track_count=count,
                )
                add_favorite_artist(name, source="auto")
                newly.append(name)
        except Exception:
            pass
    return newly

# ---------- Last.fm client ----------
_lastfm_user_top_artists = {"data": None, "fetched_at": 0}
_lfm_cache = {}  # key -> (stored_at, value[, expires_at])
_lfm_cache_lock = threading.Lock()
_LFM_CACHE_MAX = 5000
# Spotify catalog metadata only; heard flags are recomputed from the plays ledger per request.
_artist_discography_cache = {}  # artist_id -> {"ts": float, "albums": list}
_artist_discography_lock = threading.Lock()
_artist_release_track_cache = {}  # album_id -> {"ts": float, "album": dict}
_artist_release_track_lock = threading.Lock()


class _LastfmNoData:
    """Falsy, non-dict result of lastfm_call(), so legacy `if not d` checks keep working."""
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __bool__(self):
        return False

    def __repr__(self):
        return f"<Last.fm {self.name}>"


# Failures (no key, network error, non-200, error body, rate limit) are never cached for
# the long TTLs below: they used to be stored as "no tags / no similar artists" for up to
# 7 days. A genuine "not found" (Last.fm error 6) is cached, as {"missing": True}.
_LFM_FAIL = _LastfmNoData("failure")
_LFM_MISSING = _LastfmNoData("not found")
_lfm_last_error = {"message": None, "code": None, "ts": 0.0}
# Enrichment calls (not lastfm_call itself, which importers use) back off briefly after
# repeated failures, so a Last.fm outage doesn't cost 8 s per artist on every page view.
_lfm_breaker = {"failures": 0, "open_until": 0.0}
_lfm_breaker_lock = threading.Lock()


def _lfm_cache_get(key, ttl_s):
    now = time.time()
    with _lfm_cache_lock:
        v = _lfm_cache.get(key)
        if v and (now - v[0]) < ttl_s and (len(v) < 3 or v[2] is None or now < v[2]):
            return v[1]
    return None

def _lfm_cache_set(key, value, ttl_s=None):
    now = time.time()
    with _lfm_cache_lock:
        _lfm_cache[key] = (now, value, (now + ttl_s) if ttl_s else None)
        if len(_lfm_cache) > _LFM_CACHE_MAX:
            oldest = sorted(_lfm_cache.items(), key=lambda kv: kv[1][0])[: len(_lfm_cache) // 4]
            for k, _ in oldest:
                _lfm_cache.pop(k, None)


def _lfm_is_missing(value):
    return isinstance(value, dict) and value.get("missing") is True


def lastfm_reset_caches():
    """Forget every cached Last.fm answer (after the API key or username changed)."""
    with _lfm_cache_lock:
        _lfm_cache.clear()
    _lastfm_user_top_artists.update(data=None, fetched_at=0)
    with _lfm_breaker_lock:
        _lfm_breaker.update(failures=0, open_until=0.0)


def lastfm_call(method, **params):
    """GET a Last.fm API method.

    Returns the JSON dict on success, _LFM_MISSING when Last.fm says the artist/track does
    not exist (error 6), and _LFM_FAIL for anything else (not configured, network error,
    non-200, error body). Both sentinels are falsy and not dicts.
    """
    api_key = lastfm_api_key()
    if not api_key:
        return _LFM_FAIL
    p = {"method": method, "api_key": api_key, "format": "json", **params}
    try:
        r = requests.get(LASTFM_BASE, params=p, timeout=8,
                         headers={"User-Agent": LRCLIB_UA})
    except Exception as e:
        _lfm_last_error.update(message=f"{method}: {e}", code=None, ts=time.time())
        return _LFM_FAIL
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("error") is not None:
        try:
            code = int(data.get("error"))
        except (TypeError, ValueError):
            code = None
        if code == 6:
            return _LFM_MISSING
        _lfm_last_error.update(message=f"{method}: Last.fm error {code}: {data.get('message')}",
                               code=code, ts=time.time())
        return _LFM_FAIL
    if r.status_code != 200 or not isinstance(data, dict):
        _lfm_last_error.update(message=f"{method}: HTTP {r.status_code}", code=r.status_code, ts=time.time())
        return _LFM_FAIL
    return data


def _lfm_fetch(method, **params):
    """lastfm_call() for enrichment wrappers, behind a small circuit breaker: after 5
    consecutive failures (or a Last.fm rate-limit error) calls fail fast for a while."""
    now = time.time()
    with _lfm_breaker_lock:
        if _lfm_breaker["open_until"] > now:
            return _LFM_FAIL
    d = lastfm_call(method, **params)
    with _lfm_breaker_lock:
        if d is _LFM_FAIL and lastfm_api_key():
            _lfm_breaker["failures"] += 1
            if _lfm_last_error.get("code") == 29:  # Last.fm "rate limit exceeded"
                _lfm_breaker["open_until"] = now + 300
            elif _lfm_breaker["failures"] >= 5:
                _lfm_breaker["open_until"] = now + 60
                _lfm_breaker["failures"] = 0
        elif d is not _LFM_FAIL:
            _lfm_breaker["failures"] = 0
    return d


def _lfm_cached_list(key, ttl_s, method, parse, **params):
    """Shared cache policy for list-returning wrappers: success (even an empty list) and a
    genuine not-found are cached for ttl_s; failures are not cached at all. Not-found is
    stored as [] here (other code reads these keys and expects a list)."""
    cached = _lfm_cache_get(key, ttl_s=ttl_s)
    if cached is not None:
        return [] if _lfm_is_missing(cached) else cached
    d = _lfm_fetch(method, **params)
    if d is _LFM_MISSING:
        _lfm_cache_set(key, [])
        return []
    if not isinstance(d, dict):
        return []
    try:
        out = parse(d)
    except Exception as e:
        _log_app_error(e, context=f"Last.fm {method} parse")
        return []
    _lfm_cache_set(key, out)
    return out

def _strip_lfm_bio(raw):
    """Last.fm bio HTML -> plain text (rendered as text by the UI, never as HTML)."""
    if not raw:
        return ""
    # Strip the "Read more on Last.fm" trailing link Last.fm always appends
    s = re.sub(r'<a href="[^"]*last\.fm[^"]*"[^>]*>.*?</a>', "", raw, flags=re.DOTALL)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()

def lastfm_artist_info(artist_name):
    if not artist_name:
        return None
    key = f"artistinfo:{artist_name.lower()}"
    cached = _lfm_cache_get(key, ttl_s=24 * 3600)
    if cached is not None:
        return None if _lfm_is_missing(cached) else cached
    d = _lfm_fetch("artist.getInfo", artist=artist_name, autocorrect=1)
    if d is _LFM_MISSING:
        _lfm_cache_set(key, {"missing": True})
        return None
    if not isinstance(d, dict) or not isinstance(d.get("artist"), dict):
        return None  # failure: not cached, so the next view retries
    a = d["artist"]
    bio = a.get("bio") or {}
    bio_summary = _strip_lfm_bio(bio.get("summary"))
    tags = [t["name"] for t in ((a.get("tags") or {}).get("tag") or []) if isinstance(t, dict) and t.get("name")]
    stats = a.get("stats") or {}
    result = {
        "name": a.get("name"),
        "bio": bio_summary,
        "bio_full": _strip_lfm_bio(bio.get("content")) or bio_summary,
        "tags": tags[:6],
        "listeners": int(stats.get("listeners", 0) or 0),
        "playcount": int(stats.get("playcount", 0) or 0),
        "url": a.get("url"),
    }
    _lfm_cache_set(key, result)
    return result

def _lfm_items(d, outer, inner):
    items = (d.get(outer) or {}).get(inner) or []
    if isinstance(items, dict):  # Last.fm returns a bare object when there is one item
        items = [items]
    return [i for i in items if isinstance(i, dict)]


def _lfm_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _lfm_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def lastfm_artist_top_tags(artist_name, limit=6):
    if not artist_name:
        return []

    def parse(d):
        tags = []
        for t in _lfm_items(d, "toptags", "tag"):
            if t.get("name"):
                tags.append(t["name"])
            if len(tags) >= limit:
                break
        return tags

    return _lfm_cached_list(f"artist_top_tags:{artist_name.lower()}:{limit}", 7 * 24 * 3600,
                            "artist.getTopTags", parse, artist=artist_name, autocorrect=1)

def lastfm_artist_top_tracks(artist_name, limit=8):
    if not artist_name:
        return []

    def parse(d):
        tracks = []
        for t in _lfm_items(d, "toptracks", "track"):
            name = t.get("name")
            if not name:
                continue
            tracks.append({
                "name": name,
                "artist": (t.get("artist") or {}).get("name") or artist_name,
                "playcount": _lfm_int(t.get("playcount")),
                "listeners": _lfm_int(t.get("listeners")),
                "lastfm_url": t.get("url"),
            })
            if len(tracks) >= limit:
                break
        return tracks

    return _lfm_cached_list(f"artist_top_tracks:{artist_name.lower()}:{limit}", 24 * 3600,
                            "artist.getTopTracks", parse, artist=artist_name, autocorrect=1, limit=limit)

def lastfm_similar_artists(artist_name, limit=10):
    if not artist_name:
        return []

    def parse(d):
        return [{"name": s["name"], "match": _lfm_float(s.get("match")), "lastfm_url": s.get("url")}
                for s in _lfm_items(d, "similarartists", "artist") if s.get("name")]

    return _lfm_cached_list(f"sim_artists:{artist_name.lower()}:{limit}", 7 * 24 * 3600,
                            "artist.getSimilar", parse, artist=artist_name, limit=limit, autocorrect=1)

def lastfm_similar_tracks(artist_name, track_name, limit=12):
    if not artist_name or not track_name:
        return []

    def parse(d):
        return [{"name": s["name"], "artist": (s.get("artist") or {}).get("name"),
                 "match": _lfm_float(s.get("match")), "lastfm_url": s.get("url")}
                for s in _lfm_items(d, "similartracks", "track") if s.get("name")]

    return _lfm_cached_list(f"sim_tracks:{artist_name.lower()}|{track_name.lower()}:{limit}", 7 * 24 * 3600,
                            "track.getSimilar", parse, artist=artist_name, track=track_name,
                            limit=limit, autocorrect=1)

def lastfm_user_top_tracks(period="7day", limit=10):
    """User's top tracks for a period. Periods: overall, 7day, 1month, 3month, 6month, 12month."""
    user = lastfm_username()
    if not user:
        return []
    d = _lfm_fetch("user.getTopTracks", user=user, period=period, limit=limit)
    if not isinstance(d, dict):
        return []
    out = []
    for t in _lfm_items(d, "toptracks", "track"):
        out.append({
            "name": t.get("name"),
            "artist": (t.get("artist") or {}).get("name"),
            "playcount": _lfm_int(t.get("playcount")),
            "lastfm_url": t.get("url"),
        })
    return out

def lastfm_scrobbles_today():
    """Count scrobbles since local-midnight today (None when unknown)."""
    user = lastfm_username()
    if not user:
        return None
    # Local midnight as a unix timestamp (Last.fm uses unix timestamps)
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    from_ts = int(midnight.timestamp())
    d = _lfm_fetch("user.getRecentTracks", user=user, **{"from": from_ts}, limit=1)
    if not isinstance(d, dict):
        return None
    rt = d.get("recenttracks") or {}
    attr = rt.get("@attr") or {}
    try:
        return int(attr.get("total", 0))
    except (TypeError, ValueError):
        return None

def lastfm_user_top_artists():
    """User's top artists (overall), lowercased. Cached for 1 hour; a failed refresh keeps
    the previous list and is retried on the next call instead of caching []."""
    user = lastfm_username()
    if not user:
        return []
    if (time.time() - _lastfm_user_top_artists["fetched_at"]) < 3600 \
            and _lastfm_user_top_artists["data"] is not None:
        return _lastfm_user_top_artists["data"]
    d = _lfm_fetch("user.getTopArtists", user=user, period="overall", limit=50)
    if d is _LFM_MISSING:  # no such user: a real answer, cache it
        d = {}
    if not isinstance(d, dict):
        return _lastfm_user_top_artists["data"] or []
    arts = [a["name"].lower() for a in _lfm_items(d, "topartists", "artist") if a.get("name")]
    _lastfm_user_top_artists["data"] = arts
    _lastfm_user_top_artists["fetched_at"] = time.time()
    return arts

# ---------- Spotify search caches (for converting Last.fm names to Spotify entities) ----------
_sp_artist_cache = {}      # name_lower -> {id, name, url, image} | None
_sp_track_cache = {}       # "artist|track" -> {...} | None
_sp_search_lock = threading.Lock()
_sp_rate_lock = threading.Lock()   # serialize actual Spotify search API calls
_sp_last_search = 0.0              # time.monotonic() of last real search call
_SP_SEARCH_MIN_INTERVAL = 0.40     # seconds between Spotify search requests
_sp_search_backoff_until = 0.0
_sp_search_retry_after_s = None
_spotify_feature_flags = {
    "artist_batch": SPOTIFY_EXTENDED_ENDPOINTS,
    "artist_top_tracks": SPOTIFY_EXTENDED_ENDPOINTS,
    "artist_related": SPOTIFY_EXTENDED_ENDPOINTS,
    "recommendations": SPOTIFY_EXTENDED_ENDPOINTS,
}
_spotify_feature_lock = threading.Lock()
_liked_track_cache = {}    # track_id -> (ts, liked)
_liked_track_lock = threading.Lock()
_playlist_list_cache = {"fetched_at": 0.0, "playlists": [], "active_id": None}
_playlist_list_lock = threading.Lock()
_playlist_refresh_due_at = 0.0
_playlist_refresh_lock = threading.Lock()

def _sp_search_throttle():
    """Keep Spotify search calls >= _SP_SEARCH_MIN_INTERVAL apart.

    Never waits out a search backoff (that could be hours, while holding a lock that every
    search shares): callers check _sp_search_rate_limited() first and fail fast. The slot
    is reserved under the lock; the short sleep happens outside it.
    """
    global _sp_last_search
    with _sp_rate_lock:
        now = time.monotonic()
        start = max(now, _sp_last_search + _SP_SEARCH_MIN_INTERVAL)
        _sp_last_search = start
    wait = start - time.monotonic()
    if wait > 0:
        time.sleep(wait)

def _sp_search_rate_limited():
    return _sp_search_backoff_until > time.monotonic()

def _sp_search_backoff_remaining_s():
    return max(0, int((_sp_search_backoff_until - time.monotonic()) + 0.999))

def _sp_search_backoff_until_ms():
    remaining_s = max(0.0, _sp_search_backoff_until - time.monotonic())
    if remaining_s <= 0:
        return None
    return int(time.time() * 1000 + (remaining_s * 1000))

def _sp_search_retry_after_reported_s():
    return _sp_search_retry_after_s

def _note_sp_search_rate_limit(exc):
    """Start a search backoff, but only for a 429 that Spotify really sent: a local guard
    block (budget/protect) used to masquerade as a Spotify rate limit here."""
    global _sp_search_backoff_until, _sp_search_retry_after_s
    if not is_real_rate_limit(exc):
        return
    retry_after = spotify_retry_after_from(exc, default=30)
    _sp_search_retry_after_s = retry_after
    _sp_search_backoff_until = max(_sp_search_backoff_until, time.monotonic() + max(0, retry_after))

def _artist_name_key(name):
    """The plays ledger's artist normalization (case, accents, 'The', '&'/'and' ...)."""
    try:
        from src.services import artist_stats as _AS
        return _AS.artist_key(name or "")
    except Exception:
        return " ".join(str(name or "").casefold().split())

def _pick_artist_by_name(items, name):
    """The search result whose normalized name equals ``name``, else the first (Spotify's best)."""
    want = _artist_name_key(name)
    return next((a for a in items if _artist_name_key(a.get("name")) == want), items[0])

def _spotify_search_artist_ex(sp, name):
    """spotify_search_artist() that also says whether the lookup was blocked (by the local
    guard or a search backoff) rather than genuinely finding nothing: (result, blocked)."""
    if not name:
        return None, False
    k = name.strip().lower()
    with _sp_search_lock:
        if k in _sp_artist_cache:
            return _sp_artist_cache[k], False
    result = None
    try:
        if _sp_search_rate_limited():
            return None, True
        _sp_search_throttle()
        # Several candidates, then the one whose normalized name matches (the same rule as
        # src/web/artist_api._resolve_artist): with limit=1 a co-credited or similarly named
        # artist could win. 5 also stays inside the 2026 search cap of 10.
        r = sp.search(q=f'artist:"{name}"', type="artist", limit=5)
        items = [a for a in (((r or {}).get("artists") or {}).get("items") or []) if a and a.get("id")]
        if items:
            a = _pick_artist_by_name(items, name)
            result = {
                "id": a["id"],
                "name": a.get("name") or name,
                "url": (a.get("external_urls") or {}).get("spotify"),
                "image": a["images"][-1]["url"] if a.get("images") else None,
            }
    except (SpotifyGuardBlocked, SpotifyAppRegistrationBlocked):
        return None, True  # no request was made: not cached, no backoff
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            _note_sp_search_rate_limit(e)
            return None, True
    except Exception:
        pass
    with _sp_search_lock:
        if result is not None:
            _sp_artist_cache[k] = result
    return result, False

def spotify_search_artist(sp, name):
    """Best Spotify artist match for a name, or None (no match, or search unavailable)."""
    return _spotify_search_artist_ex(sp, name)[0]

def spotify_search_track(sp, artist, track):
    if not artist or not track:
        return None
    k = f"{artist.strip().lower()}|{track.strip().lower()}"
    with _sp_search_lock:
        if k in _sp_track_cache:
            return _sp_track_cache[k]
    result = None
    try:
        if _sp_search_rate_limited():
            return None
        # Last.fm titles often have "(Remastered)", "- 2011 Remaster", etc.
        clean_track = re.sub(r"\s*[-(\[][^)\]]*?(remaster|remastered|version|edit|mix|live|stereo|mono)[^)\]]*[)\]]?", "",
                             track, flags=re.IGNORECASE).strip()
        for q in [f'track:"{clean_track}" artist:"{artist}"',
                  f'track:"{track}" artist:"{artist}"']:
            _sp_search_throttle()
            r = sp.search(q=q, type="track", limit=1)
            items = ((r or {}).get("tracks") or {}).get("items") or []
            if items:
                t = items[0]
                album = t.get("album") or {}
                result = {
                    "id": t["id"],
                    "name": t["name"],
                    "uri": t.get("uri"),
                    "artists": ", ".join(a["name"] for a in t.get("artists") or []),
                    "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in t.get("artists") or []],
                    "url": (t.get("external_urls") or {}).get("spotify"),
                    "image": album["images"][-1]["url"] if album.get("images") else None,
                }
                break
    except (SpotifyGuardBlocked, SpotifyAppRegistrationBlocked):
        return None  # local block: no request was made; not cached, no backoff
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            _note_sp_search_rate_limit(e)
    except Exception:
        pass
    with _sp_search_lock:
        if result is not None:
            _sp_track_cache[k] = result
    return result

# ---------- Helpers ----------
def spotify_feature_enabled(name):
    with _spotify_feature_lock:
        return _spotify_feature_flags.get(name, True)

def disable_spotify_feature(name):
    with _spotify_feature_lock:
        _spotify_feature_flags[name] = False

def spotify_feature_removed(exc):
    # A local guard block (the app-registration one is a 403) or "user not registered"
    # says nothing about whether the endpoint exists; don't disable features on them.
    # 403: Spotify's answer to endpoints removed for Development Mode apps created after
    # 2026-02-11 (batch artists, artist top tracks, /browse, ...); 404/410: gone for everyone.
    return (isinstance(exc, spotipy.SpotifyException) and exc.http_status in (403, 404, 410)
            and not is_local_guard_block(exc) and not is_spotify_app_registration_error(exc))

_SPOTIFY_FIELDS_REJECTED = set()   # fields filters Spotify answered 400 to (this session): not resent

def _split_top_level_fields(fields):
    parts, depth, cur = [], 0, []
    for ch in str(fields or ""):
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]

def _spotify_playlist_fields_variants(fields):
    """``fields``; the same filter without the pre-2026 playlist ``tracks`` object; then None."""
    out = []
    if fields:
        out.append(fields)
        modern = ",".join(p for p in _split_top_level_fields(fields)
                          if p.split(".", 1)[0].split("(", 1)[0].strip() != "tracks")
        if modern and modern not in out:
            out.append(modern)
    with _spotify_feature_lock:
        rejected = set(_SPOTIFY_FIELDS_REJECTED)
    return [f for f in out if f not in rejected] + [None]

def spotify_playlist_meta(sp, playlist_id, fields=None):
    """sp.playlist() for both API generations. Development Mode apps created after 2026-02-11
    see ``items`` where older apps see ``tracks``, and Spotify may reject (400) a filter naming
    the old object: retry without those names, then unfiltered. A filter is remembered as
    rejected (never sent again this session) only once a later variant worked, so a 400 about
    the playlist id itself does not degrade later calls."""
    rejected_now = []
    for f in _spotify_playlist_fields_variants(fields):
        try:
            meta = sp.playlist(playlist_id, fields=f) if f else sp.playlist(playlist_id)
        except spotipy.SpotifyException as e:
            if f and getattr(e, "http_status", None) == 400 and not is_local_guard_block(e):
                rejected_now.append(f)
                continue
            raise
        if rejected_now:
            with _spotify_feature_lock:
                _SPOTIFY_FIELDS_REJECTED.update(rejected_now)
        return meta

def playlist_total_from_meta(meta, fallback=0):
    if not meta:
        return fallback
    tracks = meta.get("tracks")
    if isinstance(tracks, dict) and tracks.get("total") is not None:
        return tracks.get("total")
    items = meta.get("items")
    if isinstance(items, dict) and items.get("total") is not None:
        return items.get("total")
    total = meta.get("total")
    if total is not None:
        return total
    return fallback

def playlist_entry_media(entry):
    if not isinstance(entry, dict):
        return None
    media = entry.get("item")
    if isinstance(media, dict):
        return media
    media = entry.get("track")
    if isinstance(media, dict):
        return media
    return None

_GENRE_LOOKUP_BUDGET_RESERVE = 6   # artist() slots per budget window kept free for interactive use

def _spotify_budget_left(method_name):
    """Live calls ``method_name`` may still make in its guard budget window (None = unbudgeted)."""
    limit, window_s = _spotify_guard_policy(method_name).get("budget") or (0, 0)
    if not limit or not window_s:
        return None
    cutoff = time.monotonic() - window_s
    with _SPOTIFY_GUARD_LOCK:
        used = sum(1 for t in (_SPOTIFY_GUARD_BUDGET_BUCKETS.get(method_name) or ()) if t >= cutoff)
    return max(0, int(limit) - used)

def ensure_artist_genre_cache(sp, artist_ids):
    """Fill artist_genre_cache for artist_ids.

    Only a real answer is cached: genres, or [] when Spotify says the artist/endpoint is gone
    (403/404). A local guard block or a transient error caches nothing (the old code cached
    [] for the life of the process) and a guard block never shows up as last_error.
    """
    with artist_genre_lock:
        needed = [a for a in (artist_ids or []) if a and a not in artist_genre_cache]
    if not needed:
        return
    if spotify_feature_enabled("artist_batch"):
        try:
            res = sp.artists(needed[:50])
            with artist_genre_lock:
                for ar in (res or {}).get("artists", []) or []:
                    if ar and ar.get("id"):
                        artist_genre_cache[ar["id"]] = ar.get("genres", []) or []
        except Exception as e:
            if is_local_guard_block(e):
                return  # everything after this would be blocked too
            if spotify_feature_removed(e):
                disable_spotify_feature("artist_batch")
            else:
                with state_lock:
                    state["last_error"] = f"Genre fetch: {e}"
    with artist_genre_lock:
        still_needed = [a for a in needed if a not in artist_genre_cache]
    for i, artist_id in enumerate(still_needed):
        # The batch endpoint is gone for new apps, so this is one artist() call per id. Genres
        # are garnish: after the first lookup, leave headroom in the artist() budget for the
        # artist page and clicks; the rest fill in on later calls (answers are cached).
        if i > 0:
            left = _spotify_budget_left("artist")
            if left is not None and left <= _GENRE_LOOKUP_BUDGET_RESERVE:
                break
        try:
            artist = sp.artist(artist_id)
            genres = (artist or {}).get("genres") or []
        except Exception as e:
            if is_local_guard_block(e):
                break  # over budget / protect mode: try again on a later call
            if spotify_feature_removed(e):
                genres = []
            else:
                with state_lock:
                    state["last_error"] = f"Genre fetch: {e}"
                continue
        with artist_genre_lock:
            artist_genre_cache[artist_id] = genres

def get_cached_genres_for_artists(artist_ids):
    out, seen = [], set()
    with artist_genre_lock:
        for artist_id in artist_ids:
            for genre in artist_genre_cache.get(artist_id, []):
                if genre not in seen:
                    seen.add(genre)
                    out.append(genre)
    return out

def fetch_genres_for_artists(sp, artist_ids):
    ensure_artist_genre_cache(sp, artist_ids)
    return get_cached_genres_for_artists(artist_ids)

def get_cached_track_genres(track_id):
    if not track_id:
        return []
    with _track_genre_cache_lock:
        entry = _track_genre_cache.get(str(track_id)) or {}
    return normalize_genres(entry.get("genres") or [], limit=4)

def _trim_track_genre_cache_locked():
    """Cap the cache (newest entries win) and return a snapshot to persist. Caller holds
    _track_genre_cache_lock."""
    snapshot = {k: dict(v) for k, v in _track_genre_cache.items()}
    if len(snapshot) > TRACK_GENRE_CACHE_MAX:
        ordered = sorted(snapshot.items(), key=lambda item: int((item[1] or {}).get("ts") or 0), reverse=True)
        snapshot = dict(ordered[:TRACK_GENRE_CACHE_MAX])
        _track_genre_cache.clear()
        _track_genre_cache.update({k: dict(v) for k, v in snapshot.items()})
    return snapshot


_track_genre_persist_lock = threading.Lock()


def _write_track_genre_cache(snapshot):
    update_config(lambda cfg: cfg.__setitem__("track_genre_cache", snapshot))


def _persist_track_genre_cache_locked():
    _write_track_genre_cache(_trim_track_genre_cache_locked())

def cache_track_genres_bulk(entries):
    changed = False
    now_ts = int(time.time())
    # The persist lock orders snapshots and writes; readers only need the cache lock, which
    # is no longer held during the ~1 MB config write.
    with _track_genre_persist_lock:
        with _track_genre_cache_lock:
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                track_id = str(entry.get("track_id") or "").strip()
                if not track_id:
                    continue
                genres = normalize_genres(entry.get("genres") or [], limit=4)
                if not genres:
                    continue
                artist = (entry.get("artist") or "").strip()
                prev = _track_genre_cache.get(track_id) or {}
                if prev.get("genres") == genres and prev.get("artist") == artist:
                    continue
                _track_genre_cache[track_id] = {"genres": genres, "artist": artist, "ts": now_ts}
                changed = True
            snapshot = _trim_track_genre_cache_locked() if changed else None
        if snapshot is not None:
            _write_track_genre_cache(snapshot)
    return changed

def get_spotify_queue_count(sp, force=False, ttl_s=20):
    now = time.time()
    with _spotify_queue_cache_lock:
        fetched_at = float(_spotify_queue_cache.get("fetched_at") or 0)
        cached_count = int(_spotify_queue_cache.get("count") or 0)
    if not force and (now - fetched_at) < ttl_s:
        return cached_count
    count = 0
    try:
        if hasattr(sp, "queue"):
            res = sp.queue()
        else:
            res = None
        queue_items = (res or {}).get("queue") or []
        count = len(queue_items)
    except Exception:
        count = cached_count
    with _spotify_queue_cache_lock:
        _spotify_queue_cache["count"] = int(count or 0)
        _spotify_queue_cache["fetched_at"] = now
    return int(count or 0)

_PAGING_MAX_WAIT_S = 120.0


def _spotify_page_with_retry(fetch, deadline):
    """Run one guarded page request. A local guard block (budget) is waited out in steps of
    min(retry_after, 30) s while the deadline allows; otherwise it is re-raised so the caller
    can fall back. A real 429 or any other error propagates immediately."""
    while True:
        try:
            return fetch()
        except SpotifyGuardBlocked as e:
            retry_after = float(e.retry_after if e.retry_after is not None else 5)
            remaining = deadline - time.monotonic()
            if retry_after > remaining:
                raise
            time.sleep(max(0.5, min(retry_after, 30.0)))


def get_liked_songs_page(sp, force=False, ttl_s=300, limit=120):
    now = time.time()
    with _liked_songs_cache_lock:
        cached_tracks = list(_liked_songs_cache.get("tracks") or [])
        fetched_at = float(_liked_songs_cache.get("fetched_at") or 0)
    if cached_tracks and not force and (now - fetched_at) < ttl_s:
        return cached_tracks
    tracks = []
    offset = 0
    deadline = time.monotonic() + _PAGING_MAX_WAIT_S
    while len(tracks) < limit:
        try:
            res = _spotify_page_with_retry(
                lambda: sp.current_user_saved_tracks(limit=min(50, limit - len(tracks)), offset=offset),
                deadline)
        except SpotifyGuardBlocked:
            # Out of patience: an older complete list beats a partial one; never cache partial.
            if cached_tracks:
                return cached_tracks
            if tracks:
                return tracks
            raise
        items = (res or {}).get("items") or []
        if not items:
            break
        for item in items:
            tr = playlist_entry_media(item) or {}   # "track" (legacy) or "item" (2026 naming)
            tid = tr.get("id")
            if not tid:
                continue
            artists = tr.get("artists") or []
            artist_names = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            primary_artist = (artists[0].get("name") if artists else "") or ""
            images = (tr.get("album") or {}).get("images") or []
            tracks.append({
                "id": tid,
                "name": tr.get("name", ""),
                "artists": artist_names,
                "artist": primary_artist,
                "album": (tr.get("album") or {}).get("name", ""),
                "image": images[-1]["url"] if images else None,
                "uri": tr.get("uri"),
                "duration_ms": tr.get("duration_ms", 0),
                "genres": get_cached_track_genres(tid),
                "liked_at": item.get("added_at"),
            })
        if not res.get("next") or len(items) < 50:
            break
        offset += len(items)
    with _liked_songs_cache_lock:
        _liked_songs_cache["tracks"] = list(tracks)
        _liked_songs_cache["fetched_at"] = now
    return tracks

def get_cached_liked_state(track_id, ttl_s=6 * 3600):
    with _liked_track_lock:
        cached = _liked_track_cache.get(track_id)
    if cached and (time.time() - cached[0]) < ttl_s:
        return cached[1]
    return None

def set_cached_liked_state(track_id, liked):
    if not track_id:
        return
    with _liked_track_lock:
        _liked_track_cache[track_id] = (time.time(), bool(liked))

def is_track_liked(sp, track_id):
    """True/False when known; None when it could not be checked (guard block, network
    error). Callers should treat None as "unknown", not as "not liked"."""
    if not track_id:
        return None
    cached = get_cached_liked_state(track_id)
    if cached is not None:
        return cached
    try:
        r = sp.current_user_saved_tracks_contains([track_id])
        liked = bool(r and r[0])
        set_cached_liked_state(track_id, liked)
        return liked
    except Exception:
        return None

def get_liked_artist_counts(sp, force=False, ttl_s=900):
    now = time.time()
    with _LIKED_ARTIST_COUNT_LOCK:
        cached = dict(_LIKED_ARTIST_COUNT_CACHE.get("counts") or {})
        fetched_at = float(_LIKED_ARTIST_COUNT_CACHE.get("fetched_at") or 0)
    if cached and not force and (now - fetched_at) < ttl_s:
        return cached

    counts = Counter()
    display_names = {}
    offset = 0
    partial = False
    deadline = time.monotonic() + _PAGING_MAX_WAIT_S
    while True:
        try:
            res = _spotify_page_with_retry(
                lambda: sp.current_user_saved_tracks(limit=50, offset=offset), deadline)
        except SpotifyGuardBlocked:
            if cached:
                return cached  # a complete older count beats a truncated new one
            if not counts:
                raise
            partial = True
            break
        items = (res or {}).get("items") or []
        if not items:
            break
        for item in items:
            track = playlist_entry_media(item) or {}
            for artist in track.get("artists") or []:
                name = (artist.get("name") or "").strip()
                if name:
                    key = name.lower()
                    counts[key] += 1
                    display_names.setdefault(key, name)
        if not res.get("next") or len(items) < 50:
            break
        offset += len(items)

    out = dict(counts)
    with _LIKED_ARTIST_COUNT_LOCK:
        # A truncated scan undercounts (safe for auto-like thresholds); keep it ~1 min only.
        _LIKED_ARTIST_COUNT_CACHE["fetched_at"] = (now - ttl_s + 60) if partial else now
        _LIKED_ARTIST_COUNT_CACHE["counts"] = out
        _LIKED_ARTIST_COUNT_CACHE["names"] = dict(display_names)
        _LIKED_ARTIST_COUNT_CACHE["partial"] = partial
    return out

def get_liked_artist_display_names(sp=None):
    with _LIKED_ARTIST_COUNT_LOCK:
        names = dict(_LIKED_ARTIST_COUNT_CACHE.get("names") or {})
        fetched_at = float(_LIKED_ARTIST_COUNT_CACHE.get("fetched_at") or 0)
    if names or not sp or fetched_at:
        return names
    get_liked_artist_counts(sp)
    with _LIKED_ARTIST_COUNT_LOCK:
        return dict(_LIKED_ARTIST_COUNT_CACHE.get("names") or {})

def invalidate_liked_artist_counts_cache():
    with _LIKED_ARTIST_COUNT_LOCK:
        _LIKED_ARTIST_COUNT_CACHE["fetched_at"] = 0.0
        _LIKED_ARTIST_COUNT_CACHE["counts"] = {}
        _LIKED_ARTIST_COUNT_CACHE["names"] = {}

def auto_like_artists_from_spotify(sp, artist_names):
    if not _SQLITE_ENABLED:
        return []
    threshold = current_auto_artist_like_threshold()
    if threshold <= 0:
        return []
    counts = get_liked_artist_counts(sp)
    newly = []
    for raw_name in _split_artist_names(artist_names):
        key = raw_name.lower()
        if not key:
            continue
        try:
            if is_favorite_artist_opted_out(raw_name):
                continue
            if _events.is_artist_liked(raw_name):
                add_favorite_artist(raw_name, source="auto")
                continue
            liked_count = int(counts.get(key, 0))
            if liked_count >= threshold:
                _events.upsert_artist_like(
                    artist_id=raw_name,
                    artist_name=raw_name,
                    auto_liked=True,
                    track_count=liked_count,
                )
                add_favorite_artist(raw_name, source="auto")
                newly.append(raw_name)
        except Exception:
            pass
    return newly

def sync_favorite_artists_from_library(sp, force=False):
    threshold = current_auto_artist_like_threshold()
    if threshold <= 0:
        return {"threshold": threshold, "newly_favorited": [], "eligible_total": 0}
    counts = get_liked_artist_counts(sp, force=force)
    display_names = get_liked_artist_display_names(sp)
    eligible = []
    newly = []
    for key, count in counts.items():
        if int(count or 0) < threshold:
            continue
        name = display_names.get(key) or key
        eligible.append(name)
        if is_favorite_artist_opted_out(name):
            continue
        if add_favorite_artist(name, source="auto"):
            newly.append(name)
        try:
            _events.upsert_artist_like(
                artist_id=name,
                artist_name=name,
                auto_liked=True,
                track_count=int(count or 0),
            )
        except Exception:
            pass
    return {
        "threshold": threshold,
        "newly_favorited": newly,
        "eligible_total": len(eligible),
        "eligible_names": sorted(eligible, key=str.lower),
    }

def get_favorites_overview(sp=None, force=False):
    with state_lock:
        fav_names = sorted(state["favorite_artists"])
        opt_outs = sorted(state.get("favorite_artist_opt_outs") or [])
    fingerprint = "|".join(fav_names)
    now = time.time()
    with _FAVORITE_RELEASE_LOCK:
        cached = _FAVORITE_RELEASE_CACHE.get("data")
        fetched_at = float(_FAVORITE_RELEASE_CACHE.get("fetched_at") or 0)
        cache_ttl_s = float(_FAVORITE_RELEASE_CACHE.get("ttl_s") or 1800)
    if cached and not force and cached.get("fingerprint") == fingerprint and (now - fetched_at) < cache_ttl_s:
        return cached

    display_names = get_liked_artist_display_names() if sp else {}
    favorites = [{"name": name, "display_name": display_names.get(name, name.title())} for name in fav_names]
    release_feed = []
    release_error = None
    checked = 0
    unchecked = 0
    if sp and fav_names:
        # Liked counts only from the cache: a full library scan can take minutes and must
        # not hold up this request. Warm it in the background for the next call.
        with _LIKED_ARTIST_COUNT_LOCK:
            counts = dict(_LIKED_ARTIST_COUNT_CACHE.get("counts") or {})
        if not counts:
            _warm_liked_artist_counts_async(sp)
        for item in favorites:
            item["liked_count"] = int(counts.get(item["name"], 0))
        # Most-liked favorites first, so a budget-limited pass covers the ones that matter.
        ordered = sorted(favorites, key=lambda f: (-f["liked_count"], f["name"]))
        artist_errors = []
        blocked = False
        work_deadline = time.monotonic() + 12.0
        max_age_s = 600 if force else _FAVORITE_ARTIST_RELEASES_TTL_S
        for fav in ordered:
            raw_name, display_name = fav["name"], fav["display_name"]
            with _FAVORITE_RELEASE_LOCK:
                entry = _FAVORITE_ARTIST_RELEASES.get(raw_name)
            fresh = entry is not None and (now - entry["ts"]) < max_age_s
            if not fresh and not blocked and time.monotonic() < work_deadline:
                try:
                    artist, was_blocked = _spotify_search_artist_ex(sp, display_name)
                    if was_blocked:
                        blocked = True
                    else:
                        rows = []
                        if artist and artist.get("id"):
                            albums_res = sp.artist_albums(artist["id"], include_groups="album,single", limit=10)
                            for album in (albums_res or {}).get("items") or []:
                                rows.append({
                                    "artist_name": display_name,
                                    "artist_id": artist["id"],
                                    "artist_image": artist.get("image"),
                                    "album_name": album.get("name") or "",
                                    "album_id": album.get("id") or "",
                                    "album_type": album.get("album_type") or "album",
                                    "release_date": album.get("release_date") or "",
                                    "release_precision": album.get("release_date_precision") or "day",
                                    "image": small_image(album.get("images")),
                                    "url": ((album.get("external_urls") or {}).get("spotify")),
                                })
                        entry = {"ts": now, "rows": rows}
                        fresh = True
                        with _FAVORITE_RELEASE_LOCK:
                            _FAVORITE_ARTIST_RELEASES[raw_name] = entry
                except SpotifyGuardBlocked:
                    blocked = True
                except Exception as e:
                    artist_errors.append(f"{display_name}: {e}")
            if fresh:
                checked += 1
            else:
                unchecked += 1
            if entry:
                release_feed.extend(entry["rows"])
        release_feed.sort(key=lambda r: (r.get("release_date") or "", r.get("artist_name") or ""), reverse=True)
        deduped = []
        seen = set()
        for row in release_feed:
            key = (row["artist_name"].lower(), row["album_name"].lower(), row["release_date"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        release_feed = deduped[:48]
        if artist_errors:
            release_error = artist_errors[0]
        elif blocked and not release_feed:
            release_error = "Spotify is pacing requests right now; new releases will fill in shortly."

    partial = unchecked > 0
    data = {
        "fingerprint": fingerprint,
        "favorites": favorites,
        "favorite_count": len(favorites),
        "opt_outs": [{"name": n, "display_name": display_names.get(n, n.title())} for n in opt_outs],
        "release_feed": release_feed,
        "release_error": release_error,
        "release_partial": partial,
        "release_checked": checked,
        "release_total": len(fav_names),
    }
    with _FAVORITE_RELEASE_LOCK:
        # A partial pass is only reused briefly, so the next visit resumes where it stopped.
        _FAVORITE_RELEASE_CACHE["fetched_at"] = now
        _FAVORITE_RELEASE_CACHE["ttl_s"] = 90 if partial else 1800
        _FAVORITE_RELEASE_CACHE["data"] = data
    return data


_FAVORITE_ARTIST_RELEASES = {}  # favorite name (lower) -> {"ts": epoch s, "rows": [release rows]}
_FAVORITE_ARTIST_RELEASES_TTL_S = 6 * 3600
_liked_counts_warming = threading.Event()


def _warm_liked_artist_counts_async(sp):
    if _liked_counts_warming.is_set():
        return
    _liked_counts_warming.set()

    def _run():
        try:
            get_liked_artist_counts(sp)
        except Exception as e:
            if not is_local_guard_block(e):
                _log_app_error(e, context="warm liked artist counts")
        finally:
            _liked_counts_warming.clear()

    threading.Thread(target=_run, daemon=True, name="liked-artist-counts").start()

def small_image(images):
    if not images:
        return None
    # Prefer the smallest reasonable image
    return images[-1]["url"] if images else None

def _spotify_track_row(t):
    """A Spotify track object as an artist-page track row (missing fields tolerated)."""
    album = t.get("album") or {}
    artists = [a for a in (t.get("artists") or []) if isinstance(a, dict)]
    return {
        "id": t.get("id"),
        "name": t.get("name") or "",
        "artists": ", ".join(a.get("name") or "" for a in artists),
        "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in artists],
        "album": album.get("name"),
        "image": small_image(album.get("images")),
        "url": (t.get("external_urls") or {}).get("spotify"),
        "uri": t.get("uri") or (f"spotify:track:{t['id']}" if t.get("id") else None),
        "duration_ms": t.get("duration_ms"),
    }

def spotify_artist_top_tracks_via_search(sp, artist_id, artist_name, limit=SPOTIFY_SEARCH_MAX_LIMIT):
    """Stand-in for GET /artists/{id}/top-tracks, which Development Mode apps created after
    2026-02-11 no longer have: one search for ``artist:"<name>"`` (limit <= 10), keeping tracks
    credited to this artist (by id, else by normalized name) in Spotify's relevance order, one
    per title. Returns (rows, blocked); blocked = a local guard block or search cooldown."""
    if not artist_name:
        return [], False
    if _sp_search_rate_limited():
        return [], True
    try:
        _sp_search_throttle()
        r = sp.search(q=f'artist:"{artist_name}"', type="track",
                      limit=max(1, min(int(limit or SPOTIFY_SEARCH_MAX_LIMIT), SPOTIFY_SEARCH_MAX_LIMIT)))
    except (SpotifyGuardBlocked, SpotifyAppRegistrationBlocked):
        return [], True
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            _note_sp_search_rate_limit(e)
            return [], True
        return [], False
    except Exception:
        return [], False
    want = _artist_name_key(artist_name)
    rows, seen_ids, seen_titles = [], set(), set()
    for t in (((r or {}).get("tracks") or {}).get("items") or []):
        if not isinstance(t, dict) or not t.get("id") or t["id"] in seen_ids:
            continue
        credits = [a for a in (t.get("artists") or []) if isinstance(a, dict)]
        if not any((artist_id and a.get("id") == artist_id) or _artist_name_key(a.get("name")) == want
                   for a in credits):
            continue
        title = " ".join((t.get("name") or "").casefold().split())
        if title in seen_titles:
            continue  # the same song on the album and on the single
        seen_ids.add(t["id"])
        seen_titles.add(title)
        rows.append(_spotify_track_row(t))
    return rows, False

def fetch_artist_info(sp, primary_artist_id, primary_artist_name,
                      track_id, track_name, user_seed_tracks, primary_genre=None,
                      allow_spotify_resolution=True, resolve_budget_s=1.5):
    """Combine Spotify top tracks with Last.fm-sourced similar artists, similar
    tracks, bio, tags, and stats. Last.fm results are cross-searched against
    Spotify so they're clickable + show cover art.

    Live Spotify searches are limited to ``resolve_budget_s`` per call (each costs ~0.5 s of
    pacing, so resolving ~25 entities took 15 s). Past the budget only already-cached matches
    are used; everything else keeps id None and is resolved on click via /api/v2/resolve."""
    resolve_deadline = time.monotonic() + max(0.0, float(resolve_budget_s or 0))

    def _resolve_artist(name):
        if not allow_spotify_resolution or not name:
            return None
        if time.monotonic() < resolve_deadline:
            return spotify_search_artist(sp, name)
        with _sp_search_lock:
            return _sp_artist_cache.get(name.strip().lower())

    def _resolve_track(artist, title):
        if not allow_spotify_resolution or not artist or not title:
            return None
        if time.monotonic() < resolve_deadline:
            return spotify_search_track(sp, artist, title)
        with _sp_search_lock:
            return _sp_track_cache.get(f"{artist.strip().lower()}|{title.strip().lower()}")

    info = {
        "name": primary_artist_name,
        "artist_id": primary_artist_id,
        "artist_image": None,
        "spotify_url": f"https://open.spotify.com/artist/{primary_artist_id}" if primary_artist_id else None,
        "top_tracks": [],
        "related": [],
        "recommendations": [],
        "matches_taste": [],   # subset of recs that overlap with user's Last.fm top
        "bio": None,
        "lastfm_tags": [],
        "stats": None,
        "lastfm_url": None,
        "everynoise_url": None,
        "notes": [],
        "sources": [],
    }

    # --- Spotify: artist top tracks ---
    if primary_artist_id and spotify_feature_enabled("artist_top_tracks"):
        try:
            tt = sp.artist_top_tracks(primary_artist_id)
            info["top_tracks"] = [_spotify_track_row(t) for t in ((tt or {}).get("tracks") or [])[:10]
                                  if isinstance(t, dict) and t.get("id")]
            if info["top_tracks"] and not info.get("artist_image"):
                info["artist_image"] = info["top_tracks"][0].get("image")
            if info["top_tracks"]:
                info["sources"].append("spotify (top tracks)")
        except Exception as e:
            if spotify_feature_removed(e):
                disable_spotify_feature("artist_top_tracks")
            else:
                info["notes"].append(f"top tracks unavailable: {e}")

    if not info["top_tracks"] and primary_artist_name:
        lfm_top_tracks = lastfm_artist_top_tracks(primary_artist_name, limit=8)
        top_tracks = []
        seen_ids = set()
        for t in lfm_top_tracks:
            sp_t = _resolve_track(t["artist"], t["name"])
            if sp_t and sp_t["id"] in seen_ids:
                continue
            if sp_t:
                seen_ids.add(sp_t["id"])
                top_tracks.append({**sp_t, "uri": sp_t.get("uri") or f"spotify:track:{sp_t['id']}",
                                   "artist_list": sp_t.get("artist_list") or [{"id": None, "name": t["artist"]}],
                                   "lastfm_url": t.get("lastfm_url"), "source": "lastfm"})
            else:
                # Unresolved: keep it (the UI resolves a play button lazily via /api/v2/resolve).
                top_tracks.append({
                    "id": None,
                    "name": t["name"],
                    "artists": t["artist"],
                    "artist_list": [{"id": None, "name": t["artist"]}],
                    "album": None,
                    "image": None,
                    "url": t.get("lastfm_url"),
                    "lastfm_url": t.get("lastfm_url"),
                    "uri": None,
                    "duration_ms": None,
                    "source": "lastfm",
                })
            if len(top_tracks) >= 10:
                break
        info["top_tracks"] = top_tracks
        if top_tracks:
            if not info.get("artist_image"):
                info["artist_image"] = next((t.get("image") for t in top_tracks if t.get("image")), None)
            info["sources"].append("last.fm + spotify search (top tracks)")

    if not info["top_tracks"] and primary_artist_name and allow_spotify_resolution:
        # No top-tracks endpoint (new apps) and nothing from Last.fm (no key, or it failed):
        # one Spotify search for the artist's tracks.
        rows, _blocked = spotify_artist_top_tracks_via_search(sp, primary_artist_id, primary_artist_name)
        if rows:
            info["top_tracks"] = rows
            if not info.get("artist_image"):
                info["artist_image"] = next((t.get("image") for t in rows if t.get("image")), None)
            info["sources"].append("spotify search (top tracks)")

    # --- Last.fm: artist info, similar artists, similar tracks ---
    if lastfm_api_key() and primary_artist_name:
        lfm = lastfm_artist_info(primary_artist_name)
        if lfm:
            info["bio"] = lfm["bio"] or None
            # Full biography (Last.fm bio.content, plain text) when the client provides it;
            # the summary is cut at ~300 characters. Rendered as text only by the UI.
            info["bio_full"] = lfm.get("bio_full") or lfm.get("bio_content") or info["bio"]
            info["lastfm_tags"] = lfm["tags"]
            info["stats"] = {
                "listeners": lfm["listeners"],
                "playcount": lfm["playcount"],
            }
            info["lastfm_url"] = lfm["url"]
            info["sources"].append("last.fm (bio, tags, stats)")
        if not info["lastfm_tags"]:
            info["lastfm_tags"] = lastfm_artist_top_tags(primary_artist_name, limit=6)

        # Similar artists, then resolve to Spotify entities for clickable cards
        sim_artists = lastfm_similar_artists(primary_artist_name, limit=10)
        related = []
        seen_related = set()
        for s in sim_artists:
            sp_a = _resolve_artist(s["name"])
            if sp_a and sp_a.get("id") in seen_related:
                continue
            if sp_a:
                seen_related.add(sp_a.get("id"))
                related.append({**sp_a, "match": round(s["match"], 2), "lastfm_url": s.get("lastfm_url")})
            else:
                # Search budget spent, cooling down or no Spotify match: keep the Last.fm entry
                # (id None) instead of silently dropping it; the page resolves it on click.
                related.append({
                    "id": None,
                    "name": s["name"],
                    "url": s.get("lastfm_url"),
                    "lastfm_url": s.get("lastfm_url"),
                    "image": None,
                    "match": round(s["match"], 2),
                })
            if len(related) >= 10:
                break
        info["related"] = related
        if related:
            info["sources"].append("last.fm + spotify search (similar artists)" if allow_spotify_resolution else "last.fm (similar artists)")

        # Similar tracks
        sim_tracks = lastfm_similar_tracks(primary_artist_name, track_name, limit=10)
        recs = []
        seen_ids = set()
        for s in sim_tracks:
            if not s.get("artist"):
                continue
            sp_t = _resolve_track(s["artist"], s["name"])
            if sp_t and sp_t["id"] in seen_ids:
                continue
            if sp_t:
                seen_ids.add(sp_t["id"])
                recs.append({**sp_t, "uri": sp_t.get("uri") or f"spotify:track:{sp_t['id']}",
                             "match": round(s["match"], 2), "lastfm_artist": s["artist"],
                             "lastfm_url": s.get("lastfm_url")})
            else:
                recs.append({
                    "id": None,
                    "name": s["name"],
                    "artists": s["artist"],
                    "lastfm_artist": s["artist"],
                    "url": s.get("lastfm_url"),
                    "lastfm_url": s.get("lastfm_url"),
                    "uri": None,
                    "image": None,
                    "match": round(s["match"], 2),
                })
            if len(recs) >= 6:    # was 8
                break
        info["recommendations"] = recs
        if recs:
            info["sources"].append("last.fm + spotify search (similar tracks)" if allow_spotify_resolution else "last.fm (similar tracks)")

        # Personalized: which of those tracks are by artists in user's Last.fm top
        if lastfm_username():
            top_artist_set = set(lastfm_user_top_artists())
            if top_artist_set:
                # Compare the Last.fm artist name whole: splitting credits on commas broke
                # bands such as 'Woe, Is Me'.
                info["matches_taste"] = [
                    r for r in recs
                    if (r.get("lastfm_artist") or r.get("artists") or "").strip().lower() in top_artist_set
                ]
    else:
        info["notes"].append("set LASTFM_API_KEY in .env to enable similar artists/tracks/bio")

    # --- Spotify endpoint fallbacks (work for older apps; usually 403 for new) ---
    if not info["related"] and primary_artist_id and spotify_feature_enabled("artist_related"):
        try:
            rel = sp.artist_related_artists(primary_artist_id)
            info["related"] = [{
                "id": a["id"],
                "name": a["name"],
                "image": small_image(a.get("images")),
                "url": a["external_urls"].get("spotify"),
                "genres": (a.get("genres") or [])[:3],
            } for a in rel.get("artists", [])[:6]]
            if info["related"]:
                info["sources"].append("spotify (related-artists)")
        except Exception as e:
            if spotify_feature_removed(e):
                disable_spotify_feature("artist_related")
                info["notes"].append("similar artists: Spotify unavailable in this app mode")
            else:
                info["notes"].append("similar artists: Last.fm unavailable, Spotify deprecated")

    seed_pool = [t for t in ([track_id] + [t for t in (user_seed_tracks or []) if t != track_id][:2]) if t]
    if not info["recommendations"] and seed_pool and spotify_feature_enabled("recommendations"):
        try:
            seeds = seed_pool
            r = sp.recommendations(seed_tracks=seeds[:5], limit=8)
            info["recommendations"] = [{
                "id": t["id"],
                "name": t["name"],
                "artists": ", ".join(a["name"] for a in t["artists"]),
                "image": small_image(t["album"].get("images")),
                "url": t["external_urls"].get("spotify"),
            } for t in r.get("tracks", [])]
            if info["recommendations"]:
                info["sources"].append("spotify (recommendations)")
        except Exception as e:
            if spotify_feature_removed(e):
                disable_spotify_feature("recommendations")
                info["notes"].append("track recs: Spotify recommendations unavailable in this app mode")
            else:
                info["notes"].append("track recs: Last.fm unavailable, Spotify deprecated")

    # --- Every Noise at Once: deep link for primary genre ---
    if primary_genre:
        # Glenn's URL convention: spaces and special chars get stripped/lowered
        slug = re.sub(r"[^a-z0-9]+", "", primary_genre.lower())
        if slug:
            info["everynoise_url"] = f"https://everynoise.com/engenremap-{slug}.html"

    # --- Personal play counts (my_plays) and unheard top tracks from the plays ledger ---
    if _SQLITE_ENABLED:
        try:
            from src.services import artist_stats as _artist_stats_svc
            _artist_stats_svc.annotate_artist_info(_db.get_conn(), info, primary_artist_name)
        except Exception as e:
            _log_app_error(e, "fetch_artist_info_my_plays")

    return info

# ---------- Lyrics (lrclib.net) ----------
LRC_LINE_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s?(.*)")

def parse_lrc(synced_text):
    """Parse LRC-format synced lyrics into [{ts_ms, text}, ...]"""
    lines = []
    for line in (synced_text or "").splitlines():
        m = LRC_LINE_RE.match(line)
        if not m:
            continue
        mins = int(m.group(1))
        secs = float(m.group(2))
        text = m.group(3).strip()
        ts_ms = int(mins * 60_000 + secs * 1000)
        lines.append({"ts_ms": ts_ms, "text": text})
    lines.sort(key=lambda x: x["ts_ms"])
    return lines

def fetch_lyrics(track_name, artist_name, album_name, duration_ms):
    """Try lrclib for synced lyrics first, fall back to plain. Returns dict or None."""
    try:
        params = {
            "track_name": track_name,
            "artist_name": artist_name,
            "album_name": album_name or "",
            "duration": int((duration_ms or 0) / 1000),
        }
        r = requests.get(
            LRCLIB_BASE,
            params=params,
            headers={"User-Agent": LRCLIB_UA},
            timeout=8,
        )
        if r.status_code == 404:
            return {"synced": False, "lines": [], "plain": None, "source": "lrclib", "found": False}
        r.raise_for_status()
        data = r.json()
        synced = data.get("syncedLyrics")
        plain = data.get("plainLyrics")
        if synced:
            return {
                "synced": True,
                "lines": parse_lrc(synced),
                "plain": plain,
                "source": "lrclib",
                "found": True,
            }
        if plain:
            return {
                "synced": False,
                "lines": [],
                "plain": plain,
                "source": "lrclib",
                "found": True,
            }
        return {"synced": False, "lines": [], "plain": None, "source": "lrclib", "found": False}
    except Exception as e:
        return {"synced": False, "lines": [], "plain": None, "source": "lrclib",
                "found": False, "error": str(e)}

# ---------- Background enrichment on track change ----------
def enrich_in_background(sp, track_info, primary_artist_id, user_top_ids):
    """Fetch artist info + lyrics in a worker thread. Debounces rapid track changes
    by waiting 1.5s before doing the heavy fetches — if the user is quickly skipping,
    we won't burn API calls on tracks they're not listening to."""
    target_id = track_info["id"]
    first_artist = (((track_info.get("artist_list") or [{}])[0] or {}).get("name") or (track_info.get("artists") or "").split(",")[0]).strip()
    primary_genre = (track_info.get("genres") or [None])[0]

    def still_relevant():
        with state_lock:
            return state["current_track_id"] == target_id

    def worker():
        # Debounce: skip if track changed within the settle window
        time.sleep(1.5)
        if not still_relevant():
            return
        # Lyrics first (single HTTP call, cheap)
        lyrics = fetch_lyrics(
            track_info["name"], first_artist, track_info.get("album"),
            track_info.get("duration_ms", 0)
        )
        with state_lock:
            if state["current_track_id"] == target_id:
                state["current_lyrics"] = lyrics
        if not still_relevant():
            return
        # Artist info (heavier — multiple Spotify search calls + Last.fm)
        artist_info = fetch_artist_info(
            sp, primary_artist_id, first_artist,
            target_id, track_info["name"], user_top_ids,
            primary_genre=primary_genre,
            allow_spotify_resolution=False,
        )
        try:
            if _SQLITE_ENABLED:
                stats = _artist_stats_from_sqlite(_db.get_conn(), primary_artist_id, first_artist)
                if stats is not None:
                    try:
                        spotify_like_counts = get_liked_artist_counts(sp)
                        spotify_liked_count = int(spotify_like_counts.get(first_artist.strip().lower(), 0))
                    except Exception:
                        spotify_liked_count = 0
                    stats["spotify_liked_count"] = spotify_liked_count
                    stats["liked_count_total"] = max(int(stats.get("liked_count") or 0), spotify_liked_count)
                    artist_info["listen_stats"] = stats
        except Exception:
            pass
        with state_lock:
            if state["current_track_id"] == target_id:
                if not (state["current_track"] or {}).get("genres") and artist_info.get("lastfm_tags"):
                    state["current_track"]["genres"] = list(artist_info["lastfm_tags"][:6])
                state["current_artist_info"] = artist_info

    threading.Thread(target=worker, daemon=True).start()

# ---------- Playlist ops ----------
def spotify_retry_after_seconds(exc, default=30, minimum=30, maximum=300):
    retry_after = default
    try:
        if getattr(exc, "headers", None):
            retry_after = int(exc.headers.get("Retry-After", default))
    except Exception:
        retry_after = default
    return min(maximum, max(minimum, retry_after))

def _backlog_index():
    """The authoritative backlog membership index (src/services/backlog_index.py)."""
    from src.services import backlog_index
    return backlog_index.get_index()


def _backlog_engine():
    """The clearing engine: listening accounting, fate, removals, undo (src/services/clearing.py)."""
    from src.services import clearing
    return clearing.get_engine()


def refresh_playlist(sp=None, playlist_id=None, preloaded_meta=None):
    """Legacy entry point: ask the backlog index worker for a sync (it never pages inline).

    Returns the seconds until the next scheduled check, like the old implementation.
    """
    index = _backlog_index()
    if playlist_id and playlist_id != index.pid:
        index.switch(playlist_id)
    else:
        index.request_sync()
    return 60

def get_cached_playlist_list(max_age_s=120):
    with _playlist_list_lock:
        fetched_at = _playlist_list_cache["fetched_at"]
        if fetched_at and (time.time() - fetched_at) <= max_age_s:
            return {
                "playlists": list(_playlist_list_cache["playlists"]),
                "active_id": _playlist_list_cache["active_id"],
                "cached": True,
            }
    return None

def set_cached_playlist_list(playlists, active_id):
    with _playlist_list_lock:
        _playlist_list_cache["fetched_at"] = time.time()
        _playlist_list_cache["playlists"] = list(playlists)
        _playlist_list_cache["active_id"] = active_id

def get_cached_playlist_name(playlist_id):
    if not playlist_id:
        return None
    with _playlist_list_lock:
        for playlist in _playlist_list_cache["playlists"]:
            if playlist.get("id") == playlist_id:
                return playlist.get("name")
    return None

def _track_ids_from_track_list(tracks):
    ids = set()
    for track in (tracks or []):
        tid = (track or {}).get("id")
        if tid:
            ids.add(tid)
    return ids

def _known_playlist_track_ids():
    """Track ids confirmed to be in the active backlog playlist (read only from the index)."""
    return _backlog_index().ids_snapshot()

def _track_known_in_active_playlist(track_id):
    """True only when the index confirms membership (False while it is still syncing)."""
    return _backlog_index().contains(track_id) is True

def _client_allows_auto_remove(now=None):
    now = now if now is not None else time.time()
    with state_lock:
        visible = bool(state.get("client_visible", True))
        focused = bool(state.get("client_focused", True))
        seen_at = float(state.get("client_state_seen_at") or 0)
    return visible and focused and (now - seen_at) <= 30

def _remove_track_from_playlist_caches(playlist_id, track_id):
    """Apply a successful removal (one id or a list) to the index; it only decrements the
    total for ids that were actually present and snapshots the backlog. Returns the new
    total for the active playlist, else None."""
    ids = [track_id] if isinstance(track_id, str) else [t for t in (track_id or []) if t]
    index = _backlog_index()
    if not ids or playlist_id != index.pid:
        return None
    index.apply_removal(ids, playlist_id=playlist_id)
    return index.total

def remove_from_playlist(sp, track_id, playlist_id=None, raise_errors=False):
    """Remove every occurrence of track_id (or a list of up to 100 ids) in ONE Spotify call.

    The snapshot id Spotify returns for our own write is deliberately not adopted: the index
    marks itself dirty and re-validates with a metadata check instead.
    """
    pid = playlist_id or state["playlist_id"]
    ids = [track_id] if isinstance(track_id, str) else [t for t in (track_id or []) if t]
    if not ids:
        return False
    try:
        sp.playlist_remove_all_occurrences_of_items(pid, ids)
    except Exception as e:
        if raise_errors:
            raise
        with state_lock:
            state["last_error"] = f"Remove failed: {e}"
        return False
    try:
        from src.services import backlog_index as _bi
        _bi.invalidate_playlist_cache(sys.modules[__name__], pid)
    except Exception as e:
        _log_app_error(e, context="remove_from_playlist invalidate")
    _remove_track_from_playlist_caches(pid, ids)
    return True

def load_user_top_tracks(sp):
    try:
        r = sp.current_user_top_tracks(limit=10, time_range="medium_term")
        ids = [t["id"] for t in r.get("items", []) if t.get("id")]
        with state_lock:
            state["user_top_track_ids"] = ids
    except Exception:
        pass

def fetch_newly_liked(sp, limit=8):
    """Fetch most recently saved Spotify tracks."""
    try:
        r = sp.current_user_saved_tracks(limit=limit)
        items = []
        for it in r.get("items", []):
            t = playlist_entry_media(it) or {}
            if not t.get("id"):
                continue
            set_cached_liked_state(t["id"], True)
            items.append({
                "id": t["id"],
                "name": t["name"],
                "artists": ", ".join(a["name"] for a in t["artists"]),
                "image": small_image(t["album"].get("images")),
                "url": t["external_urls"].get("spotify"),
                "added_at": it.get("added_at"),
            })
        return items
    except Exception as e:
        with state_lock:
            state["last_error"] = f"Newly liked fetch: {e}"
        return []

def fetch_weekly_top(sp):
    """Last.fm 7day top tracks without automatic Spotify resolution on startup."""
    if not lastfm_api_key() or not lastfm_username():
        return []
    raw = lastfm_user_top_tracks(period="7day", limit=10)
    out = []
    for t in raw:
        if not t.get("artist") or not t.get("name"):
            continue
        out.append({
            "id": None, "name": t["name"], "artists": t["artist"],
            "image": None, "url": t.get("lastfm_url"),
            "playcount": t["playcount"], "lastfm_url": t.get("lastfm_url"),
        })
        if len(out) >= 8:
            break
    return out

def refresh_feeds(sp, force=False):
    """Refresh weekly top + scrobbles today, respecting cache TTLs."""
    now = time.time()
    # Weekly top: refresh hourly
    if force or now - state["weekly_top_fetched_at"] > 3600:
        weekly = fetch_weekly_top(sp)
        with state_lock:
            state["weekly_top"] = weekly
            state["weekly_top_fetched_at"] = now
    # Scrobbles today: refresh every 5 min
    if force or now - state["scrobbles_today_fetched_at"] > 300:
        n = lastfm_scrobbles_today()
        with state_lock:
            state["scrobbles_today"] = n
            state["scrobbles_today_fetched_at"] = now

def get_liked_library_profile(sp, force=False):
    now = time.time()
    with state_lock:
        cached = state.get("liked_library_profile")
        fetched_at = float(state.get("liked_library_profile_fetched_at") or 0)
    if cached and not force and (now - fetched_at) < LIKED_LIBRARY_PROFILE_TTL_S:
        return cached

    def publish_profile(profile):
        with state_lock:
            state["liked_library_profile"] = profile
            state["liked_library_profile_fetched_at"] = now
        save_analytics_state()

    try:
        tracks = []
        offset = 0
        full_scan = LIKED_LIBRARY_SAMPLE_LIMIT <= 0
        while full_scan or len(tracks) < LIKED_LIBRARY_SAMPLE_LIMIT:
            page_limit = 50 if full_scan else min(50, LIKED_LIBRARY_SAMPLE_LIMIT - len(tracks))
            res = sp.current_user_saved_tracks(limit=page_limit, offset=offset)
            items = (res or {}).get("items") or []
            if not items:
                break
            for item in items:
                track = playlist_entry_media(item) or {}
                if track.get("id"):
                    tracks.append(track)
            if not res.get("next") or len(items) < page_limit:
                break
            offset += len(items)

        artist_ids = []
        for track in tracks:
            for artist in track.get("artists", []):
                if artist.get("id"):
                    artist_ids.append(artist["id"])
        unique_artist_ids = list(dict.fromkeys(artist_ids))
        if SPOTIFY_EXTENDED_ENDPOINTS:
            ensure_artist_genre_cache(sp, unique_artist_ids)

        genre_counts = Counter()
        matched_tracks = 0
        lfm_artist_tags = {}
        for idx, track in enumerate(tracks, start=1):
            ids = [artist.get("id") for artist in track.get("artists", []) if artist.get("id")]
            genres = (
                fetch_genres_for_artists(sp, ids)
                if SPOTIFY_EXTENDED_ENDPOINTS
                else get_cached_genres_for_artists(ids)
            )
            genres = normalize_genres(genres, limit=6)
            if not genres:
                primary_artist = ((track.get("artists") or [{}])[0].get("name") or "").strip()
                if primary_artist:
                    if primary_artist not in lfm_artist_tags:
                        lfm_artist_tags[primary_artist] = normalize_genres(
                            lastfm_artist_top_tags(primary_artist, limit=4),
                            limit=4,
                        )
                    genres = list(lfm_artist_tags.get(primary_artist) or [])
            if not genres:
                continue
            matched_tracks += 1
            for genre in genres:
                genre_counts[genre] += 1
            if idx % 25 == 0:
                publish_profile({
                    "sample_size": len(tracks),
                    "processed_tracks": idx,
                    "is_full_scan": full_scan,
                    "tracks_with_genres": matched_tracks,
                    "genres": [{"name": name, "count": count} for name, count in genre_counts.most_common(10)],
                    "updated_at": current_ts_ms(),
                    "loading": True,
                })
    except Exception:
        if cached:
            return cached
        raise

    profile = {
        "sample_size": len(tracks),
        "is_full_scan": LIKED_LIBRARY_SAMPLE_LIMIT <= 0,
        "tracks_with_genres": matched_tracks,
        "genres": [{"name": name, "count": count} for name, count in genre_counts.most_common(10)],
        "updated_at": current_ts_ms(),
    }
    publish_profile(profile)
    return profile

def ensure_liked_library_profile_async(sp, force=False):
    now = time.time()
    with state_lock:
        cached = state.get("liked_library_profile")
        fetched_at = float(state.get("liked_library_profile_fetched_at") or 0)
        refreshing = bool(state.get("liked_library_profile_refreshing"))
        if cached and not force and (now - fetched_at) < LIKED_LIBRARY_PROFILE_TTL_S:
            return cached
        if refreshing:
            return cached or {
                "sample_size": 0,
                "tracks_with_genres": 0,
                "genres": [],
                "loading": True,
            }
        state["liked_library_profile_refreshing"] = True

    def worker():
        try:
            get_liked_library_profile(sp, force=True)
        finally:
            with state_lock:
                state["liked_library_profile_refreshing"] = False

    threading.Thread(target=worker, daemon=True).start()
    if cached:
        stale = dict(cached)
        stale["loading"] = True
        return stale
    return {
        "sample_size": 0,
        "tracks_with_genres": 0,
        "genres": [],
        "loading": True,
    }

def day_key_for(dt):
    return dt.strftime("%Y-%m-%d")

def build_recent_day_series(history, backlog_history, playlist_total):
    weekly_inflow = current_discover_weekly_inflow()
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in reversed(range(ANALYTICS_WINDOW_DAYS))]
    rows = []
    by_day = {}
    listened_ids = {}
    removed_ids = {}
    liked_ids = {}
    added_ids = {}
    for dt in days:
        key = day_key_for(dt)
        row = {
            "day": key,
            "label": dt.strftime("%a"),
            "short_label": dt.strftime("%m/%d"),
            "listened": 0,
            "listened_seconds": 0.0,
            "liked": 0,
            "removed": 0,
            "added": 0,
            "backlog": None,
            "backlog_estimated": True,
        }
        rows.append(row)
        by_day[key] = row
        listened_ids[key] = set()
        removed_ids[key] = set()
        liked_ids[key] = set()
        added_ids[key] = set()

    for event in history:
        row = by_day.get(event.get("day"))
        if not row:
            continue
        event_type = event.get("type")
        track_key = (event.get("track_id") or "").strip() or ((event.get("track_name") or "").strip().lower())
        reason_key = (event.get("reason") or "").strip().lower()
        if event_type == "listen":
            listened_ids[row["day"]].add(event.get("track_id") or f"listen:{event.get('ts_ms')}")
            row["listened_seconds"] += float(event.get("listened_s") or 0)
        elif event_type == "liked":
            liked_ids[row["day"]].add(track_key or f"liked:{event.get('ts_ms')}")
        elif event_type == "removed":
            removed_ids[row["day"]].add((track_key or f"removed:{event.get('ts_ms')}", reason_key))
        elif event_type == "added":
            added_ids[row["day"]].add((track_key or f"added:{event.get('ts_ms')}", (event.get("target") or "").strip().lower()))

    for row in rows:
        row["listened"] = len(listened_ids[row["day"]])
        row["liked"] = len(liked_ids[row["day"]])
        row["removed"] = len(removed_ids[row["day"]])
        row["added"] = len(added_ids[row["day"]])
        row["listened_minutes"] = round(row["listened_seconds"] / 60.0, 1)

    actual_backlog = {
        item.get("day"): int(item.get("total", 0))
        for item in backlog_history
        if item.get("day") in by_day
    }
    if rows:
        rows[-1]["backlog"] = actual_backlog.get(rows[-1]["day"], int(playlist_total or 0))
        rows[-1]["backlog_estimated"] = rows[-1]["day"] not in actual_backlog
        for idx in range(len(rows) - 2, -1, -1):
            next_row = rows[idx + 1]
            row = rows[idx]
            if row["day"] in actual_backlog:
                row["backlog"] = actual_backlog[row["day"]]
                row["backlog_estimated"] = False
                continue
            inflow_on_next_day = weekly_inflow if datetime.strptime(next_row["day"], "%Y-%m-%d").weekday() == 0 else 0
            row["backlog"] = max(0, int(next_row["backlog"] or 0) - inflow_on_next_day + int(next_row["removed"] or 0))
            row["backlog_estimated"] = True
    return rows


def build_listening_heatmap(history, days=35):
    today = datetime.now().date()
    dates = [today - timedelta(days=i) for i in reversed(range(days))]
    day_sets = {}
    minutes_by_day = {}
    for dt in dates:
        key = day_key_for(dt)
        day_sets[key] = set()
        minutes_by_day[key] = 0.0

    cutoff_ms = current_ts_ms() - (days * 24 * 3600 * 1000)
    for event in history:
        if event.get("type") != "listen":
            continue
        ts_ms = int(event.get("ts_ms") or 0)
        if ts_ms < cutoff_ms:
            continue
        day = event.get("day")
        if day not in day_sets:
            continue
        track_key = (event.get("track_id") or "").strip() or ((event.get("track_name") or "").strip().lower())
        day_sets[day].add(track_key or f"listen:{ts_ms}")
        minutes_by_day[day] += float(event.get("listened_s") or 0) / 60.0

    rows = []
    max_count = 0
    active_days = 0
    hottest_day = None
    hottest_count = 0
    for dt in dates:
        key = day_key_for(dt)
        count = len(day_sets[key])
        minutes = round(minutes_by_day[key], 1)
        if count > 0:
            active_days += 1
        if count > hottest_count:
            hottest_count = count
            hottest_day = key
        max_count = max(max_count, count)
        rows.append({
            "day": key,
            "short_label": dt.strftime("%m/%d"),
            "date_num": dt.day,
            "weekday": dt.weekday(),
            "month_label": dt.strftime("%b") if dt.day == 1 or dt == dates[0] else "",
            "listened": count,
            "minutes": minutes,
        })

    return {
        "days": rows,
        "max_count": max_count,
        "active_days": active_days,
        "hottest_day": hottest_day,
        "hottest_day_label": datetime.strptime(hottest_day, "%Y-%m-%d").strftime("%b %d") if hottest_day else None,
        "hottest_day_count": hottest_count,
    }

def aggregate_genre_scores(history, event_types, days, max_genres_per_event=8):
    """Count genre occurrences across history events.

    max_genres_per_event=1 counts only the primary genre per listen, preventing
    an artist with many genre tags (e.g. Dance Gavin Dance's 5 tags) from
    inflating every tag equally on each listen.
    """
    cutoff_ms = current_ts_ms() - (days * 24 * 3600 * 1000)
    counts = Counter()
    listened_seconds = Counter()
    for event in history:
        if event.get("type") not in event_types or int(event.get("ts_ms") or 0) < cutoff_ms:
            continue
        genres = normalize_genres(event.get("genres"), limit=max_genres_per_event)
        if not genres:
            continue
        for genre in genres:
            counts[genre] += 1
            listened_seconds[genre] += float(event.get("listened_s") or 0)
    return [
        {
            "name": name,
            "count": count,
            "minutes": round(listened_seconds[name] / 60.0, 1),
        }
        for name, count in counts.most_common(8)
    ]

def build_recent_listen_mix(history, days=7, limit=8):
    cutoff_ms = current_ts_ms() - (days * 24 * 3600 * 1000)
    recent = []
    seen = set()
    for event in sorted(history, key=lambda item: int(item.get("ts_ms") or 0), reverse=True):
        if event.get("type") != "listen" or int(event.get("ts_ms") or 0) < cutoff_ms:
            continue
        track_id = event.get("track_id") or f"listen:{event.get('ts_ms')}"
        if track_id in seen:
            continue
        seen.add(track_id)
        genres = normalize_genres(event.get("genres"), limit=4)
        recent.append({
            "track_id": event.get("track_id"),
            "track_name": event.get("track_name") or "",
            "artists": event.get("artists") or "",
            "image": event.get("image"),
            "url": event.get("url"),
            "genre": genres[0] if genres else None,
            "genres": genres,
            "listened_s": round(float(event.get("listened_s") or 0), 1),
            "ts_ms": int(event.get("ts_ms") or 0),
        })
        if len(recent) >= limit:
            break
    return recent

def build_timeline(history, current_track, current_listened):
    today = current_day_str()
    events = []
    for event in history:
        if event.get("day") != today or event.get("type") not in {"listen", "liked", "removed", "added"}:
            continue
        events.append({
            "type": event.get("type"),
            "ts_ms": int(event.get("ts_ms") or 0),
            "track_name": event.get("track_name") or "",
            "artists": event.get("artists") or "",
            "image": event.get("image"),
            "url": event.get("url"),
            "reason": event.get("reason"),
            "target": event.get("target"),
            "listened_s": event.get("listened_s"),
        })
    if current_track and current_listened >= 1:
        events.append({
            "type": "now",
            "ts_ms": current_ts_ms(),
            "track_name": current_track.get("name") or "",
            "artists": current_track.get("artists") or "",
            "image": current_track.get("image"),
            "url": current_track.get("url"),
            "reason": "current",
            "target": None,
            "listened_s": round(float(current_listened), 1),
        })
    events.sort(key=lambda item: item["ts_ms"], reverse=True)
    return events[:18]

def build_analytics_payload(sp):
    liked_library = ensure_liked_library_profile_async(sp)
    weekly_inflow = current_discover_weekly_inflow()
    with state_lock:
        history = list(state["event_history"])
        backlog_history = list(state["backlog_history"])
        stats = dict(state["stats"])
        playlist_total = int(state.get("playlist_total") or 0)
        current_track = dict(state["current_track"]) if state.get("current_track") else None
        current_listened = float(state.get("current_listened") or 0)
    series = build_recent_day_series(history, backlog_history, playlist_total)
    today_key = current_day_str()
    today_row = next((row for row in series if row["day"] == today_key), None) or {
        "listened": 0, "liked": 0, "removed": 0, "added": 0
    }
    has_today_events = any(event.get("day") == today_key for event in history)

    listened_today = int(today_row.get("listened") or 0)
    current_track_already_counted = False
    if current_track and current_track.get("id"):
        current_track_already_counted = any(
            event.get("day") == today_key
            and event.get("type") == "listen"
            and event.get("track_id") == current_track.get("id")
            for event in history
        )
    if current_track and current_listened >= LISTEN_EVENT_MIN_SECONDS and not current_track_already_counted:
        listened_today += 1
    removed_today = int(today_row.get("removed") or 0) if has_today_events else int(stats.get("removed_today") or 0)
    liked_today = int(today_row.get("liked") or 0) if has_today_events else int(stats.get("liked_today") or 0)
    added_today = int(today_row.get("added") or 0) if has_today_events else int(stats.get("added_today") or 0)

    liked_last_14 = sum(int(row.get("liked") or 0) for row in series)
    trailing_week = series[-7:] if len(series) >= 7 else series
    removed_last_7 = sum(int(row.get("removed") or 0) for row in trailing_week)
    liked_last_7 = sum(int(row.get("liked") or 0) for row in trailing_week)
    listened_last_7 = sum(int(row.get("listened") or 0) for row in trailing_week)
    added_last_7 = sum(int(row.get("added") or 0) for row in trailing_week)
    listened_events_last_7 = [
        event for event in history
        if event.get("type") == "listen"
        and int(event.get("ts_ms") or 0) >= current_ts_ms() - (7 * 24 * 3600 * 1000)
    ]
    avg_listen_s = round(
        sum(float(event.get("listened_s") or 0) for event in listened_events_last_7) / max(1, len(listened_events_last_7)),
        1,
    ) if listened_events_last_7 else 0.0
    removed_per_day = removed_last_7 / 7.0 if trailing_week else 0.0
    break_even_per_day = weekly_inflow / 7.0
    net_weekly = removed_last_7 - weekly_inflow
    runway_days = None
    if removed_per_day > break_even_per_day:
        runway_days = round(playlist_total / (removed_per_day - break_even_per_day), 1)

    # Only count primary genre per listen so one artist's tag-set doesn't dominate
    listened_genres = aggregate_genre_scores(history, {"listen"}, days=14, max_genres_per_event=1)
    liked_genres = aggregate_genre_scores(history, {"liked"}, days=30)
    recent_listens = build_recent_listen_mix(history, days=7, limit=8)
    keep_actions_last_7 = liked_last_7 + added_last_7
    keep_rate = round((keep_actions_last_7 / max(1, listened_last_7)) * 100.0, 1) if listened_last_7 else 0.0
    clear_rate = round((min(removed_last_7, listened_last_7) / max(1, listened_last_7)) * 100.0, 1) if listened_last_7 else 0.0

    def _event_track_key(event):
        return (event.get("track_id") or "").strip() or (event.get("track_name") or "").strip().lower()

    def _event_artist_key(event):
        artists = (event.get("artists") or "").strip()
        primary = artists.split(",")[0].strip() if artists else ""
        return primary.lower()

    def _event_album_key(event):
        return ((event.get("album") or "").strip()).lower()

    unique_tracks_7 = len({k for k in (_event_track_key(e) for e in listened_events_last_7) if k})
    unique_artists_7 = len({k for k in (_event_artist_key(e) for e in listened_events_last_7) if k})
    unique_albums_7 = len({k for k in (_event_album_key(e) for e in listened_events_last_7) if k})

    listened_events_last_30 = [
        event for event in history
        if event.get("type") == "listen"
        and int(event.get("ts_ms") or 0) >= current_ts_ms() - (30 * 24 * 3600 * 1000)
    ]
    artist_rollup = {}
    song_rollup = {}
    for event in listened_events_last_30:
        ts_ms = int(event.get("ts_ms") or 0)
        artist_name = ((event.get("artists") or "").split(",")[0]).strip()
        artist_key = artist_name.lower() if artist_name else ""
        track_name = (event.get("track_name") or "").strip()
        track_id = (event.get("track_id") or "").strip()
        song_key = track_id or (f"{track_name.lower()}|{artist_key}" if track_name and artist_key else "")
        if artist_key:
            cached_artist = _sp_artist_cache.get(artist_key) or {}
            entry = artist_rollup.setdefault(artist_key, {
                "name": artist_name,
                "listens": 0,
                "image": cached_artist.get("image") or event.get("image"),
                "url": cached_artist.get("url") or event.get("url"),
                "latest_ts": 0,
                "tracks": Counter(),
                "track_meta": {},
            })
            entry["listens"] += 1
            entry["latest_ts"] = max(entry["latest_ts"], ts_ms)
            if not entry.get("image") and event.get("image"):
                entry["image"] = event.get("image")
            if not entry.get("url") and cached_artist.get("url"):
                entry["url"] = cached_artist.get("url")
            if track_name:
                track_meta = entry["track_meta"].setdefault(track_name, {
                    "name": track_name,
                    "image": event.get("image"),
                    "url": event.get("url"),
                    "artists": event.get("artists") or "",
                })
                if not track_meta.get("image") and event.get("image"):
                    track_meta["image"] = event.get("image")
                if not track_meta.get("url") and event.get("url"):
                    track_meta["url"] = event.get("url")
                entry["tracks"][track_name] += 1
        if song_key:
            song_entry = song_rollup.setdefault(song_key, {
                "name": track_name,
                "artists": event.get("artists") or "",
                "listens": 0,
                "image": event.get("image"),
                "url": event.get("url"),
                "latest_ts": 0,
            })
            song_entry["listens"] += 1
            song_entry["latest_ts"] = max(song_entry["latest_ts"], ts_ms)
            if not song_entry.get("image") and event.get("image"):
                song_entry["image"] = event.get("image")
            if not song_entry.get("url") and event.get("url"):
                song_entry["url"] = event.get("url")
    favorite_artist = None
    if artist_rollup:
        artist_entry = max(artist_rollup.values(), key=lambda item: (item["listens"], item["latest_ts"]))
        favorite_artist = {
            "name": artist_entry["name"],
            "listens": artist_entry["listens"],
            "image": artist_entry.get("image"),
            "url": artist_entry.get("url"),
            "top_tracks": [
                {
                    "name": name,
                    "listens": count,
                    "image": artist_entry["track_meta"].get(name, {}).get("image"),
                    "url": artist_entry["track_meta"].get(name, {}).get("url"),
                    "artists": artist_entry["track_meta"].get(name, {}).get("artists", artist_entry["name"]),
                }
                for name, count in artist_entry["tracks"].most_common(5)
            ],
        }
    favorite_song = None
    if song_rollup:
        ranked_songs = sorted(song_rollup.values(), key=lambda item: (item["listens"], item["latest_ts"]), reverse=True)
        champ = ranked_songs[0]
        favorite_song = {
            "name": champ["name"],
            "artists": champ["artists"],
            "listens": champ["listens"],
            "image": champ.get("image"),
            "url": champ.get("url"),
            "runners_up": [
                {
                    "name": item["name"],
                    "artists": item["artists"],
                    "listens": item["listens"],
                    "image": item.get("image"),
                    "url": item.get("url"),
                }
                for item in ranked_songs[1:5]
            ],
        }
    listening_clock = [0] * 24
    for event in listened_events_last_30:
        try:
            hr = datetime.fromtimestamp(int(event.get("ts_ms") or 0) / 1000).hour
            listening_clock[hr] += 1
        except Exception:
            pass
    busiest_hour = None
    busiest_hour_count = 0
    if any(listening_clock):
        busiest_hour = max(range(24), key=lambda idx: listening_clock[idx])
        busiest_hour_count = listening_clock[busiest_hour]
    active_days_14 = sum(1 for row in series if int(row.get("listened") or 0) > 0)
    current_streak_days = 0
    for row in reversed(series):
        if int(row.get("listened") or 0) > 0:
            current_streak_days += 1
        else:
            break
    replay_ratio_7 = round(max(0.0, (1.0 - (unique_tracks_7 / max(1, listened_last_7)))) * 100.0, 1) if listened_last_7 else 0.0
    total_listen_minutes_7 = round(
        sum(float(event.get("listened_s") or 0) for event in listened_events_last_7) / 60.0,
        1,
    ) if listened_events_last_7 else 0.0
    listening_heatmap_35 = build_listening_heatmap(history, days=35)

    return {
        "summary": {
            "playlist_total": playlist_total,
            "discover_weekly_inflow": weekly_inflow,
            "listened_today": listened_today,
            "liked_today": liked_today,
            "removed_today": removed_today,
            "added_today": added_today,
            "listened_last_7": listened_last_7,
            "liked_last_7": liked_last_7,
            "liked_last_14": liked_last_14,
            "removed_last_7": removed_last_7,
            "added_last_7": added_last_7,
            "removed_per_day": round(removed_per_day, 1),
            "break_even_per_day": round(break_even_per_day, 1),
            "net_weekly": net_weekly,
            "runway_days": runway_days,
            "keep_rate": keep_rate,
            "clear_rate": clear_rate,
            "avg_listen_s": avg_listen_s,
        },
        "series": series,
        "genres": {
            "listened": listened_genres,
            "liked": liked_genres,
            "library": liked_library,
            "recent_tracks": recent_listens,
        },
        "profile": {
            "unique_tracks_7": unique_tracks_7,
            "unique_artists_7": unique_artists_7,
            "unique_albums_7": unique_albums_7,
            "listening_clock": listening_clock,
            "busiest_hour": busiest_hour,
            "busiest_hour_count": busiest_hour_count,
            "listen_minutes_7": total_listen_minutes_7,
            "active_days_14": active_days_14,
            "current_streak_days": current_streak_days,
            "replay_ratio_7": replay_ratio_7,
            "favorite_artist": favorite_artist,
            "favorite_song": favorite_song,
            "listening_heatmap_35": listening_heatmap_35,
        },
        "timeline": build_timeline(history, current_track, current_listened),
    }

# ---------- Background polling loop ----------
def _record_finished_play(play):
    """Log a finished play: the legacy recently-played list plus one 'listen' history event."""
    if play is None:
        return
    listened = round(float(play.listened_s or 0.0), 1)
    track = play.track
    with state_lock:
        current = state.get("current_track")
        if current and current.get("id") == play.track_id:
            track = dict(current)  # carries the genres enrichment filled in meanwhile
        if track and listened >= 1:
            state["recently_played"].appendleft({
                "track": track,
                "listened": listened,
                "was_in_playlist": bool(play.started_in_backlog),
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
    if not track or (listened < LISTEN_EVENT_MIN_SECONDS and not play.auto_removed):
        return
    record_history_event(
        "listen",
        track=track,
        listened_s=listened,
        source="playback",
        extra={
            "started_ms": play.started_ms,
            "started_in_backlog": play.started_in_backlog,
            "was_in_playlist": bool(play.started_in_backlog),
            "auto_removed": bool(play.auto_removed),
            "duration_ms": play.duration_ms or None,
        },
    )


def _flush_current_play_at_exit():
    """atexit: the play in progress when the app stops is still a play."""
    try:
        _record_finished_play(_backlog_engine().tracker.flush())
    except Exception:
        pass


def _poll_error(message, source, **extra):
    with state_lock:
        state["last_error"] = message
        state["last_error_info"] = {"message": message, "source": source, "ts_ms": current_ts_ms(), **extra}


def poll_loop():
    import atexit as _atexit
    last_track_id = None
    last_feeds_kick = 0
    last_queue_count_refresh = 0
    waiting_logged = False
    liked_unknown_tid = None
    engine = _backlog_engine()
    index = _backlog_index()
    tracker = engine.tracker
    core = sys.modules[__name__]
    _atexit.register(_flush_current_play_at_exit)

    while True:
        tracker.poll_interval_s = POLL_INTERVAL
        wait_fate = None
        app_reg_wait = _spotify_app_reg_block_remaining_s()
        if app_reg_wait > 0:
            with state_lock:
                state["auth_ready"] = False
            _poll_error(
                f"{spotify_app_access_message(_SPOTIFY_APP_REG_BLOCK_MSG)} "
                f"Quieting API calls for {app_reg_wait}s.", "spotify_app_registration")
            engine.wait(max(POLL_INTERVAL, min(30, app_reg_wait)))
            continue

        guard_wait = _spotify_guard_remaining_s()
        if guard_wait > 0:
            # A genuine 429 from Spotify locked all traffic: make no calls until it expires.
            _poll_error(f"Spotify rate limit: no Spotify calls for {guard_wait}s.", "spotify_rate_limit",
                        retry_after_s=guard_wait)
            engine.wait(max(POLL_INTERVAL, min(30, guard_wait)))
            continue

        sp = get_sp()
        if not sp:
            if not waiting_logged:
                with state_lock:
                    state["auth_ready"] = False
                _poll_error("Not authenticated. Visit http://127.0.0.1:8888 to log in.", "auth")
                waiting_logged = True
            engine.wait(max(2.0, POLL_INTERVAL))
            continue

        if waiting_logged:
            with state_lock:
                state["auth_ready"] = True
                state["last_error"] = None
            waiting_logged = False
            load_user_top_tracks(sp)
            # Kick off feeds in a background thread so we don't block first poll
            threading.Thread(target=refresh_feeds, args=(sp,), kwargs={"force": True},
                             daemon=True).start()
            last_feeds_kick = time.time()  # or the periodic kick below fires a second refresh at once

        try:
            cp = sp.current_playback()
            # Accounting uses the moment Spotify answered, not when this cycle's other work ends.
            now_mono = time.monotonic()
            now_ms = current_ts_ms()
            if time.time() - last_queue_count_refresh > 20:
                try:
                    queue_count = get_spotify_queue_count(sp)
                    with state_lock:
                        state["spotify_queue_count"] = queue_count
                except Exception:
                    pass
                last_queue_count_refresh = time.time()

            with state_lock:
                state["last_poll"] = datetime.now().strftime("%H:%M:%S")
                state["last_poll_mono"] = now_mono
                state["auth_ready"] = True
                # A successful poll clears the previous poll's error.
                state["last_error"] = None
                state["last_error_info"] = None

            item = (cp or {}).get("item") or {}
            has_track = bool(item.get("type") == "track" and item.get("id"))
            if time.time() - last_feeds_kick > 60:
                # refresh_feeds is internally TTL-aware; the kick is just frequency
                threading.Thread(target=refresh_feeds, args=(sp,), daemon=True).start()
                last_feeds_kick = time.time()

            if has_track:
                track = item
                tid = track["id"]
                is_playing = bool(cp.get("is_playing", False))
                progress_ms = int(cp.get("progress_ms") or 0)
                duration_ms = int(track.get("duration_ms") or 0)
                artists = [a for a in (track.get("artists") or []) if isinstance(a, dict)]
                artist_ids = [a["id"] for a in artists if a.get("id")]
                primary_artist_id = artist_ids[0] if artist_ids else None
                album = track.get("album") or {}

                track_changed = (tid != last_track_id)
                recheck_like = False
                if track_changed:
                    genres = fetch_genres_for_artists(sp, artist_ids)
                    liked = is_track_liked(sp, tid)
                elif liked_unknown_tid == tid:
                    # The like lookup failed earlier (guard/network): retry instead of trusting False.
                    with state_lock:
                        genres = (state["current_track"] or {}).get("genres", [])
                    liked = is_track_liked(sp, tid)
                    recheck_like = True
                else:
                    with state_lock:
                        genres = (state["current_track"] or {}).get("genres", [])
                        liked = state["current_is_liked"]
                if liked is None:
                    liked_unknown_tid = tid
                    liked = bool(get_cached_liked_state(tid))
                elif liked_unknown_tid == tid:
                    liked_unknown_tid = None

                track_info = {
                    "id": tid,
                    "uri": track.get("uri") or f"spotify:track:{tid}",
                    "name": track.get("name") or "",
                    "artists": ", ".join(a.get("name") or "" for a in artists),
                    "artist_list": [{"id": a["id"], "name": a.get("name")}
                                    for a in artists if a.get("id")],
                    "album": album.get("name") or "",
                    "album_id": album.get("id"),
                    "image": ((album.get("images") or [{}])[0].get("url")
                              if album.get("images") else None),
                    "url": (track.get("external_urls") or {}).get("spotify"),
                    "duration_ms": duration_ms,
                    "genres": genres,
                }

                membership = index.contains(tid)
                # What auto-clear may trust (unknown while the index is an unconfirmed copy).
                play_membership = index.clearable(tid)
                context_uri = str(((cp.get("context") or {}).get("uri")) or "")
                if membership is not True and index.pid and context_uri.endswith(":" + index.pid):
                    # Playing from the backlog itself but unknown to the index: it was probably
                    # added elsewhere, so check the playlist now instead of in up to a minute.
                    index.hint_stale(tid)
                    # Membership at play start decides auto-clear. "Not in the backlog" only
                    # counts once the index was confirmed after this play began; before that the
                    # track may simply have been added just before it started playing.
                    if not index.confirmed_since(now_ms - progress_ms):
                        play_membership = None
                finished, started = tracker.observe(
                    now_mono, now_ms, tid, is_playing, progress_ms, duration_ms,
                    membership=play_membership, track=track_info)
                play = tracker.play
                if started:
                    engine.on_play_started(play)

                with state_lock:
                    if track_changed:
                        state["current_track"] = track_info
                        state["current_track_id"] = tid
                        state["current_is_liked"] = bool(liked)
                        state["current_artist_info"] = None  # will be filled in by enrichment
                        state["current_lyrics"] = None
                        last_track_id = tid
                    elif recheck_like:
                        state["current_is_liked"] = bool(liked)
                    state["current_listened"] = round(play.total_listened_s, 1)
                    state["current_in_playlist"] = membership is True
                    state["is_playing"] = is_playing
                    state["progress_ms"] = progress_ms
                    state["duration_ms"] = duration_ms
                    state["current_play"] = play.as_state()
                    threshold_s = state["threshold_seconds"]
                    user_top = list(state["user_top_track_ids"])

                if finished is not None:
                    _record_finished_play(finished)

                # Session heartbeat — fires every poll cycle while playing
                if _SQLITE_ENABLED and is_playing:
                    try:
                        _sessions.heartbeat(tid)
                    except Exception:
                        pass
                elif _SQLITE_ENABLED and not is_playing:
                    try:
                        _sessions.close_if_stale()
                    except Exception:
                        pass

                if track_changed and primary_artist_id:
                    enrich_in_background(sp, track_info, primary_artist_id, user_top)

                from src.services.clearing import effective_threshold
                fate = engine.fate(tid, threshold_s=threshold_s, duration_ms=duration_ms,
                                   listened_s=play.total_listened_s)
                # Once per play: auto_clear marks the play cleared, or queues the removal. Only a
                # track that was already in the backlog when this play started is cleared (one
                # added mid-play clears next time), and a queued/refused removal isn't re-sent.
                if (fate["state"] == "in_backlog" and engine.auto_clear_allowed(play)
                        and play.total_listened_s >= effective_threshold(threshold_s, duration_ms)):
                    engine.auto_clear(core, sp, play, track_info, threshold_s)
                    with state_lock:
                        state["current_in_playlist"] = index.contains(tid) is True
                        state["current_play"] = play.as_state()
                    fate = engine.fate(tid, threshold_s=threshold_s, duration_ms=duration_ms,
                                       listened_s=play.total_listened_s)
                wait_fate = fate
            else:
                finished = tracker.idle(time.monotonic())
                if finished is not None:
                    _record_finished_play(finished)
                with state_lock:
                    state["is_playing"] = False
                    if tracker.play is not None:
                        state["current_play"] = tracker.play.as_state()
                if _SQLITE_ENABLED:
                    try:
                        _sessions.close_if_stale()
                    except Exception:
                        pass

            # Removals that failed earlier (rate limit, network) go out as soon as allowed.
            engine.drain_pending(core, sp)
        except SpotifyGuardBlocked as e:
            # Our own guard said "not yet" (budget or protect mode). No request reached Spotify,
            # so this is pacing, not a 429: wait it out calmly without the global lock.
            wait_s = min(30.0, max(float(POLL_INTERVAL), float(getattr(e, "retry_after", None) or 0)))
            _poll_error(f"Pacing Spotify calls; next check in {int(round(wait_s))}s.", "guard",
                        retry_after_s=wait_s)
            engine.wait(wait_s)
            continue
        except spotipy.SpotifyException as e:
            _poll_error(f"Spotify API: {e}", "spotify", http_status=e.http_status)
            if e.http_status == 401:
                waiting_logged = False
            elif e.http_status == 429:
                # A genuine 429: the guard now blocks all Spotify traffic until Retry-After.
                retry_after = spotify_retry_after_seconds(e, minimum=1, maximum=86400)
                _poll_error(f"Spotify rate-limited (429). All Spotify calls paused for {retry_after}s.",
                            "spotify_rate_limit", http_status=429, retry_after_s=retry_after)
                # The guard's global lock (checked at the top of the loop) covers the rest of
                # Retry-After; this wait also stops a spin if no lock was recorded.
                engine.wait(max(float(POLL_INTERVAL), min(30.0, float(retry_after))))
                continue
            else:
                # 500-class errors — wait an extra beat before next poll
                engine.wait(max(float(POLL_INTERVAL), 5.0))
                continue
        except Exception as e:
            _log_app_error(e, context="poll_loop")
            _poll_error(f"Poll error: {e}", "poll")

        # Adaptive wait: faster while playing, slower when paused/idle; routes that change
        # playback (and the threshold approaching) cut it short.
        with state_lock:
            playing = state["is_playing"]
        timeout = POLL_INTERVAL if playing else POLL_INTERVAL_PAUSED
        timeout = engine.next_wait(timeout, wait_fate, playing)
        engine.wait(max(0.5, timeout))

# ---------- Flask app ----------
app = Flask(__name__)


@app.template_global()
def asset_url(rel_path):
    """URL for a file under static/, cache-busted by its modification time."""
    p = Path(app.static_folder) / rel_path
    try:
        v = int(p.stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{rel_path}?v={v}"


class BadRequestBody(ValueError):
    """Raised by _json_body() when a request body is not a JSON object."""


def _json_body():
    """Parse the request body as a JSON object ({} when empty).

    Replaces get_json(force=True), which turned malformed bodies into HTML 400 pages and
    accepted non-object JSON. Raises BadRequestBody (mapped to a 400 JSON error) otherwise.
    """
    if not request.data:
        return {}
    data = request.get_json(silent=True, force=True)
    if not isinstance(data, dict):
        raise BadRequestBody("Request body must be a JSON object.")
    return data


@app.errorhandler(BadRequestBody)
def _handle_bad_request_body(exc):
    return jsonify({"error": {"code": "bad_request", "message": str(exc)}}), 400


# ---------- Request hardening ----------
# The server only listens on 127.0.0.1, but any web page the user visits can still reach
# it: through DNS rebinding (a hostile name resolved to 127.0.0.1 arrives with a foreign
# Host header) or with cross-site "simple" POSTs (forms, text/plain fetches: no CORS
# preflight). So every request must name a loopback host, and state-changing /api/* calls
# must come from this very origin (compared with request.host, so any port works).
_ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _api_error_response(code, message, status, **extra):
    body = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return jsonify(body), status


def _request_hostname(host):
    host = (host or "").strip().lower()
    if host.startswith("["):  # [::1]:8888
        end = host.find("]")
        return host[1:end] if end > 0 else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _same_origin_url(value):
    try:
        parts = urllib.parse.urlsplit(value or "")
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.netloc.lower() == (request.host or "").lower()


@app.before_request
def _guard_request_origin():
    if _request_hostname(request.host) not in _ALLOWED_HOSTNAMES:
        if request.path.startswith("/api/"):
            return _api_error_response("forbidden_host", "This app only answers on 127.0.0.1 or localhost.", 403)
        return "Forbidden: this app only answers on 127.0.0.1 or localhost.", 403, {"Content-Type": "text/plain; charset=utf-8"}
    if request.method in _SAFE_METHODS or not request.path.startswith("/api/"):
        return None
    origin = request.headers.get("Origin")
    if origin is not None:
        allowed = _same_origin_url(origin)  # also rejects the opaque "null" origin
    else:
        # Browsers send Origin on every cross-site POST; fall back to Referer for the odd
        # same-origin one without it. No Origin and no Referer = not a browser (curl, tests).
        referer = request.headers.get("Referer")
        allowed = referer is None or _same_origin_url(referer)
    if not allowed:
        return _api_error_response("forbidden_origin", "Cross-site requests are not allowed.", 403)
    return None


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(exc):
    """/api/* errors are always JSON {error:{code,message}}: HTTP errors keep their status,
    Spotify guard/rate-limit errors become 429/403, anything else is a logged 500."""
    from werkzeug.exceptions import HTTPException, InternalServerError
    is_api = request.path.startswith("/api/")
    if isinstance(exc, HTTPException):
        if not is_api or exc.code is None or 300 <= exc.code < 400:
            return exc
        code = re.sub(r"[^a-z0-9]+", "_", (exc.name or "error").lower()).strip("_") or "error"
        return _api_error_response(code, exc.description or exc.name or "Request failed.", exc.code)
    if is_api and isinstance(exc, SpotifyAppRegistrationBlocked):
        return _api_error_response("app_not_registered", spotify_app_access_message(str(exc)), 403,
                                   retry_after=int(exc.retry_after or 0), local=True)
    if is_api and isinstance(exc, spotipy.SpotifyException) and getattr(exc, "http_status", None) == 429:
        retry_after = spotify_retry_after_from(exc, default=30)
        return _api_error_response("rate_limited",
                                   f"Spotify is paused for about {retry_after}s to stay within its rate limits.",
                                   429, retry_after=retry_after, local=is_local_guard_block(exc))
    _log_app_error(exc, context=f"{request.method} {request.path}")
    if is_api:
        detail = str(exc)[:200]
        return _api_error_response(
            "internal_error",
            f"Unexpected server error ({type(exc).__name__}{': ' + detail if detail else ''}).",
            500,
        )
    return InternalServerError(original_exception=exc)


# ---------- Extension points (used by src/web/* blueprints and src/services/*) ----------
# Functions returning a dict merged into the /api/status payload. Each one must be cheap
# (it runs on every status poll) and must never raise; failures are logged and skipped.
STATUS_EXTENSIONS = []


def register_status_extension(fn):
    """Register fn() -> dict to be merged into /api/status. Usable as a decorator."""
    if fn not in STATUS_EXTENSIONS:
        STATUS_EXTENSIONS.append(fn)
    return fn


def _apply_status_extensions(payload):
    for ext in list(STATUS_EXTENSIONS):
        try:
            extra = ext()
            if extra:
                payload.update(extra)
        except Exception as exc:  # never let one extension break the status poll
            _log_app_error(exc, context=f"status extension {getattr(ext, '__name__', ext)}")
    return payload


# Long-running daemon workers (index syncs, scheduled imports, ...). Registered at import
# time, started exactly once by ensure_background_workers() (called from main() and by the
# dev sandbox harness, which never calls main()).
_BACKGROUND_WORKERS = {}
_BACKGROUND_WORKERS_LOCK = threading.Lock()
_BACKGROUND_WORKERS_STARTED = set()


def register_background_worker(name, target):
    """Register a zero-arg callable to run in its own daemon thread once workers start."""
    with _BACKGROUND_WORKERS_LOCK:
        _BACKGROUND_WORKERS[name] = target
        already_running = bool(_BACKGROUND_WORKERS_STARTED)
    if already_running:  # registered after startup: start it right away
        ensure_background_workers()
    return target


def ensure_background_workers():
    """Start every registered worker that is not running yet. Safe to call repeatedly."""
    with _BACKGROUND_WORKERS_LOCK:
        pending = [(n, t) for n, t in _BACKGROUND_WORKERS.items() if n not in _BACKGROUND_WORKERS_STARTED]
        _BACKGROUND_WORKERS_STARTED.update(n for n, _ in pending)
        _BACKGROUND_WORKERS_STARTED.add("__started__")
    for name, target in pending:
        threading.Thread(target=target, name=f"worker:{name}", daemon=True).start()


# ---------- Routes ----------
@app.route("/")
def index():
    # The v2 UI lives in templates/index.html; until it exists, / falls back to the classic UI.
    use_v2 = (Path(app.template_folder) / "index.html").exists() and not request.args.get("classic")
    return render_template("index.html" if use_v2 else "classic.html", poll_interval=POLL_INTERVAL,
                           demo_mode=DEMO_MODE)


@app.route("/classic")
def classic_ui():
    """The original single-page UI, kept as a fallback."""
    return render_template("classic.html", poll_interval=POLL_INTERVAL)

@app.route("/widget")
def widget():
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<title>Discover Widget</title>
<style>
:root{--bg:#121212;--surface:#1a1a1a;--text:#fff;--muted:#888;--accent:#1db954;--like:#e05;--border:#333;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;font-size:13px;overflow:hidden;}
#w{display:flex;align-items:center;gap:10px;padding:8px 10px;height:100vh;position:relative;}
#w-art{width:56px;height:56px;border-radius:4px;object-fit:cover;flex-shrink:0;background:#222;}
#w-meta{flex:1;min-width:0;}
#w-title{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
#w-artist{color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;}
.w-bar{height:3px;background:#333;border-radius:2px;margin-top:5px;}
.w-fill{height:100%;border-radius:2px;transition:width 0.5s linear;}
#w-track-fill{background:#555;}
#w-thresh-fill{background:var(--accent);}
.w-btns{display:flex;flex-direction:column;gap:5px;flex-shrink:0;}
.w-btn{border:1px solid var(--border);border-radius:5px;background:var(--surface);color:var(--text);cursor:pointer;font-size:11px;padding:4px 7px;white-space:nowrap;}
.w-btn:hover{background:#2a2a2a;}
.w-btn.like{border-color:var(--like);color:var(--like);}
.w-btn.skip{border-color:var(--accent);color:var(--accent);}
.w-btn:disabled{opacity:0.35;cursor:default;}
#w-idle{color:var(--muted);font-size:12px;margin:auto;text-align:center;}
#btn-pin{position:absolute;top:4px;right:6px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:2px 4px;line-height:1;}
#btn-pin:hover{color:var(--text);}
#btn-pin.hidden{display:none;}
</style>
</head>
<body>
<button id="btn-pin" title="Always on top (Chrome)" onclick="pinToTop()">&#8679;</button>
<div id="w" style="display:none">
  <img id="w-art" src="" alt="">
  <div id="w-meta">
    <div id="w-title">—</div>
    <div id="w-artist"></div>
    <div class="w-bar"><div class="w-fill" id="w-track-fill" style="width:0%"></div></div>
    <div class="w-bar" style="margin-top:3px"><div class="w-fill" id="w-thresh-fill" style="width:0%"></div></div>
  </div>
  <div class="w-btns">
    <button class="w-btn like" id="w-like" disabled>&#9829; Like</button>
    <button class="w-btn skip" id="w-skip" disabled>&#9197; Skip</button>
  </div>
</div>
<div id="w-idle">Nothing playing</div>
<script>
if (!window.documentPictureInPicture) document.getElementById("btn-pin").classList.add("hidden");

function q(id){return document.getElementById(id);}

async function pinToTop() {
  if (!window.documentPictureInPicture) return;
  try {
    const pip = await window.documentPictureInPicture.requestWindow({width:320,height:160});
    const d = pip.document;
    // Copy styles
    const st = d.createElement("style");
    st.textContent = document.querySelector("style").textContent;
    d.head.appendChild(st);
    // Clone body contents (minus the pin button itself)
    const pinBtn = document.getElementById("btn-pin");
    pinBtn.remove();
    d.body.innerHTML = document.body.innerHTML;
    document.body.innerHTML = "<div id=\\"w-idle\\">Pinned to always-on-top window.</div>";
    // Wire up logic in PiP window
    wireWidget(d);
  } catch(e) { alert("Could not pin: " + e.message); }
}

function wireWidget(doc) {
  var pollTimer;
  var shownId=null;
  function q2(id){return doc.getElementById(id);}
  function pinned(){return JSON.stringify(shownId?{track_id:shownId}:{});}
  async function poll(){
    try{
      var resp = await fetch("/api/status");
      var data = await resp.json();
      var t = data.current_track;
      shownId=(t&&data.auth_ready&&t.id)||null;
      if(!t||!data.auth_ready){q2("w").style.display="none";q2("w-idle").style.display="";return;}
      q2("w").style.display="flex";q2("w-idle").style.display="none";
      q2("w-art").src=t.image||"";
      q2("w-title").textContent=t.name||"";
      q2("w-artist").textContent=t.artists||"";
      var prog=data.duration_ms>0?Math.min(100,(data.progress_ms/data.duration_ms)*100):0;
      q2("w-track-fill").style.width=prog+"%";
      var thresh=data.threshold_seconds>0?Math.min(100,(data.current_listened/data.threshold_seconds)*100):0;
      q2("w-thresh-fill").style.width=thresh+"%";
      q2("w-like").disabled=!t;
      q2("w-skip").disabled=!t||!data.current_in_playlist;
    }catch(e){}
  }
  async function doLike(){q2("w-like").disabled=true;await fetch("/api/like",{method:"POST",headers:{"Content-Type":"application/json"},body:pinned()});poll();}
  async function doSkip(){q2("w-skip").disabled=true;await fetch("/api/skip-remove",{method:"POST",headers:{"Content-Type":"application/json"},body:pinned()});poll();}
  q2("w-like").onclick=doLike;
  q2("w-skip").onclick=doSkip;
  poll();
  pollTimer = setInterval(poll,3000);
}

// Main widget logic (when not pinned)
(function(){
  var shownId=null;
  function pinned(){return JSON.stringify(shownId?{track_id:shownId}:{});}
  async function poll(){
    try{
      var resp = await fetch("/api/status");
      var data = await resp.json();
      var t = data.current_track;
      shownId=(t&&data.auth_ready&&t.id)||null;
      if(!t||!data.auth_ready){q("w").style.display="none";q("w-idle").style.display="";return;}
      q("w").style.display="flex";q("w-idle").style.display="none";
      q("w-art").src=t.image||"";
      q("w-title").textContent=t.name||"";
      q("w-artist").textContent=t.artists||"";
      var prog=data.duration_ms>0?Math.min(100,(data.progress_ms/data.duration_ms)*100):0;
      q("w-track-fill").style.width=prog+"%";
      var thresh=data.threshold_seconds>0?Math.min(100,(data.current_listened/data.threshold_seconds)*100):0;
      q("w-thresh-fill").style.width=thresh+"%";
      q("w-like").disabled=!t;
      q("w-skip").disabled=!t||!data.current_in_playlist;
    }catch(e){}
  }
  async function doLike(){q("w-like").disabled=true;await fetch("/api/like",{method:"POST",headers:{"Content-Type":"application/json"},body:pinned()});poll();}
  async function doSkip(){q("w-skip").disabled=true;await fetch("/api/skip-remove",{method:"POST",headers:{"Content-Type":"application/json"},body:pinned()});poll();}
  q("w-like").onclick=doLike;
  q("w-skip").onclick=doSkip;
  poll();
  setInterval(poll,3000);
})();
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/favicon.ico")
def favicon():
    icon = Path(__file__).parent / "static" / "favicon.svg"
    if icon.exists():
        return send_file(icon, mimetype="image/svg+xml")
    return ("", 204)

# ---------- Platform status extensions ----------
def _fmt_wait(seconds):
    """'2h 11m', '4m 5s', '35s'."""
    s = max(0, int(round(seconds or 0)))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


_SPOTIFY_FEATURE_BY_METHOD = {
    "search": "Spotify search",
    "artist": "Artist details from Spotify", "artists": "Artist details from Spotify",
    "artist_albums": "Artist details from Spotify", "artist_top_tracks": "Artist details from Spotify",
    "album": "Artist details from Spotify", "albums": "Artist details from Spotify",
    "album_tracks": "Artist details from Spotify",
    "playlist": "Backlog sync", "playlist_items": "Backlog sync", "next": "Backlog sync",
    "playlist_tracks": "Backlog sync", "current_user_playlists": "Playlist list",
    "current_user_saved_tracks": "Liked songs", "current_user_saved_tracks_contains": "Liked songs",
    "current_playback": "Now playing updates", "devices": "Device list",
    "current_user_recently_played": "Recently played",
}
_WORKS_WITHOUT_SPOTIFY = ["History & stats", "Last.fm", "Lyrics", "Cached pages"]


def spotify_status_summary():
    """One honest line about Spotify access for the UI's status pill.

    state: ok | pacing (local per-minute caps or a search cool-down; everything important
    still works) | protect (after a real 429: only playback, auto-clear and likes go
    through) | rate_limited (Spotify said stop: nothing reaches Spotify until until_ms) |
    app_not_registered | signed_out. paused/working list what the user will notice.
    """
    now_mono = time.monotonic()
    now_ms = int(time.time() * 1000)

    def to_ms(mono_until):
        return int(now_ms + max(0.0, mono_until - now_mono) * 1000)

    with _SPOTIFY_GUARD_LOCK:
        g_until, g_reason = _SPOTIFY_GUARD_GLOBAL_UNTIL, _SPOTIFY_GUARD_GLOBAL_REASON
        p_until, p_reason = _SPOTIFY_GUARD_PROTECT_MODE_UNTIL, _SPOTIFY_GUARD_PROTECT_REASON
        app_until, app_msg = _SPOTIFY_APP_REG_BLOCK_UNTIL, _SPOTIFY_APP_REG_BLOCK_MSG
        restored = None
        if _SPOTIFY_GUARD_RESTORED_PENDING and _SPOTIFY_GUARD_RESTORED_UNTIL_WALL > time.time():
            restored = {"until_ms": int(_SPOTIFY_GUARD_RESTORED_UNTIL_WALL * 1000),
                        "reason": _SPOTIFY_GUARD_RESTORED_REASON, "verifying": True}
        full = []
        for method, bucket in list(_SPOTIFY_GUARD_BUDGET_BUCKETS.items()):
            limit, window_s = _spotify_guard_policy(method).get("budget") or (0, 0)
            if limit and window_s:
                live = [t for t in bucket if t >= now_mono - window_s]
                if len(live) >= limit:
                    full.append((method, live[0] + window_s))
    search_until = _sp_search_backoff_until

    out = {"state": "ok", "until_ms": None, "reason": None, "message": "Spotify OK",
           "paused": [], "working": ["Everything"], "restored": restored}
    try:
        signed_in = bool(get_cached_token())
    except Exception:
        signed_in = False
    if not signed_in:
        out.update(state="signed_out", message="Signed out of Spotify. Sign in to track playback and clear the backlog.",
                   paused=["Now playing", "Auto-clear", "Likes & playback controls", "Spotify search"],
                   working=list(_WORKS_WITHOUT_SPOTIFY))
        return out
    if app_until > now_mono:
        out.update(state="app_not_registered", until_ms=to_ms(app_until), reason="app_registration",
                   message=spotify_app_access_message(app_msg),
                   detail=app_msg,
                   paused=["Now playing", "Auto-clear", "Likes & playback controls", "Spotify search"],
                   working=list(_WORKS_WITHOUT_SPOTIFY))
        return out
    if g_until > now_mono:
        out.update(state="rate_limited", until_ms=to_ms(g_until), reason=g_reason,
                   message=f"Spotify asked the app to pause for {_fmt_wait(g_until - now_mono)}.",
                   paused=["Now playing", "Auto-clear", "Likes & playback controls", "Spotify search", "Backlog sync"],
                   working=list(_WORKS_WITHOUT_SPOTIFY))
        return out
    if p_until > now_mono:
        out.update(state="protect", until_ms=to_ms(p_until), reason=p_reason,
                   message=(f"Easing off Spotify for {_fmt_wait(p_until - now_mono)} after a rate limit: "
                            "only playback, auto-clear and likes go through."),
                   paused=["Spotify search", "Artist details from Spotify", "Backlog sync", "Liked songs"],
                   working=["Now playing", "Auto-clear", "Likes & playback controls"] + _WORKS_WITHOUT_SPOTIFY)
        return out
    paused, until = [], 0.0
    if search_until > now_mono:
        paused.append("Spotify search")
        until = max(until, search_until)
    for method, free_at in full:
        feature = _SPOTIFY_FEATURE_BY_METHOD.get(method, method.replace("_", " "))
        if feature not in paused:
            paused.append(feature)
        until = max(until, free_at)
    if paused:
        out.update(state="pacing", until_ms=to_ms(until), reason="local_budget" if full else "search_backoff",
                   message=f"Pacing Spotify requests for {_fmt_wait(until - now_mono)}: {', '.join(paused)} will resume shortly.",
                   paused=paused, working=["Now playing", "Auto-clear", "Likes & playback controls"] + _WORKS_WITHOUT_SPOTIFY)
    return out


def _spotify_status_extension():
    return {"spotify_status": spotify_status_summary()}


def _config_status_extension():
    msg = config_error_message()
    if msg:
        # Overrides the transient "error" so the warning stays up until the file is fixed.
        return {"config_error": msg, "error": msg}
    return {"config_error": None}


register_status_extension(_spotify_status_extension)
register_status_extension(_config_status_extension)


# ---------- Spotify sign-in ----------
_OAUTH_STATES = {}  # state token -> expiry (epoch s); a /callback must present one we issued
_OAUTH_STATE_LOCK = threading.Lock()
_OAUTH_STATE_TTL_S = 600


def _new_oauth_state():
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _OAUTH_STATE_LOCK:
        for k in [k for k, exp in _OAUTH_STATES.items() if exp <= now]:
            _OAUTH_STATES.pop(k, None)
        while len(_OAUTH_STATES) >= 20:
            _OAUTH_STATES.pop(next(iter(_OAUTH_STATES)))
        _OAUTH_STATES[token] = now + _OAUTH_STATE_TTL_S
    return token


def _consume_oauth_state(token):
    if not token:
        return False
    with _OAUTH_STATE_LOCK:
        expires = _OAUTH_STATES.pop(token, None)
    return bool(expires and expires > time.time())


def reset_spotify_session_caches():
    """Forget everything cached for the previous Spotify account/app: run after sign-in and
    after the app credentials change, so one account's data never shows for another."""
    global _user_display_name_cache
    reset_token_memory()
    with _SP_CLIENT_LOCK:
        _sp_client_cache.update(access_token=None, client=None)
    with _SPOTIFY_GUARD_LOCK:
        _SPOTIFY_GUARD_CACHE.clear()
    with _liked_track_lock:
        _liked_track_cache.clear()
    with _liked_songs_cache_lock:
        _liked_songs_cache.update(tracks=[], fetched_at=0.0)
    invalidate_liked_artist_counts_cache()
    with _FAVORITE_RELEASE_LOCK:
        _FAVORITE_RELEASE_CACHE.update(fetched_at=0.0, data=None)
    try:
        with _playlist_list_lock:
            _playlist_list_cache["fetched_at"] = 0.0
    except NameError:
        pass
    _user_display_name_cache = None


def _auth_page(title, detail, status):
    body = ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Spotify sign-in</title>"
            f"<h2>{html.escape(title)}</h2>")
    if detail:
        body += f"<p>{html.escape(str(detail))}</p>"
    body += "<p><a href='/login'>Try again</a> · <a href='/'>Back to the app</a></p></html>"
    return body, status, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/login")
def login():
    if DEMO_MODE:
        return redirect("/#/now")  # the offline demo is always "signed in" to its simulated Spotify
    if _oauth is None:
        return redirect("/#/setup")  # no Spotify app yet: the Setup screen collects it
    auth_url = _oauth.get_authorize_url(state=_new_oauth_state())
    return redirect(auth_url)

@app.route("/callback")
def callback():
    if _oauth is None:
        return _auth_page("Spotify isn't set up yet",
                          "Add your Spotify app's Client ID and Secret on the Setup screen first.", 400)
    state_ok = _consume_oauth_state(request.args.get("state"))
    err = request.args.get("error")
    if err:
        return _auth_page("Spotify auth failed", err, 400)
    if not state_ok:
        # A callback we did not start (or one older than 10 minutes): never exchange it,
        # or another site could sign this app into an account of its choosing.
        return _auth_page("This sign-in link has expired or was not started from this app.",
                          "Start the sign-in again.", 400)
    code = request.args.get("code")
    if not code:
        return _auth_page("No code returned", None, 400)
    try:
        # exchanges code -> tokens, writes to cache
        _oauth.get_access_token(code, as_dict=False, check_cache=False)
    except Exception as e:
        _log_app_error(e, context="oauth callback")
        return _auth_page("Token exchange failed", str(e), 500)
    reset_spotify_session_caches()
    _clear_spotify_app_registration_block()
    with state_lock:
        state["auth_ready"] = True
        state["last_error"] = None
    _revalidate_restored_guard_state()
    return redirect("/")

@app.route("/api/health")
def api_health():
    """Lock-free health check — useful for diagnosing state_lock contention."""
    return jsonify({"ok": True, "ts": time.time()})

def _fmt_setting_num(value):
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(fv)) if fv.is_integer() else str(fv)


def _effective_setting_value(key, env):
    """Value in .env, else the live environment, else what the app actually runs with."""
    if env.get(key) is not None:
        return env[key]
    if os.environ.get(key) is not None:
        return os.environ[key]
    defaults = {
        "SPOTIPY_REDIRECT_URI": spotify_redirect_uri(),
        "APP_DEBUG": "1" if DEBUG_MODE else "0",
        "APP_USE_RELOADER": "1" if USE_RELOADER else "0",
        "SPOTIFY_EXTENDED_ENDPOINTS": "1" if SPOTIFY_EXTENDED_ENDPOINTS else "0",
        "PLAYLIST_ID": DEFAULT_PLAYLIST_ID,
        "POLL_INTERVAL": _fmt_setting_num(POLL_INTERVAL),
        "POLL_INTERVAL_PAUSED": _fmt_setting_num(POLL_INTERVAL_PAUSED),
        "DEFAULT_THRESHOLD": _fmt_setting_num(DEFAULT_THRESHOLD),
        "DISCOVER_WEEKLY_INFLOW": _fmt_setting_num(DISCOVER_WEEKLY_INFLOW),
        "AUTO_ARTIST_LIKE_THRESHOLD": _fmt_setting_num(AUTO_ARTIST_LIKE_THRESHOLD),
    }
    return defaults.get(key, "")


def _mask_secret(value):
    value = str(value or "")
    if not value:
        return ""
    return "••••" + (value[-4:] if len(value) > 4 else "")


def _settings_items():
    env = read_env_file()
    items = []
    for section, key, label, typ, secret, help_text in SETTINGS_SCHEMA:
        current_value = _effective_setting_value(key, env)
        item = {
            "section": section,
            "key": key,
            "label": label,
            "type": typ,
            "secret": secret,
            "readonly": key in READONLY_SETTINGS,
            "live": key in LIVE_APPLY_SETTINGS or key in SPOTIFY_AUTH_SETTINGS,
            "help": help_text,
            "hint": help_text,
            "has_value": bool(current_value),
            "value": current_value,
        }
        if secret:
            # Never send secrets back to the page: only whether one is set and its last 4.
            item.update(value="", hint=_mask_secret(current_value), masked=True)
        bounds = SETTINGS_BOUNDS.get(key)
        if bounds:
            item.update(min=bounds["min"], max=bounds["max"], step=bounds["step"],
                        num_type=bounds["type"], bounds=dict(bounds))
        items.append(item)
    return items


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Current values of the UI-editable .env settings + metadata (secrets masked).

    pending_restart marks a saved value the running process does not use yet (settings only
    read at startup), so the page keeps saying "restart to apply" after a reload.
    """
    running = {
        "DEFAULT_THRESHOLD": DEFAULT_THRESHOLD, "PLAYLIST_ID": DEFAULT_PLAYLIST_ID,
        "APP_DEBUG": DEBUG_MODE, "APP_USE_RELOADER": USE_RELOADER,
        "SPOTIFY_EXTENDED_ENDPOINTS": SPOTIFY_EXTENDED_ENDPOINTS,
    }
    items = _settings_items()
    for item in items:
        if item["key"] not in running:
            continue  # live settings apply at once; auth settings force a new sign-in instead
        now, saved = running[item["key"]], item.get("value")
        if isinstance(now, bool):
            differs = now != (str(saved).strip().lower() in {"1", "true", "yes", "on"})
        elif isinstance(now, (int, float)):
            try:
                differs = float(saved) != float(now)
            except (TypeError, ValueError):
                differs = False
        else:
            differs = str(saved or "").strip() != str(now or "").strip()
        item["pending_restart"] = differs
    return jsonify({"settings": items, "env_path": str(ENV_PATH),
                    "restart_pending": [i["key"] for i in items if i.get("pending_restart")]})


def _settings_error(field, message, status=400):
    return _api_error_response("invalid_setting", message, status, field=field)


def _validate_setting(key, value):
    """Normalize one submitted setting. Returns (normalized_str | None to skip, error | None)."""
    spec = _SETTINGS_KEY_TO_SPEC[key]
    label, typ, secret = spec[2], spec[3], spec[4]
    if value is None:
        value = ""
    if isinstance(value, (dict, list)):
        return None, f"{label} must be a single value."
    if isinstance(value, bool):
        raw = "1" if value else "0"
    else:
        raw = str(value)
    if _ENV_FORBIDDEN_CHARS_RE.search(raw):
        return None, f"{label} must not contain line breaks."
    raw = raw.strip()
    if len(raw) > 500:
        return None, f"{label} is too long."
    if secret:
        # Empty (or the masked hint sent back) means "keep the saved secret".
        if not raw or raw.startswith("••••"):
            return None, None
        return raw, None
    if typ == "bool":
        low = raw.lower()
        if low in {"1", "true", "yes", "on"}:
            return "1", None
        if low in {"0", "false", "no", "off", ""}:
            return "0", None
        return None, f"{label} must be on or off."
    if typ == "number":
        bounds = SETTINGS_BOUNDS.get(key) or {}
        try:
            fv = float(raw)
        except ValueError:
            return None, f"{label} must be a number."
        if not math.isfinite(fv):
            return None, f"{label} must be a finite number."
        if bounds.get("type") == "int" and not fv.is_integer():
            return None, f"{label} must be a whole number."
        lo, hi = bounds.get("min"), bounds.get("max")
        if (lo is not None and fv < lo) or (hi is not None and fv > hi):
            return None, f"{label} must be between {_fmt_setting_num(lo)} and {_fmt_setting_num(hi)}."
        return _fmt_setting_num(fv), None
    if key == "PLAYLIST_ID" and raw:
        pid = parse_playlist_id(raw)
        if not pid:
            return None, f"{label} must be a Spotify playlist link or ID."
        return pid, None
    if key == "SPOTIPY_CLIENT_ID" and not raw:
        return None, None  # clearing the app ID would only break sign-in; treat as no change
    return raw, None


def _setting_unchanged(key, new_value, env):
    current = _effective_setting_value(key, env)
    if _SETTINGS_KEY_TO_SPEC[key][3] == "number":
        try:
            return float(current) == float(new_value)
        except (TypeError, ValueError):
            return False
    return str(current) == str(new_value)

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """Validate and write settings to .env (atomically), then apply what can be applied live.

    Body: {"updates": {KEY: value}}. Every value is validated before anything is written,
    so a 400 (which names the field) leaves .env untouched. Empty secrets mean "keep the
    saved one"; values equal to the current ones are skipped (re-saving an unchanged app ID
    must not sign the user out).
    """
    global _oauth, LASTFM_API_KEY, LASTFM_USERNAME, POLL_INTERVAL, POLL_INTERVAL_PAUSED
    global DISCOVER_WEEKLY_INFLOW, AUTO_ARTIST_LIKE_THRESHOLD
    data = _json_body()
    updates = data.get("updates")
    if not isinstance(updates, dict) or not updates:
        return _settings_error("updates", 'Send {"updates": {"SETTING": value}} with at least one setting.')
    if DEMO_MODE and any(k in SPOTIFY_AUTH_SETTINGS for k in updates):
        return _api_error_response("demo_mode", "The offline demo uses a simulated Spotify, so it has no "
                                   "Spotify app to set up. Run the app without --demo to use your own.", 403)

    env_now = read_env_file()
    changed, skipped, ignored = {}, [], []
    for k, v in updates.items():
        if k not in _SETTINGS_KEY_TO_SPEC or k in READONLY_SETTINGS:
            ignored.append(k)
            continue
        normalized, error = _validate_setting(k, v)
        if error:
            return _settings_error(k, error)
        if normalized is None or _setting_unchanged(k, normalized, env_now):
            skipped.append(k)
            continue
        changed[k] = normalized

    if not changed:
        if not skipped:
            return _settings_error("updates", "None of the submitted settings can be changed here.")
        return jsonify({"message": "No changes to save.", "saved": [], "skipped": skipped, "ignored": ignored,
                        "live_applied": [], "restart_required": [], "favorite_sync": None,
                        "cleared_spotify_cache": False,
                        "settings": api_settings_get().get_json()["settings"]})

    try:
        write_env_updates(changed)
    except ValueError as e:
        return _settings_error(next(iter(changed)), str(e))
    except OSError as e:
        _log_app_error(e, context="write .env")
        return _api_error_response("env_write_failed", f"Could not write .env: {e}", 500)

    # Update the running process too, so everything that reads os.environ (or the
    # globals refreshed below) picks the new values up immediately.
    for k, v in changed.items():
        os.environ[k] = v

    auth_changed = any(k in SPOTIFY_AUTH_SETTINGS for k in changed)
    cleared_cache = False
    cache_clear_error = None
    if auth_changed:
        cleared_cache, cache_clear_error = clear_spotify_cache_file()
        _oauth = build_oauth_manager()
        reset_spotify_session_caches()
        with state_lock:
            state["auth_ready"] = False
            state["last_error"] = "Spotify app credentials changed. Sign in again to continue."
            state["spotify_user_id"] = None
    if "LASTFM_API_KEY" in changed or "LASTFM_USERNAME" in changed:
        LASTFM_API_KEY = lastfm_api_key()
        LASTFM_USERNAME = lastfm_username()
        lastfm_reset_caches()
        with state_lock:  # make the Last.fm feeds refresh on the next poll
            state["scrobbles_today"] = None
            state["scrobbles_today_fetched_at"] = 0
            state["weekly_top_fetched_at"] = 0
    if "POLL_INTERVAL" in changed:
        POLL_INTERVAL = float(changed["POLL_INTERVAL"])
    if "POLL_INTERVAL_PAUSED" in changed:
        POLL_INTERVAL_PAUSED = float(changed["POLL_INTERVAL_PAUSED"])
    if "DISCOVER_WEEKLY_INFLOW" in changed:
        DISCOVER_WEEKLY_INFLOW = int(changed["DISCOVER_WEEKLY_INFLOW"])
    favorite_sync = None
    if "AUTO_ARTIST_LIKE_THRESHOLD" in changed:
        AUTO_ARTIST_LIKE_THRESHOLD = int(changed["AUTO_ARTIST_LIKE_THRESHOLD"])
        sp = get_sp()
        if sp:
            # A library scan can take minutes under Spotify's pacing: run it off-request.
            def _resync():
                try:
                    sync_favorite_artists_from_library(sp, force=True)
                except Exception as e:
                    if not is_local_guard_block(e):
                        _log_app_error(e, context="favorite sync after settings change")
            threading.Thread(target=_resync, daemon=True, name="favorite-sync").start()
            favorite_sync = {"started": True}

    live_applied = [k for k in changed if k in LIVE_APPLY_SETTINGS]
    restart_required = [k for k in changed if k not in LIVE_APPLY_SETTINGS and k not in SPOTIFY_AUTH_SETTINGS]
    message = f"Saved {len(changed)} setting(s)."
    if live_applied:
        message += " Live now: " + ", ".join(live_applied) + "."
    if favorite_sync:
        message += " Favorite artists are being re-synced in the background."
    if restart_required:
        message += " Restart to fully apply: " + ", ".join(restart_required) + "."
    if auth_changed:
        if cleared_cache:
            message = "Saved Spotify app credentials, cleared cached auth, and sign-in is required again. No app restart needed."
        elif cache_clear_error:
            message = f"Saved Spotify app credentials, but couldn't clear cached auth automatically: {cache_clear_error}"
    return jsonify({
        "message": message,
        "saved": list(changed.keys()),
        "skipped": skipped,
        "ignored": ignored,
        "live_applied": live_applied,
        "restart_required": restart_required,
        "favorite_sync": favorite_sync,
        "cleared_spotify_cache": cleared_cache,
        "settings": api_settings_get().get_json()["settings"],  # with pending_restart flags
    })

@app.route("/api/analytics")
def api_analytics():
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        return jsonify(build_analytics_payload(sp))
    except Exception as e:
        return jsonify({"error": f"Analytics failed: {e}"}), 500

_user_display_name_cache = None
def _get_user_display_name():
    global _user_display_name_cache
    if _user_display_name_cache:
        return _user_display_name_cache
    if _spotify_app_reg_block_remaining_s() > 0:
        return _user_display_name_cache or "Spotify"
    try:
        _sp = get_sp()
        if _sp:
            _me = _sp.me()
            _user_display_name_cache = _me.get("display_name") or _me.get("id") or "Spotify"
    except Exception:
        pass
    return _user_display_name_cache or "Spotify"

@app.route("/api/status")
def api_status():
    with state_lock:
        history = list(state["event_history"])
        _stats = dict(state["stats"])
        _favs = sorted(state["favorite_artists"])
        _weekly = list(state["weekly_top"])
        _scrobbles = state["scrobbles_today"]
        _snap = {
            "auth_ready": state["auth_ready"],
            "threshold_seconds": state["threshold_seconds"],
            "current_track": state["current_track"],
            "current_listened": state["current_listened"],
            "current_in_playlist": state["current_in_playlist"],
            "current_is_liked": state["current_is_liked"],
            "current_artist_info": state["current_artist_info"],
            "current_lyrics": state["current_lyrics"],
            "lyrics_offset_ms": state["lyrics_offset_ms"],
            "is_playing": state["is_playing"],
            "progress_ms": state["progress_ms"],
            "duration_ms": state["duration_ms"],
            "playlist_name": state["playlist_name"],
            "playlist_total": state["playlist_total"],
            "playlist_duration_ms": state.get("playlist_duration_ms", 0),
            "spotify_queue_count": state.get("spotify_queue_count", 0),
            "playlist_track_ids_loaded": state["playlist_track_ids_loaded"],
            "playlist_loaded_count": len(state["playlist_track_ids"]),
            "recently_removed": list(state["recently_removed"]),
            "last_poll": state["last_poll"],
            "error": state["last_error"],
            "spotify_search_backoff_remaining_s": _sp_search_backoff_remaining_s(),
            "spotify_search_backoff_until_ms": _sp_search_backoff_until_ms(),
            "spotify_search_retry_after_s": _sp_search_retry_after_reported_s(),
            "spotify_guard_backoff_remaining_s": _spotify_guard_remaining_s(),
            "spotify_guard_backoff_until_ms": _spotify_guard_until_ms(),
            "spotify_guard_retry_after_s": _SPOTIFY_GUARD_GLOBAL_RETRY_AFTER_S,
            "spotify_guard_reason": _SPOTIFY_GUARD_GLOBAL_REASON,
            "spotify_guard_protect_remaining_s": _spotify_guard_protect_remaining_s(),
            "spotify_guard_protect_until_ms": _spotify_guard_protect_until_ms(),
            "spotify_guard_protect_reason": _SPOTIFY_GUARD_PROTECT_REASON,
            "spotify_guard_restored_remaining_s": _spotify_guard_restored_remaining_s(),
            "spotify_guard_restored_until_ms": _spotify_guard_restored_until_ms(),
            "spotify_guard_restored_reason": _SPOTIFY_GUARD_RESTORED_REASON,
            "spotify_guard_restored_retry_after_s": _SPOTIFY_GUARD_RESTORED_RETRY_AFTER_S,
            "spotify_guard_restored_pending": _SPOTIFY_GUARD_RESTORED_PENDING,
            "spotify_guard_revalidating": _SPOTIFY_GUARD_REVALIDATING,
            "spotify_app_registration_block_remaining_s": _spotify_app_reg_block_remaining_s(),
            "spotify_app_registration_block_until_ms": _spotify_app_reg_block_until_ms(),
            "spotify_app_registration_block_message": _SPOTIFY_APP_REG_BLOCK_MSG,
        }
    # Compute time-window stats outside the lock
    now_ms = current_ts_ms()
    cutoff_30d = now_ms - (30 * 24 * 3600 * 1000)
    cutoff_7d  = now_ms - (7  * 24 * 3600 * 1000)
    liked_30d     = sum(1 for e in history if e.get("type") == "liked"   and int(e.get("ts_ms") or 0) >= cutoff_30d)
    removed_last_7 = sum(1 for e in history if e.get("type") == "removed" and int(e.get("ts_ms") or 0) >= cutoff_7d)
    current_track_snap = _snap.get("current_track")
    current_listened_snap = _snap.get("current_listened") or 0
    timeline = build_timeline(history, current_track_snap, current_listened_snap)
    return jsonify(_apply_status_extensions({
        **_snap,
        "stats": _stats,
        "liked_30d": liked_30d,
        "removed_last_7": removed_last_7,
        "favorite_artists": _favs,
        "weekly_top": _weekly,
        "scrobbles_today": _scrobbles,
        "session_started_ms": APP_START_MS,
        "user_display_name": _get_user_display_name(),
        "timeline": timeline,
    }))

@app.route("/api/history")
def api_history():
    """Return up to 50 recent tracks from Spotify recently-played (primary)
    or fall back to Last.fm recent scrobbles. Each track includes genres where
    available from the genre cache."""
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401

    tracks = []

    def _genres_for_artist_name(name):
        """Look up genres by artist name from LFM tag cache (name-keyed, no Spotify needed)."""
        if not name:
            return []
        key = f"artist_top_tags:{name.lower()}:6"
        cached = _lfm_cache_get(key, ttl_s=7 * 24 * 3600)
        if cached is not None:
            return normalize_genres(cached, limit=3)
        return []

    # ── Try Spotify recently played ──
    try:
        result = sp.current_user_recently_played(limit=50)
        for item in (result.get("items") or []):
            tr = item.get("track") or {}
            if not tr.get("id"):
                continue
            played_at = item.get("played_at", "")
            artists_objs = tr.get("artists") or []
            artists_list = [a["name"] for a in artists_objs]
            primary_artist_id = artists_objs[0]["id"] if artists_objs else None
            images = (tr.get("album") or {}).get("images") or []
            image = images[-1]["url"] if images else None
            # Genres: try artist_genre_cache by Spotify ID first, then LFM name cache
            genres = []
            if primary_artist_id and primary_artist_id in artist_genre_cache:
                genres = artist_genre_cache[primary_artist_id][:3]
            elif artists_list:
                genres = _genres_for_artist_name(artists_list[0])
            tracks.append({
                "id": tr["id"],
                "name": tr.get("name", ""),
                "artists": ", ".join(artists_list),
                "album": (tr.get("album") or {}).get("name", ""),
                "image": image,
                "uri": tr.get("uri"),
                "played_at": played_at,
                "duration_ms": tr.get("duration_ms", 0),
                "genres": genres,
                "source": "spotify",
            })
        if tracks:
            return jsonify({"tracks": tracks, "source": "spotify"})
    except SpotifyGuardBlocked as e:
        return jsonify({"error": "guard", "retry_after": int(e.retry_after or 10)}), 503
    except Exception:
        pass

    # ── Fall back to Last.fm recent tracks ──
    _lfm_key, _lfm_user = lastfm_api_key(), lastfm_username()
    if _lfm_key and _lfm_user:
        try:
            import requests as _req
            r = _req.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "user.getrecenttracks",
                "user": _lfm_user,
                "api_key": _lfm_key,
                "format": "json",
                "limit": 50,
            }, timeout=8)
            data = r.json()
            for t in (data.get("recenttracks") or {}).get("track") or []:
                name = t.get("name", "")
                artist = (t.get("artist") or {}).get("#text", "")
                album  = (t.get("album") or {}).get("#text", "")
                image_list = t.get("image") or []
                image = next((i.get("#text") for i in reversed(image_list) if i.get("#text")), None)
                date_val = (t.get("date") or {}).get("uts")
                played_at = date_val or ""
                genres = _genres_for_artist_name(artist)
                tracks.append({
                    "id": None,
                    "name": name,
                    "artists": artist,
                    "album": album,
                    "image": image,
                    "uri": None,
                    "played_at": played_at,
                    "duration_ms": 0,
                    "genres": genres,
                    "source": "lastfm",
                    "now_playing": bool(t.get("@attr", {}).get("nowplaying")),
                })
            return jsonify({"tracks": tracks, "source": "lastfm"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"tracks": [], "source": "none", "message": "No history source available"})


def _signed_out_response():
    return _api_error_response("signed_out", "Sign in to Spotify first.", 401)


def _guard_blocked_response(exc):
    retry_after = spotify_retry_after_from(exc, default=10)
    return _api_error_response("rate_limited",
                               f"Spotify requests are paused for about {retry_after}s. Try again shortly.",
                               429, retry_after=retry_after, local=is_local_guard_block(exc))


LIKED_PAGE_MAX = 50   # Spotify's page size for saved tracks


def _liked_songs_slice(sp, offset, limit):
    """One page of Liked Songs straight from Spotify: (rows, total, next_offset or None)."""
    res = _spotify_page_with_retry(lambda: sp.current_user_saved_tracks(limit=limit, offset=offset),
                                   time.monotonic() + _PAGING_MAX_WAIT_S) or {}
    items = res.get("items") or []
    rows = []
    for item in items:
        tr = playlist_entry_media(item) or {}
        tid = tr.get("id")
        if not tid:
            continue
        artists = tr.get("artists") or []
        album = tr.get("album") or {}
        images = album.get("images") or []
        rows.append({
            "id": tid,
            "name": tr.get("name", ""),
            "artists": ", ".join(a.get("name", "") for a in artists if a.get("name")),
            "artist": (artists[0].get("name") if artists else "") or "",
            "album": album.get("name", ""),
            "image": images[-1]["url"] if images else None,
            "uri": tr.get("uri"),
            "duration_ms": tr.get("duration_ms", 0),
            "genres": get_cached_track_genres(tid),
            "liked_at": item.get("added_at"),
        })
    total = res.get("total")
    total = int(total) if isinstance(total, int) else offset + len(items)
    return rows, total, (offset + len(items) if res.get("next") and items else None)


@app.route("/api/liked-songs")
def api_liked_songs():
    """Without limit/offset: the most recent likes (cached, ~120), as the classic UI expects.
    With them: one page straight from Spotify, {tracks, total (all your likes), offset, limit,
    next_offset (null on the last page)}, so a list can page through every like."""
    sp = get_sp()
    if not sp:
        return _signed_out_response()
    force = request.args.get("force") == "1"
    paged = "limit" in request.args or "offset" in request.args
    if paged:
        try:
            limit = int(request.args.get("limit", LIKED_PAGE_MAX))
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            return _api_error_response("bad_request", "limit and offset must be whole numbers.", 400)
        if not 1 <= limit <= LIKED_PAGE_MAX:
            return _api_error_response("bad_request", f"limit must be between 1 and {LIKED_PAGE_MAX}.", 400, field="limit")
        if not 0 <= offset <= 1_000_000:
            return _api_error_response("bad_request", "offset must be 0 or more.", 400, field="offset")
    try:
        if paged:
            rows, total, next_offset = _liked_songs_slice(sp, offset, limit)
            return jsonify({"tracks": rows, "total": total, "offset": offset, "limit": limit,
                            "next_offset": next_offset})
        tracks = get_liked_songs_page(sp, force=force)
        return jsonify({"tracks": tracks, "total": len(tracks)})
    except SpotifyGuardBlocked as e:
        return _guard_blocked_response(e)


def _lfm_genres_for_artist(name):
    key = f"artist_top_tags:{name.lower()}:6"
    cached = _lfm_cache_get(key, ttl_s=7 * 24 * 3600)
    if cached is None:
        time.sleep(0.08)  # be gentle with Last.fm when resolving a whole queue
        cached = lastfm_artist_top_tags(name, limit=6)
    return normalize_genres(cached if isinstance(cached, list) else [], limit=4)


@app.route("/api/queue-genres", methods=["GET", "POST"])
def api_queue_genres():
    """Resolve queue genres via Last.fm and persist them by track id.

    POST accepts {"tracks":[{"id":"...","artist":"..."}]} and returns both
    by-track and by-artist maps. GET remains as a compatibility path for artist
    names only.
    """
    result_by_artist = {}
    result_by_track = {}
    to_persist = []

    if request.method == "POST":
        data = _json_body()
        tracks = data.get("tracks") or []
        if not isinstance(tracks, list):
            return _api_error_response("bad_request", "tracks must be a list of {id, artist}.", 400, field="tracks")
        for item in tracks[:120]:
            if not isinstance(item, dict):
                continue
            track_id = str(item.get("id") or "").strip()
            artist = str(item.get("artist") or "").strip()
            if not artist:
                continue
            cached_track = get_cached_track_genres(track_id) if track_id else []
            if cached_track:
                result_by_track[track_id] = cached_track
                result_by_artist.setdefault(artist, cached_track)
                continue
            if artist not in result_by_artist:
                result_by_artist[artist] = _lfm_genres_for_artist(artist)
            genres = result_by_artist.get(artist) or []
            if track_id and genres:
                result_by_track[track_id] = genres
                to_persist.append({"track_id": track_id, "artist": artist, "genres": genres})
        if to_persist:
            cache_track_genres_bulk(to_persist)
        return jsonify({"by_track": result_by_track, "by_artist": result_by_artist})

    artists = request.args.getlist("a")
    if not artists:
        return jsonify({})
    for name in artists[:60]:
        name = (name or "").strip()
        if not name:
            continue
        result_by_artist[name] = _lfm_genres_for_artist(name)
    return jsonify(result_by_artist)

_artist_liked_scan = {"thread": None, "box": {}}
_artist_liked_scan_lock = threading.Lock()


def _artist_page_liked_counts(sp, wait_s=0.3):
    """Liked-songs-per-artist counts without making the artist page wait for a library scan.

    The scan pages through every liked song (~1 s of pacing per 50), which made a first
    artist-page open take 5-20 s. Run it in one shared background thread, wait briefly
    (instant when cached) and return None while it is still loading.
    """
    with _artist_liked_scan_lock:
        th = _artist_liked_scan["thread"]
        if th is None or not th.is_alive():
            box = {}

            def run():
                try:
                    box["counts"] = get_liked_artist_counts(sp)
                except Exception as e:
                    box["error"] = e

            th = threading.Thread(target=run, name="artist-liked-counts", daemon=True)
            _artist_liked_scan.update(thread=th, box=box)
            th.start()
        box = _artist_liked_scan["box"]
    th.join(wait_s)
    if th.is_alive():
        return None
    return box.get("counts")


@app.route("/api/artist-info")
def api_artist_info():
    artist_id = request.args.get("artist_id", "").strip()
    artist_name = request.args.get("artist_name", "").strip()
    track_id = request.args.get("track_id", "").strip()
    track_name = request.args.get("track_name", "").strip()
    if not artist_id and not artist_name:
        return jsonify({"error": "artist_id or artist_name required"}), 400
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    with state_lock:
        user_top_ids = list(state.get("user_top_track_ids") or [])
        history = list(state["event_history"])
        liked_profile = state.get("liked_library_profile") or {}
    try:
        if artist_id and not artist_name:
            # Personal stats and Last.fm are keyed by name; one cached metadata call recovers it.
            try:
                artist_name = ((sp.artist(artist_id) or {}).get("name") or "").strip()
            except Exception as e:
                _log_app_error(e, "api_artist_info_name")
        search_blocked = False
        if not artist_id and artist_name:
            # Resolve the Spotify id up front (cached, and it respects the search cooldown) so the
            # Spotify-backed sections below can use it.
            sp_artist, search_blocked = _spotify_search_artist_ex(sp, artist_name)
            if sp_artist and sp_artist.get("id"):
                artist_id = sp_artist["id"]
        info = fetch_artist_info(sp, artist_id or None, artist_name,
                                 track_id or None, track_name, user_top_ids,
                                 allow_spotify_resolution=True)
        # The page UI says why links/images are missing when Spotify search is cooling down.
        info["spotify_limited"] = bool(search_blocked or _sp_search_rate_limited())
        # ...and why the bio / similar artists are missing when Last.fm is failing right now
        # (breaker open or a recent error), as opposed to Last.fm simply having nothing.
        if lastfm_api_key() and not (info.get("bio") or info.get("related") or info.get("lastfm_tags")):
            with _lfm_breaker_lock:
                breaker_open = _lfm_breaker["open_until"] > time.time()
            recent_error = (time.time() - float(_lfm_last_error.get("ts") or 0)) < 120
            info["lastfm_unavailable"] = bool(breaker_open or recent_error)
        if artist_id:
            # A real artist photo (fetch_artist_info falls back to a top track's album cover).
            # sp.artist is cached for days by the guard, so this is normally free.
            try:
                sp_meta = sp.artist(artist_id) or {}
                images = [i for i in (sp_meta.get("images") or []) if isinstance(i, dict) and i.get("url")]
                if images:
                    info["artist_image"] = images[0]["url"]
                    info["artist_image_small"] = images[-1]["url"]
                    info["artist_image_kind"] = "artist"
                info["spotify_genres"] = list(sp_meta.get("genres") or [])[:6]
                followers = (sp_meta.get("followers") or {}).get("total")
                info["spotify_followers"] = int(followers) if isinstance(followers, (int, float)) else None
            except SpotifyGuardBlocked:
                info["spotify_limited"] = True  # local budget/cooldown: keep the album-cover fallback
            except Exception as e:
                _log_app_error(e, "api_artist_info_artist_meta")
        if info.get("artist_image") and not info.get("artist_image_kind"):
            info["artist_image_kind"] = "album"
        # Taste similarity: genre overlap between artist tags and liked library
        artist_tags = set(normalize_genres(info.get("lastfm_tags") or [], limit=10))
        liked_genres = set(
            normalize_genres(
                [g.get("name") for g in (liked_profile.get("genres") or []) if isinstance(g, dict)],
                limit=20,
            )
        )
        overlap = artist_tags & liked_genres
        if artist_tags:
            similarity_pct = round(len(overlap) / len(artist_tags) * 100)
        else:
            similarity_pct = None
        info["artist_id"] = artist_id or None
        spotify_liked_count = 0
        try:
            spotify_like_counts = _artist_page_liked_counts(sp)
            if spotify_like_counts is not None:
                spotify_liked_count = int(spotify_like_counts.get((artist_name or "").strip().lower(), 0))
                # A one-item list: a bare string would be split on commas ('Woe, Is Me').
                auto_like_artists_from_spotify(sp, [artist_name] if artist_name else [])
            else:
                info["liked_counts_loading"] = True
        except Exception:
            spotify_liked_count = 0
        artist_stats = None
        if _SQLITE_ENABLED and artist_name:
            try:
                artist_stats = _artist_stats_from_sqlite(_db.get_conn(), artist_id, artist_name)
            except Exception as e:
                _log_app_error(e, "api_artist_info_stats")
                artist_stats = None
        if artist_stats is None:
            # Ledger not built yet: small in-memory feed. Match the credit whole or as its
            # primary artist, never as a substring ('Time' must not match 'Time, The Valuator').
            name_lower = (artist_name or "").strip().lower()
            listen_events = [e for e in history
                             if name_lower and ((e.get("artists") or "").strip().lower() == name_lower
                                                or (e.get("artists") or "").strip().lower().startswith(name_lower + ","))]
            recent_tracks = []
            seen_names = set()
            for e in sorted(listen_events, key=lambda x: x.get("ts_ms", 0), reverse=True):
                tn = e.get("track_name", "")
                if tn and tn not in seen_names:
                    seen_names.add(tn)
                    recent_tracks.append({"name": tn, "type": e.get("type"),
                                          "image": e.get("image"), "url": e.get("url")})
                if len(recent_tracks) >= 5:
                    break
            artist_stats = {
                "listen_count": sum(1 for e in listen_events if e.get("type") in ("listen", "now")),
                "imported_listens": 0,
                "app_listens": sum(1 for e in listen_events if e.get("type") in ("listen", "now")),
                "liked_count": sum(1 for e in listen_events if e.get("type") == "liked"),
                "removed_count": sum(1 for e in listen_events if e.get("type") == "removed"),
                "distinct_tracks": len({(e.get("track_id") or e.get("track_name")) for e in listen_events if (e.get("track_id") or e.get("track_name"))}),
                "distinct_albums": len({e.get("album") for e in listen_events if e.get("album")}),
                "distinct_days": len({e.get("day") for e in listen_events if e.get("day")}),
                "revisit_days": max(0, len({e.get("day") for e in listen_events if e.get("day")}) - 1),
                "first_heard_at": min([int(e.get("ts_ms") or 0) for e in listen_events if int(e.get("ts_ms") or 0)], default=None),
                "last_heard_at": max([int(e.get("ts_ms") or 0) for e in listen_events if int(e.get("ts_ms") or 0)], default=None),
                "top_tracks": [],
                "top_albums": [],
            }
        try:
            info["artist_liked"] = is_favorite_artist(artist_name) or (
                _events.is_artist_liked(artist_id or artist_name)
                if _SQLITE_ENABLED else False
            )
        except Exception:
            info["artist_liked"] = is_favorite_artist(artist_name)
        artist_stats["spotify_liked_count"] = spotify_liked_count
        artist_stats["liked_count_total"] = max(int(artist_stats.get("liked_count") or 0), spotify_liked_count)
        artist_stats["similarity_pct"] = similarity_pct
        artist_stats["matching_genres"] = sorted(overlap)[:6]
        top_track_ids = [t.get("id") for t in (info.get("top_tracks") or []) if t.get("id")]
        liked_track_ids = set()
        if top_track_ids:
            try:
                liked_flags = sp.current_user_saved_tracks_contains(top_track_ids)
                liked_track_ids = {tid for tid, flag in zip(top_track_ids, liked_flags or []) if flag}
            except Exception:
                liked_track_ids = set()
        for track in info.get("top_tracks") or []:
            track["is_liked"] = bool(track.get("id") and track.get("id") in liked_track_ids)
        if not info.get("my_plays_available"):
            # fetch_artist_info could not annotate from the ledger: fall back to the names we have.
            heard_track_names = {str((row.get("name") or "")).strip().lower()
                                 for row in (artist_stats.get("top_tracks") or []) if row.get("name")}
            info["unheard_top_tracks"] = [
                t for t in (info.get("top_tracks") or [])
                if (t.get("name") or "").strip().lower() not in heard_track_names
            ][:10]
        info["listen_stats"] = artist_stats
        # One ARTIST_VIEWED per artist page open: panels, drawers and prefetches pass no source.
        if _SQLITE_ENABLED and request.args.get("source") == "page":
            try:
                _events.record_action(
                    _events.ARTIST_VIEWED,
                    artist_id=artist_id or None,
                    payload={"artist_name": artist_name, "source": "page"},
                )
            except Exception as e:
                _log_app_error(e, "api_artist_info_viewed")
        return jsonify(info)
    except Exception as e:
        _log_app_error(e, "api_artist_info")
        # The raw exception text can carry request URLs (API keys): details go to the error log only.
        return jsonify({"error": "Could not load this artist right now. Try again in a moment.",
                        "code": "artist_info_failed"}), 500

# ---------- History imports ----------
# The Last.fm / Spotify importers and their routes (/api/lastfm/import, /api/spotify/import)
# live in src/services/importers.py and src/web/data_api.py (same paths as before).


# ---------- Dev error log ----------

@app.route("/api/dev/errors")
def api_dev_errors():
    return jsonify({"errors": list(_app_error_log)})

@app.route("/api/dev/spotify-calls")
def api_dev_spotify_calls():
    return jsonify({"calls": _spotify_guard_snapshot(limit=120)})


@app.route("/api/dev/clear-guard", methods=["POST"])
def api_dev_clear_guard():
    _clear_guard_state(persist=True, include_search=True)
    return jsonify({"ok": True, "message": "Cleared local Spotify guard state."})


@app.route("/api/dev/search-backoff", methods=["POST"])
def api_dev_search_backoff():
    """Test hook: start (seconds > 0) or clear (0) a Spotify search cool-down locally,
    to check how the UI and /api/search behave without waiting for a real 429."""
    global _sp_search_backoff_until, _sp_search_retry_after_s
    data = _json_body()
    try:
        seconds = float(data.get("seconds", 60))
    except (TypeError, ValueError):
        return _api_error_response("bad_request", "seconds must be a number.", 400, field="seconds")
    if not math.isfinite(seconds) or not 0 <= seconds <= 3600:
        return _api_error_response("bad_request", "seconds must be between 0 and 3600.", 400, field="seconds")
    _sp_search_backoff_until = time.monotonic() + seconds if seconds > 0 else 0.0
    _sp_search_retry_after_s = int(seconds) if seconds > 0 else None
    return jsonify({"ok": True, "search_backoff_remaining_s": _sp_search_backoff_remaining_s()})


# ---------- Artist Likes ----------

@app.route("/api/artist-likes")
def api_artist_likes():
    if not _SQLITE_ENABLED:
        return jsonify({"likes": []})
    try:
        return jsonify({"likes": _events.get_artist_likes()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/favorites/overview")
def api_favorites_overview():
    sp = get_sp()
    if not sp:
        return _signed_out_response()
    force = request.args.get("force") == "1"
    try:
        return jsonify(get_favorites_overview(sp, force=force))
    except SpotifyGuardBlocked as e:
        return _guard_blocked_response(e)


# ---------- Research Queue ----------

def _research_unavailable():
    return _api_error_response("unavailable", "The research list needs the local database, which is not available.", 503)


@app.route("/api/research", methods=["GET"])
def api_research_list():
    if not _SQLITE_ENABLED:
        return _research_unavailable()
    from src.persistence import research as _research
    status = request.args.get("status")  # active | promoted | archived | None=all
    entity_type = request.args.get("entity_type")
    if status and status not in ("active", "promoted", "archived"):
        return _api_error_response("bad_request", "status must be active, promoted or archived.", 400, field="status")
    if entity_type and entity_type not in ("track", "artist", "album"):
        return _api_error_response("bad_request", "entity_type must be track, artist or album.", 400, field="entity_type")
    items = _research.get_interests(status=status or None, entity_type=entity_type or None)
    counts = _research.get_count_by_status()
    return jsonify({"items": items, "counts": counts,
                    "seed_tags": _research.SEED_REASON_TAGS})


def _optional_str(data, key):
    value = data.get(key)
    if value is None:
        return None, None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None, f"{key} must be a string."
    return str(value).strip() or None, None


@app.route("/api/research", methods=["POST"])
def api_research_add():
    if not _SQLITE_ENABLED:
        return _research_unavailable()
    from src.persistence import research as _research
    data = _json_body()
    entity_type = data.get("entity_type") or "track"
    entity_name, err_name = _optional_str(data, "entity_name")
    entity_id, err_id = _optional_str(data, "entity_id")
    # Who made the track/album: an item without a Spotify id is otherwise just a title.
    artist_name, err_artist = _optional_str(data, "artist_name")
    reasons = data.get("reasons")
    if isinstance(reasons, str):
        reasons = [reasons]
    note = data.get("note")
    for field, err in (("entity_name", err_name), ("entity_id", err_id), ("artist_name", err_artist)):
        if err:
            return _api_error_response("bad_request", err, 400, field=field)
    if entity_type not in ("track", "artist", "album"):
        return _api_error_response("bad_request", "entity_type must be track, artist or album.", 400, field="entity_type")
    if not entity_name:
        return _api_error_response("bad_request", "entity_name is required.", 400, field="entity_name")
    if reasons is not None and (not isinstance(reasons, list) or not all(isinstance(t, str) for t in reasons)):
        return _api_error_response("bad_request", "reasons must be a list of strings.", 400, field="reasons")
    if note is not None and not isinstance(note, str):
        return _api_error_response("bad_request", "note must be a string.", 400, field="note")
    note = (note or "").strip() or None
    reasons = reasons or []
    try:
        interest_id = _research.add_interest(
            entity_type=entity_type,
            entity_name=entity_name,
            entity_id=entity_id,
            reasons=reasons,
            note=note,
            artist_name=artist_name,
        )
        # Record RESEARCH_ADDED event
        if _SQLITE_ENABLED:
            try:
                _events.record_action(
                    _events.RESEARCH_ADDED,
                    track_id=entity_id if entity_type == "track" else None,
                    artist_id=entity_id if entity_type == "artist" else None,
                    payload={"entity_type": entity_type, "entity_name": entity_name,
                             "reasons": reasons, "interest_id": interest_id},
                )
            except Exception:
                pass
        item = _research.get_interest(interest_id)
        return jsonify({"message": "Added to Research Queue.", "item": item}), 201
    except ValueError as e:
        return _api_error_response("bad_request", str(e), 400)


@app.route("/api/research/<int:interest_id>", methods=["PATCH"])
def api_research_update(interest_id):
    if not _SQLITE_ENABLED:
        return _research_unavailable()
    from src.persistence import research as _research
    data = _json_body()
    status = data.get("status")
    note = data.get("note")
    reasons = data.get("reasons")
    if isinstance(reasons, str):
        reasons = [reasons]
    if status is not None and status not in ("active", "promoted", "archived"):
        return _api_error_response("bad_request", "status must be active, promoted or archived.", 400, field="status")
    if note is not None and not isinstance(note, str):
        return _api_error_response("bad_request", "note must be a string.", 400, field="note")
    if reasons is not None and (not isinstance(reasons, list) or not all(isinstance(t, str) for t in reasons)):
        return _api_error_response("bad_request", "reasons must be a list of strings.", 400, field="reasons")
    try:
        found = _research.update_interest(interest_id, status=status, note=note, reasons=reasons)
    except ValueError as e:
        return _api_error_response("bad_request", str(e), 400)
    if found is False:
        return _api_error_response("not_found", "No research item with that id.", 404)
    # Record update event
    if status in ("promoted", "archived"):
        etype = _events.RESEARCH_PROMOTED if status == "promoted" else _events.RESEARCH_UPDATED
        try:
            _events.record_action(etype, payload={"interest_id": interest_id, "status": status})
        except Exception:
            pass
    item = _research.get_interest(interest_id)
    if not item:
        return _api_error_response("not_found", "No research item with that id.", 404)
    return jsonify({"message": "Updated.", "item": item})


@app.route("/api/research/seed-tags", methods=["GET"])
def api_research_seed_tags():
    if not _SQLITE_ENABLED:
        return jsonify({"tags": []})
    from src.persistence import research as _research
    return jsonify({"tags": _research.SEED_REASON_TAGS})


def _discography_fetch_error(e, what):
    """Legacy-shaped error (string ``error`` for the classic UI) for a failed Spotify fetch."""
    status = getattr(e, "http_status", None)
    retry_after = getattr(e, "retry_after", None)
    if retry_after is None:
        try:
            retry_after = int((getattr(e, "headers", None) or {}).get("Retry-After") or 0) or None
        except (TypeError, ValueError):
            retry_after = None
    if status == 429 or isinstance(e, SpotifyGuardBlocked):
        retry_after = int(retry_after or 30)
        return jsonify({"error": f"Spotify is cooling down. Retry in {retry_after}s.",
                        "code": "rate_limited", "retry_after": retry_after}), 429
    return jsonify({"error": f"{what} is unavailable right now. Try again in a moment.",
                    "code": "spotify_unavailable", "status": status}), 502


@app.route("/api/artist/discography")
def api_artist_discography():
    artist_id = request.args.get("artist_id", "").strip()
    artist_name = request.args.get("artist_name", "").strip()
    if not artist_id and not artist_name:
        return jsonify({"error": "artist_id or artist_name required", "code": "invalid_artist_id",
                        "field": "artist_id"}), 400
    if len(artist_name) > 300:
        return jsonify({"error": "artist_name is too long.", "code": "invalid_artist_name",
                        "field": "artist_name"}), 400
    if not artist_id:
        # The artist page knows only the name until /api/artist-info returns (seconds, Last.fm):
        # resolve the id here so the discography loads in parallel. The guard's single-flight
        # search cache means the page's concurrent artist-info lookup costs no second search.
        sp = get_sp()
        if not sp:
            return jsonify({"error": "Not authenticated", "code": "not_authenticated"}), 401
        found, blocked = _spotify_search_artist_ex(sp, artist_name)
        if blocked:
            retry_after = int(_sp_search_backoff_remaining_s() or 30)
            return jsonify({"error": f"Spotify search is cooling down. Retry in {retry_after}s.",
                            "code": "rate_limited", "retry_after": retry_after}), 429
        if not found or not found.get("id"):
            return jsonify({"error": f"{artist_name} wasn't found on Spotify.", "code": "artist_not_found"}), 404
        artist_id = found["id"]
    force = request.args.get("force") == "1"
    now = time.time()
    from src.services import discography as _disco
    # Only Spotify metadata is cached; heard state is recomputed from the plays ledger on every
    # request so a new play shows up immediately.
    with _artist_discography_lock:
        cached = _artist_discography_cache.get(artist_id) or {}
    raw = cached.get("albums")
    stale_notice = None
    if raw is None or force or (now - float(cached.get("ts") or 0)) >= 12 * 3600:
        sp = get_sp()
        if not sp and raw is None:
            return jsonify({"error": "Not authenticated", "code": "not_authenticated"}), 401
        if sp:
            try:
                raw = _disco.fetch_artist_albums(sp, artist_id)
                with _artist_discography_lock:
                    _artist_discography_cache[artist_id] = {"ts": now, "albums": raw}
            except Exception as e:
                _log_app_error(e, "api_artist_discography")
                if raw is None:
                    return _discography_fetch_error(e, "The discography")
                stale_notice = "Spotify metadata is cooling off. Showing the cached discography."
    try:
        conn = _db.get_conn() if _SQLITE_ENABLED else None
        data = _disco.build_discography_from_albums(raw, conn, artist_name=artist_name or None, artist_id=artist_id)
    except Exception as e:
        _log_app_error(e, "api_artist_discography_build")
        return jsonify({"error": "Could not build the discography.", "code": "discography_failed"}), 500
    if stale_notice:
        data["_stale_notice"] = stale_notice
    return jsonify(data)


@app.route("/api/artist/discography/release")
def api_artist_discography_release():
    album_id = request.args.get("album_id", "").strip()
    artist_name = request.args.get("artist_name", "").strip()
    if not album_id:
        return jsonify({"error": "album_id required", "code": "invalid_album_id", "field": "album_id"}), 400
    now = time.time()
    from src.services import discography as _disco
    # Cache the Spotify release only; heard flags come fresh from the plays ledger.
    with _artist_release_track_lock:
        cached = _artist_release_track_cache.get(album_id) or {}
    album = cached.get("album")
    stale_notice = None
    if album is None or (now - float(cached.get("ts") or 0)) >= 3 * 24 * 3600:
        sp = get_sp()
        if not sp and album is None:
            return jsonify({"error": "Not authenticated", "code": "not_authenticated"}), 401
        if sp:
            try:
                album = _disco.fetch_album(sp, album_id)
                album.setdefault("id", album_id)
                with _artist_release_track_lock:
                    _artist_release_track_cache[album_id] = {"ts": now, "album": album}
            except Exception as e:
                _log_app_error(e, "api_artist_discography_release")
                if album is None:
                    return _discography_fetch_error(e, "This tracklist")
                stale_notice = "Spotify metadata is cooling off. Showing the cached tracklist."
    try:
        conn = _db.get_conn() if _SQLITE_ENABLED else None
        data = _disco.build_album_tracklist_from_album(album, conn, artist_name=artist_name or None)
    except Exception as e:
        _log_app_error(e, "api_artist_discography_release_build")
        return jsonify({"error": "Could not build the tracklist.", "code": "tracklist_failed"}), 500
    if stale_notice:
        data["_stale_notice"] = stale_notice
    return jsonify(data)


def _number_field(data, *names):
    """First present field among names as a finite float: (value, field, error)."""
    for name in names:
        if name in data:
            raw = data.get(name)
            if isinstance(raw, bool) or raw is None:
                return None, name, f"{name} must be a number."
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return None, name, f"{name} must be a number."
            if not math.isfinite(value):
                return None, name, f"{name} must be a finite number."
            return value, name, None
    return None, names[0], f"{names[0]} is required."


@app.route("/api/threshold", methods=["POST"])
def api_threshold():
    """Body {seconds} (legacy {threshold}); clamped to 3..600 like the slider."""
    value, field, error = _number_field(_json_body(), "seconds", "threshold")
    if error:
        return _api_error_response("bad_request", error, 400, field=field)
    v = max(3, min(600, int(round(value))))
    with state_lock:
        state["threshold_seconds"] = v
    # Write whatever is current at write time, so racing saves end with the latest value.
    update_config(lambda cfg: cfg.__setitem__("threshold_seconds", state["threshold_seconds"]))
    return jsonify({"threshold_seconds": v})

@app.route("/api/client-state", methods=["POST"])
def api_client_state():
    data = _json_body()
    with state_lock:
        state["client_visible"] = bool(data.get("visible", True))
        state["client_focused"] = bool(data.get("focused", True))
        state["client_state_seen_at"] = time.time()
    return jsonify({"ok": True})

@app.route("/api/lyrics-offset", methods=["POST"])
def api_lyrics_offset():
    value, field, error = _number_field(_json_body(), "offset_ms")
    if error:
        return _api_error_response("bad_request", error, 400, field=field)
    # clamp to a reasonable range (-10s to +10s)
    v = max(-10000, min(10000, int(round(value))))
    with state_lock:
        state["lyrics_offset_ms"] = v
    update_config(lambda cfg: cfg.__setitem__("lyrics_offset_ms", state["lyrics_offset_ms"]))
    return jsonify({"lyrics_offset_ms": v})

@app.route("/api/playlists")
def api_playlists():
    """Return user's playlists (owned + collaborative — ones they can modify)."""
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    with state_lock:
        active = state["playlist_id"]
        my_id = state.get("spotify_user_id")
    try:
        if not my_id:
            me = sp.current_user()
            my_id = me["id"]
            with state_lock:
                state["spotify_user_id"] = my_id
        playlists = []
        results = sp.current_user_playlists(limit=50)
        while results:
            for pl in results.get("items", []):
                if not pl:
                    continue
                # Modifiable = owned by me or collaborative
                owned = (pl.get("owner") or {}).get("id") == my_id
                collab = bool(pl.get("collaborative"))
                if not (owned or collab):
                    continue
                playlists.append({
                    "id": pl["id"],
                    "name": pl["name"],
                    "owner": (pl.get("owner") or {}).get("display_name") or "—",
                    "tracks": playlist_total_from_meta(pl, 0),
                    "image": (pl["images"][0]["url"]
                              if pl.get("images") else None),
                    "owned": owned,
                    "collaborative": collab,
                })
            if results.get("next"):
                results = sp.next(results)
            else:
                break
        playlists.sort(key=lambda p: (p["id"] != active, p["name"].lower()))
        set_cached_playlist_list(playlists, active)
        return jsonify({"playlists": playlists, "active_id": active})
    except SpotifyGuardBlocked as e:
        # Local guard blocked it — serve cache silently if available
        cached = get_cached_playlist_list()
        if cached:
            return jsonify({**cached, "warning": None})
        return jsonify({"error": "guard", "retry_after": int(e.retry_after or 10)}), 503
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            cached = get_cached_playlist_list()
            if cached:
                return jsonify({
                    **cached,
                    "warning": "Spotify is rate-limiting playlist lookups, showing cached results.",
                })
            retry_after = None
            try:
                if getattr(e, "headers", None):
                    retry_after = e.headers.get("Retry-After")
            except Exception:
                pass
            return jsonify({"error": "rate_limited", "retry_after": int(retry_after or 30)}), 429
        return jsonify({"error": f"Couldn't list playlists: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"Couldn't list playlists: {e}"}), 500

@app.route("/api/set-playlist", methods=["POST"])
def api_set_playlist():
    """Switch the active backlog playlist. Accepts a raw ID, share URL, or spotify URI.

    Validates access (two cheap Spotify reads) and returns at once with the sync status:
    the backlog index loads the playlist in the background (a persisted copy is usable
    immediately), so this request never pages the playlist itself.
    """
    data = _json_body()
    raw = data.get("playlist_id") or data.get("playlist") or data.get("id") or ""
    pid = parse_playlist_id(raw) if isinstance(raw, str) else None
    if not pid:
        return jsonify({"error": "Couldn't parse a Spotify playlist ID from that input."}), 400

    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401

    # Validate the playlist exists & we can read it
    try:
        meta = spotify_playlist_meta(
            sp, pid, fields="name,owner.id,owner.display_name,collaborative,tracks.total,items.total,snapshot_id")
    except SpotifyGuardBlocked as e:
        return jsonify({"error": "Spotify is pacing requests; try again shortly.",
                        "retry_after": int(e.retry_after or 10)}), 503
    except Exception as e:
        return jsonify({"error": f"Couldn't load that playlist: {e}"}), 400

    # Check whether we can modify it (otherwise removals will silently fail)
    with state_lock:
        my_id = state.get("spotify_user_id")
    if not my_id:
        try:
            me = sp.current_user()
            my_id = me["id"]
            with state_lock:
                state["spotify_user_id"] = my_id
        except Exception:
            my_id = None
    owner_id = (meta.get("owner") or {}).get("id")
    can_modify = (owner_id == my_id) or bool(meta.get("collaborative"))
    if not can_modify:
        return jsonify({
            "error": (f"You don't own '{meta.get('name')}' and it's not collaborative — "
                      f"removals will fail. Pick a playlist you own or copy it first.")
        }), 400

    # Apply: the index is the only writer of membership/total state from here on.
    name = meta.get("name", "")
    with state_lock:
        state["playlist_id"] = pid
        state["playlist_name"] = name
    _backlog_update_config(lambda cfg: cfg.__setitem__("playlist_id", pid))
    with _playlist_list_lock:
        _playlist_list_cache["active_id"] = pid
    index = _backlog_index()
    index.switch(pid)
    _backlog_engine().poke()

    return jsonify({
        "message": f"Now cleaning from '{name}'.",
        "playlist_id": pid,
        "playlist_name": name,
        "backlog": index.status(),
    })


@app.route("/api/playlist-tracks")
def api_playlist_tracks():
    """Read-only view of the backlog index (legacy shape, used by the classic UI's queue).

    ?offset=&limit= page it (without limit, every known track from offset on). It never
    pages Spotify and never writes membership state; ?force=1 asks the index to re-check.
    """
    try:
        offset = max(0, int(request.args.get("offset") or 0))
        raw_limit = request.args.get("limit")
        limit = None if raw_limit in (None, "") else max(1, min(1000, int(raw_limit)))
    except (TypeError, ValueError):
        return jsonify({"error": "offset and limit must be whole numbers"}), 400
    index = _backlog_index()
    if request.args.get("force") == "1":
        index.request_sync()
    status = index.status()
    items = index.items()
    end = len(items) if limit is None else offset + limit
    tracks = []
    for pos in range(offset, min(end, len(items))):
        e = items[pos]
        tid = e.get("track_id")
        if not tid:
            continue  # local files / episodes can't be played or cleared from here
        tracks.append({
            "id": tid,
            "position": pos,
            "name": e.get("name") or "",
            "artists": e.get("artists") or "",
            "artist_list": e.get("artist_list") or [],
            "album": e.get("album") or "",
            "image": e.get("image"),
            "uri": e.get("uri") or f"spotify:track:{tid}",
            "duration_ms": int(e.get("duration_ms") or 0),
            "added_at": e.get("added_at"),
            "genres": get_cached_track_genres(tid),
        })
    return jsonify({
        "playlist_id": status["playlist_id"],
        "tracks": tracks,
        "total": status["total"],
        "offset": offset,
        "limit": limit,
        "loaded": status["known"],
        "complete": status["complete"],
        "loading": status["loading"],
        "partial": not status["complete"],
        "duration_ms": status["duration_ms"] if status["duration_ms"] is not None
        else sum(t["duration_ms"] for t in tracks),
    })


_TRACK_URI_RE = re.compile(r"spotify:track:[A-Za-z0-9]{1,64}")
_CONTEXT_URI_RE = re.compile(r"spotify:(album|playlist|artist|show):[A-Za-z0-9]{1,64}")


def _playback_error_response(e):
    """Legacy-shaped error for a failed playback command (guard blocks answer 503 + retry_after)."""
    if isinstance(e, SpotifyGuardBlocked):
        return jsonify({"error": "Spotify is pacing requests; try again shortly.",
                        "retry_after": int(e.retry_after or 5)}), 503
    if isinstance(e, spotipy.SpotifyException) and e.http_status == 429:
        return jsonify({"error": "Spotify is rate-limiting playback commands.",
                        "retry_after": spotify_retry_after_seconds(e, minimum=1, maximum=86400)}), 429
    return jsonify({"error": str(e)}), 500


def _playback_changed(is_playing=None, progress_ms=None):
    """Reflect a playback command in state right away and wake the poller to confirm it."""
    with state_lock:
        if is_playing is not None:
            state["is_playing"] = bool(is_playing)
        if progress_ms is not None:
            state["progress_ms"] = int(progress_ms)
    _backlog_engine().poke()


@app.route("/api/play-track", methods=["POST"])
def api_play_track():
    """Play a track. Backlog tracks play in the backlog's context (so the queue keeps flowing
    through it); anything else plays in the given album/playlist context or on its own."""
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    data = _json_body()
    track_uri = data.get("uri")
    if not isinstance(track_uri, str) or not _TRACK_URI_RE.fullmatch(track_uri.strip()):
        return jsonify({"error": "Missing or invalid track uri (expected spotify:track:<id>)."}), 400
    track_uri = track_uri.strip()
    context_uri = data.get("context_uri")
    if context_uri is not None and (not isinstance(context_uri, str)
                                    or not _CONTEXT_URI_RE.fullmatch(context_uri.strip())):
        return jsonify({"error": "Invalid context_uri."}), 400
    tid = track_uri.rsplit(":", 1)[-1]
    index = _backlog_index()
    if index.contains(tid) is True and index.pid:
        kwargs = {"context_uri": f"spotify:playlist:{index.pid}", "offset": {"uri": track_uri}}
        mode = "backlog"
    elif context_uri and not context_uri.startswith("spotify:artist:"):
        kwargs = {"context_uri": context_uri.strip(), "offset": {"uri": track_uri}}
        mode = "context"
    else:
        # Artist contexts can't start at a given track; play the track itself.
        kwargs = {"uris": [track_uri]}
        mode = "track"
    try:
        # Find a device to play on — prefer active one, fall back to first available
        device_id = None
        try:
            devices = sp.devices().get("devices") or []
            active = next((d for d in devices if d.get("is_active")), None)
            if active:
                device_id = active["id"]
            elif devices:
                device_id = devices[0]["id"]
        except Exception:
            pass
        sp.start_playback(device_id=device_id, **kwargs)
    except Exception as e:
        return _playback_error_response(e)
    _playback_changed(is_playing=True, progress_ms=0)
    return jsonify({"ok": True, "is_playing": True, "context": mode})


@app.route("/api/playback/<action>", methods=["POST"])
def api_playback_control(action):
    if action not in ("toggle", "next", "prev"):
        return jsonify({"error": f"Unknown action: {action}"}), 400
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        if action == "toggle":
            try:
                pb = sp.current_playback()
                playing = bool(pb and pb.get("is_playing"))
            except SpotifyGuardBlocked:
                with state_lock:
                    playing = bool(state["is_playing"])
            if playing:
                sp.pause_playback()
            else:
                sp.start_playback()
            now_playing = not playing
            _playback_changed(is_playing=now_playing)
        elif action == "next":
            sp.next_track()
            now_playing = True
            _playback_changed(is_playing=True, progress_ms=0)
        else:
            sp.previous_track()
            now_playing = True
            _playback_changed(is_playing=True, progress_ms=0)
    except Exception as e:
        return _playback_error_response(e)
    return jsonify({"ok": True, "is_playing": now_playing})


@app.route("/api/seek", methods=["POST"])
def api_seek():
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    data = _json_body()
    pos = data.get("position_ms")
    # bool is an int subclass; NaN/inf fail the range check.
    if isinstance(pos, bool) or not isinstance(pos, (int, float)) or not (0 <= pos <= 24 * 3600 * 1000):
        return jsonify({"error": "position_ms must be a number of milliseconds >= 0."}), 400
    pos = int(pos)
    with state_lock:
        duration = int(state.get("duration_ms") or 0)
    if duration:
        pos = min(pos, max(0, duration - 1000))
    try:
        sp.seek_track(pos)
    except Exception as e:
        return _playback_error_response(e)
    _playback_changed(progress_ms=pos)
    return jsonify({"ok": True, "position_ms": pos})


def _search_rate_limited_response(retry_after, local):
    return _api_error_response(
        "rate_limited",
        f"Spotify search is cooling down for {_fmt_wait(retry_after)}. Your history and Last.fm still work.",
        429, retry_after=int(retry_after), local=bool(local))


@app.route("/api/search")
def api_search():
    sp = get_sp()
    if not sp:
        return _signed_out_response()
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"tracks": [], "artists": []})
    if len(q) > 200:
        return _api_error_response("bad_request", "Search text is too long.", 400, field="q")
    # Answer at once during a cool-down: this used to sleep out the whole backoff
    # (sometimes hours) while holding the lock every search shares.
    if _sp_search_rate_limited():
        return _search_rate_limited_response(_sp_search_backoff_remaining_s(), local=False)
    search_type = request.args.get("type", "track")
    # Normalize — only allow track and artist to avoid unexpected 400s
    allowed = {p for p in search_type.split(",") if p in ("track", "artist")}
    if not allowed:
        allowed = {"track"}
    search_type = ",".join(sorted(allowed))
    try:
        _sp_search_throttle()
        results = sp.search(q=q, type=search_type, limit=8) or {}
    except SpotifyGuardBlocked as e:
        return _search_rate_limited_response(spotify_retry_after_from(e, default=10), local=True)
    except spotipy.SpotifyException as e:
        if e.http_status == 429:
            _note_sp_search_rate_limit(e)
            return _search_rate_limited_response(spotify_retry_after_from(e, default=30), local=False)
        return _api_error_response("spotify_error", f"Spotify search failed: {e.msg if hasattr(e, 'msg') else e}", 502)
    tracks = []
    for item in ((results.get("tracks") or {}).get("items") or []):
        if not item or not item.get("id"):
            continue
        images = (item.get("album") or {}).get("images") or []
        tracks.append({
            "id": item["id"],
            "name": item.get("name", ""),
            "artists": ", ".join(a.get("name", "") for a in (item.get("artists") or [])),
            "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in (item.get("artists") or [])],
            "album": (item.get("album") or {}).get("name", ""),
            "album_id": (item.get("album") or {}).get("id"),
            "image": images[-1]["url"] if images else None,
            "uri": item.get("uri"),
            "duration_ms": item.get("duration_ms", 0),
        })
    artists = []
    for a in ((results.get("artists") or {}).get("items") or []):
        if not a or not a.get("id"):
            continue
        images = a.get("images") or []
        artists.append({
            "id": a["id"],
            "name": a.get("name", ""),
            "genres": (a.get("genres") or [])[:3],
            "image": images[-1]["url"] if images else None,
            "url": (a.get("external_urls") or {}).get("spotify"),
        })
    return jsonify({"tracks": tracks, "artists": artists})


@app.route("/api/catch-up", methods=["POST"])
def api_catch_up():
    """Legacy: clear every recently played track that is still in the backlog (one Spotify call)."""
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    from src.web import backlog_api
    core = sys.modules[__name__]
    index = _backlog_index()
    engine = _backlog_engine()
    try:
        candidates = backlog_api.catch_up_candidates(core, sp, index, engine)
    except Exception as e:
        if isinstance(e, spotipy.SpotifyException) and e.http_status == 429:
            return jsonify({"error": "Spotify is pacing requests; try again shortly.",
                            "retry_after": spotify_retry_after_seconds(e, minimum=1, maximum=86400)}), 429
        return jsonify({"error": f"Couldn't fetch recently played: {e}"}), 500
    syncing = "" if index.status()["complete"] else " (the backlog is still syncing, so only confirmed tracks were checked)"
    if not candidates:
        return jsonify({"message": "Nothing to catch up - no recent tracks were in the playlist." + syncing,
                        "removed": 0, "cleared": []})
    try:
        entries, _skipped = backlog_api.clear_catch_up(core, sp, index, engine,
                                                       [c["id"] for c in candidates][:100])
    except Exception as e:
        return _playback_error_response(e)
    engine.poke()
    return jsonify({"message": f"Caught up: removed {len(entries)} track(s).{syncing}",
                    "removed": len(entries), "cleared": entries})


def _track_changed_response(current_tid):
    """409 for an action pinned to a track that is no longer the current one."""
    return jsonify({"error": {
        "code": "track_changed",
        "message": "The track changed, so nothing was done. Check what's playing and try again.",
        "current_track_id": current_tid,
    }}), 409


def _expected_track_id(data):
    """The optional track_id a client pins its action to (None when absent)."""
    expected = data.get("track_id")
    if expected is None:
        return None, None
    if not isinstance(expected, str) or not expected.strip():
        return None, (jsonify({"error": "track_id must be a Spotify track id."}), 400)
    return expected.strip(), None


def _unpinned_action_refused(tid):
    """409 for an action without track_id right after the track changed, else None.

    Clients that don't pin their action (classic UI, /widget) can still show the previous track
    for one of their polls after a change; acting on "whatever plays now" would then like or
    clear a track that has barely started. Restarting the same track is not a change.
    """
    try:
        play = _backlog_engine().tracker.play
    except Exception:
        return None
    if (play is None or play.track_id != tid or play.seen_mono is None
            or play.prev_track_id in (None, tid)):
        return None
    grace_s = max(3.5, float(POLL_INTERVAL) + 1.0)
    if time.monotonic() - play.seen_mono >= grace_s:
        return None
    return jsonify({"error": "The track just changed, so nothing was done. Check what's playing and try again.",
                    "code": "track_changed", "current_track_id": tid}), 409


def _clear_current_from_backlog(sp, track_info, reason, source, listened):
    """Clear the current track if the index confirms it is in the backlog.

    Returns (removed, queued): a rate-limited removal is queued in pending_removals and
    happens as soon as Spotify allows, so the action the user asked for is never lost.
    """
    tid = (track_info or {}).get("id")
    index = _backlog_index()
    if not tid or index.contains(tid) is not True:
        return False, False
    engine = _backlog_engine()
    try:
        entries = engine.clear_tracks(sys.modules[__name__], sp, [track_info], reason, source,
                                      listened_s=round(float(listened or 0), 1))
        if not entries:
            return False, False   # removed elsewhere meanwhile; nothing of ours to report
    except Exception as e:
        _log_app_error(e, context=f"clear current ({reason})")
        if isinstance(e, spotipy.SpotifyException) and e.http_status == 429 and engine.pending is not None:
            from src.services.clearing import _retry_after_of
            engine.pending.add(tid, index.pid, reason, round(float(listened or 0), 1), track_info,
                               error=str(e), retry_after_s=_retry_after_of(e))
            return False, True
        return False, False
    with state_lock:
        if state.get("current_track_id") == tid:
            state["current_in_playlist"] = False
    engine.poke()
    return True, False


@app.route("/api/skip-remove", methods=["POST"])
def api_skip_remove():
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    data = _json_body()
    expected, err = _expected_track_id(data)
    if err:
        return err
    with state_lock:
        track_info = dict(state["current_track"]) if state["current_track"] else None
        tid = state["current_track_id"]
        listened = state["current_listened"]

    if not tid or not track_info:
        return jsonify({"error": "No current track."}), 400
    if expected and expected != tid:
        return _track_changed_response(tid)
    refused = None if expected else _unpinned_action_refused(tid)
    if refused:
        return refused

    removed, queued = _clear_current_from_backlog(sp, track_info, "manual", "manual-skip", listened)

    try:
        sp.next_track()
    except Exception as e:
        msg = "Removed from playlist" if removed else ("Removal queued" if queued else "")
        return jsonify({"error": f"{msg}{' but ' if msg else ''}skip failed: {e}. Make sure a Spotify device is active."}), 500
    _playback_changed(is_playing=True, progress_ms=0)

    if removed:
        return jsonify({"message": "Removed and skipped.", "removed": True})
    if queued:
        return jsonify({"message": "Skipped. Spotify is pacing requests, so the removal is queued.",
                        "removed": False, "queued": True})
    return jsonify({"message": "Skipped (track wasn't in the playlist).", "removed": False})

@app.route("/api/like", methods=["POST"])
def api_like():
    """Like the current track (optionally pinned by track_id) and clear it from the backlog."""
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    data = _json_body()
    expected, err = _expected_track_id(data)
    if err:
        return err
    with state_lock:
        track_info = dict(state["current_track"]) if state["current_track"] else None
        tid = state["current_track_id"]
        already_liked = bool(state["current_is_liked"])
        listened = state["current_listened"]
        today = current_day_str()
        liked_today_already = any(
            e.get("type") == "liked" and e.get("track_id") == tid and e.get("day") == today
            for e in state["event_history"][-500:]
        )

    if not tid or not track_info:
        return jsonify({"error": "No current track."}), 400
    if expected and expected != tid:
        return _track_changed_response(tid)
    refused = None if expected else _unpinned_action_refused(tid)
    if refused:
        return refused
    already_liked = already_liked or get_cached_liked_state(tid) is True

    _auto_liked = []
    try:
        if not already_liked:
            sp.current_user_saved_tracks_add([tid])
            note_liked_track_in_artist_counts(track_info)
            if not liked_today_already:
                bump_stat("liked_total")
                record_history_event("liked", track=track_info, listened_s=listened, source="manual-like")
        # Auto-like artist if threshold reached, including pre-existing Spotify likes
        _auto_liked = _check_auto_artist_like(sp, track_info)
        set_cached_liked_state(tid, True)
        with state_lock:
            if state["current_track_id"] == tid:
                state["current_is_liked"] = True
    except Exception as e:
        if isinstance(e, spotipy.SpotifyException) and e.http_status == 429:
            return _playback_error_response(e)
        return jsonify({"error": f"Like failed: {e}"}), 500

    removed, queued = _clear_current_from_backlog(sp, track_info, "like", "manual-like", listened)

    auto_note = (", ".join(f"★ {n}" for n in (_auto_liked or [])))
    auto_suffix = f" {auto_note} auto-liked!" if auto_note else ""
    if queued:
        auto_suffix = " Removal from the backlog is queued (Spotify is pacing requests)." + auto_suffix

    # Structured flags so clients never have to parse the message text.
    flags = {"auto_liked": _auto_liked or [], "liked": True, "already_liked": bool(already_liked),
             "removed": bool(removed), "queued": bool(queued)}
    if already_liked and not removed:
        return jsonify({"message": "Already in your Liked Songs." + auto_suffix, **flags})
    if already_liked and removed:
        return jsonify({"message": "Already liked - removed from discover playlist." + auto_suffix, **flags})
    if removed:
        return jsonify({"message": "Liked & removed from discover playlist." + auto_suffix, **flags})
    return jsonify({"message": "Liked." + auto_suffix, **flags})

@app.route("/api/favorites", methods=["GET", "POST"])
def api_favorites():
    if request.method == "GET":
        with state_lock:
            return jsonify({
                "artists": sorted(state["favorite_artists"]),
                "opt_outs": sorted(state.get("favorite_artist_opt_outs") or []),
            })
    data = _json_body()
    action = data.get("action") or "toggle"
    name = data.get("artist")
    if not isinstance(action, str) or action.lower() not in ("add", "remove", "toggle"):
        return _api_error_response("bad_request", "action must be add, remove or toggle.", 400, field="action")
    action = action.lower()
    if not isinstance(name, str) or not name.strip():
        return _api_error_response("bad_request", "Provide the artist name.", 400, field="artist")
    name = name.strip()[:200]
    if action == "add":
        add_favorite_artist(name, source="manual")
        verb = "added"
        opted_out = False
    elif action == "remove":
        remove_favorite_artist(name, opt_out=True)
        verb = "removed"
        opted_out = True
    else:  # toggle
        if is_favorite_artist(name):
            remove_favorite_artist(name, opt_out=True)
            verb = "removed"
            opted_out = True
        else:
            add_favorite_artist(name, source="manual")
            verb = "added"
            opted_out = False
    return jsonify({
        "message": f"{verb}: {name}",
        "is_favorite": is_favorite_artist(name),
        "opted_out": opted_out,
    })

@app.route("/api/refresh-feeds", methods=["POST"])
def api_refresh_feeds():
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    threading.Thread(target=refresh_feeds, args=(sp,), kwargs={"force": True},
                     daemon=True).start()
    return jsonify({"message": "Refreshing feeds..."})

@app.route("/api/add-to-playlist", methods=["POST"])
def api_add_to_playlist():
    """Add the current track (optionally pinned by track_id) to another playlist and, unless
    remove_from_backlog is false, clear it from the backlog ("routed")."""
    sp = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401

    data = _json_body()
    raw_target = data.get("playlist_id") or data.get("playlist") or ""
    target_pid = parse_playlist_id(raw_target) if isinstance(raw_target, str) else None
    if "remove_from_backlog" in data:
        also_remove = data.get("remove_from_backlog")
        if not isinstance(also_remove, bool):
            return jsonify({"error": "remove_from_backlog must be true or false."}), 400
    else:
        also_remove = bool(data.get("also_remove", True))
    expected, err = _expected_track_id(data)
    if err:
        return err

    if not target_pid:
        return jsonify({"error": "Pick a playlist."}), 400

    with state_lock:
        track_info = dict(state["current_track"]) if state["current_track"] else None
        tid = state["current_track_id"]
        listened = state["current_listened"]
        discover_id = state["playlist_id"]

    if not tid or not track_info:
        return jsonify({"error": "No current track."}), 400
    if expected and expected != tid:
        return _track_changed_response(tid)
    refused = None if expected else _unpinned_action_refused(tid)
    if refused:
        return refused

    if target_pid == discover_id:
        return jsonify({"error": "That's your discover playlist \u2014 pick a different one."}), 400

    # Add
    try:
        sp.playlist_add_items(target_pid, [tid])
    except Exception as e:
        if isinstance(e, spotipy.SpotifyException) and e.http_status == 429:
            return _playback_error_response(e)
        return jsonify({"error": f"Add failed: {e}"}), 500

    # Get target playlist name for the toast
    pl_name = get_cached_playlist_name(target_pid) or "playlist"
    if pl_name == "playlist":
        try:
            pl_name = sp.playlist(target_pid, fields="name").get("name", "playlist")
        except Exception:
            pass

    # One 'added' history event (dual-written to SQLite as TRACK_ADDED_TO_PLAYLIST).
    record_history_event("added", track=track_info, listened_s=listened, source="manual-route",
                         target=pl_name, extra={"target_playlist_id": target_pid})
    bump_stat("added_total")

    removed, queued = (False, False)
    if also_remove:
        removed, queued = _clear_current_from_backlog(sp, track_info, "routed", "manual-route", listened)

    msg = f"Added to '{pl_name}'."
    if removed:
        msg += " Removed from discover playlist."
    elif queued:
        msg += " Removal from the backlog is queued (Spotify is pacing requests)."

    return jsonify({"message": msg, "removed": removed, "queued": queued, "playlist_name": pl_name})


# ---------- Main ----------
def _parse_cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Discover Cleaner: clears your Spotify backlog playlist as you listen, "
                    "with Last.fm-style listening reports and artist pages.")
    parser.add_argument("--demo", action="store_true",
                        help="offline demo: a simulated Spotify and a bundled listening history "
                             "(kept in .demo-data/; no Spotify account needed)")
    parser.add_argument("--reset", action="store_true",
                        help="with --demo: start the demo over from the bundled snapshot")
    parser.add_argument("--port", type=int, default=None, metavar="N",
                        help="port to serve on (default 8888)")
    args = parser.parse_args(argv)
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if args.reset and not args.demo:
        parser.error("--reset only applies to the offline demo: use --demo --reset")
    return args


def main(argv=None):
    global SERVER_PORT, _oauth
    args = _parse_cli(argv)
    # A redirected console on Windows (run.bat > log) encodes as cp1252, which cannot print the
    # status marks below; an unencodable mark must never abort startup (e.g. inside the SQLite init).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass
    if args.demo and not DEMO_MODE:
        # The demo's data folder is chosen at import time; it cannot be switched on afterwards.
        print("  ✗ Start the offline demo with: python app.py --demo")
        return 2
    SERVER_PORT = args.port or 8888
    url = f"http://127.0.0.1:{SERVER_PORT}"
    if not DEMO_MODE:
        _oauth = build_oauth_manager()  # its default Redirect URI follows --port
        _uri = (os.environ.get("SPOTIPY_REDIRECT_URI") or "").strip()
        if _uri and urllib.parse.urlsplit(_uri).port not in (SERVER_PORT, None):
            print(f"  ! SPOTIPY_REDIRECT_URI ({_uri}) points at another port than {SERVER_PORT}: "
                  f"Spotify sign-in will fail until they match.")

    # With the auto-reloader on, this process only watches files and re-spawns a child
    # (WERKZEUG_RUN_MAIN=true) that serves requests. Background work must start in that
    # child only, or the poll loop (and every auto-removal) would run twice.
    serving_process = not USE_RELOADER or os.environ.get("WERKZEUG_RUN_MAIN") == "true"

    # --- SQLite init + one-time migration ---
    if _SQLITE_ENABLED and serving_process:
        try:
            _db.init_db()
            _migrate.migrate_if_needed()
            _migration_error = _db.MIGRATION_STATUS.get("error")
            if _migration_error:
                # The app keeps running on the older schema; say so instead of "ready".
                print(f"  \u2717 Database upgrade failed ({_migration_error}); the database was left "
                      f"unchanged at v{_db.schema_version()} and Last.fm sync is paused until the "
                      f"upgrade succeeds on a later start.")
            else:
                print("  \u2713 SQLite persistence ready (music_explorer.db)")
            _restore_guard_state()
            _revalidate_restored_guard_state()
        except Exception as _dbe:
            print(f"  \u2717 SQLite init failed: {_dbe}")

    if serving_process:
        # Start the poll loop. It will idle until /callback completes auth.
        t = threading.Thread(target=poll_loop, daemon=True)
        t.start()
        ensure_background_workers()

    if lastfm_api_key():
        _lfm_user = lastfm_username()
        print(f"  \u2713 Last.fm enrichment enabled" + (f" (user: {_lfm_user})" if _lfm_user else " (no username \u2014 set LASTFM_USERNAME for personalization)"))
    elif not DEMO_MODE:
        print("  \u00b7 Last.fm not configured (optional: add it on the Setup screen or in Settings for your full history)")

    if DEMO_MODE:
        print("\n  \u2713 Offline demo: simulated Spotify, the author's listening history (data in .demo-data/).")
        print("    Nothing here talks to Spotify. Start over with --demo --reset.")
        print(f"  \u2192 Open {url}\n")
    elif not spotify_credentials_configured():
        print("\n  \u00b7 No Spotify app set up yet.")
        print(f"  \u2192 Open {url} and follow the Setup steps (Redirect URI: {spotify_redirect_uri()})\n")
    elif not get_cached_token():
        print("\n  No valid token cached.")
        print(f"  \u2192 Open {url} and click 'Connect Spotify'\n")
    else:
        print("\n  \u2713 Cached token loaded.")
        print(f"  \u2192 Open {url}\n")

    _dm = 'on' if DEBUG_MODE else 'off'
    _rl = 'on' if USE_RELOADER else 'off'
    _ext = 'on' if SPOTIFY_EXTENDED_ENDPOINTS else 'off'
    print(f"  \u00b7 Debug mode: {_dm} (reloader: {_rl})")
    print(f"  \u00b7 Spotify extended endpoints: {_ext}")
    app.run(host="127.0.0.1", port=SERVER_PORT, debug=DEBUG_MODE, use_reloader=USE_RELOADER)


# v2 API blueprints (src/web/*). Registered at import time so every entry point
# (main() and the dev sandbox harness) serves them.
try:
    from src.web import register_blueprints as _register_blueprints
    _register_blueprints(app)
except Exception as _bp_err:  # pragma: no cover
    _log_app_error(_bp_err, context="register_blueprints")

# Offline demo: every Spotify call goes to the simulated FakeSpotify (src/devtools/fake_spotify.py)
# through the real guard, so the poll loop, clearing engine and UI run exactly as with Spotify.
if DEMO_MODE:
    _demo.install(sys.modules[__name__])


if __name__ == "__main__":
    sys.exit(main())

