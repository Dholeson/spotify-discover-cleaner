"""
History importers: Last.fm scrobble sync and the Spotify personal-data import.

Moved out of app.py. Both write canonical rows into ``events`` and feed the plays ledger
(src/persistence/plays.py) in the same transaction as each insert.

Last.fm sync
------------
- The forward cursor is derived from the data, never from a stored counter:
  ``from = MAX(ts_ms)/1000 + 1`` over ``source='lastfm'`` rows, with a fixed ``to = now``
  so pages cannot shift while we walk them. Pages are processed OLDEST first, so if a page
  fails the newest stored scrobble is still older than everything not yet fetched and the
  next run resumes without leaving a gap.
- Backfill (``to = MIN(ts_ms)/1000 - 1``, newest page first) runs only while app_meta
  ``lastfm_backfill_done`` is unset; a completed walk sets it, and so do empty answers on two
  consecutive runs (a single empty answer may be a transient Last.fm glitch).
- Nothing is fetched while the database lacks the Last.fm unique key (schema < v2, e.g. a
  failed migration): INSERT OR IGNORE would then store every re-read scrobble twice.
- Each page is fetched with retries (1, 2, 4, 8 s). If it still fails the run stops with
  status 'error' naming the page, and nothing past it is written.
- One transaction per page with ``INSERT OR IGNORE`` on the unique key (ts, artist, track):
  two different scrobbles in the same second are both kept, a re-run inserts nothing.
- ``day`` is the LOCAL date (datetime.fromtimestamp), like every other event.
- An auto-sync worker runs at startup and every 30 minutes when a key and username are
  configured (read at call time, so Settings changes apply without a restart).
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime

from ..persistence import db as _db
from ..persistence import events as _events
from ..persistence import plays as _plays

log = logging.getLogger(__name__)

LASTFM_PAGE_SIZE = 200
LASTFM_RETRY_DELAYS = (1, 2, 4, 8)      # seconds between attempts of one page
LASTFM_PAGE_PAUSE_S = 0.25              # stay under Last.fm's 5 requests/second
LASTFM_PERMANENT_ERRORS = {6: "Last.fm user not found", 10: "invalid Last.fm API key",
                           17: "this Last.fm profile hides its recent listening",
                           26: "Last.fm API key suspended"}
AUTO_SYNC_MINUTES = 30
AUTO_SYNC_STARTUP_DELAY_S = 15
AUTO_SYNC_OVERLAP_S = 3 * 24 * 3600     # once a day re-read the last 3 days (late scrobbles)
LASTFM_MODES = ("sync", "backfill", "full")

META_BACKFILL_DONE = "lastfm_backfill_done"
META_BACKFILL_EMPTY = "lastfm_backfill_empty_runs"   # consecutive runs whose backfill came back empty
BACKFILL_EMPTY_RUNS_TO_FINISH = 2   # one empty answer can be a Last.fm hiccup
META_LAST_SYNC = "lastfm_last_sync_at"
META_LAST_RESULT = "lastfm_last_sync_result"
UPGRADE_PENDING_MESSAGE = ("The database upgrade hasn't completed, so Last.fm sync is paused "
                           "(without it, every sync would store duplicate scrobbles).")

# Indirections that tests patch (no app import needed).
_sleep = time.sleep


def _core():
    """The live app module when it is loaded (never imported from here: tests run without it)."""
    return sys.modules.get("app")


def _log_error(exc, context):
    core = _core()
    if core is not None and hasattr(core, "_log_app_error"):
        try:
            core._log_app_error(exc, context)
            return
        except Exception:
            pass
    log.exception("%s: %s", context, exc)


def lastfm_call(method, **params):
    """Delegate to app.lastfm_call (patched in tests)."""
    core = _core()
    if core is None or not hasattr(core, "lastfm_call"):
        return None
    return core.lastfm_call(method, **params)


def lastfm_credentials():
    """(api_key, username) as configured right now."""
    core = _core()
    key = user = None
    if core is not None:
        try:
            key = core.lastfm_api_key() if hasattr(core, "lastfm_api_key") else None
            user = core.lastfm_username() if hasattr(core, "lastfm_username") else None
        except Exception:
            key = user = None
    key = key or (os.environ.get("LASTFM_API_KEY") or "").strip() or None
    user = user or (os.environ.get("LASTFM_USERNAME") or "").strip() or None
    return key, user


def lastfm_configured():
    key, user = lastfm_credentials()
    return bool(key and user)


def _now_ms():
    return int(time.time() * 1000)


def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def dedup_key_ready(conn=None):
    """True when the Last.fm unique key exists (schema v2+). Without it INSERT OR IGNORE ignores
    nothing, so a sync must not run: every overlap re-read would insert duplicates."""
    try:
        conn = conn or _db.get_conn()
        if _db.schema_version(conn) < 2:
            return False
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' "
                            "AND name = 'events_lastfm_uniq'").fetchone() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Last.fm sync
# ---------------------------------------------------------------------------

class LastfmSyncError(RuntimeError):
    def __init__(self, message, page=None):
        super().__init__(message)
        self.page = page


_lfm_lock = threading.Lock()   # one Last.fm run at a time (auto-sync and manual share it)
_lfm_state = {
    "running": False, "status": "idle", "mode": None, "trigger": None, "phase": None,
    "pages_done": 0, "pages_total": 0, "inserted": 0, "duplicates": 0, "corrected": 0, "failed": 0,
    "error": None, "failed_page": None, "started_at": None, "finished_at": None,
}
_auto_wake = threading.Event()


def _new_run_state(mode, trigger):
    return {
        "running": True, "status": "running", "mode": mode, "trigger": trigger, "phase": None,
        "pages_done": 0, "pages_total": 0, "inserted": 0, "duplicates": 0, "corrected": 0, "failed": 0,
        "error": None, "failed_page": None, "started_at": _now_ms(), "finished_at": None,
    }


def _lastfm_error_reason(result, since=0.0):
    """(human reason, permanent?) for a failed page. app.lastfm_call returns a falsy
    sentinel and records the details in app._lfm_last_error, which other threads share, so
    that record is only trusted when it was written after this call started (``since``)."""
    core = _core()
    if core is not None and result is getattr(core, "_LFM_MISSING", object()):
        return LASTFM_PERMANENT_ERRORS[6], True
    info = getattr(core, "_lfm_last_error", None) if core is not None else None
    if isinstance(info, dict) and float(info.get("ts") or 0) < since:
        info = None
    if isinstance(result, dict) and result.get("error") is not None:
        try:
            code = int(result.get("error"))
        except (TypeError, ValueError):
            code = None
        msg = result.get("message") or "Last.fm error"
        return (LASTFM_PERMANENT_ERRORS.get(code) or f"Last.fm error {code}: {msg}"), code in LASTFM_PERMANENT_ERRORS
    if isinstance(info, dict) and info.get("message"):
        code = info.get("code")
        return (LASTFM_PERMANENT_ERRORS.get(code) or info["message"]), code in LASTFM_PERMANENT_ERRORS
    return "no response from Last.fm", False


def _fetch_page(call, user, page, params, sleep):
    """One user.getRecentTracks page (the 'recenttracks' dict), retried with backoff."""
    reason = "no response from Last.fm"
    attempts = len(LASTFM_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        t_call = time.time()
        try:
            result = call("user.getRecentTracks", user=user, limit=LASTFM_PAGE_SIZE, page=page, **params)
        except Exception as exc:  # a transport error is retried like an empty answer
            result, reason, permanent = None, f"Last.fm request failed: {exc}", False
        else:
            rt = result.get("recenttracks") if isinstance(result, dict) else None
            if isinstance(rt, dict):
                return rt
            reason, permanent = _lastfm_error_reason(result, since=t_call)
        if permanent:
            raise LastfmSyncError(reason, page)
        if attempt < attempts - 1:
            sleep(LASTFM_RETRY_DELAYS[attempt])
    raise LastfmSyncError(f"Last.fm page {page} failed after {attempts} attempts ({reason})", page)


def _page_info(rt):
    attr = rt.get("@attr") or {}
    try:
        total_pages = int(attr.get("totalPages") or 0)
    except (TypeError, ValueError):
        total_pages = 0
    try:
        total = int(attr.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return total_pages, total


def _text(v):
    if isinstance(v, dict):
        return str(v.get("#text") or v.get("name") or "")
    return str(v or "")


def _parse_scrobble(t):
    """(ts_ms, day, payload) for one recenttracks item, None to skip it, or raises ValueError."""
    if not isinstance(t, dict):
        raise ValueError("not an object")
    if str((t.get("@attr") or {}).get("nowplaying", "")).lower() == "true":
        return None  # the track playing right now has no timestamp yet
    uts = int((t.get("date") or {}).get("uts") or 0)
    if uts <= 0:
        raise ValueError("missing timestamp")
    artist = _text(t.get("artist")).strip()
    name = str(t.get("name") or "").strip()
    if not artist and not name:
        raise ValueError("missing artist and title")
    album = _text(t.get("album")).strip()
    payload = {"artists": artist, "track_name": name, "album": album, "source": "lastfm_import"}
    return uts * 1000, datetime.fromtimestamp(uts).date().isoformat(), payload


def _corrected_copy_exists(conn, ts_ms, payload, prior_max_id):
    """True when a scrobble stored by an EARLIER run has this timestamp and the same recording
    title but different text: the same play after a Last.fm correction or a scrobble edit
    (artist renamed, title re-cased or '(feat. ...)' added), which the exact unique key misses.

    Rows from the current run never count, so two genuinely different scrobbles that share a
    second are both kept on a first import."""
    if prior_max_id is None:
        return False
    rows = conn.execute(
        "SELECT json_extract(payload, '$.artists'), json_extract(payload, '$.track_name') FROM events "
        "WHERE ts_ms = ? AND source = 'lastfm' AND id <= ?", (ts_ms, prior_max_id)).fetchall()
    if not rows:
        return False
    title = _plays._recording_title(payload.get("track_name") or "")
    for artists, name in rows:
        if (artists, name) == (payload.get("artists"), payload.get("track_name")):
            return False  # the exact row: the unique key handles it
        if title and _plays._recording_title(name or "") == title:
            return True
    return False


def _store_page(conn, rt, st, prior_max_id=None):
    """Insert one page in one transaction (INSERT OR IGNORE) and feed the plays ledger."""
    tracks = rt.get("track") or []
    if isinstance(tracks, dict):
        tracks = [tracks]
    parsed = []
    for t in tracks:
        try:
            item = _parse_scrobble(t)
        except (TypeError, ValueError):
            st["failed"] += 1
            continue
        if item is not None:
            parsed.append(item)
    oldest = None
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for ts_ms, day, payload in parsed:
            oldest = ts_ms if oldest is None else min(oldest, ts_ms)
            if _corrected_copy_exists(conn, ts_ms, payload, prior_max_id):
                st["duplicates"] += 1
                st["corrected"] = st.get("corrected", 0) + 1
                continue
            payload_json = json.dumps(payload, ensure_ascii=False)
            cur = conn.execute(
                "INSERT OR IGNORE INTO events (event_type, ts_ms, day, payload, source) "
                "VALUES (?, ?, ?, ?, 'lastfm')",
                (_events.SONG_PLAYED, ts_ms, day, payload_json),
            )
            if cur.rowcount == 1:
                st["inserted"] += 1
                _plays.ingest_event(conn, {
                    "id": cur.lastrowid, "event_type": _events.SONG_PLAYED, "ts_ms": ts_ms,
                    "source": "lastfm", "track_id": None, "payload": payload,
                }, commit=False)
            else:
                st["duplicates"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(parsed)


def _walk(conn, call, user, params, st, sleep, oldest_first, prior_max_id=None):
    """Fetch and store every page of one fixed time window."""
    first = _fetch_page(call, user, 1, params, sleep)
    total_pages, total = _page_info(first)
    if total <= 0 or total_pages <= 0:
        return 0
    st["pages_total"] += total_pages
    order = range(total_pages, 0, -1) if oldest_first else range(1, total_pages + 1)
    for page in order:
        if page == 1:
            rt = first
        else:
            sleep(LASTFM_PAGE_PAUSE_S)
            rt = _fetch_page(call, user, page, params, sleep)
        try:
            _store_page(conn, rt, st, prior_max_id)
        except Exception as exc:
            raise LastfmSyncError(f"could not save Last.fm page {page}: {exc}", page) from exc
        st["pages_done"] += 1
    return total


def run_lastfm_sync(conn=None, mode="sync", *, user=None, call=None, sleep=None, state=None,
                    now_s=None, overlap_s=0):
    """Run one Last.fm import synchronously and return its state dict.

    mode 'sync': forward from the newest stored scrobble, then backfill older history while
    it is not known to be complete. 'backfill': only the older history. 'full': re-read the
    whole account (existing rows are skipped by the unique key).
    """
    if mode not in LASTFM_MODES:
        raise ValueError(f"unknown Last.fm import mode: {mode}")
    conn = conn or _db.get_conn()
    call = call or lastfm_call
    sleep = sleep or _sleep
    if user is None:
        user = lastfm_credentials()[1]
    st = state if state is not None else {}
    st.update(_new_run_state(mode, st.get("trigger")))
    now_s = int(now_s or time.time())
    try:
        if not user:
            raise LastfmSyncError("Last.fm username is not set")
        if not dedup_key_ready(conn):
            raise LastfmSyncError(UPGRADE_PENDING_MESSAGE)
        # Rows up to here were stored by earlier runs (see _corrected_copy_exists).
        prior_max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        if mode in ("sync", "full"):
            st["phase"] = "forward"
            params = {"to": now_s}
            if mode == "sync":
                newest = conn.execute("SELECT MAX(ts_ms) FROM events WHERE source = 'lastfm'").fetchone()[0]
                if newest:
                    params["from"] = max(0, int(newest) // 1000 + 1 - int(overlap_s or 0))
            _walk(conn, call, user, params, st, sleep, oldest_first=True, prior_max_id=prior_max_id)
            if mode == "full":
                _meta_set(conn, META_BACKFILL_DONE, _now_ms())
        if mode == "backfill" or (mode == "sync" and not _meta_get(conn, META_BACKFILL_DONE)):
            st["phase"] = "backfill"
            oldest = conn.execute("SELECT MIN(ts_ms) FROM events WHERE source = 'lastfm'").fetchone()[0]
            found = 0
            if oldest:
                found = _walk(conn, call, user, {"to": int(oldest) // 1000 - 1}, st, sleep, oldest_first=False,
                              prior_max_id=prior_max_id)
            if found:
                # A completed walk of everything older than the oldest stored scrobble.
                _meta_set(conn, META_BACKFILL_EMPTY, 0)
                _meta_set(conn, META_BACKFILL_DONE, _now_ms())
            else:
                # Nothing older: believe it only when it repeats on a later run, since Last.fm
                # sometimes answers an empty page by mistake and this flag is never re-checked.
                try:
                    empty_runs = int(_meta_get(conn, META_BACKFILL_EMPTY) or 0) + 1
                except (TypeError, ValueError):
                    empty_runs = 1
                _meta_set(conn, META_BACKFILL_EMPTY, empty_runs)
                if empty_runs >= BACKFILL_EMPTY_RUNS_TO_FINISH:
                    _meta_set(conn, META_BACKFILL_DONE, _now_ms())
        st.update(status="done", phase=None)
        _meta_set(conn, META_LAST_SYNC, _now_ms())
    except LastfmSyncError as exc:
        st.update(status="error", error=str(exc), failed_page=exc.page)
    except Exception as exc:
        st.update(status="error", error=f"Last.fm import failed: {exc}")
        _log_error(exc, "lastfm import")
    finally:
        st.update(running=False, finished_at=_now_ms())
        try:
            _meta_set(conn, META_LAST_RESULT, json.dumps({
                k: st.get(k) for k in ("status", "mode", "inserted", "duplicates", "failed", "error",
                                       "failed_page", "finished_at")}))
        except Exception:
            pass
    if st.get("inserted"):
        try:
            if _plays.is_ready(conn):
                _plays.refresh_rollups(conn)
        except Exception as exc:
            _log_error(exc, "plays rollup refresh after Last.fm import")
    return st


def _run_locked(mode, trigger, overlap_s=0):
    """Body of a sync thread; the caller already holds _lfm_lock."""
    try:
        _lfm_state["trigger"] = trigger
        run_lastfm_sync(_db.get_conn(), mode, state=_lfm_state, overlap_s=overlap_s)
    except Exception as exc:
        _lfm_state.update(running=False, status="error", error=str(exc), finished_at=_now_ms())
        _log_error(exc, "lastfm import thread")
    finally:
        _lfm_lock.release()


def start_lastfm_sync(mode="sync", trigger="manual"):
    """Start a sync in a daemon thread. Returns (started, reason) with reason in
    None | 'not_configured' | 'running' | 'upgrade_pending'."""
    if mode not in LASTFM_MODES:
        raise ValueError(f"unknown Last.fm import mode: {mode}")
    if not lastfm_configured():
        return False, "not_configured"
    if not dedup_key_ready():
        return False, "upgrade_pending"
    if not _lfm_lock.acquire(blocking=False):
        return False, "running"
    _lfm_state.update(_new_run_state(mode, trigger))
    try:
        threading.Thread(target=_run_locked, args=(mode, trigger), name="lastfm-import", daemon=True).start()
    except Exception:
        _lfm_lock.release()
        raise
    return True, None


def lastfm_status(conn=None):
    """GET /api/lastfm/import payload (new fields plus the legacy ones the classic UI reads)."""
    conn = conn or _db.get_conn()
    st = dict(_lfm_state)
    try:
        newest = conn.execute("SELECT MAX(ts_ms) FROM events WHERE source = 'lastfm'").fetchone()[0]
        last_sync = _meta_get(conn, META_LAST_SYNC)
        backfill_done = bool(_meta_get(conn, META_BACKFILL_DONE))
        last_result = _meta_get(conn, META_LAST_RESULT)
    except Exception:
        newest, last_sync, backfill_done, last_result = None, None, False, None
    if st.get("status") == "idle" and last_result:
        try:  # show how the last run (maybe before a restart) ended
            prev = json.loads(last_result)
            st["last_result"] = prev
        except ValueError:
            pass
    st.update(
        newest_scrobble_ts=newest,
        last_sync_at=int(last_sync) if last_sync else None,
        auto_sync_minutes=AUTO_SYNC_MINUTES if auto_sync_enabled() else None,
        configured=lastfm_configured(),
        backfill_done=backfill_done,
        # legacy keys (classic UI)
        page=st.get("pages_done", 0),
        total_pages=st.get("pages_total", 0),
        imported=st.get("inserted", 0),
        total=st.get("inserted", 0) + st.get("duplicates", 0),
    )
    return st


def auto_sync_enabled():
    """LASTFM_AUTO_SYNC=0/1 wins; otherwise on, except in the offline dev sandbox harness
    (it sets app.FAKE_SPOTIFY), where parallel sandboxes must not all hit Last.fm."""
    raw = (os.environ.get("LASTFM_AUTO_SYNC") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    core = _core()
    return not (core is not None and getattr(core, "FAKE_SPOTIFY", None) is not None)


def lastfm_auto_sync_worker():
    """Background worker: sync at startup and every AUTO_SYNC_MINUTES. Never dies."""
    _sleep(AUTO_SYNC_STARTUP_DELAY_S)
    last_run = None
    last_overlap = None
    while True:
        try:
            now = time.monotonic()
            due = last_run is None or (now - last_run) >= AUTO_SYNC_MINUTES * 60
            if due and auto_sync_enabled() and lastfm_configured() and _SQLITE_OK():
                if _lfm_lock.acquire(blocking=False):
                    last_run = now
                    overlap = last_overlap is None or (now - last_overlap) >= 24 * 3600
                    _lfm_state.update(_new_run_state("sync", "auto"))
                    _run_locked("sync", "auto", overlap_s=AUTO_SYNC_OVERLAP_S if overlap else 0)
                    if overlap and _lfm_state.get("status") == "done":
                        last_overlap = now
        except Exception as exc:
            _log_error(exc, "lastfm auto-sync")
        _auto_wake.wait(60)
        _auto_wake.clear()


def _SQLITE_OK():
    core = _core()
    return core is None or bool(getattr(core, "_SQLITE_ENABLED", True))


# ---------------------------------------------------------------------------
# Spotify personal-data import
# ---------------------------------------------------------------------------

_sp_lock = threading.Lock()
_spotify_import_state = {
    "running": False, "status": "idle", "step": "", "imported_plays": 0, "imported_artists": 0,
    "imported_liked": 0, "duplicates": 0, "failed": 0, "error": None, "seed_artist_likes": False,
    "steps": {}, "started_at": None, "finished_at": None,
}
DEDUP_WINDOW_MS = 6 * 60 * 1000   # a recently-played item within 6 min of a known play is that play


def _dedup_event(conn, track_id, ts_ms, track_name=None, artist_name=None, window_ms=DEDUP_WINDOW_MS):
    """True if a SONG_PLAYED from any source already covers this listen.

    Live rows are stamped at the END of a play and scrobbles at the START, so the match is
    by Spotify id or by recording title (feat./remaster noise stripped) within +-6 minutes.
    """
    lo, hi = ts_ms - window_ms, ts_ms + window_ms
    want = _plays._recording_title(track_name) if track_name else None
    rows = conn.execute(
        "SELECT e.track_id, json_extract(e.payload, '$.track_name'), t.name FROM events e "
        "LEFT JOIN tracks t ON t.id = e.track_id WHERE e.event_type = 'SONG_PLAYED' AND e.ts_ms BETWEEN ? AND ?",
        (lo, hi),
    ).fetchall()
    for tid, pname, tname in rows:
        if track_id and tid == track_id:
            return True
        name = pname or tname
        if want and name and _plays._recording_title(name) == want:
            return True
    return False


def _dedup_liked(conn, track_id):
    """Return True if a TRACK_LIKED event already exists for this track."""
    if not track_id:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'TRACK_LIKED' AND track_id = ? LIMIT 1",
        (track_id,),
    ).fetchone())


def _sp_call(fn, *args, sleep=None, **kwargs):
    """Call Spotify through the guard; wait out short guard pauses instead of failing."""
    sleep = sleep or _sleep
    core = _core()
    blocked = getattr(core, "SpotifyGuardBlocked", None) if core is not None else None
    for _ in range(4):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if blocked is None or not isinstance(exc, blocked):
                raise
            wait = min(60.0, max(1.0, float(getattr(exc, "retry_after", 10) or 10)))
            sleep(wait)
    return fn(*args, **kwargs)


def _local_day_from_iso(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000), dt.astimezone().date().isoformat()


def _track_meta(track):
    artists = [a for a in (track.get("artists") or []) if isinstance(a, dict) and a.get("name")]
    album = track.get("album") or {}
    return {
        "artists": ", ".join(a["name"] for a in artists),
        "artist_names": [a["name"] for a in artists],
        "artist_ids": [a.get("id") for a in artists if a.get("id")],
        "track_name": track.get("name") or "",
        "album": album.get("name") or "",
        "album_id": album.get("id"),
        "duration_ms": track.get("duration_ms"),
    }


def run_spotify_import(sp, conn=None, seed_artist_likes=False, state=None, sleep=None):
    """Import recently played, top artists/tracks and saved tracks. Returns the state."""
    conn = conn or _db.get_conn()
    st = state if state is not None else {}
    now_ms = _now_ms()
    steps = {}
    st.update(running=True, status="running", step="Starting...", imported_plays=0, imported_artists=0,
              imported_liked=0, duplicates=0, failed=0, error=None, steps=steps,
              seed_artist_likes=bool(seed_artist_likes), started_at=now_ms, finished_at=None)

    def step(name, fn):
        st["step"] = name
        try:
            fn()
            steps[name] = "ok"
        except Exception as exc:
            steps[name] = f"error: {exc}"
            st["failed"] += 1
            _log_error(exc, f"spotify import: {name}")

    def recent():
        data = _sp_call(sp.current_user_recently_played, limit=50, sleep=sleep) or {}
        for item in data.get("items") or []:
            track = item.get("track") or {}
            played_at = item.get("played_at") or ""
            if not track.get("id") or not played_at:
                continue
            try:
                ts_ms, day = _local_day_from_iso(played_at)
            except ValueError:
                st["failed"] += 1
                continue
            meta = _track_meta(track)
            if _dedup_event(conn, track["id"], ts_ms, meta["track_name"], meta["artists"]):
                st["duplicates"] += 1
                continue
            _events.upsert_track(track["id"], meta["track_name"], album=meta["album"],
                                 album_id=meta["album_id"], duration_ms=meta["duration_ms"])
            _events.write_event(
                event_type=_events.SONG_PLAYED, ts_ms=ts_ms, day=day, track_id=track["id"],
                artist_id=(meta["artist_ids"] or [None])[0], album_id=meta["album_id"],
                payload=dict(meta, source="spotify_recent"), source="spotify_recent",
            )
            st["imported_plays"] += 1

    def top_artists():
        seen = set()
        for time_range in ("short_term", "medium_term", "long_term"):
            result = _sp_call(sp.current_user_top_artists, limit=50, time_range=time_range, sleep=sleep) or {}
            for artist in result.get("items") or []:
                aid, name = artist.get("id"), artist.get("name") or ""
                if not aid:
                    continue
                img = ((artist.get("images") or [{}])[-1] or {}).get("url")
                ext_url = (artist.get("external_urls") or {}).get("spotify")
                conn.execute(
                    "INSERT INTO artist_exploration (artist_id, updated_at) VALUES (?, ?) "
                    "ON CONFLICT(artist_id) DO UPDATE SET updated_at = excluded.updated_at",
                    (aid, now_ms),
                )
                conn.execute(
                    "INSERT INTO artists (id, name, image_url, spotify_url, created_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                    "image_url = COALESCE(excluded.image_url, image_url), "
                    "spotify_url = COALESCE(excluded.spotify_url, spotify_url)",
                    (aid, name, img, ext_url, now_ms),
                )
                if aid not in seen:
                    seen.add(aid)
                    st["imported_artists"] += 1
                    # Seeding favorites is opt-in: top artists are not necessarily liked artists.
                    if seed_artist_likes:
                        conn.execute(
                            "INSERT INTO artist_likes (artist_id, artist_name, auto_liked, liked_at) "
                            "VALUES (?, ?, 1, ?) ON CONFLICT(artist_id) DO NOTHING",
                            (aid, name, now_ms),
                        )
                for genre in (artist.get("genres") or [])[:5]:
                    conn.execute(
                        "INSERT INTO traits (name, source, score, updated_at) VALUES (?, 'spotify_top', 1.0, ?) "
                        "ON CONFLICT(name) DO UPDATE SET score = MIN(traits.score + 0.2, 5.0), "
                        "updated_at = excluded.updated_at",
                        (genre, now_ms),
                    )
            conn.commit()

    def top_tracks():
        for time_range in ("short_term", "medium_term", "long_term"):
            result = _sp_call(sp.current_user_top_tracks, limit=50, time_range=time_range, sleep=sleep) or {}
            for track in result.get("items") or []:
                if not track.get("id"):
                    continue
                meta = _track_meta(track)
                _events.upsert_track(track["id"], meta["track_name"], album=meta["album"],
                                     album_id=meta["album_id"], duration_ms=meta["duration_ms"])

    def saved():
        offset = 0
        while True:
            result = _sp_call(sp.current_user_saved_tracks, limit=50, offset=offset, sleep=sleep) or {}
            items = result.get("items") or []
            if not items:
                break
            for item in items:
                track = item.get("track") or item.get("item") or {}   # "item": Spotify's 2026 naming
                tid = track.get("id")
                if not tid:
                    continue
                try:
                    ts_ms, day = _local_day_from_iso(item.get("added_at") or "")
                except ValueError:
                    ts_ms, day = now_ms, datetime.now().date().isoformat()
                conn.execute("INSERT OR REPLACE INTO saved_tracks (track_id, added_at) VALUES (?, ?)", (tid, ts_ms))
                conn.commit()
                if _dedup_liked(conn, tid):
                    st["duplicates"] += 1
                    continue
                meta = _track_meta(track)
                _events.upsert_track(tid, meta["track_name"], album=meta["album"],
                                     album_id=meta["album_id"], duration_ms=meta["duration_ms"])
                _events.write_event(
                    event_type=_events.TRACK_LIKED, ts_ms=ts_ms, day=day, track_id=tid,
                    artist_id=(meta["artist_ids"] or [None])[0], album_id=meta["album_id"],
                    payload=dict(meta, source="spotify_saved"), source="spotify_saved",
                )
                st["imported_liked"] += 1
            if not result.get("next"):
                break
            offset += 50
            (sleep or _sleep)(0.3)

    try:
        step("Recently played", recent)
        step("Top artists", top_artists)
        step("Top tracks", top_tracks)
        step("Saved tracks", saved)
        st.update(status="done", step="Complete")
    except Exception as exc:
        st.update(status="error", error=str(exc), step="")
        _log_error(exc, "spotify import")
    finally:
        st.update(running=False, finished_at=_now_ms())
    return st


def _spotify_import_worker(seed_artist_likes=False):
    """Thread body (kept under its old name); the caller holds _sp_lock."""
    try:
        core = _core()
        sp = core.get_sp() if core is not None else None
        if not sp:
            _spotify_import_state.update(running=False, status="error", step="",
                                         error="Not signed in to Spotify", finished_at=_now_ms())
            return
        run_spotify_import(sp, _db.get_conn(), seed_artist_likes=seed_artist_likes, state=_spotify_import_state)
    except Exception as exc:
        _spotify_import_state.update(running=False, status="error", error=str(exc), finished_at=_now_ms())
        _log_error(exc, "spotify import worker")
    finally:
        _sp_lock.release()


def start_spotify_import(seed_artist_likes=False):
    """Returns (started, reason) with reason in None | 'running'."""
    if not _sp_lock.acquire(blocking=False):
        return False, "running"
    _spotify_import_state.update(running=True, status="running", step="Starting...", imported_plays=0,
                                 imported_artists=0, imported_liked=0, duplicates=0, failed=0,
                                 error=None, steps={}, seed_artist_likes=bool(seed_artist_likes),
                                 started_at=_now_ms(), finished_at=None)
    try:
        threading.Thread(target=_spotify_import_worker, kwargs={"seed_artist_likes": bool(seed_artist_likes)},
                         name="spotify-import", daemon=True).start()
    except Exception:
        _sp_lock.release()
        raise
    return True, None


def spotify_import_status():
    return dict(_spotify_import_state, steps=dict(_spotify_import_state.get("steps") or {}))


# Legacy alias: app.py used to own this state under the same name.
_lfm_import_state = _lfm_state
