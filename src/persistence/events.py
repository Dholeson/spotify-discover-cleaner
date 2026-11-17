"""
Event repository.

Every SONG_PLAYED written here is also applied to the plays ledger
(src/persistence/plays.py) in the same transaction, so reports see live plays at once.
"""

import json
import logging
import math
import sqlite3
import time
from datetime import date, timedelta
from typing import Optional

from .db import get_conn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

SONG_PLAYED                = "SONG_PLAYED"
SONG_SKIPPED               = "SONG_SKIPPED"
TRACK_COMPLETED            = "TRACK_COMPLETED"
TRACK_REPEATED             = "TRACK_REPEATED"
TRACK_LIKED                = "TRACK_LIKED"
TRACK_ADDED_TO_PLAYLIST    = "TRACK_ADDED_TO_PLAYLIST"
TRACK_REMOVED_FROM_QUEUE   = "TRACK_REMOVED_FROM_QUEUE"
TRACK_RESTORED             = "TRACK_RESTORED"      # a cleared backlog track was put back (undo)
THRESHOLD_REACHED          = "THRESHOLD_REACHED"   # auto-clear threshold hit; not a listen
ARTIST_VIEWED              = "ARTIST_VIEWED"
RELATED_ARTIST_CLICKED     = "RELATED_ARTIST_CLICKED"
ALBUM_VIEWED               = "ALBUM_VIEWED"
ALBUM_COMPLETED            = "ALBUM_COMPLETED"
LYRICS_OPENED              = "LYRICS_OPENED"
RESEARCH_ADDED             = "RESEARCH_ADDED"
RESEARCH_UPDATED           = "RESEARCH_UPDATED"
RESEARCH_PROMOTED          = "RESEARCH_PROMOTED"
ARTIST_REVISITED           = "ARTIST_REVISITED"
ARTIST_LIKED               = "ARTIST_LIKED"

_LEGACY_TYPE_MAP = {
    "listen":   SONG_PLAYED,
    "removed":  TRACK_REMOVED_FROM_QUEUE,
    "liked":    TRACK_LIKED,
    "added":    TRACK_ADDED_TO_PLAYLIST,
    "skipped":  SONG_SKIPPED,
    "restored": TRACK_RESTORED,
}

# Fields of a legacy (config.history) entry that are persisted in the event payload.
# track_name/album/ids let the plays ledger name live plays without guessing; started_ms,
# started_in_backlog and auto_removed come from the listen accounting in poll_loop.
_LEGACY_PAYLOAD_FIELDS = (
    "artists", "genres", "listened_s", "reason", "source", "target", "was_in_playlist",
    "track_name", "album", "album_id", "artist_ids", "artist_names", "duration_ms",
    "started_ms", "started_in_backlog", "auto_removed",
)

# A listen is "completed" when at least this share of the track was heard.
COMPLETED_SHARE = 0.8


def _now_ms() -> int:
    return int(time.time() * 1000)


def _today() -> str:
    return date.today().isoformat()  # local date, same rule as the plays ledger


# ---------------------------------------------------------------------------
# Track upsert
# ---------------------------------------------------------------------------

