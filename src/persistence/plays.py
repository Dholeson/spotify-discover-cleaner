"""
The plays ledger: one canonical, normalized row per listen.

``events`` keeps everything that happened (Last.fm scrobbles, live polling, imports,
removals, ...). ``plays`` is the derived, query-friendly view every report reads:

- Last.fm scrobbles are canonical. A live SONG_PLAYED row for the same listen is merged
  into its scrobble (Spotify id, seconds heard and backlog flag are copied over) instead
  of being counted twice. The merge works in both directions: a live play that arrives
  after its scrobble links immediately; a scrobble that arrives after a provisional live
  play supersedes it (``superseded_by``).
- A live play under 30 s with no scrobble is not a play (kept in ``plays_skipped``).
- ``ts_ms`` is the play START (live rows are logged at the END of the play).
- Local calendar fields come from ``datetime.fromtimestamp`` (the OS time zone; the venv
  has no tzdata, so never ZoneInfo).
- Offline scrobble batches (runs of >= 5 plays less than 10 s apart) are flagged
  ``ts_quality='bulk'`` because their timestamps are not real.

Canonical plays are ``SELECT ... FROM plays WHERE superseded_by IS NULL``.

Public helpers (imported lazily by reports/artist code): ``artist_key``, ``track_key``,
``song_key``, ``album_key`` and ``ensure_plays_ready``. Changing any normalization rule
means bumping ``RULES_VERSION``: the background worker then rebuilds the whole ledger
(about 2 s for 50K events) while ``ensure_plays_ready`` reports False.
"""

import functools
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime

from . import db as _db

log = logging.getLogger(__name__)

RULES_VERSION = "3"  # 2: live/scrobble merge also requires a compatible artist; 3: no album back-fill

SONG_PLAYED = "SONG_PLAYED"
MIN_LIVE_S = 30.0                      # shorter unmatched live plays are not plays
MATCH_BEFORE_MS = 10 * 60 * 1000       # a scrobble may sit up to 10 min before the live start
MATCH_AFTER_MS = 2 * 60 * 1000         # ... or up to 2 min after the live end
MAX_LISTEN_MS = 60 * 60 * 1000         # bounds window queries on live END timestamps
BULK_GAP_MS = 10_000
BULK_MIN_RUN = 5
PROVISIONAL_SOURCES = ("live", "spotify_recent")
# Scrobbles without an album (almost all from a 2021 bulk import) stay album-less, as on
# Last.fm, so album counts match what the user sees there (Mothership ~874, not ~1,334).
FILL_MISSING_ALBUMS = False

META_RULES = "plays_rules_version"      # rules version the current ledger was built with
META_WATERMARK = "plays_max_event_id"   # highest events.id ingested
META_BUILT_AT = "plays_built_at"        # epoch ms of the last full rebuild
META_BUILD_MS = "plays_build_ms"        # duration of the last full rebuild
META_DIRTY = "plays_rollups_dirty"      # incremental ingest left peak/estimate columns stale
META_NEEDS_REBUILD = "plays_needs_rebuild"

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-"})

