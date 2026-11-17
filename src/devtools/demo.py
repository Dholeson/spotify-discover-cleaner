"""Offline demo mode: ``python app.py --demo`` (see README "Try it offline").

The demo runs the real app (poll loop, clearing engine, reports, UI) against FakeSpotify, a
simulated Spotify that "plays" through a backlog playlist, with the author's listening history
from ``demo/snapshot/`` (built by ``dev/make_snapshot.py``). Everything lives in its own data
folder (``.demo-data/`` by default), so a demo never reads or writes a real install's files.

- ``prepare(data_dir, reset)`` runs at import time, before app.py reads .config.json: it blocks
  every request to *.spotify.com for this process and seeds ``data_dir`` from the snapshot when
  it is empty (or when ``reset`` is set).
- ``install(core)`` runs once app.py is fully imported: it routes ``get_sp()`` to FakeSpotify
  (through the real GuardedSpotify) and reports a permanent, simulated sign-in.

Seeding moves every date forward by whole weeks so the snapshot's last day lands in the
current week (weekdays and hours stay as they were): reports for "this week" and "this month"
then show the history instead of an empty period.
"""

import gzip
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent
SNAPSHOT_DIR = APP_DIR / "demo" / "snapshot"
BANNER = "Offline demo · simulated Spotify"
SEED_MARKER = ".demo-seed.json"
DAY_MS = 86400 * 1000

# Files a demo data folder may hold; --reset removes exactly these (and nothing else).
_DATA_FILES = ("music_explorer.db", "music_explorer.db-wal", "music_explorer.db-shm", "music_explorer.db-journal",
               ".config.json", ".config.json.bak", ".config.json.tmp", ".spotify_cache", SEED_MARKER)

# Timestamp columns moved by the date shift: (table, column, unit) with unit "ms" or "s".
_SHIFT_COLUMNS = {
    "events": [("ts_ms", "ms")],
    "tracks": [("created_at", "ms")],
    "interests": [("created_at", "ms"), ("updated_at", "ms")],
    "artist_likes": [("liked_at", "ms")],
    "listen_sessions": [("started_at", "ms"), ("ended_at", "ms")],
    "track_listens": [("ts_ms", "ms")],
}
# Local-date columns recomputed from the (shifted) timestamp next to them.
_DAY_FROM = {"events": "ts_ms", "track_listens": "ts_ms", "listen_sessions": "started_at"}
_SHIFT_META_MS = ("lastfm_backfill_done",)
# Load order (parents first) for the rows in the snapshot.
TABLES = ("tracks", "track_artists", "events", "interests", "interest_reasons", "artist_likes",
          "listen_sessions", "track_listens", "app_meta")

_network_blocked = False


class DemoSetupError(RuntimeError):
    """The demo data folder could not be prepared (message is shown to the user)."""


# ---------------------------------------------------------------------------- network
def block_spotify_network():
    """Refuse every HTTP request to *.spotify.com from this process (requests and spotipy)."""
    global _network_blocked
    if _network_blocked:
        return
    import requests

    original = requests.Session.send

    def _send(self, req, **kw):
        host = (requests.utils.urlparse(req.url).hostname or "").lower()
        if host == "spotify.com" or host.endswith(".spotify.com"):
            raise requests.exceptions.ConnectionError(f"offline demo: network to {host} is blocked")
        return original(self, req, **kw)

    requests.Session.send = _send
    _network_blocked = True


# ---------------------------------------------------------------------------- snapshot IO
def read_meta(snapshot_dir=SNAPSHOT_DIR):
    path = Path(snapshot_dir) / "meta.json"
    if not path.exists():
        raise DemoSetupError(f"The demo snapshot is missing ({path.name} not found in demo/snapshot/).")
    return json.loads(path.read_text(encoding="utf-8"))


def iter_table(snapshot_dir, table):
    """(columns, row iterator) of demo/snapshot/<table>.jsonl.gz: a header line, then arrays."""
    path = Path(snapshot_dir) / f"{table}.jsonl.gz"
    if not path.exists():
        return None, iter(())
    fh = gzip.open(path, "rt", encoding="utf-8")
    header = json.loads(fh.readline())

    def rows():
        with fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    return header["columns"], rows()


