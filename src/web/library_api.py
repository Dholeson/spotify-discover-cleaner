"""Library endpoints for the v2 UI: the full play history feed, local (offline) search over it,
the watchlist (research items enriched from local data) and play counts for Spotify lists."""

import sqlite3

from flask import Blueprint, jsonify, request

from src.web import api_error

bp = Blueprint("library_api", __name__)


def _core():
    import app as core  # lazy: app.py is the live module (see src/web/__init__.py)
    return core


def _ledger_conn():
    from src.services import artist_stats as AS

    core = _core()
    if not getattr(core, "_SQLITE_ENABLED", False):
        return None, api_error("history_unavailable", "Listening history storage is not available.", 503)
    conn = core._db.get_conn()
    if not AS.ledger_ready(conn):
        return None, api_error("history_not_ready",
                               "Your listening history is still being prepared. Try again in a minute.", 503)
    return conn, None


def _int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, api_error("invalid_" + name, f"'{name}' must be a whole number.", 400, field=name)
    if val < lo or val > hi:
        return None, api_error("invalid_" + name, f"'{name}' must be between {lo} and {hi}.", 400, field=name)
    return val, None


def _text_arg(name, max_len=200):
    val = (request.args.get(name) or "").strip()
    if len(val) > max_len:
        return None, api_error("invalid_" + name, f"'{name}' is too long.", 400, field=name)
    return val, None


@bp.route("/api/v2/history")
def history():
    from src.services import artist_stats as AS

    before_ms, err = _int_arg("before_ms", None, 0, 10 ** 15)
    if err:
        return err
    limit, err = _int_arg("limit", 50, 1, 200)
    if err:
        return err
    artist, err = _text_arg("artist", 300)
    if err:
        return err
    q, err = _text_arg("q")
    if err:
        return err
    conn, err = _ledger_conn()
    if err:
        return err
    try:
        page = AS.history_page(conn, before_ms=before_ms, limit=limit, artist=artist or None, q=q or None)
        attach_art(conn, page.get("items") or [])
        return jsonify(page)
    except AS.LedgerNotReady:
        return api_error("history_not_ready", "Your listening history is still being prepared.", 503)
    except Exception as e:
        _core()._log_app_error(e, "api_v2_history")
        return api_error("history_failed", "Could not load your listening history.", 500)


@bp.route("/api/v2/search/local")
def search_local():
    from src.services import artist_stats as AS

    q, err = _text_arg("q")
    if err:
        return err
    limit, err = _int_arg("limit", 8, 1, 50)
    if err:
        return err
    if not q:
        return jsonify({"artists": [], "tracks": [], "albums": []})
    conn, err = _ledger_conn()
    if err:
        return err
    try:
        return jsonify(AS.search_local(conn, q, limit=limit))
    except AS.LedgerNotReady:
        return api_error("history_not_ready", "Your listening history is still being prepared.", 503)
    except Exception as e:
        _core()._log_app_error(e, "api_v2_search_local")
        return api_error("search_failed", "Could not search your library.", 500)


# ---------------------------------------------------------------------------
# Album art for history rows
# ---------------------------------------------------------------------------

def _in_clause(values):
    return ",".join("?" * len(values))


def attach_art(conn, items):
    """Give history rows an ``image``: the track's own art when the app has seen that Spotify id,
    else the art of another recording of the same song (most Last.fm scrobbles carry no id).
    Rows without a match keep image None; a DB without the ``tracks`` table is fine."""
    for it in items:
        it.setdefault("image", None)
    if not items:
        return items
    ids = sorted({it["spotify_track_id"] for it in items if it.get("spotify_track_id")})
    by_id, by_song = {}, {}
    try:
        if ids:
            for tid, img in conn.execute(
                    f"SELECT id, image_url FROM tracks WHERE id IN ({_in_clause(ids)}) AND image_url IS NOT NULL",
                    ids).fetchall():
                by_id[tid] = img
        songs = sorted({it["song_key"] for it in items
                        if it.get("song_key") and not by_id.get(it.get("spotify_track_id"))})
        if songs:
            for sk, img in conn.execute(
                    "SELECT p.song_key, t.image_url FROM plays p JOIN tracks t ON t.id = p.spotify_track_id "
                    f"WHERE p.song_key IN ({_in_clause(songs)}) AND p.spotify_track_id IS NOT NULL "
                    "AND t.image_url IS NOT NULL ORDER BY p.ts_ms DESC", songs).fetchall():
                by_song.setdefault(sk, img)
    except sqlite3.Error:
        return items
    for it in items:
        it["image"] = by_id.get(it.get("spotify_track_id")) or by_song.get(it.get("song_key"))
    return items


