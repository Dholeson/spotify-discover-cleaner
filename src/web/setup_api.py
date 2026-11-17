"""First-run setup: connect your own Spotify app (and optionally Last.fm).

GET  /api/v2/setup
    What the Setup screen needs: whether Spotify app credentials are configured, the exact
    Redirect URI to register, whether Spotify is connected and a backlog playlist chosen, and
    whether this is the offline demo (where setup is switched off). Secrets are never returned.

POST /api/v2/setup {client_id, client_secret, lastfm_api_key?, lastfm_username?}
    Validates everything first (Client ID / Secret / Last.fm key: 32 hex characters; Last.fm
    username: 2-15 letters, digits, - or _), then writes .env atomically through the settings
    writer (other keys and comments are kept; a missing .env is created) and applies the values
    to the running app, so "Connect Spotify" (/login) works without a restart. An empty
    client_id / client_secret keeps the saved one. Like every POST it must come from this
    app's own origin (see app._guard_request_origin). 403 in the offline demo.

Status extension: /api/status gains {"setup": {demo, needs_credentials, needs_playlist}}.
"""

from __future__ import annotations

import os
import re

from flask import Blueprint, jsonify

from src.web import api_error

bp = Blueprint("setup_api", __name__)

DASHBOARD_URL = "https://developer.spotify.com/dashboard"
LASTFM_API_URL = "https://www.last.fm/api/account/create"
HEX32_RE = re.compile(r"[0-9a-fA-F]{32}")
LASTFM_USER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,14}")
ENV_HEADER = (
    "# Discover Cleaner settings, written by the Setup screen.\n"
    "# Every option is explained in .env.example. Keep this file private: it holds your\n"
    "# Spotify app's secret. Most settings can also be changed in the app under Settings.\n"
)


def _core():
    import app as core
    return core


def _env(name):
    return (os.environ.get(name) or "").strip()


def _signed_in(core):
    with core.state_lock:
        if core.state.get("auth_ready"):
            return True
    try:
        return bool(core.get_cached_token())
    except Exception:
        return False


def _needs_playlist(core, signed_in):
    with core.state_lock:
        return bool(signed_in and not (core.state.get("playlist_id") or "").strip())


def setup_state(core=None):
    core = core or _core()
    demo = bool(getattr(core, "DEMO_MODE", False))
    configured = demo or core.spotify_credentials_configured()
    signed_in = demo or (configured and _signed_in(core))
    client_id = _env("SPOTIPY_CLIENT_ID")
    return {
        "demo": demo,
        "needs_credentials": not configured,
        "signed_in": signed_in,
        "needs_playlist": _needs_playlist(core, signed_in),
        "redirect_uri": core.spotify_redirect_uri(),
        "dashboard_url": DASHBOARD_URL,
        "lastfm_api_url": LASTFM_API_URL,
        "login_url": "/login",
        "client_id_set": bool(client_id),
        "client_id_hint": ("…" + client_id[-4:]) if len(client_id) > 8 else "",
        "client_secret_set": bool(_env("SPOTIPY_CLIENT_SECRET")),
        "lastfm_api_key_set": bool(_env("LASTFM_API_KEY")),
        "lastfm_username": _env("LASTFM_USERNAME"),
        "env_file_exists": core.ENV_PATH.exists(),
    }


def status_extension():
    core = _core()
    demo = bool(getattr(core, "DEMO_MODE", False))
    configured = demo or core.spotify_credentials_configured()
    with core.state_lock:
        auth_ready = bool(core.state.get("auth_ready"))
        has_playlist = bool((core.state.get("playlist_id") or "").strip())
    return {"setup": {
        "demo": demo,
        "needs_credentials": not configured,
        "needs_playlist": bool(configured and auth_ready and not has_playlist),
    }}


def init(core):
    core.register_status_extension(status_extension)


@bp.route("/api/v2/setup", methods=["GET"])
def get_setup():
    return jsonify(setup_state())


