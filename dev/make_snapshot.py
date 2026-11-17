"""Build demo/snapshot/, the data behind the offline demo (`python app.py --demo`).

    python dev/make_snapshot.py --source <folder with music_explorer.db and .config.json>
                                [--out demo/snapshot] [--frozen-on YYYY-MM-DD]

The source folder is a Discover Cleaner data folder (or a copy of one). It is opened
read-only; nothing in it is changed. What goes into the snapshot (everything else is left out):

- events: every listening event (Last.fm scrobbles and what the app recorded) with an
  allowlist of payload fields; playlist ids are replaced by the demo's.
- tracks, track_artists: the tracks the app saw play. The demo's simulated backlog
  playlist is built from these.
- interests + interest_reasons (the watchlist) without notes; artist_likes; listen_sessions;
  track_listens.
- app_meta: only the keys in META_KEEP (no Spotify guard, OAuth or sync state).
- .config.json -> config.json.gz: threshold, lyrics offset, counters, favorite artists, genre
  caches and the backlog size history. The playlist id becomes the demo's; backlog sizes are
  offset so the last one matches the simulated playlist (day-to-day changes are unchanged).

Never included: .env, .spotify_cache, the backlog index (the real playlist's contents),
pending removals, the derived plays ledger and stats tables (the app rebuilds them on first
start), the legacy event list in .config.json, and any key not listed here.

The build fails if any playlist id from the source, an email address or a local file path
is still present in the output. Output files are gzip-compressed and byte-reproducible.
"""

import argparse
import gzip
import io
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "demo" / "snapshot"

DEMO_PLAYLIST_ID = "5bxDiscoverBacklogDemo"     # 22 chars, like a real playlist id
OTHER_PLAYLIST_ID = "5bxGraveyardSandbox002"    # FakeSpotify's second playlist
DEMO_PLAYLIST_NAME = "Discover Backlog (demo)"
DEMO_USER_NAME = "Dholeson"
SIZE_BUDGET = 6 * 1024 * 1024

TABLE_COLUMNS = {
    "tracks": ["id", "name", "album", "album_id", "image_url", "spotify_url", "duration_ms", "created_at"],
    "track_artists": ["track_id", "artist_id", "artist_name", "position"],
    "events": ["id", "event_type", "ts_ms", "day", "track_id", "artist_id", "album_id", "payload", "source"],
    "interests": ["id", "entity_type", "entity_id", "entity_name", "status", "note", "created_at", "updated_at"],
    "interest_reasons": ["id", "interest_id", "tag"],
    "artist_likes": ["artist_id", "artist_name", "auto_liked", "liked_at", "track_count"],
    "listen_sessions": ["id", "started_at", "ended_at", "track_count", "day"],
    "track_listens": ["id", "track_id", "ts_ms", "day", "listen_duration_s", "completed", "session_id"],
    "app_meta": ["key", "value"],
}
ORDER_BY = {"tracks": "created_at, id", "track_artists": "track_id, position", "app_meta": "key",
            "artist_likes": "artist_id"}