# Recording-level noise: same audio, different label.
_FEAT = re.compile(r"\s*[\(\[](?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.I)
_FEAT_DASH = re.compile(r"\s+-\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
_FEAT_BARE = re.compile(r"\s+(?:feat\.|ft\.)\s.*$", re.I)
_REC_NOISE = re.compile(
    r"\s*(?:[\(\[](?:explicit|clean|remaster(?:ed)?(?: \d{4})?(?: version)?|\d{4} remaster(?:ed)?"
    r"|single version|radio edit|album version|mono|stereo)[\)\]]"
    r"|\s-\s(?:explicit|remaster(?:ed)?(?: \d{4})?(?: version)?|\d{4} remaster(?:ed)?|single version"
    r"|radio edit|album version|mono|stereo))\s*$", re.I)
# Song-level: a different performance of the same composition.
_SONG_NOISE = re.compile(
    r"\s*(?:[\(\[](?:live(?: [^\)\]]*)?|tree city sessions(?: \d)?|acoustic(?: version)?|instrumental"
    r"|heavy edit|(?:lost )?demo|stripped|unplugged)[\)\]]"
    r"|\s-\s(?:live(?: .*)?|tree city sessions(?: \d)?|acoustic(?: version)?|instrumental|heavy edit"
    r"|(?:lost )?demo|stripped|unplugged))\s*$", re.I)
_ALB_NOISE = re.compile(
    r"\s*(?:[\(\[](?:deluxe(?: edition| version)?|expanded edition|bonus (?:track )?edition"
    r"|remaster(?:ed)?(?: \d{4})?|\d{4} remaster(?:ed)?|explicit|special edition|anniversary edition"
    r"|definitive)[\)\]]"
    r"|\s-\s(?:ep|single|deluxe(?: edition)?|remaster(?:ed)?(?: \d{4})?))\s*$", re.I)


@functools.lru_cache(maxsize=131072)
def fold(s):
    """NFKC + quote/dash fold + casefold + whitespace collapse (the base of every key)."""
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES).casefold().strip()
    return _WS.sub(" ", s)


@functools.lru_cache(maxsize=131072)
def _recording_title(title):
    s = title or ""
    for _ in range(3):
        s2 = _FEAT.sub("", s)
        s2 = _FEAT_DASH.sub("", s2)
        s2 = _FEAT_BARE.sub("", s2)
        s2 = _REC_NOISE.sub("", s2)
        if s2 == s:
            break
        s = s2
    return fold(s)


@functools.lru_cache(maxsize=131072)
def _composition_title(title):
    s = _recording_title(title)
    for _ in range(3):
        s2 = _SONG_NOISE.sub("", s)
        s2 = _REC_NOISE.sub("", s2)
        if s2 == s:
            break
        s = s2
    return s.strip()


@functools.lru_cache(maxsize=65536)
def _album_title(album):
    s = album or ""
    for _ in range(3):
        s2 = _ALB_NOISE.sub("", s)
        if s2 == s:
            break
        s = s2
    return fold(s)


def artist_key(name):
    """Key for an artist name: 'Woe, Is Me' -> 'woe, is me' (never split on commas)."""
    return fold(name)


def track_key(artist_key, title):
    """Recording key: artist_key|title without feat./explicit/remaster/radio-edit noise."""
    return f"{fold(artist_key)}|{_recording_title(title)}"


def song_key(artist_key, title):
    """Composition key: the recording key minus live/Tree City Sessions/acoustic/demo/..."""
    return f"{fold(artist_key)}|{_composition_title(title)}"


def album_key(artist_key, album):
    """Album key (deluxe/expanded/remastered/' - EP'/' - Single' stripped), None if unknown."""
    a = _album_title(album)
    return f"{fold(artist_key)}|{a}" if a else None


def primary_artist(credit, is_known=None):
    """Primary artist of a Spotify credit that was joined with ', '.

    Band names contain commas ('Woe, Is Me', 'Tyler, The Creator'), so prefer the longest
    comma prefix that is a known artist; fall back to the first token. Last.fm strings are
    a single artist already and must never be passed through here.
    """
    credit = (credit or "").strip()
    if not credit or "," not in credit:
        return credit
    if is_known is not None:
        if is_known(fold(credit)):
            return credit
        cuts = [i for i, ch in enumerate(credit) if ch == ","]
        for pos in reversed(cuts):
            cand = credit[:pos].strip()
            if cand and is_known(fold(cand)):
                return cand
    return credit.split(",", 1)[0].strip()


@functools.lru_cache(maxsize=65536)
def _loose(s):
    """Accent/punctuation-blind form of a key, for matching one listen across two sources."""
    s = unicodedata.normalize("NFKD", fold(s))
    return "".join(ch for ch in s if ch.isalnum())


def credit_keys(credit, artist_names=None):
    """Loose keys of every artist a live credit may name (list entries, comma prefixes and
    comma tokens), so the scrobble of a featured or comma-named artist still matches."""
    keys = set()
    if isinstance(artist_names, (list, tuple)):
        keys.update(_loose(n) for n in artist_names if isinstance(n, str) and n.strip())
    credit = (credit or "").strip()
    if credit:
        keys.add(_loose(credit))
        parts = credit.split(",")
        for i in range(1, len(parts)):
            keys.add(_loose(",".join(parts[:i])))
        keys.update(_loose(p) for p in parts)
    keys.discard("")
    return frozenset(keys)


def _artist_compatible(scrobble_artist_key, live):
    """A scrobble and a live play are the same listen only if the scrobble's artist is one
    of the live play's credits. The title alone is not enough: two artists' songs can share
    a title within minutes. A live row with no artist information at all is trusted on title."""
    keys = live.get("credit_keys")
    if not keys:
        return True
    return _loose(scrobble_artist_key) in keys or _loose(live.get("artist_key") or "") == _loose(scrobble_artist_key)


def local_parts(ts_ms):
    """(day 'YYYY-MM-DD', hour, dow 0=Mon, ym 'YYYY-MM', year) in the OS local time zone."""
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
    except (OverflowError, OSError, ValueError):
        dt = datetime.fromtimestamp(0)
    y, m = dt.year, dt.month
    return f"{y:04d}-{m:02d}-{dt.day:02d}", dt.hour, dt.weekday(), f"{y:04d}-{m:02d}", y


def local_day(ts_ms):
    return local_parts(ts_ms)[0]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

PLAYS_DDL = [
    """CREATE TABLE IF NOT EXISTS plays (
      id INTEGER PRIMARY KEY,
      event_id INTEGER NOT NULL UNIQUE,
      source TEXT NOT NULL,
      ts_ms INTEGER NOT NULL,
      day_local TEXT NOT NULL,
      hour_local INTEGER NOT NULL,
      dow INTEGER NOT NULL,
      ym TEXT NOT NULL,
      year INTEGER NOT NULL,
      artist_name TEXT NOT NULL, artist_key TEXT NOT NULL,
      track_name TEXT NOT NULL,  track_key TEXT NOT NULL,
      song_key TEXT NOT NULL,
      album_name TEXT, album_key TEXT,
      spotify_track_id TEXT,
      listened_s REAL,
      linked_live_event_id INTEGER,
      is_backlog INTEGER NOT NULL DEFAULT 0,
      ts_quality TEXT NOT NULL DEFAULT 'ok',
      superseded_by INTEGER,
      album_filled INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts_ms)",
    "CREATE INDEX IF NOT EXISTS plays_artist_ts ON plays(artist_key, ts_ms)",
    "CREATE INDEX IF NOT EXISTS plays_track_ts ON plays(track_key, ts_ms)",
    "CREATE INDEX IF NOT EXISTS plays_song_ts ON plays(song_key, ts_ms)",
    "CREATE INDEX IF NOT EXISTS plays_album_ts ON plays(album_key, ts_ms)",
    "CREATE INDEX IF NOT EXISTS plays_day ON plays(day_local)",
    # Extra (not in the shared contract): the merge/consistency checks look links up.
    "CREATE INDEX IF NOT EXISTS plays_linked ON plays(linked_live_event_id) "
    "WHERE linked_live_event_id IS NOT NULL",
    # Heard-flags for Spotify tracklists and top tracks look plays up by Spotify id.
    "CREATE INDEX IF NOT EXISTS plays_sptid ON plays(spotify_track_id)",
    """CREATE TABLE IF NOT EXISTS artist_stats (artist_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
      first_ts INTEGER, last_ts INTEGER, plays INTEGER NOT NULL DEFAULT 0, days_listened INTEGER,
      peak_year INTEGER, is_music INTEGER NOT NULL DEFAULT 1, spotify_artist_id TEXT,
      lfm_listeners INTEGER, lfm_fetched_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS track_stats (track_key TEXT PRIMARY KEY, song_key TEXT, artist_key TEXT,
      display_name TEXT NOT NULL, first_ts INTEGER, last_ts INTEGER, plays INTEGER NOT NULL DEFAULT 0,
      est_duration_s REAL, peak7 INTEGER, peak7_start INTEGER)""",
    """CREATE TABLE IF NOT EXISTS album_stats (album_key TEXT PRIMARY KEY, artist_key TEXT,
      display_name TEXT NOT NULL, first_ts INTEGER, last_ts INTEGER, plays INTEGER NOT NULL DEFAULT 0)""",
    # SONG_PLAYED events that are deliberately not plays (short unmatched live plays), so the
    # consistency check can tell "skipped on purpose" from "never ingested".
    "CREATE TABLE IF NOT EXISTS plays_skipped (event_id INTEGER PRIMARY KEY, reason TEXT NOT NULL)",
]

_PLAY_COLS = ("id", "event_id", "source", "ts_ms", "day_local", "hour_local", "dow", "ym", "year",
              "artist_name", "artist_key", "track_name", "track_key", "song_key", "album_name",
              "album_key", "spotify_track_id", "listened_s", "linked_live_event_id", "is_backlog",
              "ts_quality", "superseded_by", "album_filled")
_INSERT_PLAY = (f"INSERT INTO plays ({', '.join(_PLAY_COLS)}) "
                f"VALUES ({', '.join('?' * len(_PLAY_COLS))})")


# Columns added after the shared contract was published (older ledgers get them via ALTER).
_EXTRA_COLUMNS = {
    # 1 = album_name/album_key were borrowed from other plays of the same recording (the
    # scrobble itself had no album; mostly the 2021 offline import). Filter album_filled = 0
    # to see only albums the source reported.
    "album_filled": "INTEGER NOT NULL DEFAULT 0",
}


def ensure_schema(conn):
    """Create the ledger tables/indexes if missing (no commit, no data)."""
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for stmt in PLAYS_DDL:
        conn.execute(stmt)
    have = {r[1] for r in conn.execute("PRAGMA table_info(plays)").fetchall()}
    for col, decl in _EXTRA_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE plays ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------------------
# app_meta helpers
# ---------------------------------------------------------------------------

def _meta_get(conn, key):
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (key, str(value)))


def _meta_del(conn, *keys):
    for key in keys:
        conn.execute("DELETE FROM app_meta WHERE key = ?", (key,))


def _bump_watermark(conn, event_id):
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
        "CASE WHEN CAST(value AS INTEGER) < CAST(excluded.value AS INTEGER) THEN excluded.value ELSE value END",
        (META_WATERMARK, str(int(event_id))),
    )


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _log_error(exc, context):
    try:
        import app as core  # noqa: WPS433 - lazy: app imports this package
        core._log_app_error(exc, context)
    except Exception:
        log.exception("%s: %s", context, exc)


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def _is_app_db(conn):
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main" and row[2]:
                return (os.path.normcase(os.path.abspath(row[2]))
                        == os.path.normcase(os.path.abspath(str(_db.DB_PATH))))
    except Exception:
        pass
    return False


def is_ready(conn):
    """True when the ledger exists and was built with the current RULES_VERSION."""
    try:
        if not _table_exists(conn, "plays"):
            return False
        if not _table_exists(conn, "app_meta"):
            return True  # a standalone ledger (analysis copy / test fixture) has no build state
        return _meta_get(conn, META_RULES) == RULES_VERSION
    except sqlite3.Error:
        return False


def ensure_plays_ready(conn=None, start_build=True):
    """True when the plays ledger is built and current (reports may read it).

    When it is not, a background rebuild of the app database is started (non-blocking) and
    False is returned; callers should show a 'building your history' state meanwhile.
    """
    try:
        if conn is None:
            conn = _db.get_conn()
        ready = is_ready(conn)
        if not ready and start_build and _is_app_db(conn):
            start_background_rebuild()
        return ready
    except Exception:
        return False


def mark_ready(conn):
    """Declare a hand-built ledger current (test fixtures that insert plays rows directly)."""
    ensure_schema(conn)
    _meta_set(conn, META_RULES, RULES_VERSION)
    conn.commit()


# ---------------------------------------------------------------------------
# Event -> play
# ---------------------------------------------------------------------------

def _loads(payload):
    if isinstance(payload, dict):
        return payload
    if not payload:
        return {}
    try:
        v = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean(s):
    return s.strip() if isinstance(s, str) else ""


def _make_play(event_id, source, start_ms, artist, title, album, spotify_id=None,
               listened_s=None, is_backlog=0, end_ms=None):
    ak = fold(artist)
    rt = _recording_title(title)
    day, hour, dow, ym, year = local_parts(start_ms)
    album = _clean(album) or None
    alk = None
    if album:
        a = _album_title(album)
        alk = f"{ak}|{a}" if a else None
    return {
        "event_id": int(event_id), "source": source, "ts_ms": int(start_ms),
        "end_ms": int(end_ms if end_ms is not None else start_ms),
        "day_local": day, "hour_local": hour, "dow": dow, "ym": ym, "year": year,
        "artist_name": artist, "artist_key": ak, "track_name": title, "title_key": rt,
        "track_key": f"{ak}|{rt}", "song_key": f"{ak}|{_composition_title(title)}",
        "album_name": album, "album_key": alk, "spotify_track_id": spotify_id,
        "listened_s": listened_s, "linked_live_event_id": None,
        "is_backlog": 1 if is_backlog else 0, "ts_quality": "ok", "superseded_by": None,
        "album_filled": 0,
    }


def _lastfm_play(event_id, ts_ms, artist, title, album):
    artist, title = _clean(artist), _clean(title)
    if not artist and not title:
        return None
    return _make_play(event_id, "lastfm", ts_ms, artist, title, album)


def _provisional_play(event_id, ts_ms, source, track_id, payload, t_name, t_album, is_known):
    """A live (or Spotify recently-played) SONG_PLAYED event as a play dict, or None."""
    p = _loads(payload)
    names = p.get("artist_names")
    artist = ""
    if isinstance(names, list) and names and isinstance(names[0], str):
        artist = names[0].strip()
    if not artist:
        artist = primary_artist(_clean(p.get("artists")), is_known)
    title = _clean(p.get("track_name")) or _clean(t_name)
    if not artist and not title:
        return None
    album = _clean(p.get("album")) or _clean(t_album) or None
    listened = _num(p.get("listened_s"))
    end = int(ts_ms)
    started = _num(p.get("started_ms"))
    if started and 0 < started <= end:
        start = int(started)
    elif listened is not None and listened > 0:
        start = int(end - listened * 1000)
    else:
        start = end
    in_backlog = p.get("started_in_backlog")
    if in_backlog is None:
        in_backlog = p.get("was_in_playlist")
    play = _make_play(event_id, source or "live", start, artist, title, album,
                      spotify_id=track_id or None, listened_s=listened,
                      is_backlog=bool(in_backlog), end_ms=end)
    play["credit_keys"] = credit_keys(_clean(p.get("artists")) or artist, names)
    return play


def _play_row(play, row_id=None):
    return (row_id, play["event_id"], play["source"], play["ts_ms"], play["day_local"],
            play["hour_local"], play["dow"], play["ym"], play["year"], play["artist_name"],
            play["artist_key"], play["track_name"], play["track_key"], play["song_key"],
            play["album_name"], play["album_key"], play["spotify_track_id"], play["listened_s"],
            play["linked_live_event_id"], play["is_backlog"], play["ts_quality"],
            play["superseded_by"], play["album_filled"])


def _link_live_into(scrobble, live):
    """Copy what only the live row knows onto the canonical scrobble."""
    scrobble["linked_live_event_id"] = live["event_id"]
    if live.get("spotify_track_id"):
        scrobble["spotify_track_id"] = live["spotify_track_id"]
    if live["source"] == "live":
        scrobble["listened_s"] = live.get("listened_s")
        scrobble["is_backlog"] = live.get("is_backlog", 0)


def _in_window(scrobble_ts, live_start, live_end):
    return live_start - MATCH_BEFORE_MS <= scrobble_ts <= live_end + MATCH_AFTER_MS


# ---------------------------------------------------------------------------
# Full rebuild
# ---------------------------------------------------------------------------

_rebuild_lock = threading.Lock()
_bg_lock = threading.Lock()
_bg_thread = None
_last_rebuild = {}


def _flag_bulk_runs(rows):
    """Flag runs of >= BULK_MIN_RUN plays spaced < BULK_GAP_MS apart (in place)."""
    order = sorted(rows, key=lambda r: (r["ts_ms"], r["event_id"]))
    flagged = 0
    run = []
    prev_ts = None
    for r in order:
        if prev_ts is not None and r["ts_ms"] - prev_ts < BULK_GAP_MS:
            run.append(r)
        else:
            if len(run) >= BULK_MIN_RUN:
                for x in run:
                    x["ts_quality"] = "bulk"
                flagged += len(run)
            run = [r]
        prev_ts = r["ts_ms"]
    if len(run) >= BULK_MIN_RUN:
        for x in run:
            x["ts_quality"] = "bulk"
        flagged += len(run)
    return flagged


def _fill_missing_albums(rows):
    """Borrow the most common album of the same recording for plays without one."""
    by_track = defaultdict(Counter)
    names = defaultdict(Counter)
    for r in rows:
        if r["album_key"]:
            by_track[r["track_key"]][r["album_key"]] += 1
            names[r["album_key"]][r["album_name"]] += 1
    filled = 0
    for r in rows:
        if r["album_key"] is None:
            c = by_track.get(r["track_key"])
            if c:
                alk = c.most_common(1)[0][0]
                r["album_key"] = alk
                r["album_name"] = names[alk].most_common(1)[0][0]
                r["album_filled"] = 1
                filled += 1
    return filled


def _select_events_sql(where):
    return ("SELECT e.id, e.event_type, e.ts_ms, COALESCE(e.source, 'live'), e.track_id, e.payload, "
            "t.name, t.album FROM events e LEFT JOIN tracks t ON t.id = e.track_id "
            f"WHERE {where}")


def rebuild(conn=None, if_needed=False):
    """Rebuild the whole ledger from events (under 5 s for 50K events). Returns stats.

    Everything is computed from a consistent snapshot outside any write lock; the write
    transaction only swaps the rows, ingests events that arrived meanwhile and refreshes
    the rollups, so live ingest waits well under a second. With ``if_needed`` it returns
    None when the ledger is already current (another thread just built it). A failure is
    recorded so automatic retries back off (see start_background_rebuild / worker).
    """
    try:
        stats = _rebuild(conn, if_needed)
    except Exception as exc:
        _note_rebuild_failure(exc)
        raise
    if stats is not None:
        _note_rebuild_success()
    return stats


def _rebuild(conn=None, if_needed=False):
    conn = conn or _db.get_conn()
    with _rebuild_lock:
        if if_needed and is_ready(conn) and not _meta_get(conn, META_NEEDS_REBUILD):
            return None
        t0 = time.perf_counter()
        ensure_schema(conn)
        conn.commit()
        has_tracks = _table_exists(conn, "tracks")

        # 1) consistent snapshot (a WAL read transaction)
        conn.execute("BEGIN")
        try:
            max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            lfm = conn.execute(
                "SELECT id, ts_ms, json_extract(payload, '$.artists'), json_extract(payload, '$.track_name'), "
                "json_extract(payload, '$.album') FROM events "
                "WHERE source = 'lastfm' AND event_type = ? AND id <= ? ORDER BY id",
                (SONG_PLAYED, max_id),
            ).fetchall()
            if has_tracks:
                others = conn.execute(
                    _select_events_sql("e.event_type = ? AND COALESCE(e.source, 'live') <> 'lastfm' "
                                       "AND e.id <= ? ORDER BY e.id"),
                    (SONG_PLAYED, max_id),
                ).fetchall()
            else:
                others = [(r[0], r[1], r[2], r[3], r[4], r[5], None, None) for r in conn.execute(
                    "SELECT id, event_type, ts_ms, COALESCE(source, 'live'), track_id, payload FROM events "
                    "WHERE event_type = ? AND COALESCE(source, 'live') <> 'lastfm' AND id <= ? ORDER BY id",
                    (SONG_PLAYED, max_id),
                ).fetchall()]
            base_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM plays").fetchone()[0]
            durations = _track_durations(conn) if has_tracks else {}
            current_rollups = _read_rollups(conn)
        finally:
            conn.commit()

        # 2) compute
        rows, skipped = [], []
        by_title = defaultdict(list)
        known = set()
        for eid, ts, art, trk, alb in lfm:
            play = _lastfm_play(eid, ts, art, trk, alb)
            if play is None:
                skipped.append((eid, "empty"))
                continue
            known.add(play["artist_key"])
            rows.append(play)
            by_title[play["title_key"]].append(play)
        parsed = []
        for eid, _etype, ts, src, tid, payload, t_name, t_album in others:
            p = _loads(payload)
            names = p.get("artist_names")
            if isinstance(names, list):
                known.update(fold(n) for n in names if isinstance(n, str) and n.strip())
            parsed.append((eid, ts, src, tid, p, t_name, t_album))
        stats = {"lastfm": len(rows), "merged": 0, "provisional": 0, "dropped_short": 0}
        for eid, ts, src, tid, p, t_name, t_album in parsed:
            play = _provisional_play(eid, ts, src, tid, p, t_name, t_album, known.__contains__)
            if play is None:
                skipped.append((eid, "empty"))
                continue
            best = None
            for r in by_title.get(play["title_key"], ()):
                if (r["linked_live_event_id"] is None and _in_window(r["ts_ms"], play["ts_ms"], play["end_ms"])
                        and _artist_compatible(r["artist_key"], play)):
                    if best is None or abs(r["ts_ms"] - play["ts_ms"]) < abs(best["ts_ms"] - play["ts_ms"]):
                        best = r
            if best is not None:
                _link_live_into(best, play)
                stats["merged"] += 1
                continue
            if play["source"] == "live" and (play["listened_s"] or 0) < MIN_LIVE_S:
                skipped.append((eid, "short"))
                stats["dropped_short"] += 1
                continue
            rows.append(play)
            stats["provisional"] += 1
        stats["albums_filled"] = _fill_missing_albums(rows) if FILL_MISSING_ALBUMS else 0
        stats["bulk"] = _flag_bulk_runs(rows)
        # ids follow time order and always grow across rebuilds (MAX(id) works as a cache key)
        rows.sort(key=lambda r: (r["ts_ms"], r["event_id"]))
        insert_rows = [_play_row(r, base_id + i + 1) for i, r in enumerate(rows)]
        rollups = _compute_rollups([_rollup_input(r, base_id + i + 1) for i, r in enumerate(rows)], durations)
        t_compute = time.perf_counter()

        # 3) swap under the write lock; ingest what arrived after the snapshot
        conn.execute("PRAGMA cache_size=-131072")  # the index-heavy bulk insert is 2-3x faster in memory
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM plays")
                conn.execute("DELETE FROM plays_skipped")
                conn.executemany(_INSERT_PLAY, insert_rows)
                conn.executemany("INSERT OR REPLACE INTO plays_skipped (event_id, reason) VALUES (?, ?)", skipped)
                _apply_rollups(conn, rollups, current_rollups)
                late = conn.execute(_select_events_sql("e.event_type = ? AND e.id > ? ORDER BY e.id")
                                    if has_tracks else
                                    "SELECT id, event_type, ts_ms, COALESCE(source, 'live'), track_id, payload, "
                                    "NULL, NULL FROM events WHERE event_type = ? AND id > ? ORDER BY id",
                                    (SONG_PLAYED, max_id)).fetchall()
                watermark = max_id
                for ev in late:
                    _ingest_one(conn, ev)
                    watermark = max(watermark, ev[0])
                stats["late"] = len(late)
                _meta_set(conn, META_RULES, RULES_VERSION)
                _bump_watermark(conn, watermark)
                _meta_set(conn, META_BUILT_AT, int(time.time() * 1000))
                _meta_del(conn, META_DIRTY, META_NEEDS_REBUILD)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                _meta_set(conn, META_BUILD_MS, elapsed_ms)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.execute("PRAGMA cache_size=-2000")
        _checkpoint_quietly(conn)
        stats.update(rows=len(rows) + stats.get("late", 0), ms=elapsed_ms,
                     compute_ms=int((t_compute - t0) * 1000),
                     write_ms=int((time.perf_counter() - t_compute) * 1000))
        _last_rebuild.clear()
        _last_rebuild.update(stats, finished_at=int(time.time() * 1000))
        log.info("plays ledger rebuilt: %s", stats)
        return stats


CHECKPOINT_BUSY_MS = 250


def _checkpoint_quietly(conn):
    """Fold the rebuild's WAL (~25 MB) back into the database without stalling live writes.

    A TRUNCATE checkpoint holds the writer lock while it waits for readers, for up to the
    connection's busy timeout (10 s), and a poll-loop write waiting behind it could time out.
    So it may wait only briefly here; if readers are busy the WAL is folded in later by
    SQLite's automatic checkpoints (journal_size_limit then shrinks the file).
    """
    try:
        conn.execute(f"PRAGMA busy_timeout={CHECKPOINT_BUSY_MS}")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        finally:
            conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.Error:
        pass


# A rebuild that failed is not retried at once by every status/report request (each attempt
# costs seconds of CPU, ~25 MB of rolled-back WAL and the write lock): back off 5 min, doubling
# up to 1 h. Cleared by the next successful rebuild.
REBUILD_BACKOFF_S = 5 * 60
REBUILD_BACKOFF_MAX_S = 60 * 60
_failure = {"count": 0, "error": None, "at_mono": None, "retry_mono": None, "retry_at_ms": None}
_failure_lock = threading.Lock()


def _note_rebuild_failure(exc):
    with _failure_lock:
        _failure["count"] += 1
        delay = min(REBUILD_BACKOFF_MAX_S, REBUILD_BACKOFF_S * (2 ** (_failure["count"] - 1)))
        now = time.monotonic()
        _failure.update(error=str(exc) or exc.__class__.__name__, at_mono=now, retry_mono=now + delay,
                        retry_at_ms=int(time.time() * 1000 + delay * 1000))


def _note_rebuild_success():
    with _failure_lock:
        _failure.update(count=0, error=None, at_mono=None, retry_mono=None, retry_at_ms=None)


def rebuild_backoff_remaining_s():
    """Seconds until a failed rebuild may be retried automatically (0 = allowed now)."""
    with _failure_lock:
        retry = _failure["retry_mono"]
    return 0.0 if retry is None else max(0.0, retry - time.monotonic())


def rebuild_failure():
    """{error, retry_at_ms} of the last failed rebuild, or None."""
    with _failure_lock:
        if _failure["error"] is None:
            return None
        return {"error": _failure["error"], "retry_at_ms": _failure["retry_at_ms"], "failures": _failure["count"]}


def _bg_rebuild():
    try:
        rebuild(_db.get_conn(), if_needed=True)
    except Exception as exc:
        _log_error(exc, "plays rebuild")


def start_background_rebuild():
    """Start a rebuild of the app database in a daemon thread unless one is running (or a
    recent one failed and its backoff hasn't passed)."""
    global _bg_thread
    with _bg_lock:
        if _bg_thread is not None and _bg_thread.is_alive():
            return False
        if _rebuild_lock.locked():
            return False
        if rebuild_backoff_remaining_s() > 0:
            return False
        _bg_thread = threading.Thread(target=_bg_rebuild, name="plays-rebuild", daemon=True)
        _bg_thread.start()
        return True


def rebuilding():
    return _rebuild_lock.locked()


# ---------------------------------------------------------------------------
# Incremental ingest
# ---------------------------------------------------------------------------

def _event_tuple(conn, row):
    """Normalize a dict / sqlite3.Row / tuple event into the SELECT tuple layout."""
    if isinstance(row, (tuple, list)) and len(row) >= 8:
        return tuple(row[:8])
    get = row.get if isinstance(row, dict) else (lambda k, d=None: row[k] if k in row.keys() else d)
    track_id = get("track_id")
    t_name = get("t_name")
    t_album = get("t_album")
    payload = get("payload")
    source = get("source") or "live"
    if track_id and t_name is None and source != "lastfm":
        p = _loads(payload)
        if not p.get("track_name") or not p.get("album"):
            try:
                tr = conn.execute("SELECT name, album FROM tracks WHERE id = ?", (track_id,)).fetchone()
            except sqlite3.OperationalError:
                tr = None
            if tr:
                t_name, t_album = tr[0], tr[1]
    return (int(get("id")), get("event_type") or SONG_PLAYED, int(get("ts_ms")), source,
            track_id, payload, t_name, t_album)


def _known_artist_fn(conn):
    def known(key):
        return conn.execute("SELECT 1 FROM plays WHERE artist_key = ? LIMIT 1", (key,)).fetchone() is not None
    return known


def _already_ingested(conn, eid):
    return conn.execute(
        "SELECT 1 FROM plays WHERE event_id = ? UNION ALL "
        "SELECT 1 FROM plays WHERE linked_live_event_id = ? LIMIT 1", (eid, eid)
    ).fetchone() is not None


def _fill_album_from_history(conn, play):
    row = conn.execute(
        "SELECT album_key, album_name, COUNT(*) n FROM plays WHERE track_key = ? AND album_key IS NOT NULL "
        "AND superseded_by IS NULL GROUP BY album_key ORDER BY n DESC LIMIT 1",
        (play["track_key"],),
    ).fetchone()
    if row:
        play["album_key"], play["album_name"] = row[0], row[1]
        play["album_filled"] = 1


def _find_scrobble_for(conn, live):
    """Closest unlinked canonical scrobble of the same recording around a live play."""
    rows = conn.execute(
        "SELECT id, ts_ms, track_name, artist_key FROM plays WHERE source = 'lastfm' AND superseded_by IS NULL "
        "AND linked_live_event_id IS NULL AND ts_ms BETWEEN ? AND ?",
        (live["ts_ms"] - MATCH_BEFORE_MS, live["end_ms"] + MATCH_AFTER_MS),
    ).fetchall()
    best = None
    for pid, ts, name, ak in rows:
        if _recording_title(name) == live["title_key"] and _artist_compatible(ak, live):
            if best is None or abs(ts - live["ts_ms"]) < abs(best[1] - live["ts_ms"]):
                best = (pid, ts)
    return best


def _find_live_for(conn, scrobble):
    """Closest unlinked live / recently-played event of the same recording around a scrobble."""
    s = scrobble["ts_ms"]
    cands = conn.execute(
        _select_events_sql("e.event_type = ? AND e.source IN ('live', 'spotify_recent') "
                           "AND e.ts_ms BETWEEN ? AND ?"),
        (SONG_PLAYED, s - MATCH_AFTER_MS, s + MATCH_BEFORE_MS + MAX_LISTEN_MS),
    ).fetchall()
    best = None
    known = _known_artist_fn(conn)
    for eid, _et, ts, src, tid, payload, t_name, t_album in cands:
        live = _provisional_play(eid, ts, src, tid, payload, t_name, t_album, known)
        if live is None or live["title_key"] != scrobble["title_key"]:
            continue
        if not _artist_compatible(scrobble["artist_key"], live):
            continue
        if not _in_window(s, live["ts_ms"], live["end_ms"]):
            continue
        row = conn.execute("SELECT id, superseded_by FROM plays WHERE event_id = ?", (eid,)).fetchone()
        if row is not None and row[1] is not None:
            continue  # already replaced by another scrobble
        if conn.execute("SELECT 1 FROM plays WHERE linked_live_event_id = ?", (eid,)).fetchone():
            continue
        if best is None or abs(live["ts_ms"] - s) < abs(best[0]["ts_ms"] - s):
            best = (live, row[0] if row else None)
    return best


def _rollup_add(conn, play):
    ak, ts = play["artist_key"], play["ts_ms"]
    new_day = 0
    if play["ts_quality"] == "ok":
        other = conn.execute(
            "SELECT 1 FROM plays WHERE artist_key = ? AND ts_ms BETWEEN ? AND ? AND day_local = ? "
            "AND superseded_by IS NULL AND ts_quality = 'ok' AND event_id <> ? LIMIT 1",
            (ak, ts - 86_400_000, ts + 86_400_000, play["day_local"], play["event_id"]),
        ).fetchone()
        new_day = 0 if other else 1
    conn.execute(
        "INSERT INTO artist_stats (artist_key, display_name, first_ts, last_ts, plays, days_listened, peak_year) "
        "VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT(artist_key) DO UPDATE SET "
        "display_name = CASE WHEN plays = 0 THEN excluded.display_name ELSE display_name END, "
        "first_ts = MIN(COALESCE(first_ts, excluded.first_ts), excluded.first_ts), "
        "last_ts = MAX(COALESCE(last_ts, excluded.last_ts), excluded.last_ts), "
        "days_listened = COALESCE(days_listened, 0) + excluded.days_listened, "
        "peak_year = COALESCE(peak_year, excluded.peak_year), plays = plays + 1",
        (ak, play["artist_name"] or ak, ts, ts, new_day, play["year"]),
    )
    conn.execute(
        "INSERT INTO track_stats (track_key, song_key, artist_key, display_name, first_ts, last_ts, plays) "
        "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(track_key) DO UPDATE SET "
        "display_name = CASE WHEN plays = 0 THEN excluded.display_name ELSE display_name END, "
        "first_ts = MIN(COALESCE(first_ts, excluded.first_ts), excluded.first_ts), "
        "last_ts = MAX(COALESCE(last_ts, excluded.last_ts), excluded.last_ts), plays = plays + 1",
        (play["track_key"], play["song_key"], ak, play["track_name"] or play["track_key"], ts, ts),
    )
    if play["album_key"]:
        conn.execute(
            "INSERT INTO album_stats (album_key, artist_key, display_name, first_ts, last_ts, plays) "
            "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(album_key) DO UPDATE SET "
            "display_name = CASE WHEN plays = 0 THEN excluded.display_name ELSE display_name END, "
            "first_ts = MIN(COALESCE(first_ts, excluded.first_ts), excluded.first_ts), "
            "last_ts = MAX(COALESCE(last_ts, excluded.last_ts), excluded.last_ts), plays = plays + 1",
            (play["album_key"], ak, play["album_name"] or play["album_key"], ts, ts),
        )


def _rollup_remove(conn, artist_key_, track_key_, album_key_):
    conn.execute("UPDATE artist_stats SET plays = MAX(plays - 1, 0) WHERE artist_key = ?", (artist_key_,))
    conn.execute("UPDATE track_stats SET plays = MAX(plays - 1, 0) WHERE track_key = ?", (track_key_,))
    if album_key_:
        conn.execute("UPDATE album_stats SET plays = MAX(plays - 1, 0) WHERE album_key = ?", (album_key_,))


def _flag_bulk_around(conn, play_id, ts):
    """Flag the run around a newly inserted play when it reaches BULK_MIN_RUN."""
    lo, hi = ts - 6 * BULK_GAP_MS, ts + 6 * BULK_GAP_MS
    for _ in range(12):  # widen while the run touches the window edge (long offline batches)
        rows = conn.execute(
            "SELECT id, ts_ms, ts_quality FROM plays WHERE superseded_by IS NULL AND ts_ms BETWEEN ? AND ? "
            "ORDER BY ts_ms, event_id", (lo, hi),
        ).fetchall()
        idx = next((i for i, r in enumerate(rows) if r[0] == play_id), None)
        if idx is None:
            return 0
        a = idx
        while a > 0 and rows[a][1] - rows[a - 1][1] < BULK_GAP_MS:
            a -= 1
        b = idx
        while b < len(rows) - 1 and rows[b + 1][1] - rows[b][1] < BULK_GAP_MS:
            b += 1
        grow_lo = rows[a][1] - lo < BULK_GAP_MS
        grow_hi = hi - rows[b][1] < BULK_GAP_MS
        if not (grow_lo or grow_hi):
            break
        lo = lo - 10 * 60 * 1000 if grow_lo else lo
        hi = hi + 10 * 60 * 1000 if grow_hi else hi
    run = rows[a:b + 1]
    if len(run) < BULK_MIN_RUN:
        return 0
    ids = [r[0] for r in run if r[2] != "bulk"]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        conn.execute(f"UPDATE plays SET ts_quality = 'bulk' WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return len(ids)


def _insert_play(conn, play):
    cur = conn.execute(_INSERT_PLAY, _play_row(play))
    play_id = cur.lastrowid
    if _flag_bulk_around(conn, play_id, play["ts_ms"]):
        play["ts_quality"] = conn.execute("SELECT ts_quality FROM plays WHERE id = ?", (play_id,)).fetchone()[0]
    _rollup_add(conn, play)
    return play_id


def _ingest_one(conn, ev):
    """Apply one SONG_PLAYED event to the ledger (caller owns the transaction)."""
    eid, etype, ts, src, tid, payload, t_name, t_album = ev
    if etype != SONG_PLAYED:
        return "ignored"
    if _already_ingested(conn, eid):
        return "exists"
    if src == "lastfm":
        p = _loads(payload)
        play = _lastfm_play(eid, ts, p.get("artists"), p.get("track_name"), p.get("album"))
        if play is None:
            conn.execute("INSERT OR REPLACE INTO plays_skipped (event_id, reason) VALUES (?, 'empty')", (eid,))
            return "skipped"
        found = _find_live_for(conn, play)
        if found is not None:
            live, live_row_id = found
            _link_live_into(play, live)
            if live_row_id is not None:
                old = conn.execute("SELECT artist_key, track_key, album_key FROM plays WHERE id = ?",
                                   (live_row_id,)).fetchone()
                conn.execute("UPDATE plays SET superseded_by = ? WHERE id = ?", (eid, live_row_id))
                if old:
                    _rollup_remove(conn, old[0], old[1], old[2])
            conn.execute("DELETE FROM plays_skipped WHERE event_id = ?", (live["event_id"],))
        if play["album_key"] is None and FILL_MISSING_ALBUMS:
            _fill_album_from_history(conn, play)
        _insert_play(conn, play)
        outcome = "inserted_linked" if found is not None else "inserted"
    else:
        play = _provisional_play(eid, ts, src, tid, payload, t_name, t_album, _known_artist_fn(conn))
        if play is None:
            conn.execute("INSERT OR REPLACE INTO plays_skipped (event_id, reason) VALUES (?, 'empty')", (eid,))
            return "skipped"
        best = _find_scrobble_for(conn, play)
        if best is not None:
            if play["source"] == "live":
                conn.execute(
                    "UPDATE plays SET spotify_track_id = COALESCE(?, spotify_track_id), listened_s = ?, "
                    "is_backlog = ?, linked_live_event_id = ? WHERE id = ?",
                    (play["spotify_track_id"], play["listened_s"], play["is_backlog"], eid, best[0]),
                )
            else:
                conn.execute(
                    "UPDATE plays SET spotify_track_id = COALESCE(spotify_track_id, ?), "
                    "linked_live_event_id = ? WHERE id = ?", (play["spotify_track_id"], eid, best[0]),
                )
            outcome = "linked"
        elif play["source"] == "live" and (play["listened_s"] or 0) < MIN_LIVE_S:
            conn.execute("INSERT OR REPLACE INTO plays_skipped (event_id, reason) VALUES (?, 'short')", (eid,))
            return "skipped"
        else:
            if play["album_key"] is None and FILL_MISSING_ALBUMS:
                _fill_album_from_history(conn, play)
            _insert_play(conn, play)
            outcome = "inserted"
    _meta_set(conn, META_DIRTY, f"{eid}:{time.time_ns()}")
    return outcome


def ingest_event(conn, row, commit=True):
    """Apply one new SONG_PLAYED event to the ledger (a few ms). Never raises.

    ``row`` is the events row as a dict/sqlite3.Row (id, event_type, ts_ms, source, track_id,
    payload) or the SELECT tuple layout. With ``commit=False`` the caller owns the enclosing
    transaction (importers ingest a whole page at once). Returns the outcome string, or
    'deferred' while the ledger is not built (the rebuild picks the event up).
    """
    try:
        ev = _event_tuple(conn, row)
    except Exception as exc:
        _log_error(exc, "plays ingest (bad row)")
        return "error"
    if ev[1] != SONG_PLAYED:
        return "ignored"
    if not is_ready(conn):
        return "deferred"
    own_txn = commit and not conn.in_transaction
    try:
        if own_txn:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT plays_ingest")
        try:
            outcome = _ingest_one(conn, ev)
            _bump_watermark(conn, ev[0])
            conn.execute("RELEASE SAVEPOINT plays_ingest")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT plays_ingest")
            conn.execute("RELEASE SAVEPOINT plays_ingest")
            raise
        if commit:
            conn.commit()
        return outcome
    except Exception as exc:
        _log_error(exc, f"plays ingest event {ev[0]}")
        try:
            _meta_set(conn, META_NEEDS_REBUILD, "1")
            if commit:
                conn.commit()
        except Exception:
            pass
        return "error"


def catch_up(conn=None, full_check=False):
    """Ingest SONG_PLAYED events past the watermark (and, with full_check, any gap)."""
    conn = conn or _db.get_conn()
    if not is_ready(conn):
        return 0
    wm = int(_meta_get(conn, META_WATERMARK) or 0)
    has_tracks = _table_exists(conn, "tracks")
    base = (_select_events_sql("{w} ORDER BY e.id") if has_tracks else
            "SELECT e.id, e.event_type, e.ts_ms, COALESCE(e.source, 'live'), e.track_id, e.payload, NULL, NULL "
            "FROM events e WHERE {w} ORDER BY e.id")
    evs = conn.execute(base.format(w="e.event_type = ? AND e.id > ?"), (SONG_PLAYED, wm)).fetchall()
    if full_check:
        missing = conn.execute(base.format(
            w="e.event_type = ? AND e.id <= ? "
              "AND NOT EXISTS (SELECT 1 FROM plays p WHERE p.event_id = e.id) "
              "AND NOT EXISTS (SELECT 1 FROM plays p WHERE p.linked_live_event_id = e.id) "
              "AND NOT EXISTS (SELECT 1 FROM plays_skipped s WHERE s.event_id = e.id)"),
            (SONG_PLAYED, wm)).fetchall()
        evs = list(missing) + list(evs)
    if not evs:
        return 0
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = 0
        for ev in evs:
            if _ingest_one(conn, ev) not in ("exists", "ignored"):
                n += 1
            _bump_watermark(conn, ev[0])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

_ROLLUP_INPUT_SQL = (
    "SELECT id, ts_ms, day_local, year, artist_name, artist_key, track_name, track_key, song_key, "
    "album_name, album_key, spotify_track_id, ts_quality FROM plays WHERE superseded_by IS NULL "
    "ORDER BY ts_ms, id"
)


def _rollup_input(play, row_id):
    return (row_id, play["ts_ms"], play["day_local"], play["year"], play["artist_name"], play["artist_key"],
            play["track_name"], play["track_key"], play["song_key"], play["album_name"], play["album_key"],
            play["spotify_track_id"], play["ts_quality"])


def _track_durations(conn):
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, duration_ms FROM tracks WHERE duration_ms > 0")}
    except sqlite3.OperationalError:
        return {}


def _read_rollups(conn):
    """Current rollup rows keyed by their primary key (to write only what changed)."""
    return (
        {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT artist_key, display_name, first_ts, last_ts, plays, days_listened, peak_year FROM artist_stats")},
        {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT track_key, song_key, artist_key, display_name, first_ts, last_ts, plays, est_duration_s, "
            "peak7, peak7_start FROM track_stats")},
        {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT album_key, artist_key, display_name, first_ts, last_ts, plays FROM album_stats")},
    )


def _pick_name(variants):
    """Most frequent spelling; ties go to the most recently used one."""
    return max(variants.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]


def _compute_rollups(rows, durations):
    """Rollups from canonical plays sorted by (ts_ms, id); pure Python, no locks held.

    rows: (id, ts_ms, day_local, year, artist_name, artist_key, track_name, track_key,
    song_key, album_name, album_key, spotify_track_id, ts_quality).
    """
    art, trk, alb = {}, {}, {}
    gaps = defaultdict(lambda: [0, 0])   # track_key -> [sum_ms, n] of plausible gaps to the next play
    spotify_dur = defaultdict(list)      # track_key -> Spotify durations (ms) of its linked ids
    ok_ts = defaultdict(list)            # track_key -> real (non-bulk) timestamps, ascending
    prev = None
    for r in rows:
        _id, ts, day, year, aname, ak, tname, tk, sk, alname, alk, spid, quality = r
        a = art.get(ak)
        if a is None:
            a = art[ak] = [0, ts, ts, set(), {}, Counter()]
        a[0] += 1
        a[1] = min(a[1], ts)
        a[2] = max(a[2], ts)
        if quality == "ok":
            a[3].add(day)
        v = a[4].setdefault(aname, [0, 0])
        v[0] += 1
        v[1] = max(v[1], ts)
        a[5][year] += 1

        t = trk.get(tk)
        if t is None:
            t = trk[tk] = [0, ts, ts, sk, ak, {}]
        t[0] += 1
        t[1] = min(t[1], ts)
        t[2] = max(t[2], ts)
        v = t[5].setdefault(tname, [0, 0])
        v[0] += 1
        v[1] = max(v[1], ts)
        if quality == "ok":
            ok_ts[tk].append(ts)
        if spid and durations.get(spid):
            spotify_dur[tk].append(durations[spid])

        if alk:
            b = alb.get(alk)
            if b is None:
                b = alb[alk] = [0, ts, ts, ak, {}]
            b[0] += 1
            b[1] = min(b[1], ts)
            b[2] = max(b[2], ts)
            v = b[4].setdefault(alname, [0, 0])
            v[0] += 1
            v[1] = max(v[1], ts)

        if prev is not None:
            gap = ts - prev[1]
            if 30_000 <= gap <= 720_000:  # looks like one song played to its end
                g = gaps[prev[7]]
                g[0] += gap
                g[1] += 1
        prev = r

    artists = {}
    for ak, (n, first, last, days, names, years) in art.items():
        peak_year = max(years.items(), key=lambda kv: (kv[1], kv[0]))[0]
        artists[ak] = (_pick_name(names), first, last, n, len(days), peak_year)
    tracks = {}
    for tk, (n, first, last, sk, ak, names) in trk.items():
        est = None
        if spotify_dur.get(tk):
            est = sum(spotify_dur[tk]) / len(spotify_dur[tk]) / 1000.0
        elif gaps.get(tk) and gaps[tk][1]:
            est = gaps[tk][0] / gaps[tk][1] / 1000.0
        if est is not None:
            est = round(est, 1)
        peak7 = peak7_start = None
        if n >= 10 and ok_ts.get(tk):
            tss = ok_ts[tk]
            j = 0
            for i in range(len(tss)):
                if j < i:
                    j = i
                while j + 1 < len(tss) and tss[j + 1] - tss[i] < 604_800_000:
                    j += 1
                if peak7 is None or j - i + 1 > peak7:
                    peak7, peak7_start = j - i + 1, tss[i]
        tracks[tk] = (sk, ak, _pick_name(names), first, last, n, est, peak7, peak7_start)
    albums = {}
    for alk, (n, first, last, ak, names) in alb.items():
        albums[alk] = (ak, _pick_name(names), first, last, n)
    return artists, tracks, albums


def _same(a, b):
    if a is None or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, float) or isinstance(y, float):
            if x is None or y is None or abs(x - y) > 0.05:
                return False
        elif x != y:
            return False
    return True


def _apply_rollups(conn, new, current):
    """Write changed rollup rows; zero rows whose key has no canonical plays any more.

    Upserts only touch computed columns, so is_music (hidden artists), spotify_artist_id
    and the Last.fm columns survive every refresh and rebuild.
    """
    (na, nt, nb), (ca, ct, cb) = new, current
    conn.executemany(
        "INSERT INTO artist_stats (artist_key, display_name, first_ts, last_ts, plays, days_listened, peak_year) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(artist_key) DO UPDATE SET display_name = excluded.display_name, "
        "first_ts = excluded.first_ts, last_ts = excluded.last_ts, plays = excluded.plays, "
        "days_listened = excluded.days_listened, peak_year = excluded.peak_year",
        [(k,) + v for k, v in na.items() if not _same(ca.get(k), v)],
    )
    conn.executemany(
        "UPDATE artist_stats SET plays = 0, first_ts = NULL, last_ts = NULL, days_listened = 0, peak_year = NULL "
        "WHERE artist_key = ?",
        [(k,) for k, v in ca.items() if k not in na and (v[3] or v[1] is not None)],
    )
    conn.executemany(
        "INSERT INTO track_stats (track_key, song_key, artist_key, display_name, first_ts, last_ts, plays, "
        "est_duration_s, peak7, peak7_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(track_key) DO UPDATE "
        "SET song_key = excluded.song_key, artist_key = excluded.artist_key, display_name = excluded.display_name, "
        "first_ts = excluded.first_ts, last_ts = excluded.last_ts, plays = excluded.plays, "
        "est_duration_s = excluded.est_duration_s, peak7 = excluded.peak7, peak7_start = excluded.peak7_start",
        [(k,) + v for k, v in nt.items() if not _same(ct.get(k), v)],
    )
    conn.executemany(
        "UPDATE track_stats SET plays = 0, first_ts = NULL, last_ts = NULL, peak7 = NULL, peak7_start = NULL "
        "WHERE track_key = ?",
        [(k,) for k, v in ct.items() if k not in nt and (v[5] or v[3] is not None)],
    )
    conn.executemany(
        "INSERT INTO album_stats (album_key, artist_key, display_name, first_ts, last_ts, plays) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(album_key) DO UPDATE SET artist_key = excluded.artist_key, "
        "display_name = excluded.display_name, first_ts = excluded.first_ts, last_ts = excluded.last_ts, "
        "plays = excluded.plays",
        [(k,) + v for k, v in nb.items() if not _same(cb.get(k), v)],
    )
    conn.executemany(
        "UPDATE album_stats SET plays = 0, first_ts = NULL, last_ts = NULL WHERE album_key = ?",
        [(k,) for k, v in cb.items() if k not in nb and (v[4] or v[2] is not None)],
    )


def refresh_rollups(conn=None):
    """Recompute artist/track/album rollups from plays; writes only rows that changed.

    Reads a snapshot and computes outside the write lock. If plays changed meanwhile the
    dirty flag stays set, so the next refresh picks the difference up.
    """
    conn = conn or _db.get_conn()
    conn.commit()
    conn.execute("BEGIN")
    try:
        token = _meta_get(conn, META_DIRTY)
        rows = conn.execute(_ROLLUP_INPUT_SQL).fetchall()
        durations = _track_durations(conn)
        current = _read_rollups(conn)
    finally:
        conn.commit()
    new = _compute_rollups(rows, durations)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _apply_rollups(conn, new, current)
        if _meta_get(conn, META_DIRTY) == token:
            _meta_del(conn, META_DIRTY)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Status + background worker
# ---------------------------------------------------------------------------

def status(conn=None):
    """Cheap ledger facts for /api/v2/data/health."""
    conn = conn or _db.get_conn()
    out = {"plays_ready": False, "plays_total": 0, "plays_rules_version": None,
           "rules_version_expected": RULES_VERSION, "rebuilding": rebuilding(),
           "newest_play_ts": None, "newest_lastfm_ts": None, "live_since_ts": None,
           "built_at": None, "build_ms": None, "rebuild_error": None, "rebuild_retry_at": None}
    failure = rebuild_failure()
    if failure:
        out["rebuild_error"] = failure["error"]
        out["rebuild_retry_at"] = failure["retry_at_ms"]
    try:
        out["plays_ready"] = is_ready(conn)
        out["plays_rules_version"] = _meta_get(conn, META_RULES)
        out["built_at"] = _int_or_none(_meta_get(conn, META_BUILT_AT))
        out["build_ms"] = _int_or_none(_meta_get(conn, META_BUILD_MS))
        if _table_exists(conn, "plays"):
            out["plays_total"] = conn.execute(
                "SELECT COUNT(*) FROM plays WHERE superseded_by IS NULL").fetchone()[0]
            out["newest_play_ts"] = conn.execute(
                "SELECT MAX(ts_ms) FROM plays WHERE superseded_by IS NULL").fetchone()[0]
        out["newest_lastfm_ts"] = conn.execute(
            "SELECT MAX(ts_ms) FROM events WHERE source = 'lastfm'").fetchone()[0]
        out["live_since_ts"] = conn.execute(
            "SELECT MIN(ts_ms) FROM events WHERE source = 'live' AND event_type = ?", (SONG_PLAYED,)
        ).fetchone()[0]
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    return out


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_worker_wake = threading.Event()
WORKER_INTERVAL_S = 60
FULL_CHECK_EVERY_S = 15 * 60
ROLLUP_REFRESH_EVERY_S = 5 * 60


def wake_worker():
    _worker_wake.set()


def worker():
    """Background worker: build when needed, catch up, refresh stale rollups. Never dies."""
    last_full_check = 0.0
    last_refresh = 0.0
    time.sleep(0.5)
    while True:
        try:
            conn = _db.get_conn()
            now = time.monotonic()
            if (not is_ready(conn)) or _meta_get(conn, META_NEEDS_REBUILD):
                if rebuild_backoff_remaining_s() <= 0:
                    rebuild(conn, if_needed=True)
                    last_full_check = last_refresh = time.monotonic()
            else:
                full = (now - last_full_check) >= FULL_CHECK_EVERY_S
                catch_up(conn, full_check=full)
                if full:
                    last_full_check = now
                if _meta_get(conn, META_DIRTY) and (now - last_refresh) >= ROLLUP_REFRESH_EVERY_S:
                    refresh_rollups(conn)
                    last_refresh = time.monotonic()
        except Exception as exc:
            _log_error(exc, "plays worker")
        _worker_wake.wait(WORKER_INTERVAL_S)
        _worker_wake.clear()
