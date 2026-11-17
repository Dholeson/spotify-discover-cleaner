"""Data & imports API: Last.fm / Spotify import control and the data-health report.

The import routes keep their original paths (/api/lastfm/import, /api/spotify/import) so
the classic UI keeps working; their GET payloads keep the legacy keys (page, total_pages,
imported, total, step, imported_*) next to the new ones.
"""

import logging
import sqlite3
import threading
import time

from flask import Blueprint, jsonify, request

from . import api_error

log = logging.getLogger(__name__)

bp = Blueprint("data_api", __name__)

# Per-source play counts need one scan of the ledger (~40 ms on 50K plays); the health page is
# opened far more often than the ledger changes, so the result is kept until it does.
_sources_cache = {"key": None, "value": None}
_sources_lock = threading.Lock()

# Last.fm error codes that mean the saved credentials are wrong (not a passing hiccup).
_LFM_BAD_USER = 6
_LFM_BAD_KEY = {10, 26}
_LFM_RATE_LIMIT = 29


def _sqlite_enabled():
    import app as core
    return bool(getattr(core, "_SQLITE_ENABLED", False))


def _body():
    """The JSON object body ({} when empty); None when it is not a JSON object."""
    if not request.data:
        return {}
    data = request.get_json(silent=True, force=True)
    return data if isinstance(data, dict) else None


@bp.get("/api/lastfm/import")
def lastfm_import_status():
    from src.services import importers
    if not _sqlite_enabled():
        return api_error("storage_unavailable", "The local history database is not available.", 503)
    return jsonify(importers.lastfm_status())


@bp.post("/api/lastfm/import")
def lastfm_import_start():
    from src.services import importers
    body = _body()
    if body is None:
        return api_error("bad_request", "Request body must be a JSON object.", 400)
    mode = body.get("mode")
    if mode is None:
        mode = "full" if body.get("reset") else "sync"  # legacy {reset: bool}
    if mode not in importers.LASTFM_MODES:
        return api_error("invalid_mode", "mode must be one of sync, backfill or full.", 400, field="mode")
    if not _sqlite_enabled():
        return api_error("storage_unavailable", "The local history database is not available.", 503)
    started, reason = importers.start_lastfm_sync(mode, trigger="manual")
    if reason == "not_configured":
        return api_error("lastfm_not_configured",
                         "Add your Last.fm API key and username in Settings first.", 400)
    if reason == "running":
        return api_error("already_running", "A Last.fm import is already running.", 409,
                         status=importers.lastfm_status())
    if reason == "upgrade_pending":
        return api_error("upgrade_pending", importers.UPGRADE_PENDING_MESSAGE, 409)
    payload = importers.lastfm_status()
    payload["message"] = "Import started"
    return jsonify(payload), 202


@bp.get("/api/spotify/import")
def spotify_import_status():
    from src.services import importers
    return jsonify(importers.spotify_import_status())


@bp.post("/api/spotify/import")
def spotify_import_start():
    import app as core
    from src.services import importers
    body = _body()
    if body is None:
        return api_error("bad_request", "Request body must be a JSON object.", 400)
    seed = body.get("seed_artist_likes", False)
    if not isinstance(seed, bool):
        return api_error("invalid_field", "seed_artist_likes must be true or false.", 400,
                         field="seed_artist_likes")
    if not _sqlite_enabled():
        return api_error("storage_unavailable", "The local history database is not available.", 503)
    if not core.get_sp():
        return api_error("not_authenticated", "Sign in to Spotify first.", 401)
    started, reason = importers.start_spotify_import(seed_artist_likes=seed)
    if reason == "running":
        return api_error("already_running", "A Spotify import is already running.", 409,
                         status=importers.spotify_import_status())
    payload = importers.spotify_import_status()
    payload["message"] = "Import started"
    return jsonify(payload), 202


