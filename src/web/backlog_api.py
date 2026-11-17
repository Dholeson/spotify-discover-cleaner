"""v2 backlog API: the playlist index as a list, clear / keep / undo, auto-clean, catch-up.

Also registers the backlog status extensions (backlog, current_fate, recently_cleared,
auto_clean + the legacy playlist_* aliases) and the index sync worker.
"""

from __future__ import annotations

import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from src.web import api_error

bp = Blueprint("backlog_api", __name__)

MAX_PAGE_LIMIT = 500


def _core():
    import app as core
    return core


def _services():
    from src.services import backlog_index, clearing
    return backlog_index.get_index(), clearing.get_engine()


def init(core):
    from src.services import backlog_index
    core.register_status_extension(status_extension)
    core.register_background_worker("backlog_index", backlog_index.run_worker)


# ---------------------------------------------------------------------------
# Status extension
# ---------------------------------------------------------------------------

def _current_snapshot(core):
    with core.state_lock:
        track = core.state.get("current_track") or {}
        tid = core.state.get("current_track_id") or track.get("id")
        return {
            "track_id": tid,
            "track": dict(track) if track else None,
            "listened_s": float(core.state.get("current_listened") or 0.0),
            "threshold_s": core.state.get("threshold_seconds"),
            "duration_ms": int(core.state.get("duration_ms") or track.get("duration_ms") or 0),
            "is_playing": bool(core.state.get("is_playing")),
            "last_poll_mono": core.state.get("last_poll_mono"),
            "stats": dict(core.state.get("stats") or {}),
        }


def _fate_for(core, engine, snap):
    extra_s = 0.0
    if snap["is_playing"] and snap["last_poll_mono"]:
        # Project listening since the last poll so the countdown doesn't stall between polls.
        extra_s = min(max(0.0, time.monotonic() - snap["last_poll_mono"]),
                      3.0 * float(getattr(core, "POLL_INTERVAL", 3.0) or 3.0))
    return engine.fate(snap["track_id"], threshold_s=snap["threshold_s"], duration_ms=snap["duration_ms"],
                       listened_s=snap["listened_s"], extra_s=extra_s)


