"""Clearing engine: listening accounting, the fate of the current track, and safe removals.

The poll loop in app.py feeds every playback poll into ``PlayTracker.observe``; the tracker
credits listening time from the monotonic clock *bounded by the progress Spotify reports*
(so slow poll cycles can neither under- nor over-credit), splits plays on track changes and
repeats, and flushes a play after a long pause. ``compute_fate`` turns that into what the
UI shows ("clears in 6 s", "kept", "syncing", ...). ``ClearingEngine`` performs removals
exactly once per play, queues failed ones in SQLite (``pending_removals``) for retry, keeps
an undo list, and restores tracks at their original position.

Pure pieces (tracker, fate, RecentlyCleared, PendingRemovals) take no app imports so they
are unit-testable; the engine reaches the live app lazily through ``core`` arguments.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

UNDO_WINDOW_S = 30 * 60
IDLE_FLUSH_S = 120.0          # a play ends after this long without playback
REPEAT_RESTART_MS = 5000      # progress jumping back under 5 s = the track started over
PROGRESS_SLACK_S = 1.5        # jitter allowance between wall clock and reported progress
MAX_CATCHUP_IDS = 100         # Spotify's per-request limit for playlist removals
PENDING_MAX_ATTEMPTS = 50     # ~4 h of consecutive failures at the 5-minute backoff ceiling
PENDING_MAX_AGE_S = 24 * 3600  # a removal older than this no longer reflects what the user wants
PERMANENT_HTTP = (400, 403, 404)  # retrying cannot help (bad request, no write access, playlist gone)
MAX_KEPT_MEMORY = 500

FATE_STATES = ("in_backlog", "clearing_paused", "kept", "cleared", "queued",
               "not_in_backlog", "syncing", "idle")
CLEAR_REASONS = ("threshold", "manual", "like", "routed", "catch-up")


def _now_ms():
    return int(time.time() * 1000)


def _http_status_of(exc):
    try:
        return int(getattr(exc, "http_status", None))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Listening accounting
# ---------------------------------------------------------------------------

class Play:
    """One continuous play of one track (a repeat or a resume after a long pause is a new one)."""

    def __init__(self, track_id, track=None, started_ms=None, duration_ms=0, membership=None,
                 carried_s=0.0, continuation=False, seen_mono=None):
        self.track_id = track_id
        self.track = track
        self.started_ms = started_ms
        self.duration_ms = int(duration_ms or 0)
        self.listened_s = 0.0
        # Listening from the same sitting before a long pause still counts toward the threshold.
        self.carried_s = float(carried_s or 0.0)
        self.started_in_backlog = membership      # True / False / None until known
        self.auto_removed = False                 # cleared by the threshold rule during this play
        self.cleared_reason = None                # any clear of this track during this play
        self.threshold_logged = False
        self.removal_queued = False
        # A removal Spotify refused for good (403/404): don't retry it every poll of this play.
        self.removal_error = None
        # The listener un-kept a track that joined the backlog mid-play: let this play clear it.
        self.clear_override = False
        self.last_progress_ms = 0
        self.last_playing = False
        self.continuation = continuation
        self.seen_mono = seen_mono                # monotonic time the poll loop first saw this play
        self.prev_track_id = None                 # the track playing before this play started

    @property
    def added_during_play(self):
        """The track joined the backlog while this play was already under way (not clearable now)."""
        return self.started_in_backlog is False and not self.clear_override

    @property
    def total_listened_s(self):
        return self.listened_s + self.carried_s

    def as_state(self):
        return {
            "track_id": self.track_id,
            "started_ms": self.started_ms,
            "started_in_backlog": self.started_in_backlog,
            "auto_removed": self.auto_removed,
            "cleared_reason": self.cleared_reason,
            "duration_ms": self.duration_ms,
            "listened_s": round(self.total_listened_s, 1),
        }


class PlayTracker:
    """Turns successive playback polls into plays with honestly credited listening time.

    Credit between two polls of the same playing track is
    ``min(wall-clock delta, dprog + min(1.5 s, dprog), 3 x poll interval)`` where dprog is the
    (positive) progress delta: the wall clock stops a seek-forward from over-crediting, the
    progress delta stops a stalled or slow cycle from over-crediting, and the cap bounds any
    single step. The jitter slack scales with real progress, so a device that reports
    ``is_playing`` while its position is frozen (buffering, offline speaker) earns nothing.
    A new play earns its head (the progress already made) only when that head fits in the
    gap since the last poll, i.e. it plausibly started from the beginning meanwhile; a track
    started or scrubbed mid-way earns nothing until it is seen advancing.
    """

    def __init__(self, poll_interval_s=3.0, idle_flush_s=IDLE_FLUSH_S):
        self.poll_interval_s = float(poll_interval_s)
        self.idle_flush_s = float(idle_flush_s)
        self.play = None
        self._last_poll = None
        self._last_playing = None

    @property
    def cap_s(self):
        return 3.0 * max(0.5, self.poll_interval_s)

    def observe(self, now, now_ms, track_id, is_playing, progress_ms, duration_ms=0,
                membership=None, track=None):
        """Feed one poll that returned a track. Returns (finished_play | None, started_new_play)."""
        dt = None if self._last_poll is None else max(0.0, now - self._last_poll)
        self._last_poll = now
        progress_ms = max(0, int(progress_ms or 0))
        is_playing = bool(is_playing)
        p = self.play
        finished = None
        started = False
        repeat = (p is not None and p.track_id == track_id and is_playing
                  and progress_ms < REPEAT_RESTART_MS
                  and p.last_progress_ms - progress_ms >= REPEAT_RESTART_MS)
        if p is None or p.track_id != track_id or repeat:
            head_s = progress_ms / 1000.0 if is_playing else 0.0
            if p is not None:
                if p.last_playing and dt is not None:
                    # The previous track kept playing after our last poll until it ended or was
                    # skipped; credit that tail, bounded by the gap not explained by the new head.
                    tail_s = (max(0.0, (p.duration_ms - p.last_progress_ms) / 1000.0)
                              if p.duration_ms else dt)
                    p.listened_s += max(0.0, min(tail_s, dt - head_s, self.cap_s))
                if p.listened_s > 0 or not p.continuation:
                    finished = p
            prev_tid = p.track_id if p is not None else None
            p = Play(track_id, track=track, started_ms=int(now_ms - progress_ms),
                     duration_ms=duration_ms, membership=membership, seen_mono=now)
            p.prev_track_id = prev_tid
            if is_playing and dt is not None and head_s <= dt + PROGRESS_SLACK_S:
                # The new track has been playing since it began (bounded by the poll gap).
                p.listened_s += max(0.0, min(head_s, dt, self.cap_s))
            self.play = p
            started = True
            if self._last_playing is None or is_playing:
                self._last_playing = now
        else:
            if track is not None:
                p.track = track
            if duration_ms:
                p.duration_ms = int(duration_ms)
            if dt is not None and is_playing:
                dprog_s = (progress_ms - p.last_progress_ms) / 1000.0
                if p.last_playing:
                    moved = max(0.0, dprog_s)
                    credit = min(dt, moved + min(PROGRESS_SLACK_S, moved), self.cap_s)
                else:
                    # Resumed between polls: the progress delta is exactly what played.
                    credit = min(dt, max(0.0, dprog_s), self.cap_s)
                p.listened_s += max(0.0, credit)
            if p.started_ms is None and is_playing:
                p.started_ms = int(now_ms - progress_ms)
        if p.started_in_backlog is None and membership is not None:
            p.started_in_backlog = membership
        p.last_progress_ms = progress_ms
        p.last_playing = is_playing
        if is_playing:
            self._last_playing = now
        elif finished is None:
            finished = self._flush_if_idle(now, membership)
        return finished, started

    def idle(self, now, membership=None):
        """Feed a poll with nothing (or no track) playing. Returns a flushed play or None."""
        self._last_poll = now
        if self.play is not None:
            self.play.last_playing = False
        return self._flush_if_idle(now, membership)

    def _flush_if_idle(self, now, membership=None):
        p = self.play
        if p is None:
            return None
        if p.continuation and p.listened_s <= 0:
            return None
        last = self._last_playing if self._last_playing is not None else now
        if now - last < self.idle_flush_s:
            return None
        # End the play now so it is logged promptly; keep a continuation for the same track so
        # a resume neither re-triggers a removal nor loses listening toward the threshold.
        # Same sitting, so backlog membership is still judged at the original start: a track
        # added to the backlog during the pause must not clear on the listening carried over.
        cont = Play(p.track_id, track=p.track, started_ms=None, duration_ms=p.duration_ms,
                    membership=p.started_in_backlog if p.started_in_backlog is not None else membership,
                    carried_s=p.total_listened_s, continuation=True, seen_mono=p.seen_mono)
        cont.prev_track_id = p.prev_track_id
        cont.auto_removed = p.auto_removed
        cont.cleared_reason = p.cleared_reason
        cont.threshold_logged = p.threshold_logged
        cont.removal_queued = p.removal_queued
        cont.removal_error = p.removal_error
        cont.clear_override = p.clear_override
        cont.last_progress_ms = p.last_progress_ms
        self.play = cont
        return p

    def flush(self):
        """End the current play unconditionally (app shutdown). Returns it or None."""
        p = self.play
        self.play = None
        if p is None or (p.continuation and p.listened_s <= 0):
            return None
        return p


# ---------------------------------------------------------------------------
# Fate
# ---------------------------------------------------------------------------

def effective_threshold(threshold_s, duration_ms=0):
    """min(threshold, 80% of the track) so very short tracks can still clear."""
    try:
        t = float(threshold_s)
    except (TypeError, ValueError):
        t = 30.0
    if duration_ms and int(duration_ms) > 0:
        t = min(t, 0.8 * int(duration_ms) / 1000.0)
    return max(0.0, t)


def compute_fate(track_id, *, play=None, membership=None, auto_clean_enabled=True, kept_track_id=None,
                 queued=False, threshold_s=30, duration_ms=0, listened_s=0.0, extra_s=0.0,
                 kept_reason=None):
    """What will happen to the current track. ``extra_s`` projects listening since the last poll.

    Auto-clear judges membership at the start of the play: a track that joined the backlog
    while it was already playing is reported as ``kept`` (reason ``added_during_play``) and
    clears the next time it plays. ``reason`` says why a track is kept (``keep`` when the
    listener pressed Keep, ``auto_clean_resumed`` when auto-clean was switched back on after
    the threshold had already passed).
    """
    eff = effective_threshold(threshold_s, duration_ms)
    fate = {
        "state": "idle",
        "track_id": track_id,
        "listened_s": round(float(listened_s or 0.0), 1),
        "effective_threshold_s": round(eff, 1),
        "clears_in_s": None,
        "reason": None,
    }
    if not track_id:
        fate["listened_s"] = 0.0
        return fate
    if play is not None and play.track_id == track_id and play.cleared_reason:
        fate["state"] = "cleared"
    elif queued:
        fate["state"] = "queued"
    elif membership is None:
        fate["state"] = "syncing"
    elif membership is False:
        fate["state"] = "not_in_backlog"
    elif not auto_clean_enabled:
        fate["state"] = "clearing_paused"
    elif kept_track_id and kept_track_id == track_id:
        fate["state"] = "kept"
        fate["reason"] = kept_reason or "keep"
    elif play is not None and play.track_id == track_id and getattr(play, "added_during_play", False):
        fate["state"] = "kept"
        fate["reason"] = "added_during_play"
    else:
        fate["state"] = "in_backlog"
        fate["clears_in_s"] = round(max(0.0, eff - float(listened_s or 0.0) - max(0.0, extra_s)), 1)
    return fate


# ---------------------------------------------------------------------------
# Undo list
# ---------------------------------------------------------------------------

class RecentlyCleared:
    """The last 50 clears (in memory), newest first, each undoable for 30 minutes."""

    def __init__(self, maxlen=50, clock_ms=_now_ms, seq_start=None, undo_window_s=UNDO_WINDOW_S):
        self._lock = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self._clock_ms = clock_ms
        self._undo_window_ms = int(undo_window_s * 1000)
        # Seconds-based start keeps seq unique across restarts without persisting a counter.
        self._seq = itertools.count(int(seq_start if seq_start is not None else time.time()))

    def add(self, *, track_id, uri=None, name="", artists="", artist_list=None, image=None,
            reason="manual", listened_s=None, position=None, playlist_id=None, ts_ms=None,
            added_at=None, album=None, album_id=None, duration_ms=None):
        """added_at/album/album_id/duration_ms describe the backlog entry before the clear, so
        Undo can put back the same entry (Spotify stamps a re-added item with the current time)."""
        ts = int(ts_ms if ts_ms is not None else self._clock_ms())
        entry = {
            "seq": next(self._seq),
            "track_id": track_id,
            "uri": uri or (f"spotify:track:{track_id}" if track_id else None),
            "name": name or "",
            "artists": artists or "",
            "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in (artist_list or [])
                            if isinstance(a, dict)],
            "image": image,
            "reason": reason,
            "listened_s": None if listened_s is None else round(float(listened_s), 1),
            "ts_ms": ts,
            "position": position,
            "playlist_id": playlist_id,
            "added_at": added_at or None,
            "album": album or "",
            "album_id": album_id or None,
            "duration_ms": int(duration_ms or 0),
            "undone": False,
            "undo_expires_ms": ts + self._undo_window_ms,
        }
        with self._lock:
            self._items.appendleft(entry)
        return dict(entry)

    def list(self, limit=10):
        with self._lock:
            return [dict(e) for e in list(self._items)[: max(0, int(limit))]]

    def get(self, seq):
        with self._lock:
            for e in self._items:
                if e["seq"] == seq:
                    return dict(e)
        return None

    def mark_undone(self, seq):
        with self._lock:
            for e in self._items:
                if e["seq"] == seq:
                    e["undone"] = True
                    return dict(e)
        return None

    def latest_for(self, track_id):
        with self._lock:
            for e in self._items:
                if e["track_id"] == track_id:
                    return dict(e)
        return None


# ---------------------------------------------------------------------------
# Original "added" dates of tracks brought back with Undo
# ---------------------------------------------------------------------------

RESTORED_DATES_META_KEY = "restored_added_at"
MAX_RESTORED_DATES = 500
RESTORE_MATCH_MS = 15 * 60 * 1000   # Spotify's stamp on the re-added item is within minutes of the undo


class RestoredDates:
    """When Undo puts a track back, Spotify stamps the re-added item with the current time. The
    listener asked for the clear to be undone, so lists show the date it was *first* added.

    Each entry is {added_at, restored_ms}: the original date applies only to the copy Undo added
    (an ``added_at`` within RESTORE_MATCH_MS of the restore), never to a later, separate add.
    The index itself keeps Spotify's stamp (catch-up relies on it: plays before a restore must
    not count as "heard since added"). Persisted in app_meta when a connection factory is given.
    """

    def __init__(self, conn_factory=None, clock_ms=_now_ms):
        self._conn_factory = conn_factory
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._map = None

    def _load(self):
        if self._map is not None:
            return self._map
        self._map = {}
        if self._conn_factory is not None:
            try:
                conn = self._conn_factory()
                conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (RESTORED_DATES_META_KEY,)).fetchone()
                data = json.loads(row[0]) if row else {}
                if isinstance(data, dict):
                    self._map = {k: v for k, v in data.items()
                                 if isinstance(v, dict) and isinstance(v.get("added_at"), str)}
            except Exception:
                log.exception("restored dates unavailable")
        return self._map

    def _save(self):
        if self._conn_factory is None:
            return
        try:
            conn = self._conn_factory()
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                         (RESTORED_DATES_META_KEY, json.dumps(self._map, separators=(",", ":"))))
            conn.commit()
        except Exception:
            log.exception("restored dates not saved")

    def remember(self, track_id, added_at, restored_ms=None):
        if not track_id or not isinstance(added_at, str) or not added_at:
            return
        with self._lock:
            m = self._load()
            m.pop(track_id, None)   # re-insert: newest last, so trimming drops the oldest
            m[track_id] = {"added_at": added_at,
                           "restored_ms": int(restored_ms if restored_ms is not None else self._clock_ms())}
            while len(m) > MAX_RESTORED_DATES:
                m.pop(next(iter(m)))
            self._save()

    def display_added_at(self, track_id, added_at):
        """The date to show for an index entry: the original one for the copy Undo put back."""
        if not track_id:
            return added_at
        with self._lock:
            rec = self._load().get(track_id)
        if not rec:
            return added_at
        stamp = _iso_ms(added_at)
        if stamp is None or abs(stamp - int(rec.get("restored_ms") or 0)) > RESTORE_MATCH_MS:
            return added_at
        return rec["added_at"]


def _iso_ms(value):
    """Epoch ms of an ISO-8601 UTC timestamp ('2026-09-25T10:00:00Z'), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Pending removals (SQLite outbox)
