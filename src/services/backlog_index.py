"""Authoritative membership index for the active backlog playlist.

Why this exists: auto-clear has to answer one question on every poll -- "is the track
playing right now in the backlog playlist?" -- for playlists well over 1,000 tracks. The
old code kept a partial id set that a 900-track paging budget, UI side-writes and cached
re-reads kept corrupting, so most of the backlog was never cleared and removed tracks
came back. Here a single background worker owns membership:

* it pages the playlist resumably (a guard block sleeps and retries the SAME page),
* swaps the finished result in atomically, only after the last page arrived,
* never adopts the snapshot id returned by our own writes (it marks the index dirty and
  re-validates with a cheap metadata check instead),
* persists the committed index to SQLite so a restart is usable immediately.

Fail-safe rule: while membership is unknown, ``contains()`` returns None (fate "syncing")
and nothing is removed.

Everything Spotify- or app-specific is injected, so the class is unit-testable without
importing app.py. ``get_index()`` / ``get_worker()`` wire the live singletons.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from contextlib import nullcontext

log = logging.getLogger(__name__)

PAGE_LIMIT = 100
META_INTERVAL_S = 60.0          # cheap snapshot/total check cadence
HINT_MIN_INTERVAL_S = 20.0      # an unknown track playing from the backlog context triggers an early check
REMOVED_MEMORY_S = 3600.0       # our own removals never count as "unknown track playing" hints
PERSIST_DEBOUNCE_S = 5.0
MAX_RUN_RESTARTS = 3            # external edits while paging: restart at most this often, then commit dirty
MAX_PAGE_ERRORS = 4
MAX_BLOCK_SLEEP_S = 60.0

META_FIELDS = "name,snapshot_id,tracks.total,items.total"
# Spotify has been migrating playlist entries from "track" to "item"; ask for both.
_MEDIA_FIELDS = "id,uri,type,is_local,name,duration_ms,artists(id,name),album(id,name,images)"
ITEM_FIELDS = f"items(added_at,is_local,track({_MEDIA_FIELDS}),item({_MEDIA_FIELDS})),next,total"


def _now_ms():
    return int(time.time() * 1000)


def _small_image(images):
    """Smallest album image URL (list rows render ~40-64 px)."""
    best = None
    for img in images or []:
        if not isinstance(img, dict) or not img.get("url"):
            continue
        if best is None or (img.get("width") or 0) < (best.get("width") or 0):
            best = img
    return best.get("url") if best else None


def entry_from_item(item):
    """Normalise one playlist entry. Local files / episodes get track_id None (never cleared)."""
    item = item if isinstance(item, dict) else {}
    media = item.get("item") if isinstance(item.get("item"), dict) else item.get("track")
    media = media if isinstance(media, dict) else {}
    is_local = bool(item.get("is_local") or media.get("is_local"))
    kind = media.get("type") or "track"
    tid = media.get("id") if (not is_local and kind == "track") else None
    artists = [{"id": a.get("id"), "name": a.get("name") or ""}
               for a in (media.get("artists") or []) if isinstance(a, dict)]
    album = media.get("album") if isinstance(media.get("album"), dict) else {}
    return {
        "track_id": tid or None,
        "uri": media.get("uri"),
        "added_at": item.get("added_at"),
        "name": media.get("name") or "",
        "artists": ", ".join(a["name"] for a in artists if a["name"]),
        "artist_list": artists,
        "album": album.get("name") or "",
        "album_id": album.get("id"),
        "image": _small_image(album.get("images")),
        "duration_ms": int(media.get("duration_ms") or 0),
        "is_local": is_local,
    }


def entry_from_track(track):
    """Build an index entry from a Spotify track object or an app track_info dict."""
    track = track if isinstance(track, dict) else {}
    tid = track.get("id") or track.get("track_id")
    artist_list = track.get("artist_list")
    if not artist_list and isinstance(track.get("artists"), list):
        artist_list = [{"id": a.get("id"), "name": a.get("name") or ""} for a in track["artists"] if isinstance(a, dict)]
    artist_list = artist_list or []
    artists = track.get("artists")
    if not isinstance(artists, str):
        artists = ", ".join(a.get("name") or "" for a in artist_list if a.get("name"))
    album = track.get("album")
    album_name = album.get("name") if isinstance(album, dict) else (album or "")
    image = track.get("image")
    if not image and isinstance(album, dict):
        image = _small_image(album.get("images"))
    return {
        "track_id": tid,
        "uri": track.get("uri") or (f"spotify:track:{tid}" if tid else None),
        "added_at": track.get("added_at"),
        "name": track.get("name") or "",
        "artists": artists or "",
        "artist_list": [{"id": a.get("id"), "name": a.get("name") or ""} for a in artist_list],
        "album": album_name or "",
        "album_id": track.get("album_id") or (album.get("id") if isinstance(album, dict) else None),
        "image": image,
        "duration_ms": int(track.get("duration_ms") or 0),
        "is_local": False,
    }


def retry_after_of(exc, default=5.0):
    """Seconds to wait for a guard block / 429 (SpotifyGuardBlocked carries .retry_after)."""
    ra = getattr(exc, "retry_after", None)
    if ra is None:
        try:
            headers = getattr(exc, "headers", None) or {}
            ra = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:
            ra = None
    try:
        ra = float(ra)
    except (TypeError, ValueError):
        ra = default
    return max(1.0, ra)


def is_rate_limited(exc):
    return getattr(exc, "http_status", None) == 429


class SQLiteIndexStore:
    """backlog_index(pid PK, snapshot_id, payload_json, synced_at_ms), created lazily."""

    def __init__(self, conn_factory):
        self._conn_factory = conn_factory
        self._ensured = False

    def _conn(self):
        conn = self._conn_factory()
        if not self._ensured:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS backlog_index ("
                " pid TEXT PRIMARY KEY, snapshot_id TEXT, payload_json TEXT NOT NULL,"
                " synced_at_ms INTEGER)"
            )
            conn.commit()
            self._ensured = True
        return conn

    def load(self, pid):
        row = self._conn().execute(
            "SELECT snapshot_id, payload_json, synced_at_ms FROM backlog_index WHERE pid = ?", (pid,)
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row[1] or "{}")
        payload["snapshot_id"] = row[0]
        payload["synced_at_ms"] = row[2]
        return payload

    def save(self, pid, snapshot_id, payload, synced_at_ms):
        conn = self._conn()
        conn.execute(
            "INSERT INTO backlog_index (pid, snapshot_id, payload_json, synced_at_ms) VALUES (?,?,?,?)"
            " ON CONFLICT(pid) DO UPDATE SET snapshot_id=excluded.snapshot_id,"
            " payload_json=excluded.payload_json, synced_at_ms=excluded.synced_at_ms",
            (pid, snapshot_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), synced_at_ms),
        )
        conn.commit()


class PlaylistIndex:
    """Membership + order of one playlist. All public methods are thread-safe."""

    def __init__(self, store=None, on_change=None, clock=time.monotonic, wall_ms=_now_ms):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._store = store
        self._on_change = on_change
        self._clock = clock
        self._wall_ms = wall_ms
        self.generation = 0
        self.removal_seq = 0
        self._reset(None)

    # ---------- internal state ----------
    def _reset(self, pid):
        self.generation += 1
        self.pid = pid
        self.name = ""
        self.snapshot_id = None
        self.order = []            # committed entries; list position == playlist position
        self.ids = set()           # committed clearable track ids
        self.complete = False      # True once a full page run (or persisted copy) is in place
        self.synced_at_ms = None   # last time the committed index was confirmed against Spotify
        self.checked_at_ms = None  # last metadata check
        self.dirty = False         # we wrote to the playlist since the snapshot was confirmed
        self.error = None
        self._run = None           # in-progress page run
        self._meta_due = 0.0       # monotonic; 0 = due now
        self._last_hint = -1e9
        self._removed_at = {}      # track_id -> monotonic time we removed it (our own writes)
        self._persist_due = None
        # A persisted copy is shown at once but not trusted for automatic removals until a
        # metadata check or page run confirms it (the playlist may have changed while we were off).
        self.provisional = False
        # A page run that raced our own writes can hold a shifted page (one track missing, one
        # doubled) with matching counts; the next check must re-page instead of adopting it.
        self._force_repage = False

    def _notify(self, reason):
        cb = self._on_change
        if cb is None:
            return
        try:
            cb(self, reason)
        except Exception:  # a broken mirror must never break membership
            log.exception("backlog index on_change failed")

    def _mark_persist(self):
        if self._persist_due is None:
            self._persist_due = self._clock() + PERSIST_DEBOUNCE_S

    # ---------- reads ----------
    def contains(self, track_id):
        """True / False, or None while the index is incomplete and the track is unknown."""
        if not track_id:
            return False
        with self._lock:
            if track_id in self.ids:
                return True
            run = self._run
            if run is not None and track_id in run["ids"]:
                return True
            return False if self.complete else None

    def clearable(self, track_id):
        """Membership for automatic removals: like contains(), but None (unknown) while the
        index is only a persisted copy that Spotify hasn't confirmed yet this session."""
        if not track_id:
            return False
        with self._lock:
            if not self.provisional:
                return self.contains(track_id)
            run = self._run
            if run is not None and track_id in run["ids"]:
                return True   # seen on a page fetched this session
            return None

    def confirmed_since(self, wall_ms):
        """True when the committed index was confirmed against Spotify at or after wall_ms (so an
        absent track really was absent then), with no page run in progress."""
        with self._lock:
            return (not self.provisional and self._run is None and self.synced_at_ms is not None
                    and wall_ms is not None and int(self.synced_at_ms) >= int(wall_ms))

    def position_of(self, track_id):
        if not track_id:
            return None
        with self._lock:
            source = self.order if (self.complete or self._run is None) else self._run["entries"]
            for i, e in enumerate(source):
                if e.get("track_id") == track_id:
                    return i
            if source is not self.order:
                for i, e in enumerate(self.order):
                    if e.get("track_id") == track_id:
                        return i
        return None

    def entry(self, track_id):
        if not track_id:
            return None
        with self._lock:
            for source in (self.order, (self._run or {}).get("entries") or []):
                for e in source:
                    if e.get("track_id") == track_id:
                        return dict(e)
        return None

    def items(self):
        """Entries to display: the committed order, or the partial first load while syncing."""
        with self._lock:
            if self.complete or self._run is None:
                return list(self.order)
            return list(self._run["entries"])

    def ids_snapshot(self):
        with self._lock:
            ids = set(self.ids)
            if self._run is not None:
                ids |= self._run["ids"]
            return ids

    @property
    def total(self):
        with self._lock:
            if self.complete:
                return len(self.order)
            run = self._run
            if run is not None and run.get("page_total") is not None:
                return int(run["page_total"])
            if run is not None:
                return int(run.get("expected_total") or 0)
            return len(self.order)

    def status(self):
        with self._lock:
            run = self._run
            if self.complete:
                known = len(self.order)
            elif run is not None:
                known = len(run["entries"])
            else:
                known = 0
            total = self.total
            if run is not None:
                progress = {"loaded": len(run["entries"]),
                            "total": int(run.get("page_total") or run.get("expected_total") or 0)}
            else:
                progress = {"loaded": known, "total": total}
            return {
                "playlist_id": self.pid,
                "name": self.name,
                "total": total,
                "known": known,
                "complete": bool(self.complete),
                "loading": run is not None,
                "synced_at_ms": self.synced_at_ms,
                "checked_at_ms": self.checked_at_ms,
                "removal_seq": self.removal_seq,
                "progress": progress,
                "dirty": bool(self.dirty),
                "confirmed": bool(self.complete and not self.provisional),
                "error": self.error,
                "duration_ms": sum(int(e.get("duration_ms") or 0) for e in self.order) if self.complete else None,
            }

    # ---------- local mutations (our own writes) ----------
    def apply_removal(self, track_ids, playlist_id=None):
        """Prune ids after a successful removal. Decrements totals only for ids actually present."""
        ids = {t for t in (track_ids or []) if t}
        if not ids:
            return 0
        with self._lock:
            if playlist_id and playlist_id != self.pid:
                return 0
            before = len(self.order)
            self.order = [e for e in self.order if e.get("track_id") not in ids]
            removed = before - len(self.order)
            present = bool(ids & self.ids)
            self.ids -= ids
            run = self._run
            run_removed = 0
            if run is not None:
                run_before = len(run["entries"])
                run["entries"] = [e for e in run["entries"] if e.get("track_id") not in ids]
                run_removed = run_before - len(run["entries"])
                present = present or bool(ids & run["ids"])
                run["ids"] -= ids
                run["removed"] |= ids
                run["touched"] = True
                run["mutations"] += 1
                if run.get("page_total") is not None:
                    run["page_total"] = max(0, int(run["page_total"]) - max(run_removed, 1 if present else 0))
            if not (removed or run_removed or present):
                return 0
            now = self._clock()
            for tid in ids:
                self._removed_at[tid] = now
            if len(self._removed_at) > 500:
                cutoff = now - REMOVED_MEMORY_S
                self._removed_at = {t: ts for t, ts in self._removed_at.items() if ts >= cutoff}
            self.dirty = True
            self.removal_seq += 1
            self._mark_persist()
        self._notify("removal")
        return max(removed, run_removed)

    def apply_add(self, entry, position=None, playlist_id=None):
        """Insert an entry we just added (undo) at its playlist position."""
        entry = dict(entry or {})
        tid = entry.get("track_id")
        with self._lock:
            if playlist_id and playlist_id != self.pid:
                return False
            pos = len(self.order) if position is None else max(0, min(int(position), len(self.order)))
            self.order.insert(pos, entry)
            if tid:
                self.ids.add(tid)
                self._removed_at.pop(tid, None)
            run = self._run
            if run is not None:
                run["touched"] = True
                run["mutations"] += 1
                run["removed"].discard(tid)
                if position is not None and int(position) < len(run["entries"]):
                    run["entries"].insert(max(0, int(position)), dict(entry))
                    if tid:
                        run["ids"].add(tid)
                if run.get("page_total") is not None:
                    run["page_total"] = int(run["page_total"]) + 1
            self.dirty = True
            self.removal_seq += 1
            self._mark_persist()
        self._notify("add")
        return True

    # ---------- control ----------
    def switch(self, pid, notify=True):
        """Make pid the indexed playlist. Loads a persisted copy (usable at once) and schedules a sync."""
        with self._lock:
            if pid == self.pid:
                self._meta_due = 0.0
                self._wake.set()
                return False
        self.flush_persist(force=True)
        loaded = None
        if pid and self._store is not None:
            try:
                loaded = self._store.load(pid)
            except Exception:
                log.exception("backlog index: loading persisted index failed")
                loaded = None
        with self._lock:
            self._reset(pid)
            if loaded and isinstance(loaded.get("order"), list):
                self.order = [dict(e) for e in loaded["order"] if isinstance(e, dict)]
                self.ids = {e.get("track_id") for e in self.order if e.get("track_id")}
                self.name = loaded.get("name") or ""
                self.snapshot_id = loaded.get("snapshot_id")
                self.synced_at_ms = loaded.get("synced_at_ms")
                self.dirty = bool(loaded.get("dirty"))
                self.complete = True
                self.provisional = True
            self.removal_seq += 1
            self._meta_due = 0.0
        self._wake.set()
        if notify:
            self._notify("switch")
        return True

    def request_sync(self):
        with self._lock:
            self._meta_due = 0.0
        self._wake.set()

    def hint_stale(self, track_id=None):
        """An unknown track is playing from the backlog's own context: check for external adds soon."""
        with self._lock:
            now = self._clock()
            if self._run is not None or now - self._last_hint < HINT_MIN_INTERVAL_S:
                return False
            removed_at = self._removed_at.get(track_id) if track_id else None
            if removed_at is not None and now - removed_at < REMOVED_MEMORY_S:
                # We removed it ourselves and Spotify keeps playing it out: not an external add.
                return False
            self._last_hint = now
            self._meta_due = min(self._meta_due, now)
        self._wake.set()
        return True

    def meta_due_in(self):
        with self._lock:
            return max(0.0, self._meta_due - self._clock())

    def defer_meta(self, seconds):
        with self._lock:
            self._meta_due = self._clock() + max(0.0, float(seconds))

    def wait(self, timeout):
        self._wake.wait(max(0.0, timeout))
        self._wake.clear()

    # ---------- sync protocol (driven by IndexSyncWorker) ----------
    def after_meta(self, gen, snapshot_id, total, name=None):
        """Decide what a metadata check means: 'unchanged' | 'adopted' | 'repage' | 'aborted'."""
        adopted = False
        with self._lock:
            if gen != self.generation:
                return "aborted"
            now_ms = self._wall_ms()
            if name:
                self.name = name
            self.checked_at_ms = now_ms
            self._meta_due = self._clock() + META_INTERVAL_S
            if self.complete and snapshot_id and snapshot_id == self.snapshot_id and not self.dirty:
                self.error = None
                self.synced_at_ms = now_ms
                self.provisional = False
                return "unchanged"
            # After our own writes the snapshot always moves; if the counts still agree, the
            # local copy already reflects them and a full re-page would only burn budget.
            # A copy restored from disk isn't known to hold our writes, so it is re-paged.
            if (self.complete and self.dirty and not self.provisional and not self._force_repage
                    and total is not None and int(total) == len(self.order)):
                self.snapshot_id = snapshot_id
                self.dirty = False
                self.synced_at_ms = now_ms
                self.error = None
                self._mark_persist()
                adopted = True
        if adopted:
            self._notify("meta")
            return "adopted"
        return "repage"

    def start_run(self, gen, snapshot_id, expected_total):
        with self._lock:
            if gen != self.generation:
                return False
            self._run = {
                "gen": gen,
                "snapshot": snapshot_id,
                "expected_total": int(expected_total or 0),
                "page_total": None,
                "entries": [],
                "ids": set(),
                "removed": set(),
                "touched": False,
                "inconsistent": False,
                "mutations": 0,
            }
            return True

    def run_cursor(self, gen):
        """(offset, mutation counter) for the next page, or None when the run was aborted."""
        with self._lock:
            run = self._run
            if run is None or gen != self.generation or run["gen"] != gen:
                return None
            return len(run["entries"]), run["mutations"]

    def add_page(self, gen, offset, mutations, entries, page_total, has_next):
        """Returns 'more' | 'done' | 'retry' (a local write raced the request) | 'changed' | 'aborted'."""
        with self._lock:
            run = self._run
            if run is None or gen != self.generation:
                return "aborted"
            if run["mutations"] != mutations or len(run["entries"]) != offset:
                return "retry"
            if (page_total is not None and not run["touched"] and not run["inconsistent"]
                    and int(page_total) != run["expected_total"]):
                return "changed"
            removed = run["removed"]
            for e in entries:
                if e.get("track_id") in removed:
                    continue
                run["entries"].append(e)
                if e.get("track_id"):
                    run["ids"].add(e["track_id"])
            if page_total is not None:
                run["page_total"] = int(page_total)
            done = (not has_next) or not entries
            if not done and run["page_total"] is not None and len(run["entries"]) >= run["page_total"]:
                done = True
            return "done" if done else "more"

    def mark_run_inconsistent(self, gen):
        with self._lock:
            if self._run is not None and gen == self.generation:
                self._run["inconsistent"] = True

    def abort_run(self, gen, error=None):
        with self._lock:
            if gen != self.generation:
                return
            self._run = None
            self.error = error
            self._meta_due = self._clock() + META_INTERVAL_S

    def commit_run(self, gen):
        """Atomically swap in the finished page run."""
        with self._lock:
            run = self._run
            if run is None or gen != self.generation:
                return False
            self.order = run["entries"]
            self.ids = set(run["ids"])
            self.snapshot_id = run["snapshot"]
            self.dirty = bool(run["touched"] or run["inconsistent"])
            self.complete = True
            self.provisional = False
            tids = [e.get("track_id") for e in self.order if e.get("track_id")]
            self._force_repage = bool(run["touched"] and len(tids) != len(set(tids)))
            self.synced_at_ms = self._wall_ms()
            self.error = None
            self._run = None
            self.removal_seq += 1
            self._meta_due = self._clock() + META_INTERVAL_S
            self._persist_due = self._clock()  # persist right away
        self.flush_persist()
        self._notify("commit")
        return True

    def set_error(self, gen, error, retry_in_s=META_INTERVAL_S):
        with self._lock:
            if gen != self.generation:
                return
            self.error = error
            self._meta_due = self._clock() + retry_in_s

    # ---------- persistence ----------
    def flush_persist(self, force=False):
        with self._lock:
            if self._store is None or not self.pid or not self.complete:
                self._persist_due = None
                return False
            if not force and (self._persist_due is None or self._clock() < self._persist_due):
                return False
            pid = self.pid
            snapshot = self.snapshot_id
            synced = self.synced_at_ms
            payload = {"name": self.name, "dirty": bool(self.dirty), "order": list(self.order)}
            self._persist_due = None
        try:
            self._store.save(pid, snapshot, payload, synced)
            return True
        except Exception:
            log.exception("backlog index: persist failed")
            return False


