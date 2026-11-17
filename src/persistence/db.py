"""
SQLite database bootstrap.

Provides per-thread connections (via threading.local), the base schema DDL and the
versioned migrations. Call init_db() once at startup before any other persistence
module is used (main() and the sandbox harness both do).

Migrations are keyed by ``PRAGMA user_version``. When any pending step changes user rows, one
full copy of the database is written next to it before the first pending step
(``music_explorer.db.bak-v{n}``, n = that step), and each step runs inside one ``BEGIN IMMEDIATE`` transaction, so a failing
step leaves the database untouched. Backups are never deleted automatically.
Every step is idempotent. Heavy derived data (the plays ledger) is NOT built here: that
happens in a background worker so startup never waits for it.
"""

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "music_explorer.db"
SCHEMA_VERSION = 3

_local = threading.local()

# Outcome of the last run_migrations() call, for the data-health endpoint.
MIGRATION_STATUS = {"version": None, "applied": [], "error": None, "backups": []}


def get_conn() -> sqlite3.Connection:
    """Return a per-thread SQLite connection (created on first call per thread)."""
    path = str(DB_PATH)
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL + NORMAL is durable across app crashes (only an OS crash can drop the last commits)
    # and avoids an fsync per poll-loop write.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    # A checkpoint that finds no readers shrinks the -wal file back to this size, so one big
    # ledger rebuild (~25 MB of WAL) doesn't leave a file that large behind for good.
    conn.execute("PRAGMA journal_size_limit=16777216")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    _local.path = path
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
-- Core entities -------------------------------------------------------