def read_config(snapshot_dir=SNAPSHOT_DIR):
    path = Path(snapshot_dir) / "config.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------- date shift
def shift_days_for(frozen_on, today=None):
    """Whole weeks between the snapshot's last day and today (never negative)."""
    today = today or date.today()
    frozen = date.fromisoformat(str(frozen_on))
    return max(0, (today - frozen).days // 7 * 7)


def _local_day(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _shift_day_str(value, days):
    try:
        return date.fromordinal(date.fromisoformat(str(value)).toordinal() + days).isoformat()
    except (TypeError, ValueError):
        return value


def _shift_row(table, cols, row, shift_ms):
    if not shift_ms or table not in _SHIFT_COLUMNS:
        return row
    row = list(row)
    pos = {c: i for i, c in enumerate(cols)}
    for col, unit in _SHIFT_COLUMNS[table]:
        i = pos.get(col)
        if i is not None and isinstance(row[i], (int, float)) and row[i]:
            row[i] = row[i] + (shift_ms if unit == "ms" else shift_ms // 1000)
    day_src = _DAY_FROM.get(table)
    if day_src and "day" in pos and isinstance(row[pos[day_src]], (int, float)) and row[pos[day_src]]:
        row[pos["day"]] = _local_day(row[pos[day_src]])
    if table == "events" and "payload" in pos and row[pos["payload"]] and "started_ms" in row[pos["payload"]]:
        try:
            p = json.loads(row[pos["payload"]])
            if isinstance(p.get("started_ms"), (int, float)):
                p["started_ms"] = int(p["started_ms"] + shift_ms)
                row[pos["payload"]] = json.dumps(p, ensure_ascii=False)
        except ValueError:
            pass
    return row


def shift_config(cfg, days):
    """Move the dated entries of a snapshot .config.json forward by `days`."""
    shift_ms = days * DAY_MS
    if not days:
        return cfg
    out = dict(cfg)
    hist = []
    for h in cfg.get("backlog_history") or []:
        if isinstance(h, dict):
            h = dict(h)
            if isinstance(h.get("ts_ms"), (int, float)):
                h["ts_ms"] = int(h["ts_ms"] + shift_ms)
            if h.get("day"):
                h["day"] = _shift_day_str(h["day"], days)
            hist.append(h)
    out["backlog_history"] = hist
    if isinstance(cfg.get("stats"), dict) and cfg["stats"].get("day"):
        out["stats"] = dict(cfg["stats"], day=_shift_day_str(cfg["stats"]["day"], days))
    return out


# ---------------------------------------------------------------------------- seeding
def _same_dir(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def reset_data(data_dir):
    """Remove the demo's data files from data_dir (only the files a demo writes)."""
    data_dir = Path(data_dir)
    if _same_dir(data_dir, APP_DIR):
        raise DemoSetupError("Refusing to reset: the demo data folder is the app folder.")
    for name in _DATA_FILES:
        p = data_dir / name
        try:
            p.unlink(missing_ok=True)
        except PermissionError as exc:
            raise DemoSetupError(f"Couldn't remove {name} ({exc}). Is another demo still running?") from exc
    for p in data_dir.glob("music_explorer.db.bak-v*"):
        p.unlink(missing_ok=True)


def seed(data_dir, snapshot_dir=SNAPSHOT_DIR, today=None):
    """Build data_dir/music_explorer.db and .config.json from the snapshot. Returns a summary."""
    from src.persistence import db as _db

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    meta = read_meta(snapshot_dir)
    days = shift_days_for(meta["frozen_on"], today)
    shift_ms = days * DAY_MS

    final = data_dir / "music_explorer.db"
    tmp = data_dir / "music_explorer.db.seed-tmp"
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    counts = {}
    saved_status = dict(_db.MIGRATION_STATUS)
    try:
        conn.executescript(_db._SCHEMA)
        _db._upgrade_schema(conn)
        conn.commit()
        say, _db._say = _db._say, (lambda line: None)  # an empty schema's upgrade notes are noise here
        try:
            _db.run_migrations(conn, backup=False)
        finally:
            _db._say = say
        for table in TABLES:
            cols, rows = iter_table(snapshot_dir, table)
            if not cols:
                continue
            known = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            keep = [i for i, c in enumerate(cols) if c in known]
            names = [cols[i] for i in keep]
            sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) "
                   f"VALUES ({', '.join('?' for _ in names)})")
            n = 0
            batch = []
            for row in rows:
                row = _shift_row(table, cols, row, shift_ms)
                if table == "app_meta" and row[0] in _SHIFT_META_MS and shift_ms:
                    try:
                        row = [row[0], str(int(row[1]) + shift_ms)]
                    except (TypeError, ValueError):
                        pass
                batch.append([row[i] for i in keep])
                n += 1
                if len(batch) >= 5000:
                    conn.executemany(sql, batch)
                    batch.clear()
            if batch:
                conn.executemany(sql, batch)
            counts[table] = n
        conn.commit()
    except Exception:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        _db.MIGRATION_STATUS.clear()
        _db.MIGRATION_STATUS.update(saved_status)
    conn.close()
    os.replace(tmp, final)

    cfg = shift_config(read_config(snapshot_dir), days)
    cfg_tmp = data_dir / ".config.json.tmp"
    cfg_tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    os.replace(cfg_tmp, data_dir / ".config.json")

    summary = {"seeded_at_ms": int(time.time() * 1000), "frozen_on": meta["frozen_on"],
               "shift_days": days, "rows": counts}
    (data_dir / SEED_MARKER).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _console_safe():
    # A Windows console or redirected output may be cp1252: never fail on a status mark.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def prepare(data_dir, reset=False, snapshot_dir=SNAPSHOT_DIR):
    """Block Spotify and make sure data_dir holds a seeded demo. Exits with a message on failure."""
    _console_safe()
    block_spotify_network()
    data_dir = Path(data_dir)
    try:
        if _same_dir(data_dir, APP_DIR):
            raise DemoSetupError("The demo needs its own data folder (DISCOVER_DEMO_DIR must not be the app folder).")
        if reset:
            reset_data(data_dir)
        if not (data_dir / "music_explorer.db").exists():
            print("  · Setting up the offline demo from demo/snapshot/ (one time, a few seconds)…", flush=True)
            s = seed(data_dir, snapshot_dir)
            print(f"  ✓ Demo ready: {s['rows'].get('events', 0):,} listening events, dates moved "
                  f"forward {s['shift_days']} days so the history ends this week.", flush=True)
    except DemoSetupError as exc:
        raise SystemExit(f"  ✗ Offline demo: {exc}")


# ---------------------------------------------------------------------------- runtime
def _demo_profile(meta):
    name = meta.get("user_display_name") or "Demo listener"
    return {"id": "demo_user", "display_name": name, "images": [], "type": "user",
            "uri": "spotify:user:demo_user", "external_urls": {"spotify": ""}}


def install(core):
    """Route the app's Spotify client to FakeSpotify and report a permanent simulated sign-in."""
    from src.devtools.fake_spotify import FakeSpotify

    try:
        meta = read_meta()
    except DemoSetupError:
        meta = {}
    pid = core.state.get("playlist_id") or meta.get("playlist_id") or "5bxDiscoverBacklogDemo"
    if not core.state.get("playlist_id"):
        core.state["playlist_id"] = pid
    fake = FakeSpotify(core._db.DB_PATH if getattr(core, "_SQLITE_ENABLED", False) else core.DATA_DIR / "music_explorer.db", pid)
    profile = _demo_profile(meta)
    fake.current_user = lambda *a, **k: dict(profile)
    fake.me = fake.current_user
    lock = threading.Lock()
    client = {"sp": None}

    def get_sp():
        with lock:
            if client["sp"] is None:
                client["sp"] = core.GuardedSpotify(fake)
            return client["sp"]

    core.get_cached_token = lambda: {"access_token": "offline-demo", "refresh_token": "offline-demo",
                                     "scope": core.SCOPE, "expires_at": 9999999999}
    core.get_sp = get_sp
    core.FAKE_SPOTIFY = fake
    return fake