class IndexSyncWorker:
    """Keeps a PlaylistIndex in sync with Spotify. Runs as one daemon thread (never on the poll thread)."""

    def __init__(self, index, get_sp, invalidate=None, lock_remaining=None, log_error=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.index = index
        self._get_sp = get_sp
        self._invalidate = invalidate or (lambda pid: None)
        self._lock_remaining = lock_remaining or (lambda: 0)
        self._log_error = log_error
        self._sleep_fn = sleep
        self._clock = clock
        self.pages_fetched = 0
        self.meta_checks = 0
        # The fields filter keeps pages small; if Spotify ever rejects it (400), page unfiltered.
        self.item_fields = ITEM_FIELDS

    def _log(self, exc, context):
        if self._log_error is not None:
            try:
                self._log_error(exc, context)
                return
            except Exception:
                pass
        log.warning("%s: %s", context, exc)

    def _sleep(self, gen, seconds):
        """Sleep in 1 s slices; False as soon as the index switched playlists."""
        deadline = self._clock() + max(0.0, seconds)
        while True:
            if self.index.generation != gen:
                return False
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            self._sleep_fn(min(1.0, remaining))

    def _call(self, gen, fn):
        """Run one Spotify read. Guard blocks and 429s sleep min(retry_after, 60) and retry the
        same request; other errors propagate. Returns None if the index switched meanwhile."""
        while True:
            if self.index.generation != gen:
                return None
            try:
                return fn()
            except Exception as e:
                if not is_rate_limited(e):
                    raise
                if not self._sleep(gen, min(retry_after_of(e), MAX_BLOCK_SLEEP_S)):
                    return None

    def run_forever(self):
        while True:
            try:
                self.step()
            except Exception as e:  # never let the worker die
                self._log(e, "backlog index worker")
                try:
                    self.index.wait(10)
                except Exception:
                    time.sleep(10)

    def step(self):
        idx = self.index
        idx.flush_persist()
        due_in = idx.meta_due_in()
        if due_in > 0:
            idx.wait(min(due_in, PERSIST_DEBOUNCE_S))
            return "idle"
        if not idx.pid:
            idx.wait(5)
            return "no_playlist"
        sp = self._get_sp()
        if sp is None:
            idx.defer_meta(5)
            return "no_auth"
        locked = self._lock_remaining() or 0
        if locked > 0:
            # A real 429 lock: make no calls until it expires.
            idx.defer_meta(min(float(locked), MAX_BLOCK_SLEEP_S))
            return "locked"
        return self.sync(sp)

    def sync(self, sp):
        """One metadata check and, when needed, a full resumable page run."""
        idx = self.index
        gen, pid = idx.generation, idx.pid
        restarts = 0
        while True:
            self._invalidate(pid)
            try:
                meta = self._call(gen, lambda: sp.playlist(pid, fields=META_FIELDS))
            except Exception as e:
                self._log(e, "backlog index meta")
                idx.set_error(gen, f"Couldn't check the playlist: {e}", retry_in_s=META_INTERVAL_S)
                return "error"
            if meta is None:
                return "aborted"
            self.meta_checks += 1
            snapshot = meta.get("snapshot_id")
            total = _meta_total(meta)
            decision = idx.after_meta(gen, snapshot, total, meta.get("name"))
            if decision != "repage":
                return decision
            if not idx.start_run(gen, snapshot, total):
                return "aborted"
            outcome = self._page_run(sp, gen, pid)
            if outcome == "changed":
                restarts += 1
                if restarts <= MAX_RUN_RESTARTS:
                    continue
                # Someone keeps editing the playlist: page once more without the consistency
                # check and commit dirty so the next metadata check re-validates.
                if not idx.start_run(gen, snapshot, total):
                    return "aborted"
                idx.mark_run_inconsistent(gen)
                outcome = self._page_run(sp, gen, pid)
            if outcome == "done":
                return "committed" if idx.commit_run(gen) else "aborted"
            return outcome

    def _page_run(self, sp, gen, pid):
        idx = self.index
        errors = 0
        while True:
            cursor = idx.run_cursor(gen)
            if cursor is None:
                return "aborted"
            offset, mutations = cursor
            self._invalidate(pid)
            fields = self.item_fields
            try:
                if fields:
                    page = self._call(gen, lambda: sp.playlist_items(
                        pid, fields=fields, limit=PAGE_LIMIT, offset=offset))
                else:
                    page = self._call(gen, lambda: sp.playlist_items(pid, limit=PAGE_LIMIT, offset=offset))
            except Exception as e:
                if fields and getattr(e, "http_status", None) == 400:
                    self._log(e, "backlog index: fields filter rejected; paging unfiltered")
                    self.item_fields = None
                    continue
                errors += 1
                self._log(e, f"backlog index page offset={offset}")
                if errors >= MAX_PAGE_ERRORS:
                    idx.abort_run(gen, f"Couldn't read the playlist: {e}")
                    return "error"
                if not self._sleep(gen, 5 * errors):
                    return "aborted"
                continue
            if page is None:
                return "aborted"
            self.pages_fetched += 1
            items = page.get("items") or []
            entries = [entry_from_item(it) for it in items]
            result = idx.add_page(gen, offset, mutations, entries, page.get("total"),
                                  has_next=bool(page.get("next")))
            if result in ("more", "retry"):
                continue
            return result  # done | changed | aborted


def _meta_total(meta):
    for key in ("tracks", "items"):
        block = meta.get(key)
        if isinstance(block, dict) and block.get("total") is not None:
            try:
                return int(block["total"])
            except (TypeError, ValueError):
                return None
    if meta.get("total") is not None:
        try:
            return int(meta["total"])
        except (TypeError, ValueError):
            return None
    return None


# ---------- live wiring (lazy; tests build their own instances) ----------
_INDEX = None
_WORKER = None
_SINGLETON_LOCK = threading.Lock()


def invalidate_playlist_cache(core, pid):
    """Drop the guard's cached playlist pages/meta for pid (platform helper when available)."""
    if not pid:
        return
    # Only the playlist's own reads: the public invalidate_playlist_cache also drops the
    # playlist *list*, which this worker (every page, every minute) must not keep evicting.
    fn = getattr(core, "_spotify_cache_invalidate", None)
    methods = getattr(core, "_SPOTIFY_PLAYLIST_CACHE_METHODS", ("playlist", "playlist_items", "playlist_tracks"))
    if callable(fn):
        try:
            fn(methods, needle=pid)
            return
        except TypeError:
            pass
    fn = getattr(core, "invalidate_playlist_cache", None)
    if callable(fn):
        try:
            fn(pid)
            return
        except Exception:
            pass
    cache = getattr(core, "_SPOTIFY_GUARD_CACHE", None)
    if not isinstance(cache, dict):
        return
    lock = getattr(core, "_SPOTIFY_GUARD_LOCK", None) or nullcontext()
    with lock:
        for key in [k for k in list(cache) if isinstance(k, str)
                    and k.split(":", 1)[0] in ("playlist", "playlist_items", "playlist_tracks")
                    and pid in k]:
            cache.pop(key, None)


def _mirror_to_core(index, reason):
    """Keep the legacy state keys (read by /api/status aliases, build_analytics_payload) in step."""
    import app as core
    with index._lock:
        pid = index.pid
        complete = index.complete
        total = index.total
        ids = index.ids_snapshot()
        name = index.name
        snapshot = index.snapshot_id
        duration = sum(int(e.get("duration_ms") or 0) for e in index.order) if complete else None
    with core.state_lock:
        if core.state.get("playlist_id") != pid:
            return
        core.state["playlist_track_ids"] = ids
        core.state["playlist_track_ids_loaded"] = bool(complete)
        core.state["playlist_snapshot_id"] = snapshot
        core.state["playlist_total"] = int(total)
        if name:
            core.state["playlist_name"] = name
        if duration is not None:
            core.state["playlist_duration_ms"] = duration
    if complete and reason in ("commit", "removal", "add"):
        try:
            core.snapshot_backlog(total, playlist_id=pid)
        except TypeError:  # older signature
            core.snapshot_backlog(total)


def get_index():
    """The live index for the app's active playlist (created and loaded on first use)."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _SINGLETON_LOCK:
        if _INDEX is None:
            import app as core
            store = None
            if getattr(core, "_SQLITE_ENABLED", False):
                store = SQLiteIndexStore(core._db.get_conn)
            idx = PlaylistIndex(store=store, on_change=_mirror_to_core)
            _INDEX = idx
            with core.state_lock:
                pid = core.state.get("playlist_id")
            idx.switch(pid, notify=False)
            atexit.register(idx.flush_persist, True)
    _mirror_to_core(_INDEX, "init")
    return _INDEX


def get_worker():
    global _WORKER
    if _WORKER is not None:
        return _WORKER
    idx = get_index()
    with _SINGLETON_LOCK:
        if _WORKER is None:
            import app as core

            def _lock_remaining():
                fn = getattr(core, "_spotify_guard_remaining_s", None)
                return fn() if callable(fn) else 0

            _WORKER = IndexSyncWorker(
                idx,
                get_sp=lambda: core.get_sp(),  # resolved per call: the sandbox swaps get_sp after import
                invalidate=lambda pid: invalidate_playlist_cache(core, pid),
                lock_remaining=_lock_remaining,
                log_error=lambda e, ctx: core._log_app_error(e, context=ctx),
            )
    return _WORKER


def run_worker():
    """Background-worker entry point (registered from src/web/backlog_api.init)."""
    get_worker().run_forever()