@bp.get("/api/v2/data/health")
def data_health():
    from src.persistence import db as _db
    from src.persistence import plays as _plays
    from src.services import importers
    if not _sqlite_enabled():
        return api_error("storage_unavailable", "The local history database is not available.", 503)
    conn = _db.get_conn()
    ledger = _plays.status(conn)
    lfm = importers.lastfm_status(conn)
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += _db.DB_PATH.with_name(_db.DB_PATH.name + suffix).stat().st_size
        except OSError:
            pass
    sources = _play_sources(conn, (ledger["plays_total"], ledger["newest_play_ts"], ledger["built_at"]))
    return jsonify({
        "plays_total": ledger["plays_total"],
        "plays_ready": ledger["plays_ready"],
        "plays_rules_version": ledger["plays_rules_version"],
        "plays_rebuilding": ledger["rebuilding"],
        "plays_built_at": ledger["built_at"],
        "plays_build_ms": ledger["build_ms"],
        "rebuild_error": ledger.get("rebuild_error"),
        "rebuild_retry_at": ledger.get("rebuild_retry_at"),
        "newest_play_ts": ledger["newest_play_ts"],
        "newest_lastfm_ts": ledger["newest_lastfm_ts"],
        "live_since_ts": ledger["live_since_ts"],
        "lastfm_configured": lfm["configured"],
        "last_sync_at": lfm["last_sync_at"],
        "sync_running": bool(lfm.get("running")),
        "sync_status": lfm.get("status"),
        "sync_error": lfm.get("error"),
        "auto_sync_minutes": lfm.get("auto_sync_minutes"),
        "backfill_done": lfm.get("backfill_done"),
        "schema_version": _db.schema_version(conn),
        "schema_version_expected": _db.SCHEMA_VERSION,
        "migration_error": _db.MIGRATION_STATUS.get("error"),
        "db_size_bytes": size,
        "backups": _db.list_backups(),
        # Where the history comes from, so the page can say "Last.fm until May 12, this app since".
        "oldest_play_ts": min((s["first_ts"] for s in sources if s["first_ts"] is not None), default=None),
        "sources": sources,
        "backup_files": _backup_files(_db),
    })


def _play_sources(conn, key):
    """[{source, plays, first_ts, last_ts, bulk}] over canonical plays, oldest source first."""
    with _sources_lock:
        if _sources_cache["key"] == key and _sources_cache["value"] is not None:
            return _sources_cache["value"]
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*), MIN(ts_ms), MAX(ts_ms), SUM(ts_quality = 'bulk') FROM plays "
            "WHERE superseded_by IS NULL GROUP BY source ORDER BY MIN(ts_ms)").fetchall()
    except sqlite3.Error:
        return []  # ledger not built yet: nothing trustworthy to report
    value = [{"source": r[0], "plays": int(r[1]), "first_ts": r[2], "last_ts": r[3], "bulk": int(r[4] or 0)}
             for r in rows]
    with _sources_lock:
        _sources_cache.update(key=key, value=value)
    return value


def _backup_files(db_mod):
    out = []
    for name in db_mod.list_backups():
        try:
            st = db_mod.DB_PATH.with_name(name).stat()
        except OSError:
            continue
        out.append({"name": name, "size_bytes": st.st_size, "modified_ms": int(st.st_mtime * 1000)})
    return out


def _lastfm_failure_code(result, since):
    """The Last.fm error code behind a failed app.lastfm_call (None when unknown). The app keeps
    the last error in a shared dict, so it only counts when written after this call started."""
    from src.services import importers
    core = importers._core()
    if core is not None and result is getattr(core, "_LFM_MISSING", object()):
        return _LFM_BAD_USER
    if isinstance(result, dict) and result.get("error") is not None:
        try:
            return int(result.get("error"))
        except (TypeError, ValueError):
            return None
    info = getattr(core, "_lfm_last_error", None) if core is not None else None
    if isinstance(info, dict) and float(info.get("ts") or 0) >= since:
        return info.get("code")
    return None


def _lastfm_direct(core, params):
    """One Last.fm GET outside app.lastfm_call (which refuses to run without a saved key)."""
    import requests
    base = getattr(core, "LASTFM_BASE", "https://ws.audioscrobbler.com/2.0/")
    ua = getattr(core, "LRCLIB_UA", "DiscoverCleaner")
    try:
        r = requests.get(base, params={**params, "format": "json"}, timeout=8, headers={"User-Agent": ua})
        data = r.json()
    except Exception as exc:  # network error or a non-JSON page
        log.info("Last.fm check failed: %s", exc)
        return None
    return data if isinstance(data, dict) else None