def upsert_track(track_id: str, name: str, album: str = None,
                 album_id: str = None,
                 image_url: str = None, spotify_url: str = None,
                 duration_ms: int = None) -> None:
    if not track_id:
        return
    try:
        duration_ms = int(duration_ms) if duration_ms else None
    except (TypeError, ValueError):
        duration_ms = None
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO tracks (id, name, album, album_id, image_url, spotify_url, duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name        = CASE WHEN excluded.name <> '' THEN excluded.name ELSE name END,
            album       = COALESCE(NULLIF(excluded.album, ''), album),
            album_id    = COALESCE(excluded.album_id, album_id),
            image_url   = COALESCE(excluded.image_url, image_url),
            spotify_url = COALESCE(excluded.spotify_url, spotify_url),
            duration_ms = COALESCE(excluded.duration_ms, duration_ms)
        """,
        (track_id, name or "", album, album_id, image_url, spotify_url, duration_ms, _now_ms()),
    )
    conn.commit()


def upsert_track_artists(track_id: str, artist_ids: list, artist_names: list) -> None:
    """Record a track's credited artists in order (fills the track_artists table)."""
    if not track_id or not artist_names:
        return
    conn = get_conn()
    for pos, name in enumerate(artist_names):
        aid = (artist_ids[pos] if artist_ids and pos < len(artist_ids) else None) or f"name:{name}"
        conn.execute(
            "INSERT INTO track_artists (track_id, artist_id, artist_name, position) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(track_id, artist_id) DO UPDATE SET artist_name = excluded.artist_name, "
            "position = excluded.position",
            (track_id, aid, name, pos),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Event write
# ---------------------------------------------------------------------------

def write_event(
    event_type: str,
    ts_ms: int,
    day: str,
    track_id: Optional[str] = None,
    artist_id: Optional[str] = None,
    album_id: Optional[str] = None,
    payload: Optional[dict] = None,
    source: str = "live",
) -> int:
    """Insert one event; a SONG_PLAYED is applied to the plays ledger in the same commit."""
    conn = get_conn()
    payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.commit()  # settle anything a caller left pending before taking the write lock
    _begin_immediate(conn)
    try:
        cur = conn.execute(
            """
            INSERT INTO events (event_type, ts_ms, day, track_id, artist_id, album_id, payload, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, ts_ms, day, track_id, artist_id, album_id, payload_json, source),
        )
        event_id = cur.lastrowid
        if event_type == SONG_PLAYED:
            _ingest_play(conn, {
                "id": event_id, "event_type": event_type, "ts_ms": ts_ms, "source": source,
                "track_id": track_id, "payload": payload or {},
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return event_id


def _begin_immediate(conn, attempts=2) -> None:
    """Take the write lock, trying once more when the busy timeout (10 s) runs out.

    Callers of write_event (the poll loop's play log) only log a failure, so an event lost to
    one long lock holder (e.g. a ledger rebuild's swap) would be missing from history for good.
    """
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            log.warning("events: database busy, retrying write once: %s", exc)


def _ingest_play(conn, row) -> None:
    try:
        from . import plays
        plays.ingest_event(conn, row, commit=False)  # isolates its own failures (savepoint)
    except Exception as exc:  # the event itself must never be lost to a ledger problem
        log.warning("plays ingest failed for event %s: %s", row.get("id"), exc)


def write_listen(
    track_id: str,
    ts_ms: int,
    day: str,
    listen_duration_s: Optional[float] = None,
    completed: bool = False,
    session_id: Optional[int] = None,
) -> int:
    if not track_id:
        return -1
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO track_listens
            (track_id, session_id, ts_ms, day, listen_duration_s, completed)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (track_id, session_id, ts_ms, day, listen_duration_s, 1 if completed else 0),
    )
    conn.commit()
    return cur.lastrowid


def _float_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def is_completed_listen(listened_s, duration_ms) -> bool:
    """True when at least COMPLETED_SHARE of the track was heard (unknown duration: False)."""
    ls = _float_or_none(listened_s)
    dur = _float_or_none(duration_ms)
    return bool(ls is not None and dur and dur > 0 and ls * 1000 >= COMPLETED_SHARE * dur)


# ---------------------------------------------------------------------------
# Write from legacy history entry
# ---------------------------------------------------------------------------

def write_from_legacy(entry: dict) -> None:
    legacy_type = entry.get("type") or "listen"
    # Unknown types keep their own name: they must never be counted as plays.
    event_type  = _LEGACY_TYPE_MAP.get(legacy_type) or str(legacy_type).upper()
    ts_ms       = int(entry.get("ts_ms") or _now_ms())
    day         = entry.get("day") or _today()
    track_id    = entry.get("track_id") or None
    track_name  = entry.get("track_name") or ""
    album       = entry.get("album")
    album_id    = entry.get("album_id") or None
    image_url   = entry.get("image")
    spotify_url = entry.get("url")
    duration_ms = entry.get("duration_ms")
    artist_ids  = [a for a in (entry.get("artist_ids") or []) if a]
    artist_names = [a for a in (entry.get("artist_names") or []) if a]

    if track_id:
        upsert_track(track_id, track_name, album=album, album_id=album_id,
                     image_url=image_url, spotify_url=spotify_url, duration_ms=duration_ms)
        if artist_names:
            try:
                upsert_track_artists(track_id, artist_ids, artist_names)
            except Exception as exc:
                log.debug("track_artists upsert failed for %s: %s", track_id, exc)

    payload = {}
    for field in _LEGACY_PAYLOAD_FIELDS:
        if entry.get(field) is not None:
            payload[field] = entry[field]

    write_event(
        event_type=event_type,
        ts_ms=ts_ms,
        day=day,
        track_id=track_id,
        artist_id=artist_ids[0] if artist_ids else None,
        album_id=album_id,
        payload=payload if payload else None,
    )

    if event_type == SONG_PLAYED and track_id:
        listened_s = _float_or_none(entry.get("listened_s"))
        write_listen(
            track_id=track_id,
            ts_ms=ts_ms,
            day=day,
            listen_duration_s=listened_s,
            completed=is_completed_listen(listened_s, duration_ms),
        )


# ---------------------------------------------------------------------------
# record_action — new events with no legacy counterpart
# ---------------------------------------------------------------------------

def record_action(
    event_type: str,
    track_id: str = None,
    track_name: str = None,
    album: str = None,
    album_id: str = None,
    image_url: str = None,
    spotify_url: str = None,
    artist_id: str = None,
    payload: dict = None,
    session_id: int = None,
    listened_s: float = None,
    completed: bool = False,
    duration_ms: int = None,
) -> None:
    ts_ms = _now_ms()
    day = _today()

    if track_id:
        upsert_track(track_id, track_name or "", album=album, album_id=album_id,
                     image_url=image_url, spotify_url=spotify_url, duration_ms=duration_ms)

    write_event(
        event_type=event_type,
        ts_ms=ts_ms,
        day=day,
        track_id=track_id,
        artist_id=artist_id,
        album_id=album_id,
        payload=payload,
    )

    # Only a SONG_PLAYED is a listen. TRACK_COMPLETED / THRESHOLD_REACHED mark the same
    # listen crossing the clear threshold and used to count it twice.
    if event_type == SONG_PLAYED and track_id:
        write_listen(
            track_id=track_id,
            ts_ms=ts_ms,
            day=day,
            listen_duration_s=listened_s,
            completed=bool(completed) or is_completed_listen(listened_s, duration_ms),
            session_id=session_id,
        )


# ---------------------------------------------------------------------------
# Artist likes
# ---------------------------------------------------------------------------

_artist_likes_table_ensured = False

def _ensure_artist_likes_table() -> None:
    """Create artist_likes if it was missing from an older DB schema."""
    global _artist_likes_table_ensured
    if _artist_likes_table_ensured:
        return
    try:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS artist_likes (
                artist_id   TEXT    PRIMARY KEY,
                artist_name TEXT    NOT NULL,
                auto_liked  INTEGER DEFAULT 0,
                liked_at    INTEGER NOT NULL,
                track_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        _artist_likes_table_ensured = True
    except Exception:
        pass


def upsert_artist_like(artist_id: str, artist_name: str,
                       auto_liked: bool = False, track_count: int = 0) -> None:
    """Insert or update a liked-artist record.

    Re-syncs keep the original liked_at, and a manual like is never downgraded back to
    'auto' (auto_liked only goes 1 -> 0).
    """
    _ensure_artist_likes_table()
    key = artist_id or artist_name
    if not key:
        return
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO artist_likes (artist_id, artist_name, auto_liked, liked_at, track_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(artist_id) DO UPDATE SET
            artist_name = excluded.artist_name,
            auto_liked  = MIN(COALESCE(artist_likes.auto_liked, 1), excluded.auto_liked),
            track_count = excluded.track_count
        """,
        (key, artist_name, 1 if auto_liked else 0, _now_ms(), track_count),
    )
    conn.commit()


def is_artist_liked(artist_id_or_name: str) -> bool:
    """True if this artist is in artist_likes (by id or name, case-insensitive on name)."""
    if not artist_id_or_name:
        return False
    _ensure_artist_likes_table()
    try:
        conn = get_conn()
        row = conn.execute(
            """
            SELECT 1 FROM artist_likes
            WHERE artist_id = ?
               OR lower(artist_name) = lower(?)
            """,
            (artist_id_or_name, artist_id_or_name),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def get_artist_likes() -> list:
    """All liked artists, newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM artist_likes ORDER BY liked_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_listen_count(track_id: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM track_listens WHERE track_id = ?", (track_id,)
    ).fetchone()
    return row["n"] if row else 0


def get_heard_track_ids() -> set:
    """All distinct track_ids that appear in track_listens."""
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT track_id FROM track_listens").fetchall()
    return {r["track_id"] for r in rows}


def get_recent_events(limit: int = 100, event_type: str = None) -> list:
    conn = get_conn()
    if event_type:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_type = ? ORDER BY ts_ms DESC LIMIT ?",
            (event_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY ts_ms DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_day_counts(days: int = 14) -> list:
    """Per local day: live listens, distinct removals and completions for the last N days."""
    conn = get_conn()
    since = (date.today() - timedelta(days=int(days))).isoformat()  # local, like events.day
    rows = conn.execute(
        """
        SELECT day,
               SUM(CASE WHEN event_type = ? AND source = 'live' THEN 1 ELSE 0 END) AS listens,
               COUNT(DISTINCT CASE WHEN event_type = ? THEN track_id END) AS removed,
               COUNT(DISTINCT CASE WHEN event_type = ? THEN track_id END) AS completed
        FROM events
        WHERE day >= ?
        GROUP BY day
        ORDER BY day
        """,
        (SONG_PLAYED, TRACK_REMOVED_FROM_QUEUE, TRACK_COMPLETED, since),
    ).fetchall()
    return [dict(r) for r in rows]