# ---------------------------------------------------------------------------

class PendingRemovals:
    """Removals that failed (rate limit, network) and must still happen. Survives restarts."""

    def __init__(self, conn_factory, clock_ms=_now_ms):
        self._conn_factory = conn_factory
        self._clock_ms = clock_ms
        self._lock = threading.RLock()
        self._ensured = False
        self._ids = None  # in-memory mirror so the status poll never touches SQLite

    def _conn(self):
        conn = self._conn_factory()
        if not self._ensured:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_removals ("
                " track_id TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, reason TEXT,"
                " listened_s REAL, queued_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
                " last_error TEXT, next_try_ms INTEGER NOT NULL DEFAULT 0, track_json TEXT)"
            )
            conn.commit()
            self._ensured = True
        return conn

    def _load_ids(self):
        if self._ids is None:
            rows = self._conn().execute("SELECT track_id FROM pending_removals").fetchall()
            self._ids = {r[0] for r in rows}
        return self._ids

    def ids(self):
        with self._lock:
            try:
                return set(self._load_ids())
            except Exception:
                log.exception("pending_removals unavailable")
                return set()

    def count(self):
        return len(self.ids())

    def contains(self, track_id):
        return bool(track_id) and track_id in self.ids()

    @staticmethod
    def backoff_s(attempts):
        return min(300.0, 10.0 * (2 ** max(0, int(attempts) - 1)))

    def add(self, track_id, playlist_id, reason, listened_s=None, track=None, error=None, retry_after_s=None):
        now = self._clock_ms()
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT attempts FROM pending_removals WHERE track_id = ?", (track_id,)).fetchone()
            attempts = (int(row[0]) if row else 0) + 1
            wait_s = max(float(retry_after_s or 0), self.backoff_s(attempts))
            # A re-queue follows the newest request (the backlog may have been switched meanwhile);
            # an explicit request (manual, like, ...) is never downgraded to a cancellable 'threshold'.
            conn.execute(
                "INSERT INTO pending_removals (track_id, playlist_id, reason, listened_s, queued_ms, attempts,"
                " last_error, next_try_ms, track_json) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(track_id) DO UPDATE SET attempts=excluded.attempts, last_error=excluded.last_error,"
                " next_try_ms=excluded.next_try_ms, playlist_id=excluded.playlist_id,"
                " listened_s=COALESCE(excluded.listened_s, pending_removals.listened_s),"
                " reason=CASE WHEN COALESCE(pending_removals.reason, 'threshold') = 'threshold'"
                " THEN excluded.reason ELSE pending_removals.reason END",
                (track_id, playlist_id, reason, listened_s, now, attempts, error,
                 int(now + wait_s * 1000), json.dumps(track or {}, ensure_ascii=False)),
            )
            conn.commit()
            self._load_ids().add(track_id)

    def due(self, now_ms=None, limit=MAX_CATCHUP_IDS):
        now = self._clock_ms() if now_ms is None else now_ms
        with self._lock:
            if not self._load_ids():
                return []
            rows = self._conn().execute(
                "SELECT track_id, playlist_id, reason, listened_s, queued_ms, attempts, last_error, track_json"
                " FROM pending_removals WHERE next_try_ms <= ? ORDER BY queued_ms LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        out = []
        for r in rows:
            try:
                track = json.loads(r[7] or "{}")
            except Exception:
                track = {}
            out.append({"track_id": r[0], "playlist_id": r[1], "reason": r[2], "listened_s": r[3],
                        "queued_ms": r[4], "attempts": r[5], "last_error": r[6], "track": track})
        return out

    def failed(self, track_ids, error=None, retry_after_s=None, max_attempts=PENDING_MAX_ATTEMPTS,
               max_age_s=PENDING_MAX_AGE_S):
        """Schedule another try with backoff. Rows past the attempt or age cap are dropped
        instead; returns them as [{track_id, reason, attempts, queued_ms}] so they can be logged."""
        now = self._clock_ms()
        dropped = []
        with self._lock:
            conn = self._conn()
            for tid in track_ids:
                row = conn.execute("SELECT attempts, queued_ms, reason FROM pending_removals WHERE track_id = ?",
                                   (tid,)).fetchone()
                if not row:
                    continue
                attempts = int(row[0]) + 1
                if attempts >= max_attempts or now - int(row[1] or now) > max_age_s * 1000:
                    conn.execute("DELETE FROM pending_removals WHERE track_id = ?", (tid,))
                    dropped.append({"track_id": tid, "reason": row[2], "attempts": attempts,
                                    "queued_ms": row[1]})
                    continue
                wait_s = max(float(retry_after_s or 0), self.backoff_s(attempts))
                conn.execute(
                    "UPDATE pending_removals SET attempts = ?, last_error = ?, next_try_ms = ? WHERE track_id = ?",
                    (attempts, error, int(now + wait_s * 1000), tid),
                )
            conn.commit()
            if dropped:
                self._load_ids().difference_update(d["track_id"] for d in dropped)
        return dropped

    def cancel(self, track_id, reasons=("threshold",)):
        """Drop a queued removal whose reason is in ``reasons`` (Keep cancels an automatic clear,
        never an explicit request). Returns True when a row was removed."""
        if not track_id:
            return False
        with self._lock:
            if self._ids is not None and track_id not in self._ids:
                return False
            conn = self._conn()
            marks = ",".join("?" for _ in reasons)
            cur = conn.execute(
                f"DELETE FROM pending_removals WHERE track_id = ? AND COALESCE(reason, 'threshold') IN ({marks})",
                (track_id, *reasons))
            conn.commit()
            if cur.rowcount:
                self._load_ids().discard(track_id)
                return True
        return False

    def delete(self, track_ids):
        ids = [t for t in (track_ids or []) if t]
        if not ids:
            return
        with self._lock:
            if self._ids is not None and not (set(ids) & self._ids):
                return
            conn = self._conn()
            conn.executemany("DELETE FROM pending_removals WHERE track_id = ?", [(t,) for t in ids])
            conn.commit()
            self._load_ids().difference_update(ids)


# ---------------------------------------------------------------------------
# Engine (live orchestration)
# ---------------------------------------------------------------------------

class ClearError(Exception):
    """A clear/undo request that can't be honoured; carries the v2 error code and HTTP status."""

    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


def _retry_after_of(exc):
    ra = getattr(exc, "retry_after", None)
    if ra is None:
        try:
            headers = getattr(exc, "headers", None) or {}
            ra = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:
            ra = None
    try:
        return max(1, int(float(ra)))
    except (TypeError, ValueError):
        return None


def track_info_from_entry(entry, core=None):
    """App-style track_info (what compact_track_for_history expects) from an index entry."""
    entry = entry or {}
    tid = entry.get("track_id") or entry.get("id")
    genres = []
    if core is not None and tid:
        try:
            genres = core.get_cached_track_genres(tid) or []
        except Exception:
            genres = []
    return {
        "id": tid,
        "uri": entry.get("uri") or (f"spotify:track:{tid}" if tid else None),
        "name": entry.get("name") or "",
        "artists": entry.get("artists") or "",
        "artist_list": list(entry.get("artist_list") or []),
        "album": entry.get("album") or "",
        "album_id": entry.get("album_id"),
        "image": entry.get("image"),
        "url": entry.get("url") or (f"https://open.spotify.com/track/{tid}" if tid else None),
        "duration_ms": int(entry.get("duration_ms") or 0),
        "genres": genres,
    }


def track_info_from_spotify(tr):
    """App-style track_info from a raw Spotify track object."""
    tr = tr or {}
    album = tr.get("album") or {}
    images = album.get("images") or []
    artists = [a for a in (tr.get("artists") or []) if isinstance(a, dict)]
    return {
        "id": tr.get("id"),
        "uri": tr.get("uri") or (f"spotify:track:{tr.get('id')}" if tr.get("id") else None),
        "name": tr.get("name") or "",
        "artists": ", ".join(a.get("name") or "" for a in artists if a.get("name")),
        "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in artists],
        "album": album.get("name") or "",
        "album_id": album.get("id"),
        "image": (images[-1].get("url") if images else None),
        "url": (tr.get("external_urls") or {}).get("spotify"),
        "duration_ms": int(tr.get("duration_ms") or 0),
        "genres": [],
    }


class ClearingEngine:
    """Owns the per-play clearing state shared by the poll loop and the HTTP routes."""

    def __init__(self, index, pending=None, recent=None, poll_interval_s=3.0, auto_clean_enabled=True,
                 listen_min_s=10.0, restored_dates=None):
        self.index = index
        self.pending = pending
        self.recent = recent or RecentlyCleared()
        self.tracker = PlayTracker(poll_interval_s)
        self.listen_min_s = float(listen_min_s)
        self.wake = threading.Event()
        self.lock = threading.RLock()   # serialises clears/undo so one track is never removed twice
        self.kept_track_id = None
        self.kept_reason = None
        # Tracks kept during this session (id -> ms), so catch-up doesn't propose them.
        self._kept_ids = {}
        # Plays that passed the threshold while auto-clear was paused (id -> ms). Now told the
        # listener "this track stays", so catch-up must not propose them either.
        self._heard_paused_ids = {}
        self.restored_dates = restored_dates if restored_dates is not None else RestoredDates()
        self.auto_clean_enabled = bool(auto_clean_enabled)

    # ----- poll-loop plumbing -----
    def wait(self, timeout):
        """Sleep until the next poll; any route can cut it short with poke()."""
        self.wake.wait(max(0.0, float(timeout)))
        self.wake.clear()

    def poke(self):
        self.wake.set()

    def on_play_started(self, play):
        """Keep lasts until a different track starts: restarting the same track ('previous',
        scrub to 0:00, repeat-one) is still the listener's kept track."""
        with self.lock:
            if play is None or play.track_id != self.kept_track_id:
                self.kept_track_id = None
                self.kept_reason = None

    def pending_ids(self):
        if self.pending is None:
            return set()
        return self.pending.ids()

    def _membership(self, track_id):
        """Membership that automatic clears may act on (unknown while the index is unconfirmed)."""
        fn = getattr(self.index, "clearable", None) or self.index.contains
        return fn(track_id)

    def fate(self, track_id, *, threshold_s, duration_ms=0, listened_s=0.0, extra_s=0.0):
        play = self.tracker.play
        membership = self._membership(track_id) if track_id else False
        fate = compute_fate(
            track_id,
            play=play,
            membership=membership,
            auto_clean_enabled=self.auto_clean_enabled,
            kept_track_id=self.kept_track_id,
            kept_reason=self.kept_reason,
            queued=bool(track_id) and track_id in self.pending_ids(),
            threshold_s=threshold_s,
            duration_ms=duration_ms,
            listened_s=listened_s,
            extra_s=extra_s,
        )
        if (fate["state"] == "clearing_paused" and play is not None and play.track_id == track_id
                and float(listened_s or 0.0) >= effective_threshold(threshold_s, duration_ms)):
            self._remember_heard_paused(track_id)
        return fate

    def _remember_heard_paused(self, track_id):
        with self.lock:
            if track_id in self._heard_paused_ids:
                return
            self._heard_paused_ids[track_id] = _now_ms()
            if len(self._heard_paused_ids) > MAX_KEPT_MEMORY:
                self._heard_paused_ids.pop(next(iter(self._heard_paused_ids)))

    def heard_while_paused(self, track_id):
        """True when this session saw the track pass the threshold while auto-clear was paused."""
        with self.lock:
            return track_id in self._heard_paused_ids

    @staticmethod
    def auto_clear_allowed(play):
        """The threshold rule may act on this play: it was in the backlog when it started (or the
        listener un-kept it), and no removal is already queued or refused for good."""
        if play is None:
            return False
        started_ok = play.started_in_backlog is True or play.clear_override
        return started_ok and not play.removal_queued and play.removal_error is None

    def was_kept(self, track_id):
        with self.lock:
            return track_id in self._kept_ids

    def recently_restored(self, track_id, now_ms=None):
        """True when the listener brought this track back with Undo within the undo window."""
        entry = self.recent.latest_for(track_id)
        if not entry or not entry.get("undone"):
            return False
        now = _now_ms() if now_ms is None else now_ms
        return now <= int(entry.get("undo_expires_ms") or 0)

    def next_wait(self, default_s, fate, is_playing):
        """Wake just after the threshold instead of up to a full poll interval late."""
        if not is_playing or not fate or fate.get("state") != "in_backlog":
            return default_s
        remaining = fate.get("clears_in_s")
        if remaining is None or remaining <= 0:
            return default_s
        return min(default_s, remaining + 0.15)

    # ----- removals -----
    def clear_tracks(self, core, sp, tracks, reason, source, listened_s=None, playlist_id=None):
        """Remove tracks (one Spotify call), then log each exactly once. Raises on Spotify failure."""
        tracks = [t for t in (tracks or []) if t and t.get("id")]
        ids = []
        for t in tracks:
            if t["id"] not in ids:
                ids.append(t["id"])
        if not ids:
            return []
        pid = playlist_id or self.index.pid
        active = pid == self.index.pid
        with self.lock:
            # Callers check membership before waiting for this lock; whoever got here first may
            # already have removed a track. Re-check under the lock so a track is removed, logged,
            # counted and made undoable exactly once; the loser gets the winner's entry back.
            existing = {}
            gone = [tid for tid in ids if active and self.index.contains(tid) is False]
            for tid in gone:
                ids.remove(tid)
                prior = self.recent.latest_for(tid)
                if prior and not prior.get("undone"):
                    existing[tid] = prior
            if gone and self.pending is not None:
                try:
                    self.pending.delete(gone)   # nothing left to remove for these
                except Exception as e:
                    _log_quietly(core, e, "pending_removals delete")
            if not ids:
                return [existing[t["id"]] for t in tracks if t["id"] in existing]
            # Positions and metadata describe the active backlog only; for another playlist
            # (a queued removal after a switch) undo appends instead of guessing a position.
            positions = {tid: (self.index.position_of(tid) if active else None) for tid in ids}
            metas = {tid: ((self.index.entry(tid) or {}) if active else {}) for tid in ids}
            core.remove_from_playlist(sp, ids, pid, raise_errors=True)
            # The removal happened: mark the play first, so status never shows a gap and no
            # bookkeeping failure below can make it look like it failed (and queue it again).
            p = self.tracker.play
            if active and p is not None and p.track_id in ids:
                p.cleared_reason = reason
                p.removal_queued = False
                if reason == "threshold":
                    p.auto_removed = True
            out = []
            seen = set()
            for t in tracks:
                tid = t["id"]
                if tid in seen:
                    continue
                seen.add(tid)
                if tid not in ids:
                    if tid in existing:
                        out.append(existing[tid])
                    continue
                meta = metas.get(tid) or {}
                listened = listened_s.get(tid) if isinstance(listened_s, dict) else listened_s
                entry = self.recent.add(
                    track_id=tid,
                    uri=t.get("uri") or meta.get("uri"),
                    name=t.get("name") or meta.get("name"),
                    artists=t.get("artists") or meta.get("artists"),
                    artist_list=t.get("artist_list") or meta.get("artist_list"),
                    image=t.get("image") or meta.get("image"),
                    reason=reason,
                    listened_s=listened,
                    position=positions.get(tid),
                    playlist_id=pid,
                    # the backlog entry as it was, so Undo can put the same entry back
                    added_at=meta.get("added_at"),
                    album=meta.get("album") or (t.get("album") if isinstance(t.get("album"), str) else ""),
                    album_id=meta.get("album_id") or t.get("album_id"),
                    duration_ms=meta.get("duration_ms") or t.get("duration_ms"),
                )
                out.append(entry)
                try:
                    logged = core.note_recent_removal(
                        t, listened, reason, source,
                        extra={"playlist_id": pid, "position": positions.get(tid), "clear_seq": entry["seq"]},
                    )
                    if logged:
                        core.bump_stat("removed_total")
                except Exception as e:
                    _log_quietly(core, e, "clear bookkeeping")
            if self.pending is not None:
                try:
                    self.pending.delete(ids)
                except Exception as e:
                    _log_quietly(core, e, "pending_removals delete")
        return out

    def auto_clear(self, core, sp, play, track_info, threshold_s):
        """Threshold reached for a backlog track: remove once, or queue the removal for retry."""
        listened = round(play.total_listened_s, 1)
        pid = self.index.pid
        if not play.threshold_logged:
            play.threshold_logged = True
            _record_action(core, "THRESHOLD_REACHED", track_info, {
                "listened_s": listened, "threshold_s": threshold_s, "playlist_id": pid,
                "source": "auto-threshold",
            })
        try:
            entries = self.clear_tracks(core, sp, [track_info], "threshold", "auto-threshold",
                                        listened_s=listened, playlist_id=pid)
            return entries[0] if entries else None
        except Exception as e:
            retry_after = _retry_after_of(e)
            if _http_status_of(e) in PERMANENT_HTTP:
                # Retrying can't help (no write access, playlist gone): stop for this play.
                play.removal_error = str(e)
                core._log_app_error(e, context="auto-clear (Spotify refused the removal)")
                return None
            if self.pending is not None:
                try:
                    self.pending.add(track_info["id"], pid, "threshold", listened, track_info,
                                     error=str(e), retry_after_s=retry_after)
                    play.removal_queued = True
                except Exception as qe:
                    core._log_app_error(qe, context="pending_removals add")
            core._log_app_error(e, context="auto-clear")
            return None

    def drain_pending(self, core, sp, now_ms=None):
        """Retry queued removals whose backoff has passed (batched per playlist)."""
        if self.pending is None or not self.pending_ids():
            return 0
        rows = self.pending.due(now_ms)
        if not rows:
            return 0
        done = 0
        by_pid = {}
        now = self.pending._clock_ms() if now_ms is None else now_ms
        for row in rows:
            tid = row["track_id"]
            if now - int(row.get("queued_ms") or now) > PENDING_MAX_AGE_S * 1000:
                # Too old to still reflect what the listener wants (they may have re-added it).
                self.pending.delete([tid])
                _log_quietly(core, RuntimeError(f"queued removal of {tid} expired after 24 h"),
                             "pending_removals expiry")
                continue
            if not self.auto_clean_enabled and (row.get("reason") or "threshold") == "threshold":
                continue  # auto-clean was paused after this was queued; explicit clears still go
            if row["playlist_id"] == self.index.pid:
                member = self._membership(tid)
                if member is False:
                    # Already gone (removed elsewhere); nothing left to do.
                    self.pending.delete([tid])
                    continue
                if member is None:
                    continue
            by_pid.setdefault(row["playlist_id"], []).append(row)
        for pid, group in by_pid.items():
            tracks = []
            listened = {}
            reason_rows = {}
            for row in group:
                track = dict(row.get("track") or {})
                track["id"] = row["track_id"]
                tracks.append(track)
                listened[row["track_id"]] = row.get("listened_s")
                reason_rows.setdefault(row.get("reason") or "threshold", []).append(track)
            try:
                for reason, rtracks in reason_rows.items():
                    self.clear_tracks(core, sp, rtracks, reason, "retry-queue", listened_s=listened,
                                      playlist_id=pid)
                    done += len(rtracks)
            except Exception as e:
                ids = [r["track_id"] for r in group]
                if _http_status_of(e) in PERMANENT_HTTP:
                    # 400/403/404 won't change by waiting: give up now instead of every 5 minutes.
                    self.pending.delete(ids)
                    _log_quietly(core, e, f"queued removal dropped: Spotify refused it ({len(ids)} track(s))")
                    continue
                dropped = self.pending.failed(ids, error=str(e), retry_after_s=_retry_after_of(e))
                if dropped:
                    _log_quietly(core, e, f"queued removal dropped after {dropped[0]['attempts']} attempts "
                                          f"({len(dropped)} track(s))")
        return done

    def undo(self, core, sp, seq, now_ms=None):
        """Put a cleared track back at its original position. Raises ClearError."""
        now = _now_ms() if now_ms is None else now_ms
        with self.lock:
            entry = self.recent.get(seq)
            if entry is None:
                raise ClearError("unknown_seq", "There is no recent clear with that number.", 404)
            if entry["undone"]:
                raise ClearError("already_undone", "That clear was already undone.", 409)
            if now > entry["undo_expires_ms"]:
                raise ClearError("undo_expired", "Undo is only available for 30 minutes after a clear.", 410)
            pid = entry["playlist_id"] or self.index.pid
            active = pid == self.index.pid
            position = entry.get("position")
            if active and position is not None:
                position = min(int(position), self.index.total)
            elif not active:
                position = None   # another playlist's length is unknown here: append
            uri = entry.get("uri") or f"spotify:track:{entry['track_id']}"
            # Already back (re-added in Spotify, or a concurrent undo): adding again would duplicate it.
            already_back = active and self.index.contains(entry["track_id"]) is True
            if not already_back:
                try:
                    if position is None:
                        sp.playlist_add_items(pid, [uri])
                    else:
                        sp.playlist_add_items(pid, [uri], position=position)
                except Exception as e:
                    if _http_status_of(e) in PERMANENT_HTTP:
                        raise ClearError("restore_rejected",
                                         "Spotify refused to put the track back (the playlist may be gone "
                                         "or read-only). Nothing changed.", 409)
                    ra = _retry_after_of(e)
                    raise ClearError("spotify_unavailable",
                                     "Spotify didn't accept the restore. Nothing changed; try again shortly.",
                                     503, **({"retry_after": ra} if ra else {}))
                try:
                    from src.services import backlog_index
                    backlog_index.invalidate_playlist_cache(core, pid)
                except Exception:
                    pass
                idx_entry = {
                    # Spotify stamps a re-added item with the current time; catch-up relies on it.
                    # Lists show the original date instead (restored_dates).
                    "track_id": entry["track_id"], "uri": uri,
                    "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 1000.0)),
                    "name": entry.get("name") or "", "artists": entry.get("artists") or "",
                    "artist_list": entry.get("artist_list") or [], "album": entry.get("album") or "",
                    "album_id": entry.get("album_id"), "image": entry.get("image"),
                    "duration_ms": int(entry.get("duration_ms") or 0), "is_local": False,
                }
                self.index.apply_add(idx_entry, position, playlist_id=pid)
                if entry.get("added_at"):
                    try:
                        self.restored_dates.remember(entry["track_id"], entry["added_at"], now)
                    except Exception as e:
                        _log_quietly(core, e, "restored dates")
            restored = self.recent.mark_undone(seq) or dict(entry, undone=True)
            restored_track = {
                "id": entry["track_id"], "name": entry.get("name"), "artists": entry.get("artists"),
                "artist_list": entry.get("artist_list") or [], "image": entry.get("image"),
                "url": f"https://open.spotify.com/track/{entry['track_id']}",
            }
            extra = {"position": position, "playlist_id": pid, "clear_seq": seq}
            legacy_map = getattr(getattr(core, "_events", None), "_LEGACY_TYPE_MAP", {}) or {}
            if "restored" in legacy_map:
                # Legacy history + SQLite TRACK_RESTORED in one call.
                core.record_history_event("restored", track=restored_track, reason=entry.get("reason"),
                                          source="undo", extra=extra)
            else:
                _record_action(core, "TRACK_RESTORED", restored_track,
                               dict(extra, reason=entry.get("reason"), artists=entry.get("artists"),
                                    source="undo"))
            try:
                core.bump_stat("removed_total", -1)
            except Exception:
                pass
            p = self.tracker.play
            if p is not None and p.track_id == entry["track_id"]:
                # The listener asked for it back: don't clear it again during this play.
                p.cleared_reason = None
                p.auto_removed = False
                self.kept_track_id = entry["track_id"]
                self.kept_reason = "keep"
            if self.pending is not None:
                self.pending.delete([entry["track_id"]])
        self.poke()
        return restored

    def set_kept(self, track_id, kept=True, reason="keep"):
        """Keep (or un-keep) the current play. Keeping also cancels a queued automatic clear of
        it; un-keeping a track that joined the backlog mid-play lets this play clear it."""
        with self.lock:
            p = self.tracker.play
            is_play = p is not None and p.track_id == track_id
            if kept:
                self.kept_track_id = track_id
                self.kept_reason = reason
                if reason == "keep":
                    self._kept_ids[track_id] = _now_ms()
                    if len(self._kept_ids) > MAX_KEPT_MEMORY:
                        self._kept_ids.pop(next(iter(self._kept_ids)))
                elif reason == "auto_clean_resumed":
                    self._remember_heard_paused(track_id)
                if self.pending is not None and self.pending.cancel(track_id, ("threshold",)) and is_play:
                    p.removal_queued = False
                elif is_play and p.removal_queued and track_id not in self.pending_ids():
                    p.removal_queued = False
            else:
                if self.kept_track_id == track_id:
                    self.kept_track_id = None
                    self.kept_reason = None
                self._kept_ids.pop(track_id, None)
                if is_play and p.started_in_backlog is False:
                    p.clear_override = True
        self.poke()

    def set_auto_clean(self, core, enabled):
        """Switch auto-clean and persist it. Raises ClearError (nothing changed) when the setting
        can't be saved. Switching it on keeps the current play when its threshold already passed
        while clearing was paused, so the setting applies from the next play on."""
        enabled = bool(enabled)
        previous = self.auto_clean_enabled
        self.auto_clean_enabled = enabled
        try:
            saved = update_config_safe(core, lambda cfg: cfg.__setitem__("auto_clean_enabled", enabled))
        except Exception as e:
            _log_quietly(core, e, "auto-clean setting")
            saved = False
        if not saved:
            self.auto_clean_enabled = previous
            raise ClearError("config_write_failed",
                             "The setting couldn't be saved, so nothing changed. Try again.", 500)
        if enabled and not previous:
            p = self.tracker.play
            threshold_s = None
            try:
                with core.state_lock:
                    threshold_s = core.state.get("threshold_seconds")
            except Exception:
                threshold_s = None
            if (p is not None and threshold_s is not None and not p.cleared_reason
                    and p.total_listened_s >= effective_threshold(threshold_s, p.duration_ms)):
                self.set_kept(p.track_id, True, reason="auto_clean_resumed")
        self.poke()
        return enabled


def _log_quietly(core, exc, context):
    try:
        core._log_app_error(exc, context=context)
    except Exception:
        log.warning("%s: %s", context, exc)


def _record_action(core, event_name, track_info, payload):
    """Write an app-only SQLite event (no listen row). Constants fall back to their names."""
    if not getattr(core, "_SQLITE_ENABLED", False):
        return
    try:
        ev = core._events
        event_type = getattr(ev, event_name, event_name)
        track_info = track_info or {}
        ev.record_action(
            event_type,
            track_id=track_info.get("id"),
            track_name=track_info.get("name"),
            album=track_info.get("album"),
            image_url=track_info.get("image"),
            spotify_url=track_info.get("url"),
            payload={k: v for k, v in (payload or {}).items() if v is not None},
        )
    except Exception as e:
        try:
            core._log_app_error(e, context=f"record {event_name}")
        except Exception:
            pass


def update_config_safe(core, mutator):
    """Persist a config change via the platform's locked update_config.

    No load/save fallback: re-running the mutator outside the config lock could clobber a
    concurrent write. Returns False when nothing was written (e.g. the config is unreadable).
    """
    return core.update_config(mutator) is not None


# ---------- live singleton ----------
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            import app as core
            from src.services import backlog_index
            pending = None
            restored = RestoredDates()
            if getattr(core, "_SQLITE_ENABLED", False):
                pending = PendingRemovals(core._db.get_conn)
                restored = RestoredDates(core._db.get_conn)
            cfg = getattr(core, "_initial_cfg", None) or {}
            _ENGINE = ClearingEngine(
                backlog_index.get_index(),
                pending=pending,
                poll_interval_s=getattr(core, "POLL_INTERVAL", 3.0),
                auto_clean_enabled=cfg.get("auto_clean_enabled", True) is not False,
                listen_min_s=getattr(core, "LISTEN_EVENT_MIN_SECONDS", 10.0),
                restored_dates=restored,
            )
    return _ENGINE