# ---------------------------------------------------------------------------
# Watchlist: the research queue, enriched for display
# ---------------------------------------------------------------------------

WATCHLIST_STATUSES = ("active", "promoted", "archived")


def _no_backlog(_tid):
    return None


def _backlog_lookup(core):
    """(entry(track_id), contains(track_id)) from the backlog index; no-ops when it is unavailable."""
    try:
        index = core._backlog_index()
        return index.entry, index.contains
    except Exception:
        return _no_backlog, _no_backlog


def _play_window(conn, where, params, since):
    """(plays, plays since `since`, last play ts) over canonical plays matching `where`."""
    n, n_since, last = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN ts_ms >= ? THEN 1 ELSE 0 END), MAX(ts_ms) FROM plays "
        f"WHERE {where} AND superseded_by IS NULL", [since] + list(params)).fetchone()
    return {"plays": n or 0, "plays_since_saved": n_since or 0, "last_played_ts": last}


def _track_details(conn, item, backlog_entry, backlog_contains, ledger):
    tid = item.get("entity_id")
    since = int(item.get("created_at") or 0)
    out = {"uri": f"spotify:track:{tid}" if tid else None}
    if tid:
        try:
            row = conn.execute("SELECT album, image_url, spotify_url FROM tracks WHERE id = ?", (tid,)).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            out.update({"album": row[0] or None, "image": row[1] or None, "url": row[2] or None})
        entry = backlog_entry(tid)
        if entry:
            out["artist_list"] = [a for a in (entry.get("artist_list") or []) if a.get("name")]
            if out["artist_list"]:
                out["artist"] = out["artist_list"][0]["name"]
            out["album"] = out.get("album") or entry.get("album") or None
            out["image"] = out.get("image") or entry.get("image")
        out["in_backlog"] = backlog_contains(tid) is True
    if not ledger:
        return out
    song_key = None
    if tid:
        row = conn.execute(
            "SELECT artist_name, album_name, song_key, COUNT(*) AS n FROM plays WHERE spotify_track_id = ? "
            "GROUP BY artist_key, song_key ORDER BY n DESC LIMIT 1", (tid,)).fetchone()
        if row:
            out["artist"] = out.get("artist") or row[0]
            out["album"] = out.get("album") or row[1]
            song_key = row[2]
    if not song_key and out.get("artist"):
        from src.services import artist_stats as AS
        song_key = AS.song_key_for(out["artist"], item.get("entity_name") or "")
    if song_key:
        out.update(_play_window(conn, "song_key = ?", [song_key], since))
    return out


def _artist_details(conn, item, ledger):
    out = {"artist": item.get("entity_name")}
    if ledger:
        from src.services import artist_stats as AS
        out.update(_play_window(conn, "artist_key = ?", [AS.artist_key(item.get("entity_name") or "")],
                                int(item.get("created_at") or 0)))
    return out


def _album_details(conn, item, ledger):
    aid = item.get("entity_id")
    name = item.get("entity_name") or ""
    out = {"uri": f"spotify:album:{aid}" if aid else None}
    try:
        row = conn.execute("SELECT image_url FROM tracks WHERE (album_id = ? OR album = ?) AND image_url IS NOT NULL "
                           "LIMIT 1", (aid or "", name)).fetchone()
        if row:
            out["image"] = row[0]
    except sqlite3.Error:
        pass
    if not ledger:
        return out
    row = conn.execute(
        "SELECT artist_name, COUNT(*) AS n FROM plays WHERE album_name = ? COLLATE NOCASE AND superseded_by IS NULL "
        "GROUP BY artist_key ORDER BY n DESC LIMIT 1", (name,)).fetchone()
    if row:
        from src.services import artist_stats as AS
        out["artist"] = row[0]
        out.update(_play_window(conn, "album_name = ? COLLATE NOCASE AND artist_key = ?",
                                [name, AS.artist_key(row[0])], int(item.get("created_at") or 0)))
    else:
        out.update({"plays": 0, "plays_since_saved": 0, "last_played_ts": None})
    return out