CREATE TABLE IF NOT EXISTS tracks (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    album       TEXT,
    album_id    TEXT,
    image_url   TEXT,
    spotify_url TEXT,
    duration_ms INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artists (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    image_url   TEXT,
    spotify_url TEXT,
    lastfm_url  TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS track_artists (
    track_id    TEXT    NOT NULL REFERENCES tracks(id),
    artist_id   TEXT    NOT NULL,
    artist_name TEXT    NOT NULL,
    position    INTEGER DEFAULT 0,
    PRIMARY KEY (track_id, artist_id)
);

-- Events ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,
    ts_ms       INTEGER NOT NULL,
    day         TEXT    NOT NULL,   -- LOCAL date YYYY-MM-DD
    track_id    TEXT,
    artist_id   TEXT,
    album_id    TEXT,
    payload     TEXT,
    source      TEXT DEFAULT 'live'
);

CREATE INDEX IF NOT EXISTS events_track ON events(track_id);
CREATE INDEX IF NOT EXISTS events_day   ON events(day);
CREATE INDEX IF NOT EXISTS events_ts    ON events(ts_ms);

-- Listen sessions -------------------------------------------------------

CREATE TABLE IF NOT EXISTS listen_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    track_count  INTEGER DEFAULT 0,
    day          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ls_day ON listen_sessions(day);

-- Track listens ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS track_listens (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id          TEXT    NOT NULL REFERENCES tracks(id),
    session_id        INTEGER REFERENCES listen_sessions(id),
    ts_ms             INTEGER NOT NULL,
    day               TEXT    NOT NULL,
    listen_duration_s REAL,
    completed         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS tl_track   ON track_listens(track_id);
CREATE INDEX IF NOT EXISTS tl_day     ON track_listens(day);

-- Research queue --------------------------------------------------------

CREATE TABLE IF NOT EXISTS interests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT    NOT NULL,
    entity_id   TEXT,
    entity_name TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active',
    note        TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS interest_reasons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    interest_id INTEGER NOT NULL REFERENCES interests(id) ON DELETE CASCADE,
    tag         TEXT    NOT NULL
);

-- Artist / album exploration --------------------------------------------

CREATE TABLE IF NOT EXISTS artist_exploration (
    artist_id           TEXT    PRIMARY KEY,
    first_heard_at      INTEGER,
    last_heard_at       INTEGER,
    total_listens       INTEGER DEFAULT 0,
    albums_explored     INTEGER DEFAULT 0,
    albums_total        INTEGER DEFAULT 0,
    updated_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS album_exploration (
    album_id            TEXT    PRIMARY KEY,
    artist_id           TEXT,
    album_name          TEXT,
    release_year        INTEGER,
    total_tracks        INTEGER DEFAULT 0,
    heard_tracks        INTEGER DEFAULT 0,
    completed           INTEGER DEFAULT 0,
    first_heard_at      INTEGER,
    last_heard_at       INTEGER,
    updated_at          INTEGER NOT NULL
);

-- Taste traits ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS traits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    source      TEXT,
    score       REAL    DEFAULT 0,
    updated_at  INTEGER NOT NULL
);

-- Gateway paths ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS gateway_paths (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_track_id   TEXT,
    seed_artist_id  TEXT,
    path_json       TEXT,
    gateway_score   REAL    DEFAULT 0,
    created_at      INTEGER NOT NULL
);

-- Liked artists ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS artist_likes (
    artist_id   TEXT    PRIMARY KEY,
    artist_name TEXT    NOT NULL,
    auto_liked  INTEGER DEFAULT 0,
    liked_at    INTEGER NOT NULL,
    track_count INTEGER DEFAULT 0
);

-- App metadata ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create all tables if they do not exist and run pending migrations.

    Safe to call multiple times. A failing migration is logged and leaves the database at
    its previous version (the app keeps working on the older schema); it never raises.
    """
    conn = get_conn()
    conn.executescript(_SCHEMA)
    _upgrade_schema(conn)
    conn.commit()
    run_migrations(conn)


def _column_names(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}


def _upgrade_schema(conn: sqlite3.Connection) -> None:
    """Patch older local DBs forward without forcing a destructive rebuild."""
    track_listens_cols = _column_names(conn, "track_listens")
    if track_listens_cols and "session_id" not in track_listens_cols:
        conn.execute("ALTER TABLE track_listens ADD COLUMN session_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS tl_session ON track_listens(session_id)")

    # Add source column to events (tracks which system wrote the event)
    events_cols = _column_names(conn, "events")
    if events_cols and "source" not in events_cols:
        conn.execute("ALTER TABLE events ADD COLUMN source TEXT DEFAULT 'live'")
        # Backfill existing Last.fm import rows from their payload field
        conn.execute("""
            UPDATE events
            SET source = 'lastfm'
            WHERE event_type = 'SONG_PLAYED'
              AND json_extract(payload, '$.source') = 'lastfm_import'
              AND (source IS NULL OR source = 'live')
        """)
    # (events_source / events_type were replaced by events_type_ts in migration v2.)


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def schema_version(conn=None) -> int:
    conn = conn or get_conn()
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def backup_path(version: int) -> Path:
    return DB_PATH.with_name(f"{DB_PATH.name}.bak-v{version}")


def list_backups() -> list:
    try:
        return sorted(p.name for p in DB_PATH.parent.glob(f"{DB_PATH.name}.bak-v*")
                      if not p.name.endswith(".tmp"))
    except OSError:
        return []


def backup_db(conn: sqlite3.Connection, version: int) -> Path:
    """Write a consistent copy of the live database (WAL included) to .bak-v{version}.

    An older file with the same name can only come from an earlier attempt of this same
    step that rolled back, so the fresher copy replaces it.
    """
    dest = backup_path(version)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn.commit()
    target = sqlite3.connect(str(tmp))
    try:
        conn.backup(target)
    finally:
        target.close()
    os.replace(tmp, dest)
    return dest


def _say(line: str) -> None:
    # The console may be cp1252 (a redirected Windows stdout): a print must never fail a migration.
    try:
        print(line, flush=True)
    except Exception:
        pass


def _report(msg: str) -> None:
    log.info(msg)
    _say(f"  - DB {msg}")


def _count(conn, sql, params=()):
    return int(conn.execute(sql, params).fetchone()[0])


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Local days for imported rows, a real Last.fm dedup key, removal-loop cleanup."""
    conn.execute("CREATE INDEX IF NOT EXISTS events_type_ts ON events(event_type, ts_ms)")
    conn.execute("DROP INDEX IF EXISTS events_type")
    conn.execute("DROP INDEX IF EXISTS events_source")

    # Imports stored the UTC date; everything else (and every report) uses the local date.
    where_day = ("COALESCE(source, 'live') <> 'live' "
                 "AND day IS NOT date(ts_ms / 1000, 'unixepoch', 'localtime')")
    n = _count(conn, f"SELECT COUNT(*) FROM events WHERE {where_day}")
    _report(f"v2: {n} imported events move to their local day")
    if n:
        conn.execute(f"UPDATE events SET day = date(ts_ms / 1000, 'unixepoch', 'localtime') WHERE {where_day}")

    # Scrobbles are unique on (time, artist, track): several scrobbles can share a second. The
    # oldest row of each group stays. One grouped (uncorrelated) pass, so the cost doesn't depend
    # on which indexes exist (a correlated EXISTS planned through events_source is O(n^2)).
    # NULL keys never collide, exactly like the unique index below.
    dup_where = ("source = 'lastfm' "
                 "AND json_extract(payload, '$.artists') IS NOT NULL "
                 "AND json_extract(payload, '$.track_name') IS NOT NULL "
                 "AND id NOT IN (SELECT MIN(id) FROM events WHERE source = 'lastfm' "
                 "GROUP BY ts_ms, json_extract(payload, '$.artists'), json_extract(payload, '$.track_name'))")
    n = _count(conn, f"SELECT COUNT(*) FROM events WHERE {dup_where}")
    _report(f"v2: {n} duplicate Last.fm rows removed")
    if n:
        conn.execute(f"DELETE FROM events WHERE {dup_where}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS events_lastfm_uniq ON events("
        "ts_ms, json_extract(payload, '$.artists'), json_extract(payload, '$.track_name')) "
        "WHERE source = 'lastfm'"
    )

    # TRACK_COMPLETED also wrote a track_listens row, so each auto-cleared play was a listen twice.
    paired = ("EXISTS (SELECT 1 FROM events e WHERE e.event_type = 'TRACK_COMPLETED' "
              "AND e.track_id = track_listens.track_id AND e.ts_ms = track_listens.ts_ms) "
              "AND NOT EXISTS (SELECT 1 FROM events s WHERE s.event_type = 'SONG_PLAYED' "
              "AND s.track_id = track_listens.track_id AND s.ts_ms = track_listens.ts_ms)")
    n = _count(conn, f"SELECT COUNT(*) FROM track_listens WHERE {paired}")
    _report(f"v2: {n} track_listens rows written by TRACK_COMPLETED removed")
    if n:
        conn.execute(f"DELETE FROM track_listens WHERE {paired}")

    # A removal loop (2026-05-13) logged the same removal up to 20 times: keep one per track per day.
    for etype in ("TRACK_REMOVED_FROM_QUEUE", "TRACK_COMPLETED"):
        dup = ("event_type = ? AND track_id IS NOT NULL AND id > (SELECT MIN(d.id) FROM events d "
               "WHERE d.event_type = events.event_type AND d.track_id = events.track_id AND d.day = events.day)")
        n = _count(conn, f"SELECT COUNT(*) FROM events WHERE {dup}", (etype,))
        _report(f"v2: {n} repeated {etype} events removed")
        if n:
            conn.execute(f"DELETE FROM events WHERE {dup}", (etype,))

    # listen_sessions.track_count counted poll ticks; re-derive it from the plays in each session.
    fix = ("(SELECT COUNT(*) FROM events e WHERE e.event_type = 'SONG_PLAYED' AND e.source = 'live' "
           "AND e.ts_ms BETWEEN listen_sessions.started_at "
           "AND COALESCE(listen_sessions.ended_at, listen_sessions.started_at) + 60000)")
    n = _count(conn, f"SELECT COUNT(*) FROM listen_sessions WHERE track_count IS NOT {fix}")
    _report(f"v2: {n} listen_sessions track counts recomputed")
    if n:
        conn.execute(f"UPDATE listen_sessions SET track_count = {fix} WHERE track_count IS NOT {fix}")

    conn.execute("ANALYZE")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """The plays ledger and rollup tables (filled by the background worker, not here)."""
    from . import plays  # lazy: plays imports this module

    plays.ensure_schema(conn)
    # Saved-library ids (Spotify import), so 'kept after clearing' becomes measurable.
    conn.execute("CREATE TABLE IF NOT EXISTS saved_tracks (track_id TEXT PRIMARY KEY, added_at INTEGER)")
    conn.execute("DELETE FROM app_meta WHERE key = ?", (plays.META_RULES,))
    _report("v3: plays ledger tables ready (the ledger is built in the background)")


MIGRATIONS = [
    (2, "local days, Last.fm unique key, removal cleanup", _migrate_v2),
    (3, "plays ledger", _migrate_v3),
]
# Steps that only add tables/indexes or clear derived state: they change no user rows, so
# they don't need a full copy of the database of their own.
SCHEMA_ONLY_STEPS = frozenset({3})


def run_migrations(conn=None, backup=True) -> int:
    """Apply pending migrations in order. Returns the resulting schema version.

    One backup per run, taken before the first pending step (it holds everything every later
    step starts from), and none when all pending steps are schema-only.
    """
    conn = conn or get_conn()
    conn.commit()
    current = schema_version(conn)
    MIGRATION_STATUS.update(version=current, error=None)
    need_backup = backup and any(v > current and v not in SCHEMA_ONLY_STEPS for v, _, _ in MIGRATIONS)
    for version, label, step in MIGRATIONS:
        if current >= version:
            continue
        t0 = time.perf_counter()
        try:
            has_data = conn.execute("SELECT EXISTS (SELECT 1 FROM events)").fetchone()[0]
            if need_backup and has_data:
                dest = backup_db(conn, version)
                MIGRATION_STATUS["backups"].append(dest.name)
                _report(f"backup before v{version}: {dest.name}")
                need_backup = False
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have migrated while we waited for the write lock.
                if schema_version(conn) < version:
                    step(conn)
                    conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            current = schema_version(conn)
            MIGRATION_STATUS["applied"].append(version)
            MIGRATION_STATUS["version"] = current
            _report(f"migrated to v{version} ({label}) in {int((time.perf_counter() - t0) * 1000)} ms")
        except Exception as exc:
            MIGRATION_STATUS["error"] = f"v{version}: {exc}"
            log.exception("DB migration v%s failed", version)
            _say(f"  ! DB migration v{version} failed (database left at v{current}): {exc}")
            break
    return current
