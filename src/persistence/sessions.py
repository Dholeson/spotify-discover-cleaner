"""
Listen session tracking.

A "listen session" is a continuous listening period.  If the poll loop
detects no playback for SESSION_GAP_S seconds the current session is
considered ended and the next track starts a new one.

Usage (from poll_loop):
    sessions.heartbeat(track_id, ts_ms, day)   — call whenever a track is playing
    sessions.close_if_stale(ts_ms)             — call when playback stops
    sessions.current_session_id()              — returns int or None

track_count counts distinct consecutive tracks (a track change), not poll ticks. To keep
the poll loop cheap, ended_at is written on a track change or at most every
HEARTBEAT_WRITE_S seconds; closing a session always writes the exact last heartbeat.
"""

import threading
import time
from datetime import date

from .db import get_conn

SESSION_GAP_S = 30 * 60  # 30 minutes of silence → new session
HEARTBEAT_WRITE_S = 30

_lock = threading.Lock()
_current_session_id: int | None = None
_last_heartbeat_ms: int = 0
_last_track_id: str | None = None
_last_write_ms: int = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _start_session(ts_ms: int, day: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO listen_sessions (started_at, ended_at, track_count, day) VALUES (?, ?, 0, ?)",
        (ts_ms, ts_ms, day),
    )
    conn.commit()
    return cur.lastrowid


def _update_session(session_id: int, ts_ms: int, new_track: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE listen_sessions SET ended_at = ?, track_count = track_count + ? WHERE id = ?",
        (ts_ms, 1 if new_track else 0, session_id),
    )
    conn.commit()


def _close_session(session_id: int, ts_ms: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE listen_sessions SET ended_at = ? WHERE id = ?",
        (ts_ms, session_id),
    )
    conn.commit()


def heartbeat(track_id: str | None, ts_ms: int | None = None, day: str | None = None) -> int | None:
    """
    Called every poll cycle when music is playing.
    Returns the current session_id (starts a new one if gap exceeded).
    """
    global _current_session_id, _last_heartbeat_ms, _last_track_id, _last_write_ms
    if ts_ms is None:
        ts_ms = _now_ms()
    if day is None:
        day = date.today().isoformat()

    try:
        with _lock:
            gap_s = (ts_ms - _last_heartbeat_ms) / 1000 if _last_heartbeat_ms else SESSION_GAP_S + 1

            new_session = _current_session_id is None or gap_s > SESSION_GAP_S
            if new_session:
                if _current_session_id is not None and _last_heartbeat_ms:
                    _close_session(_current_session_id, _last_heartbeat_ms)
                _current_session_id = _start_session(ts_ms, day)
                _last_track_id = None

            new_track = bool(track_id) and track_id != _last_track_id
            if new_track or new_session or (ts_ms - _last_write_ms) >= HEARTBEAT_WRITE_S * 1000:
                _update_session(_current_session_id, ts_ms, new_track)
                _last_write_ms = ts_ms
            if track_id:
                _last_track_id = track_id
            _last_heartbeat_ms = ts_ms
            return _current_session_id
    except Exception:
        return None


def close_if_stale(ts_ms: int | None = None) -> None:
    """
    Call when playback stops.  Closes the active session if last heartbeat
    was more than SESSION_GAP_S ago.
    """
    global _current_session_id, _last_heartbeat_ms, _last_track_id, _last_write_ms
    if ts_ms is None:
        ts_ms = _now_ms()

    try:
        with _lock:
            if _current_session_id is None:
                return
            gap_s = (ts_ms - _last_heartbeat_ms) / 1000 if _last_heartbeat_ms else 0
            if gap_s > SESSION_GAP_S:
                _close_session(_current_session_id, _last_heartbeat_ms)
                _current_session_id = None
                _last_heartbeat_ms  = 0
                _last_track_id = None
                _last_write_ms = 0
    except Exception:
        pass


def current_session_id() -> int | None:
    with _lock:
        return _current_session_id


def get_recent_sessions(limit: int = 10) -> list:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, started_at, ended_at, track_count, day
        FROM listen_sessions
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