META_KEEP = {"migrated_config_history_v1", "lastfm_backfill_done", "hidden_artist_keys"}
# Event payload fields carried over; anything else (free-text notes, device or account
# details a future version might log) is dropped.
PAYLOAD_KEEP = {
    "album", "album_id", "artist_ids", "artist_name", "artist_names", "artists", "auto_removed",
    "clear_seq", "duration_ms", "entity_name", "entity_type", "genres", "interest_id", "listened_s",
    "playlist_id", "position", "reason", "reasons", "source", "started_in_backlog", "started_ms",
    "status", "target_playlist_id", "threshold_s", "track_name", "was_in_playlist",
}
PLAYLIST_KEYS = {"playlist_id", "target_playlist_id"}
CONFIG_KEEP = {"threshold_seconds", "lyrics_offset_ms", "stats", "favorite_artists",
               "favorite_artist_opt_outs", "track_genre_cache", "liked_library_profile", "backlog_history"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
PATH_RE = re.compile(r"[A-Za-z]:\\\\?Users|/Users/|/home/")


def _connect_ro(path):
    uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


class Scrubber:
    def __init__(self, primary_pid):
        self.primary = primary_pid
        self.seen_pids = {primary_pid} if primary_pid else set()

    def pid(self, value):
        if not value:
            return value
        self.seen_pids.add(str(value))
        return DEMO_PLAYLIST_ID if value == self.primary else OTHER_PLAYLIST_ID

    def payload(self, raw):
        if not raw:
            return raw
        try:
            p = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(p, dict):
            return None
        out = {}
        for k, v in p.items():
            if k not in PAYLOAD_KEEP:
                continue
            out[k] = self.pid(v) if k in PLAYLIST_KEYS else v
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _gz_bytes(text):
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(text.encode("utf-8"))
    return buf.getvalue()


def build(source, out=DEFAULT_OUT, frozen_on=None):
    source = Path(source)
    db_path, cfg_path = source / "music_explorer.db", source / ".config.json"
    if not db_path.exists() or not cfg_path.exists():
        raise SystemExit(f"{source} must contain music_explorer.db and .config.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    scrub = Scrubber(cfg.get("playlist_id"))
    conn = _connect_ro(db_path)
    if _table_exists(conn, "backlog_index"):  # ids only, so the leak check knows them
        scrub.seen_pids.update(r[0] for r in conn.execute("SELECT pid FROM backlog_index") if r[0])

    files, rows_out = {}, {}
    for table, cols in TABLE_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        cols = [c for c in cols if c in have]
        order = ORDER_BY.get(table, "id")
        lines = [json.dumps({"table": table, "columns": cols}, separators=(",", ":"))]
        n = 0
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}"):
            rec = dict(zip(cols, row))
            if table == "events":
                rec["payload"] = scrub.payload(rec.get("payload"))
            elif table == "interests":
                rec["note"] = None
            elif table == "app_meta" and rec["key"] not in META_KEEP:
                continue
            lines.append(json.dumps([rec[c] for c in cols], ensure_ascii=False, separators=(",", ":")))
            n += 1
        files[f"{table}.jsonl.gz"] = "\n".join(lines) + "\n"
        rows_out[table] = n
    max_ts = conn.execute("SELECT MAX(ts_ms) FROM events").fetchone()[0]
    conn.close()

    # .config.json: allowlisted keys, demo playlist, backlog sizes offset to the simulated playlist.
    playlist_size = rows_out.get("tracks", 0)
    demo_cfg = {k: cfg[k] for k in CONFIG_KEEP if k in cfg}
    demo_cfg["playlist_id"] = DEMO_PLAYLIST_ID
    hist = [dict(h) for h in (cfg.get("backlog_history") or []) if isinstance(h, dict)]
    hist.sort(key=lambda h: (str(h.get("day") or ""), int(h.get("ts_ms") or 0)))
    offset = 0
    if hist and playlist_size:
        offset = int(hist[-1].get("total") or 0) - playlist_size
    for h in hist:
        if h.get("playlist_id"):
            h["playlist_id"] = scrub.pid(h["playlist_id"])
        if isinstance(h.get("total"), int):
            h["total"] = max(0, h["total"] - offset)
    demo_cfg["backlog_history"] = hist
    files["config.json.gz"] = json.dumps(demo_cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    frozen = frozen_on or (datetime.fromtimestamp(max_ts / 1000).strftime("%Y-%m-%d") if max_ts else None)
    meta = {
        "format": 1,
        "frozen_on": frozen,
        "description": ("The author's real listening history (Last.fm scrobbles since 2021 plus what "
                        "Discover Cleaner recorded), frozen on " + str(frozen) + ". Account details are "
                        "removed; see dev/make_snapshot.py for exactly what is kept."),
        "playlist_id": DEMO_PLAYLIST_ID,
        "playlist_name": DEMO_PLAYLIST_NAME,
        "user_display_name": DEMO_USER_NAME,
        "backlog_size_offset": offset,
        "rows": rows_out,
    }

    # Leak check over everything that is about to be written.
    forbidden = {p for p in scrub.seen_pids if p and p not in (DEMO_PLAYLIST_ID, OTHER_PLAYLIST_ID)}
    blob = "\n".join(files.values()) + json.dumps(meta)
    problems = [f"playlist id {p[:5]}..." for p in forbidden if p in blob]
    problems += [f"email-like text {m!r}" for m in set(EMAIL_RE.findall(blob))]
    problems += [f"file path {m!r}" for m in set(PATH_RE.findall(blob))]
    if problems:
        raise SystemExit("refusing to write the snapshot, it still contains: " + "; ".join(sorted(problems)))

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.gz"):
        old.unlink()
    total = 0
    for name, text in sorted(files.items()):
        data = _gz_bytes(text)
        (out / name).write_bytes(data)
        total += len(data)
    meta_text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    (out / "meta.json").write_text(meta_text, encoding="utf-8", newline="\n")
    total += len(meta_text.encode("utf-8"))
    if total > SIZE_BUDGET:
        raise SystemExit(f"snapshot is {total / 1e6:.1f} MB, over the {SIZE_BUDGET / 1e6:.0f} MB budget")
    return meta, total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="folder with music_explorer.db and .config.json (read-only)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default: demo/snapshot)")
    ap.add_argument("--frozen-on", help="date the history ends (default: the last event's local date)")
    args = ap.parse_args(argv)
    meta, total = build(args.source, args.out, args.frozen_on)
    print(f"snapshot written: {total / 1e6:.2f} MB, frozen on {meta['frozen_on']}, rows {meta['rows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