def status_extension():
    core = _core()
    index, engine = _services()
    backlog = index.status()
    backlog["pending_removals"] = len(engine.pending_ids())
    snap = _current_snapshot(core)
    fate = _fate_for(core, engine, snap)
    stats = snap["stats"]
    today = datetime.now().strftime("%Y-%m-%d")
    if stats and stats.get("day") != today:
        # Counters are only reset by the next bump; never show yesterday's numbers as today's.
        stats.update(day=today, removed_today=0, liked_today=0, added_today=0)
    stats.pop("removal_dates", None)
    return {
        "backlog": backlog,
        "current_fate": fate,
        "recently_cleared": engine.recent.list(10),
        "auto_clean": {"enabled": bool(engine.auto_clean_enabled)},
        "current_in_playlist": index.contains(snap["track_id"]) is True if snap["track_id"] else False,
        # legacy aliases for the classic UI / older readers
        "playlist_total": backlog["total"],
        "playlist_loaded_count": backlog["known"],
        "playlist_track_ids_loaded": backlog["complete"],
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, api_error("invalid_" + name, f"'{name}' must be a whole number.", 400, field=name)
    if value < lo or value > hi:
        return None, api_error("invalid_" + name, f"'{name}' must be between {lo} and {hi}.", 400, field=name)
    return value, None


def _track_id_field(data, required=True):
    tid = data.get("track_id")
    if tid is None and not required:
        return None, None
    if not isinstance(tid, str) or not tid.strip() or len(tid) > 64:
        return None, api_error("invalid_track_id", "'track_id' must be a Spotify track id.", 400, field="track_id")
    return tid.strip(), None


def _spotify_error(exc, what):
    from src.services.clearing import _retry_after_of
    ra = _retry_after_of(exc)
    if getattr(exc, "http_status", None) == 429:
        return api_error("spotify_busy", f"Spotify is pacing requests, so {what} didn't happen yet.", 503,
                         **({"retry_after": ra} if ra else {}))
    return api_error("spotify_error", f"Spotify rejected the request, so {what} didn't happen.", 502)


def _entry_row(position, e):
    return {
        "position": position,
        "id": e.get("track_id"),
        "uri": e.get("uri"),
        "name": e.get("name") or "",
        "artists": e.get("artists") or "",
        "artist_list": e.get("artist_list") or [],
        "album": e.get("album") or "",
        "image": e.get("image"),
        "duration_ms": int(e.get("duration_ms") or 0),
        "added_at": e.get("added_at"),
        "is_local": bool(e.get("is_local")),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/api/v2/backlog", methods=["GET"])
def backlog_summary():
    core = _core()
    index, engine = _services()
    limit, err = _int_arg("limit", 10, 0, 50)
    if err:
        return err
    snap = _current_snapshot(core)
    backlog = index.status()
    backlog["pending_removals"] = len(engine.pending_ids())
    return jsonify({
        "backlog": backlog,
        "current_fate": _fate_for(core, engine, snap),
        "recently_cleared": engine.recent.list(limit),
        "auto_clean": {"enabled": bool(engine.auto_clean_enabled)},
    })


@bp.route("/api/v2/backlog/tracks", methods=["GET"])
def backlog_tracks():
    index, _ = _services()
    offset, err = _int_arg("offset", 0, 0, 10_000_000)
    if err:
        return err
    limit, err = _int_arg("limit", 100, 1, MAX_PAGE_LIMIT)
    if err:
        return err
    q = (request.args.get("q") or "").strip()
    if len(q) > 200:
        return api_error("invalid_q", "'q' is too long (200 characters max).", 400, field="q")
    words = [w for w in q.casefold().split() if w]
    status = index.status()
    rows = []
    for pos, e in enumerate(index.items()):
        if words:
            hay = f"{e.get('name') or ''} {e.get('artists') or ''} {e.get('album') or ''}".casefold()
            if not all(w in hay for w in words):
                continue
        rows.append((pos, e))
    page = rows[offset: offset + limit]
    return jsonify({
        "playlist_id": status["playlist_id"],
        "name": status["name"],
        "total": status["total"],
        "known": status["known"],
        "complete": status["complete"],
        "loading": status["loading"],
        "removal_seq": status["removal_seq"],
        "offset": offset,
        "limit": limit,
        "q": q,
        "matched": len(rows),
        "items": [_entry_row(pos, e) for pos, e in page],
    })


@bp.route("/api/v2/backlog/sync", methods=["POST"])
def backlog_sync():
    index, _ = _services()
    index.request_sync()
    return jsonify({"backlog": index.status()})


@bp.route("/api/v2/backlog/clear", methods=["POST"])
def backlog_clear():
    core = _core()
    from src.services.clearing import track_info_from_entry
    data = core._json_body()
    tid, err = _track_id_field(data)
    if err:
        return err
    skip = data.get("skip", False)
    if not isinstance(skip, bool):
        return api_error("invalid_skip", "'skip' must be true or false.", 400, field="skip")
    index, engine = _services()
    member = index.contains(tid)
    if member is None:
        return api_error("backlog_syncing", "The backlog is still syncing; try again in a moment.", 409)
    if member is False:
        return api_error("not_in_backlog", "That track isn't in the backlog playlist.", 404)
    sp = core.get_sp()
    if not sp:
        return api_error("not_authenticated", "Sign in to Spotify first.", 401)
    snap = _current_snapshot(core)
    is_current = snap["track_id"] == tid and snap["track"] is not None
    if is_current:
        track_info = snap["track"]
        listened = round(snap["listened_s"], 1)
    else:
        track_info = track_info_from_entry(index.entry(tid), core)
        listened = None
    try:
        entries = engine.clear_tracks(core, sp, [track_info], "manual", "manual-clear", listened_s=listened)
    except Exception as e:
        if getattr(e, "http_status", None) == 429 and engine.pending is not None:
            from src.services.clearing import _retry_after_of
            engine.pending.add(tid, index.pid, "manual", listened, track_info, error=str(e),
                               retry_after_s=_retry_after_of(e))
            ra = _retry_after_of(e)
            return api_error("spotify_busy", "Spotify is pacing requests; the clear is queued and will happen shortly.",
                             503, queued=True, **({"retry_after": ra} if ra else {}))
        return _spotify_error(e, "the clear")
    if not entries:
        # Removed by someone else between our check and the engine lock (not by this app).
        return api_error("not_in_backlog", "That track isn't in the backlog playlist.", 404)
    skipped = False
    if skip and is_current:
        try:
            sp.next_track()
            skipped = True
            with core.state_lock:
                core.state["progress_ms"] = 0
                core.state["is_playing"] = True
        except Exception as e:
            core._log_app_error(e, context="backlog clear skip")
    if is_current:
        with core.state_lock:
            core.state["current_in_playlist"] = False
    engine.poke()
    return jsonify({"cleared": entries[0], "skipped": skipped})


@bp.route("/api/v2/backlog/keep", methods=["POST"])
def backlog_keep():
    core = _core()
    data = core._json_body()
    tid, err = _track_id_field(data)
    if err:
        return err
    kept = data.get("kept", True)
    if not isinstance(kept, bool):
        return api_error("invalid_kept", "'kept' must be true or false.", 400, field="kept")
    _, engine = _services()
    snap = _current_snapshot(core)
    if snap["track_id"] != tid:
        return api_error("track_changed", "The track changed; nothing was kept.", 409,
                         current_track_id=snap["track_id"])
    engine.set_kept(tid, kept)
    return jsonify({"current_fate": _fate_for(core, engine, _current_snapshot(core))})


@bp.route("/api/v2/backlog/undo", methods=["POST"])
def backlog_undo():
    core = _core()
    from src.services.clearing import ClearError
    data = core._json_body()
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return api_error("invalid_seq", "'seq' must be the number of a recent clear.", 400, field="seq")
    _, engine = _services()
    sp = core.get_sp()
    if not sp:
        return api_error("not_authenticated", "Sign in to Spotify first.", 401)
    try:
        restored = engine.undo(core, sp, seq)
    except ClearError as e:
        return api_error(e.code, e.message, e.status, **e.extra)
    with core.state_lock:
        if core.state.get("current_track_id") == restored.get("track_id"):
            core.state["current_in_playlist"] = True
    return jsonify({"restored": restored})


@bp.route("/api/v2/backlog/auto-clean", methods=["POST"])
def backlog_auto_clean():
    core = _core()
    data = core._json_body()
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return api_error("invalid_enabled", "'enabled' must be true or false.", 400, field="enabled")
    from src.services.clearing import ClearError
    _, engine = _services()
    try:
        return jsonify({"enabled": engine.set_auto_clean(core, enabled)})
    except ClearError as e:
        return api_error(e.code, e.message, e.status, **e.extra)


def _iso_ms(value):
    """Epoch ms of a Spotify ISO-8601 UTC timestamp ('2026-09-25T10:00:00.123Z'), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1000)


def _catch_up_excluded(engine, tid):
    """Tracks the listener chose to keep this session (Keep, or brought back with Undo)."""
    if engine is None:
        return False
    try:
        return bool(engine.was_kept(tid) or engine.recently_restored(tid))
    except Exception:
        return False


def catch_up_candidates(core, sp, index, engine=None):
    """Recently played tracks (last 50) still in the backlog, newest play first, current track excluded.

    A play only counts when it happened after the track was added to the backlog (a track heard
    two weeks ago and added yesterday hasn't been heard since), and tracks kept or restored this
    session are left alone.
    """
    if engine is None:
        from src.services import clearing
        engine = clearing.get_engine()
    recents = sp.current_user_recently_played(limit=50) or {}
    with core.state_lock:
        current = core.state.get("current_track_id")
    seen = {}
    skipped = set()
    for item in recents.get("items") or []:
        tr = (item or {}).get("track") or {}
        tid = tr.get("id")
        if not tid or tid == current or tid in seen or tid in skipped:
            continue
        if index.contains(tid) is not True:
            continue
        if _catch_up_excluded(engine, tid):
            skipped.add(tid)
            continue
        added_ms = _iso_ms((index.entry(tid) or {}).get("added_at"))
        played_ms = _iso_ms(item.get("played_at"))
        if added_ms is not None and played_ms is not None and played_ms < added_ms:
            # Newest play first: every older play of this track predates the add as well.
            skipped.add(tid)
            continue
        album = tr.get("album") or {}
        images = album.get("images") or []
        seen[tid] = {
            "id": tid,
            "uri": tr.get("uri") or f"spotify:track:{tid}",
            "name": tr.get("name") or "",
            "artists": ", ".join(a.get("name") or "" for a in (tr.get("artists") or []) if a.get("name")),
            "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in (tr.get("artists") or [])],
            "image": images[-1].get("url") if images else None,
            "played_at": item.get("played_at"),
            "_track": tr,
        }
    return list(seen.values())


def clear_catch_up(core, sp, index, engine, track_ids):
    """Clear the given candidates in ONE Spotify call. Returns (entries, skipped_ids)."""
    from src.services.clearing import track_info_from_entry
    tracks, skipped = [], []
    for tid in track_ids:
        if index.contains(tid) is True and not _catch_up_excluded(engine, tid):
            tracks.append(track_info_from_entry(index.entry(tid), core))
        else:
            skipped.append(tid)
    if not tracks:
        return [], skipped
    return engine.clear_tracks(core, sp, tracks, "catch-up", "manual-catch-up", listened_s=None), skipped


@bp.route("/api/v2/backlog/catch-up", methods=["GET", "POST"])
def backlog_catch_up():
    core = _core()
    index, engine = _services()
    if request.method == "POST":
        data = core._json_body()
        ids = data.get("track_ids")
        if (not isinstance(ids, list) or not ids or len(ids) > 100
                or not all(isinstance(t, str) and t.strip() and len(t) <= 64 for t in ids)):
            return api_error("invalid_track_ids", "'track_ids' must be a list of 1 to 100 track ids.", 400,
                             field="track_ids")
        ids = list(dict.fromkeys(t.strip() for t in ids))
    sp = core.get_sp()
    if not sp:
        return api_error("not_authenticated", "Sign in to Spotify first.", 401)
    if request.method == "GET":
        try:
            candidates = catch_up_candidates(core, sp, index, engine)
        except Exception as e:
            return _spotify_error(e, "the recently played lookup")
        for c in candidates:
            c.pop("_track", None)
        return jsonify({"candidates": candidates, "complete": index.status()["complete"]})
    try:
        entries, skipped = clear_catch_up(core, sp, index, engine, ids)
    except Exception as e:
        return _spotify_error(e, "the catch-up")
    engine.poke()
    return jsonify({"cleared": entries, "skipped": skipped})
