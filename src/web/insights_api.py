"""Reports & insights API (v2): period reports, all-time overview, year in music, backlog
insights, and the ``today`` block of /api/status.

All numbers come from src/services/reports.py and src/services/backlog_insights.py; this module
only validates input, maps failures to the v2 error shape and wires the status extension and a
small pre-warm worker (so the whole-history cache is rebuilt off the request path).
"""

import logging
import sqlite3
import threading
import time

from flask import Blueprint, jsonify, request

from . import api_error
from src.services import backlog_insights as _backlog
from src.services import reports as _reports

log = logging.getLogger(__name__)

bp = Blueprint("insights_api", __name__)

TODAY_CACHE_S = 10.0
PREWARM_INTERVAL_S = 5.0
NOT_READY_MESSAGE = ("Your listening history is still being built. Reports will appear in a minute; "
                     "playback and the backlog keep working meanwhile.")


def _core():
    import app as core  # lazy: app imports this package
    return core


def _conn():
    core = _core()
    if not getattr(core, "_SQLITE_ENABLED", False):
        raise _reports.HistoryNotReady("SQLite persistence is unavailable")
    return core._db.get_conn()


def _log_error(exc, context):
    try:
        _core()._log_app_error(exc, context)
    except Exception:
        log.exception("%s", context)


def _run(fn, context, needs_plays=True):
    """Call fn(conn) and translate failures into v2 error responses."""
    try:
        conn = _conn()
        if needs_plays and not _reports.plays_ready(conn):
            return api_error("history_not_ready", NOT_READY_MESSAGE, 503, retry_after=30)
        return jsonify(fn(conn))
    except _reports.BadRequest as exc:
        return api_error("bad_request", str(exc), 400, field=exc.field)
    except _reports.HistoryNotReady:
        return api_error("history_not_ready", NOT_READY_MESSAGE, 503, retry_after=30)
    except sqlite3.OperationalError as exc:
        # e.g. the ledger is being rebuilt underneath us ("no such table") or the DB is busy
        _log_error(exc, context)
        return api_error("history_not_ready", NOT_READY_MESSAGE, 503, retry_after=10)
    except Exception as exc:  # never a bare 500 page
        _log_error(exc, context)
        return api_error("internal_error", "Something went wrong while building this report.", 500)


@bp.get("/api/v2/reports")
def api_reports():
    period = (request.args.get("period") or "month").strip().lower()
    anchor = (request.args.get("anchor") or "").strip() or None
    limit = (request.args.get("limit") or "10").strip()
    return _run(lambda conn: _reports.report(conn, period, anchor, limit, ledger_wait=False),
                "GET /api/v2/reports")


@bp.get("/api/v2/insights/overview")
def api_insights_overview():
    return _run(lambda conn: _reports.overview(conn, ledger_wait=False), "GET /api/v2/insights/overview")


@bp.get("/api/v2/insights/year-in-music")
def api_year_in_music():
    year = (request.args.get("year") or "").strip() or None
    if year is not None and not (year.isdigit() and len(year) == 4):
        return api_error("bad_request", "year must be a four-digit year like 2025.", 400, field="year")
    return _run(lambda conn: _reports.year_in_music(conn, year, ledger_wait=False),
                "GET /api/v2/insights/year-in-music")


@bp.get("/api/v2/backlog/insights")
def api_backlog_insights():
    core = _core()
    with core.state_lock:
        history = list(core.state.get("backlog_history") or [])
        playlist_id = core.state.get("playlist_id")
        total = core.state.get("playlist_total")
        loaded = bool(core.state.get("playlist_track_ids_loaded"))
    # playlist_total is 0 until the playlist has been read once; don't report that as "empty"
    current = int(total) if isinstance(total, int) and (total > 0 or loaded) else None
    return _run(lambda conn: _backlog.backlog_insights(conn, history, current, playlist_id),
                "GET /api/v2/backlog/insights", needs_plays=False)


# ---------------------------------------------------------------------------
# /api/status "today" extension
# ---------------------------------------------------------------------------

_today_lock = threading.Lock()
_today_cache = {"at": 0.0, "day": None, "sets": None, "last": None}
_seen_today = {"day": None, "ids": set()}   # tracks that crossed the listen threshold while polled


def today_status():
    """{"today": {...}} for /api/status. Cached for 10 s; never raises."""
    try:
        core = _core()
        if not getattr(core, "_SQLITE_ENABLED", False):
            return {}
        with core.state_lock:
            track = core.state.get("current_track") or {}
            tid = track.get("id") if isinstance(track, dict) else None
            listened = float(core.state.get("current_listened") or 0.0)
        min_s = float(getattr(core, "LISTEN_EVENT_MIN_SECONDS", 10.0))
        now = time.time()
        now_ms = int(now * 1000)
        day = _reports.local_day(now_ms).isoformat()
        with _today_lock:
            if _seen_today["day"] != day:
                _seen_today["day"], _seen_today["ids"] = day, set()
            if tid and listened >= min_s:
                # remembered so the count doesn't dip between the track ending and its event
                # showing up in the (cached) DB read
                _seen_today["ids"].add(tid)
            fresh = (_today_cache["sets"] is not None and _today_cache["day"] == day
                     and now - _today_cache["at"] < TODAY_CACHE_S)
            if not fresh:
                _today_cache["sets"] = _reports.today_sets(_conn(), now_ms)
                _today_cache["day"] = day
                _today_cache["at"] = now
            out = _reports.today_from_sets(_today_cache["sets"], extra_heard=_seen_today["ids"])
            _today_cache["last"] = out
        return {"today": out}
    except Exception as exc:
        _log_error(exc, "status extension today")
        last = _today_cache.get("last")
        return {"today": last} if last else {}


# ---------------------------------------------------------------------------
# pre-warm worker: rebuild the whole-history cache when the ledger changes
# ---------------------------------------------------------------------------

def _prewarm_loop():
    time.sleep(3.0)
    while True:
        try:
            core = _core()
            if getattr(core, "_SQLITE_ENABLED", False):
                conn = core._db.get_conn()
                if _reports.plays_ready(conn):
                    _reports.get_ledger(conn, wait=True)
                    # the overview's derived tables (7-day peaks, per-artist timelines) take ~0.1 s;
                    # build them here so the Insights page never waits (a cache hit costs ~3 ms)
                    _reports.overview(conn)
        except Exception as exc:
            _log_error(exc, "reports prewarm")
        time.sleep(PREWARM_INTERVAL_S)


def init(core):
    if core is None:
        return
    core.register_status_extension(today_status)
    register = getattr(core, "register_background_worker", None)
    if callable(register):
        register("reports_prewarm", _prewarm_loop)