@bp.route("/api/v2/data/lastfm/check", methods=["GET", "POST"])
def lastfm_check():
    """Prove a Last.fm username + API key work: one user.getInfo call.

    GET tests the saved pair. POST {username?, api_key?} tests typed values (each falls back
    to the saved one) WITHOUT saving them, so the page can refuse to save a wrong key."""
    from src.services import importers
    body = _body() if request.method == "POST" else {}
    if body is None:
        return api_error("bad_request", "Request body must be a JSON object.", 400)
    saved_key, saved_user = importers.lastfm_credentials()
    typed = {}
    for field, name in (("username", "LASTFM_USERNAME"), ("api_key", "LASTFM_API_KEY")):
        v = body.get(field)
        if v is None or v == "":
            continue
        if not isinstance(v, str) or len(v) > 200 or not v.strip() or any(c.isspace() for c in v.strip()):
            return api_error("invalid_field", f"{'Username' if field == 'username' else 'API key'} doesn't look right.",
                             400, field=name)
        typed[field] = v.strip()
    user = typed.get("username") or saved_user
    key = typed.get("api_key") or saved_key
    if not key or not user:
        missing = "LASTFM_USERNAME" if not user else "LASTFM_API_KEY"
        return api_error("lastfm_not_configured", "Add your Last.fm username and API key first.", 400,
                         field=missing)
    started = time.time()
    if key == saved_key:
        result = importers.lastfm_call("user.getinfo", user=user)
    elif saved_key:
        # app.lastfm_call lets params override the saved key (same session, UA and error record).
        result = importers.lastfm_call("user.getinfo", user=user, api_key=key)
    else:
        result = _lastfm_direct(importers._core(), {"method": "user.getinfo", "user": user, "api_key": key})
    info = result.get("user") if isinstance(result, dict) else None
    if isinstance(info, dict):
        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        registered = info.get("registered") if isinstance(info.get("registered"), dict) else {}
        reg_s = _int(registered.get("unixtime") or registered.get("#text"))
        image = None
        for img in info.get("image") or []:
            url = (img or {}).get("#text") if isinstance(img, dict) else None
            if isinstance(url, str) and url.startswith("https://"):
                image = url  # the list runs small -> extralarge; keep the largest
        url = info.get("url") if isinstance(info.get("url"), str) and info["url"].startswith("https://") else None
        return jsonify({"ok": True, "user": {
            "name": str(info.get("name") or user),
            "realname": str(info.get("realname") or "") or None,
            "playcount": _int(info.get("playcount")),
            "registered_ts": reg_s * 1000 if reg_s else None,
            "url": url,
            "image": image,
        }})
    code = _lastfm_failure_code(result, started)
    if code == _LFM_BAD_USER:
        return api_error("lastfm_user_not_found", f"Last.fm has no user named “{user}”. Check the username.",
                         400, field="LASTFM_USERNAME")
    if code in _LFM_BAD_KEY:
        return api_error("lastfm_bad_key", "Last.fm rejected the API key. Check it, or create a new one.",
                         400, field="LASTFM_API_KEY")
    if code == _LFM_RATE_LIMIT:
        return api_error("rate_limited", "Last.fm is limiting requests right now. Try again in a few minutes.",
                         429, retry_after=300)
    reason, _ = importers._lastfm_error_reason(result, since=started)
    return api_error("lastfm_unreachable", f"Couldn't reach Last.fm ({reason}). Try again shortly.", 502)


def init(core):
    """Register the plays-ledger and Last.fm auto-sync background workers."""
    if core is None or not getattr(core, "_SQLITE_ENABLED", False):
        return
    from src.persistence import plays as _plays
    from src.services import importers
    core.register_background_worker("plays_ledger", _plays.worker)
    core.register_background_worker("lastfm_auto_sync", importers.lastfm_auto_sync_worker)