def enrich_watchlist(conn, items, core=None, ledger=True):
    """Add display fields to research items: artist, artist_list, album, image, uri, url, in_backlog
    and, when the plays ledger is ready, plays / plays_since_saved / last_played_ts (song-level for
    tracks, so a live or remastered version counts too)."""
    backlog_entry, backlog_contains = _backlog_lookup(core) if core is not None else (_no_backlog, _no_backlog)
    for item in items:
        details = {"artist": None, "artist_list": [], "album": None, "image": None, "uri": None, "url": None,
                   "in_backlog": False, "plays": None, "plays_since_saved": None, "last_played_ts": None}
        etype = item.get("entity_type")
        try:
            if etype == "track":
                extra = _track_details(conn, item, backlog_entry, backlog_contains, ledger)
            elif etype == "artist":
                extra = _artist_details(conn, item, ledger)
            else:
                extra = _album_details(conn, item, ledger)
        except sqlite3.Error:
            extra = {}
        details.update({k: v for k, v in extra.items() if v is not None})
        item.update(details)
        item.setdefault("reasons", [])
    return items


@bp.route("/api/v2/watchlist")
def watchlist():
    core = _core()
    if not getattr(core, "_SQLITE_ENABLED", False):
        return api_error("watchlist_unavailable", "The watchlist needs the local database, which is not available.", 503)
    status = (request.args.get("status") or "active").strip()
    if status not in WATCHLIST_STATUSES + ("all",):
        return api_error("invalid_status", "'status' must be active, promoted, archived or all.", 400, field="status")
    from src.persistence import research as R
    from src.services import artist_stats as AS
    try:
        items = R.get_interests(status=None if status == "all" else status, limit=500)
        raw_counts = R.get_count_by_status()
    except sqlite3.Error as e:
        core._log_app_error(e, "api_v2_watchlist")
        return api_error("watchlist_failed", "Could not load your watchlist.", 500)
    conn = core._db.get_conn()
    try:
        ledger = bool(AS.ledger_ready(conn))
    except Exception:
        ledger = False
    try:
        enrich_watchlist(conn, items, core, ledger=ledger)
    except Exception as e:  # enrichment is optional: the bare items are still useful
        core._log_app_error(e, "api_v2_watchlist_enrich")
    counts = {s: int(raw_counts.get(s) or 0) for s in WATCHLIST_STATUSES}
    return jsonify({"status": status, "items": items, "counts": counts, "stats_ready": ledger,
                    "seed_tags": list(R.SEED_REASON_TAGS)})


# ---------------------------------------------------------------------------
# Play counts for lists that come from Spotify (liked songs, search results, ...)
# ---------------------------------------------------------------------------

MAX_COUNT_ITEMS = 500


def _short_text(v, max_len=300):
    return v is None or (isinstance(v, str) and len(v) <= max_len)


@bp.route("/api/v2/library/play-counts", methods=["POST"])
def play_counts():
    from src.services import artist_stats as AS

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error("invalid_body", "Send a JSON object with 'tracks' and/or 'artists'.", 400)
    tracks = data.get("tracks") or []
    artists = data.get("artists") or []
    if not isinstance(tracks, list) or len(tracks) > MAX_COUNT_ITEMS or not all(isinstance(t, dict) for t in tracks):
        return api_error("invalid_tracks", f"'tracks' must be a list of up to {MAX_COUNT_ITEMS} objects.", 400,
                         field="tracks")
    if (not isinstance(artists, list) or len(artists) > MAX_COUNT_ITEMS
            or not all(isinstance(a, str) and _short_text(a) for a in artists)):
        return api_error("invalid_artists", f"'artists' must be a list of up to {MAX_COUNT_ITEMS} names.", 400,
                         field="artists")
    entries = []
    for t in tracks:
        if not all(_short_text(t.get(k)) for k in ("name", "artist", "id")):
            return api_error("invalid_tracks", "Each track needs text 'name' and 'artist' (and an optional 'id').",
                             400, field="tracks")
        entries.append({"title": t.get("name") or "", "artist": t.get("artist") or "", "id": t.get("id") or None})
    conn, err = _ledger_conn()
    if err:
        return err
    try:
        return jsonify({
            "tracks": AS.song_play_counts(conn, entries) if entries else [],
            "artists": AS.artist_play_counts(conn, artists) if artists else {},
        })
    except AS.LedgerNotReady:
        return api_error("history_not_ready", "Your listening history is still being prepared.", 503)
    except Exception as e:
        _core()._log_app_error(e, "api_v2_play_counts")
        return api_error("play_counts_failed", "Could not count your plays.", 500)