def _validate(data):
    """(updates {ENV_KEY: value}, field errors {field: message})."""
    updates, errors = {}, {}

    def text(field):
        value = data.get(field)
        if value is None:
            return ""
        if not isinstance(value, str):
            errors[field] = "Must be text."
            return ""
        return value.strip()

    for field, key, label in (("client_id", "SPOTIPY_CLIENT_ID", "Client ID"),
                              ("client_secret", "SPOTIPY_CLIENT_SECRET", "Client Secret")):
        value = text(field)
        if not value:
            if not _env(key):
                errors.setdefault(field, f"Paste your Spotify app's {label}.")
            continue
        if not HEX32_RE.fullmatch(value):
            errors[field] = f"A {label} is 32 characters, 0-9 and a-f. Copy it again from the Spotify dashboard."
            continue
        if value != _env(key):
            updates[key] = value
    if updates.get("SPOTIPY_CLIENT_SECRET") and updates.get("SPOTIPY_CLIENT_SECRET") == (
            updates.get("SPOTIPY_CLIENT_ID") or _env("SPOTIPY_CLIENT_ID")):
        errors["client_secret"] = "That's the Client ID. Click “View client secret” in the dashboard and copy that."

    key = text("lastfm_api_key")
    if key:
        if not HEX32_RE.fullmatch(key):
            errors["lastfm_api_key"] = "A Last.fm API key is 32 characters, 0-9 and a-f."
        elif key != _env("LASTFM_API_KEY"):
            updates["LASTFM_API_KEY"] = key
    user = text("lastfm_username")
    if user:
        if not LASTFM_USER_RE.fullmatch(user):
            errors["lastfm_username"] = "Last.fm usernames are 2-15 letters, digits, - or _, starting with a letter."
        elif user != _env("LASTFM_USERNAME"):
            updates["LASTFM_USERNAME"] = user
    if (user or _env("LASTFM_USERNAME")) and not (key or _env("LASTFM_API_KEY")) and "lastfm_username" not in errors:
        errors["lastfm_api_key"] = "Add a Last.fm API key too, or leave both Last.fm fields empty."
    return updates, errors


@bp.route("/api/v2/setup", methods=["POST"])
def post_setup():
    core = _core()
    if getattr(core, "DEMO_MODE", False):
        return api_error("demo_mode", "Setup is switched off in the offline demo: it uses a simulated Spotify. "
                                      "Run the app without --demo to connect your own.", 403)
    data = core._json_body()
    updates, errors = _validate(data)
    if errors:
        first = next(iter(errors))
        return api_error("invalid_setup", errors[first], 400, field=first, fields=errors)

    if updates:
        try:
            if not core.ENV_PATH.exists():
                core._write_text_atomic(core.ENV_PATH, ENV_HEADER)
            core.write_env_updates(updates)
        except ValueError as e:
            return api_error("invalid_setup", str(e), 400)
        except OSError as e:
            core._log_app_error(e, context="setup: write .env")
            return api_error("env_write_failed", f"Couldn't write the settings file (.env): {e}", 500)
        for k, v in updates.items():
            os.environ[k] = v
        _apply(core, updates)

    return jsonify({
        "ok": True,
        "saved": sorted(updates),
        "message": "Saved. Now connect Spotify." if updates else "Nothing changed.",
        "next": "/login",
        "setup": setup_state(core),
    })


def _apply(core, updates):
    """Make the saved values live, as the Settings page does for the same keys."""
    if any(k in core.SPOTIFY_AUTH_SETTINGS for k in updates):
        core.clear_spotify_cache_file()  # a token from another Spotify app must not be reused
        core._oauth = core.build_oauth_manager()
        core.reset_spotify_session_caches()
        with core.state_lock:
            core.state["auth_ready"] = False
            core.state["spotify_user_id"] = None
            core.state["last_error"] = None
    if "LASTFM_API_KEY" in updates or "LASTFM_USERNAME" in updates:
        core.LASTFM_API_KEY = core.lastfm_api_key()
        core.LASTFM_USERNAME = core.lastfm_username()
        core.lastfm_reset_caches()
        with core.state_lock:
            core.state["scrobbles_today"] = None
            core.state["scrobbles_today_fetched_at"] = 0
            core.state["weekly_top_fetched_at"] = 0
