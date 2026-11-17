"""Listening accounting, fate and the clearing engine (WP-04), with a fake clock.

Nothing here imports app.py: the engine is driven with a small fake "core" and an
in-memory SQLite database for the pending-removals outbox.
"""

import sqlite3
import types
import unittest

from src.services.backlog_index import IndexSyncWorker, PlaylistIndex
from src.services.clearing import (
    ClearError,
    ClearingEngine,
    PendingRemovals,
    PlayTracker,
    RecentlyCleared,
    compute_fate,
    effective_threshold,
)

PID = "P" * 22
POLL = 2.0


class Poller:
    """Drives a PlayTracker like poll_loop does: fake monotonic + wall clocks."""

    def __init__(self, poll=POLL):
        self.tracker = PlayTracker(poll_interval_s=poll)
        self.mono = 100.0
        self.finished = []
        self.started = 0

    def poll(self, advance, tid, playing, progress_s, duration_s=200, membership=True):
        self.mono += advance
        fin, started = self.tracker.observe(self.mono, int(self.mono * 1000) + 1_700_000_000_000, tid, playing,
                                            int(progress_s * 1000), int(duration_s * 1000), membership=membership)
        if fin is not None:
            self.finished.append(fin)
        if started:
            self.started += 1
        return self.tracker.play

    def idle(self, advance):
        self.mono += advance
        fin = self.tracker.idle(self.mono)
        if fin is not None:
            self.finished.append(fin)
        return fin


class AccountingTests(unittest.TestCase):
    def test_steady_playback_credits_wall_time(self):
        p = Poller()
        p.poll(0, "a", True, 0.5)               # first poll ever: nothing known yet
        for i in range(1, 9):
            play = p.poll(2.0, "a", True, 0.5 + 2.0 * i)
        self.assertAlmostEqual(play.listened_s, 16.0, places=3)

    def test_slow_cycle_is_bounded_by_progress(self):
        # The old loop added POLL_INTERVAL per cycle and under-counted when cycles ran long;
        # a stalled cycle must not over-credit either.
        p = Poller()
        p.poll(0, "a", True, 1.0)
        play = p.poll(8.0, "a", True, 3.0)       # 8 s of wall time but only 2 s of progress
        self.assertAlmostEqual(play.listened_s, 3.5)  # 2 s progress + 1.5 s slack

    def test_long_gap_is_capped_at_three_poll_intervals(self):
        p = Poller()
        p.poll(0, "a", True, 1.0)
        play = p.poll(20.0, "a", True, 21.0)
        self.assertAlmostEqual(play.listened_s, 3 * POLL)

    def test_seek_forward_credits_only_wall_time(self):
        p = Poller()
        p.poll(0, "a", True, 1.0)
        play = p.poll(2.0, "a", True, 90.0)
        self.assertAlmostEqual(play.listened_s, 2.0)

    def test_paused_polls_credit_nothing_and_resume_credits_progress(self):
        p = Poller()
        p.poll(0, "a", True, 1.0)
        p.poll(2.0, "a", True, 3.0)
        p.poll(2.0, "a", False, 4.0)             # paused at some point in between
        base = p.tracker.play.listened_s
        p.poll(10.0, "a", False, 4.0)
        self.assertAlmostEqual(p.tracker.play.listened_s, base)
        play = p.poll(2.0, "a", True, 5.2)       # resumed ~1.2 s before this poll
        self.assertAlmostEqual(play.listened_s - base, 1.2, places=3)

    def test_track_change_credits_tail_and_head(self):
        p = Poller()
        p.poll(0, "a", True, 10.0, duration_s=40)
        p.poll(2.0, "a", True, 12.0, duration_s=40)
        p.poll(2.0, "a", True, 39.0, duration_s=40)   # seek near the end: +2 s wall
        # 'a' ends 1 s after the last poll; 'b' has played 0.8 s when the next poll lands.
        play_b = p.poll(2.0, "b", True, 0.8, duration_s=40)
        self.assertEqual(len(p.finished), 1)
        a = p.finished[0]
        self.assertEqual(a.track_id, "a")
        self.assertAlmostEqual(a.listened_s, 2.0 + 2.0 + 1.0, places=3)   # tail = min(1 s left, 2 - 0.8)
        self.assertAlmostEqual(play_b.listened_s, 0.8, places=3)
        self.assertEqual(play_b.started_ms, int(p.mono * 1000) + 1_700_000_000_000 - 800)

    def test_repeat_starts_a_new_play(self):
        p = Poller()
        p.poll(0, "a", True, 30.0, duration_s=40)
        first = p.poll(2.0, "a", True, 32.0, duration_s=40)
        second = p.poll(2.0, "a", True, 1.0, duration_s=40)   # progress jumped back under 5 s
        self.assertIsNot(first, second)
        self.assertEqual(p.finished, [first])
        self.assertEqual(p.started, 2)
        self.assertAlmostEqual(second.listened_s, 1.0)

    def test_small_seek_back_is_not_a_repeat(self):
        p = Poller()
        p.poll(0, "a", True, 6.0)
        p.poll(2.0, "a", True, 8.0)
        play = p.poll(2.0, "a", True, 4.0)      # jumped back 4 s: same play
        self.assertEqual(p.finished, [])
        self.assertIs(play, p.tracker.play)

    def test_long_pause_flushes_the_play_once(self):
        p = Poller()
        p.poll(0, "a", True, 1.0)
        for i in range(1, 6):
            p.poll(2.0, "a", True, 1.0 + 2 * i)
        p.poll(2.0, "a", False, 12.0)
        for _ in range(12):
            p.poll(10.0, "a", False, 12.0)
        self.assertEqual(len(p.finished), 1)
        flushed = p.finished[0]
        self.assertGreater(flushed.listened_s, 9.9)
        self.assertIsNotNone(flushed.started_ms)
        cont = p.tracker.play
        self.assertTrue(cont.continuation)
        self.assertAlmostEqual(cont.total_listened_s, flushed.listened_s)  # carried toward the threshold
        for _ in range(20):                        # still paused: no further flushes
            p.poll(10.0, "a", False, 12.0)
        self.assertEqual(len(p.finished), 1)
        p.poll(2.0, "a", True, 12.5)               # resume continues the same sitting
        self.assertIsNotNone(p.tracker.play.started_ms)
        self.assertEqual(p.started, 1)

    def test_idle_without_item_flushes_after_120s(self):
        p = Poller()
        p.poll(0, "a", True, 1.0)
        p.poll(2.0, "a", True, 3.0)
        self.assertIsNone(p.idle(60))
        self.assertIsNotNone(p.idle(61))

    def test_flush_on_shutdown(self):
        p = Poller()
        p.poll(0, "a", True, 1.0)
        p.poll(2.0, "a", True, 3.0)
        self.assertEqual(p.tracker.flush().track_id, "a")
        self.assertIsNone(p.tracker.flush())

    def test_membership_unknown_at_start_is_resolved_later(self):
        p = Poller()
        p.poll(0, "a", True, 1.0, membership=None)
        play = p.poll(2.0, "a", True, 3.0, membership=True)
        self.assertIs(play.started_in_backlog, True)


class FateTests(unittest.TestCase):
    def test_effective_threshold(self):
        self.assertEqual(effective_threshold(15, 200_000), 15)
        self.assertAlmostEqual(effective_threshold(15, 10_000), 8.0)
        self.assertEqual(effective_threshold(15, 0), 15)

    def test_states_and_precedence(self):
        base = dict(threshold_s=15, duration_ms=200_000, listened_s=5)
        self.assertEqual(compute_fate(None, **base)["state"], "idle")
        self.assertEqual(compute_fate("t", membership=None, **base)["state"], "syncing")
        self.assertEqual(compute_fate("t", membership=False, **base)["state"], "not_in_backlog")
        self.assertEqual(compute_fate("t", membership=True, auto_clean_enabled=False, **base)["state"],
                         "clearing_paused")
        self.assertEqual(compute_fate("t", membership=True, kept_track_id="t", **base)["state"], "kept")
        self.assertEqual(compute_fate("t", membership=True, queued=True, **base)["state"], "queued")
        f = compute_fate("t", membership=True, **base)
        self.assertEqual((f["state"], f["clears_in_s"], f["effective_threshold_s"]), ("in_backlog", 10.0, 15.0))
        f = compute_fate("t", membership=True, extra_s=4, **base)
        self.assertEqual(f["clears_in_s"], 6.0)
        play = types.SimpleNamespace(track_id="t", cleared_reason="threshold")
        self.assertEqual(compute_fate("t", play=play, membership=False, **base)["state"], "cleared")


class RecentlyClearedTests(unittest.TestCase):
    def test_newest_first_and_undo_window(self):
        clock = {"ms": 1_000_000}
        rc = RecentlyCleared(clock_ms=lambda: clock["ms"], seq_start=100)
        a = rc.add(track_id="a", reason="threshold", position=3, playlist_id=PID)
        clock["ms"] += 1000
        b = rc.add(track_id="b", reason="manual")
        self.assertEqual([e["seq"] for e in rc.list()], [b["seq"], a["seq"]])
        self.assertEqual(a["undo_expires_ms"], 1_000_000 + 30 * 60 * 1000)
        rc.mark_undone(a["seq"])
        self.assertTrue(rc.get(a["seq"])["undone"])
        self.assertIsNone(rc.get(999))


class PendingRemovalsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.now = {"ms": 5_000_000}
        self.store = PendingRemovals(lambda: self.conn, clock_ms=lambda: self.now["ms"])

    def test_lifecycle(self):
        self.assertEqual(self.store.count(), 0)
        self.store.add("t1", PID, "threshold", 15.2, {"id": "t1", "name": "x"}, error="429", retry_after_s=30)
        self.assertTrue(self.store.contains("t1"))
        self.assertEqual(self.store.due(), [])                   # respects retry_after
        self.now["ms"] += 31_000
        rows = self.store.due()
        self.assertEqual([r["track_id"] for r in rows], ["t1"])
        self.assertEqual(rows[0]["track"]["name"], "x")
        self.store.failed(["t1"], "again")
        self.assertEqual(self.store.due(), [])
        self.now["ms"] += 300_000
        self.assertEqual(self.store.due()[0]["attempts"], 2)
        self.store.delete(["t1"])
        self.assertEqual(self.store.count(), 0)
        # table persists across instances (restart)
        self.store.add("t2", PID, "manual")
        self.assertEqual(PendingRemovals(lambda: self.conn).ids(), {"t2"})


# ---------------------------------------------------------------------------
# Engine with a fake core
# ---------------------------------------------------------------------------

class FakeSp:
    def __init__(self, tracks):
        self.tracks = list(tracks)
        self.snapshot = 1
        self.remove_calls = []
        self.add_calls = []
        self.fail_with = None

    def playlist(self, pid, fields=None):
        return {"name": "B", "snapshot_id": f"s{self.snapshot}", "tracks": {"total": len(self.tracks)}}

    def playlist_items(self, pid, fields=None, limit=100, offset=0):
        chunk = self.tracks[offset: offset + limit]
        return {"items": [{"track": t} for t in chunk], "total": len(self.tracks),
                "next": "n" if offset + limit < len(self.tracks) else None}

    def playlist_remove_all_occurrences_of_items(self, pid, ids):
        if self.fail_with is not None:
            raise self.fail_with
        self.remove_calls.append(list(ids))
        self.tracks = [t for t in self.tracks if t["id"] not in set(ids)]
        self.snapshot += 1

    def playlist_add_items(self, pid, uris, position=None):
        self.add_calls.append((list(uris), position))
        tid = uris[0].split(":")[-1]
        self.tracks.insert(len(self.tracks) if position is None else position,
                           {"id": tid, "uri": uris[0], "type": "track", "name": tid, "artists": [], "album": {}})
        self.snapshot += 1


class RateLimited(Exception):
    http_status = 429
    retry_after = 20


def track(i):
    tid = f"trk{i:019d}"
    return {"id": tid, "uri": f"spotify:track:{tid}", "type": "track", "name": f"Song {i}",
            "artists": [{"id": "a1", "name": "Artist"}], "album": {"id": "al", "name": "Al", "images": []},
            "duration_ms": 200000}


class FakeCore:
    """Just the surface ClearingEngine uses."""

    _SQLITE_ENABLED = False
    _events = types.SimpleNamespace(_LEGACY_TYPE_MAP={"removed": "TRACK_REMOVED_FROM_QUEUE",
                                                      "restored": "TRACK_RESTORED"})

    def __init__(self, index):
        self.index = index
        self.removed_log = []
        self.stats = {"removed_total": 0}
        self.errors = []
        self.history = []

    def remove_from_playlist(self, sp, ids, pid, raise_errors=False):
        sp.playlist_remove_all_occurrences_of_items(pid, ids)
        self.index.apply_removal(ids, playlist_id=pid)
        return True

    def note_recent_removal(self, track_info, listened, reason, source, extra=None):
        self.removed_log.append((track_info["id"], reason, listened, extra))
        return True

    def bump_stat(self, key, n=1):
        self.stats[key] = self.stats.get(key, 0) + n

    def record_history_event(self, event_type, **kw):
        self.history.append((event_type, kw))

    def _log_app_error(self, e, context=""):
        self.errors.append((context, e))

    def get_cached_track_genres(self, tid):
        return []


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.sp = FakeSp([track(i) for i in range(250)])
        self.index = PlaylistIndex()
        self.index.switch(PID)
        IndexSyncWorker(self.index, get_sp=lambda: self.sp, sleep=lambda s: None).sync(self.sp)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.now = {"ms": 10_000_000}
        self.pending = PendingRemovals(lambda: self.conn, clock_ms=lambda: self.now["ms"])
        self.engine = ClearingEngine(self.index, pending=self.pending, poll_interval_s=POLL)
        self.core = FakeCore(self.index)
        self.core.index = self.index

    def play(self, i, seconds, start_progress=0.5):
        """Poll track i for `seconds` of playback; returns the live play."""
        t = track(i)
        mono = 1000.0
        tr = self.engine.tracker
        tr.observe(mono, 0, t["id"], True, int(start_progress * 1000), 200000,
                   membership=self.index.contains(t["id"]), track=t)
        steps = int(seconds / POLL)
        for k in range(1, steps + 1):
            mono += POLL
            tr.observe(mono, 0, t["id"], True, int((start_progress + POLL * k) * 1000), 200000,
                       membership=self.index.contains(t["id"]), track=t)
        return tr.play, t

    def test_auto_clear_once_per_play(self):
        play, t = self.play(200, 16)
        fate = self.engine.fate(t["id"], threshold_s=15, duration_ms=200000, listened_s=play.total_listened_s)
        self.assertEqual(fate["state"], "in_backlog")
        pos = self.index.position_of(t["id"])
        entry = self.engine.auto_clear(self.core, self.sp, play, t, 15)
        self.assertEqual(entry["position"], pos)
        self.assertEqual(entry["reason"], "threshold")
        self.assertTrue(play.auto_removed)
        self.assertIs(self.index.contains(t["id"]), False)
        self.assertEqual(self.core.stats["removed_total"], 1)
        fate = self.engine.fate(t["id"], threshold_s=15, duration_ms=200000, listened_s=play.total_listened_s)
        self.assertEqual(fate["state"], "cleared")
        self.assertEqual(len(self.sp.remove_calls), 1)

    def test_failed_removal_is_queued_then_drained(self):
        play, t = self.play(10, 16)
        self.sp.fail_with = RateLimited()
        self.assertIsNone(self.engine.auto_clear(self.core, self.sp, play, t, 15))
        self.assertEqual(self.pending.ids(), {t["id"]})
        self.assertFalse(play.auto_removed)
        self.assertEqual(self.engine.fate(t["id"], threshold_s=15, listened_s=16)["state"], "queued")
        self.assertIs(self.index.contains(t["id"]), True)       # not removed yet
        self.sp.fail_with = None
        self.assertEqual(self.engine.drain_pending(self.core, self.sp), 0)   # backoff not over
        self.now["ms"] += 25_000
        self.assertEqual(self.engine.drain_pending(self.core, self.sp), 1)
        self.assertEqual(self.pending.count(), 0)
        self.assertTrue(play.auto_removed)                      # still the current play
        self.assertIs(self.index.contains(t["id"]), False)
        self.assertEqual(self.core.stats["removed_total"], 1)

    def test_kept_blocks_until_next_play(self):
        play, t = self.play(3, 4)
        self.engine.set_kept(t["id"])
        self.assertEqual(self.engine.fate(t["id"], threshold_s=15, listened_s=20)["state"], "kept")
        self.engine.on_play_started(None)
        self.assertEqual(self.engine.fate(t["id"], threshold_s=15, listened_s=20)["state"], "in_backlog")

    def test_catch_up_is_one_call_and_undoable(self):
        tracks = [dict(track(i)) for i in range(20, 30)]
        entries = self.engine.clear_tracks(self.core, self.sp, tracks, "catch-up", "manual-catch-up")
        self.assertEqual(len(self.sp.remove_calls), 1)
        self.assertEqual(len(self.sp.remove_calls[0]), 10)
        self.assertEqual(len(entries), 10)
        self.assertEqual(len({e["seq"] for e in entries}), 10)
        self.assertEqual(self.index.status()["total"], 240)

    def test_undo_restores_original_position(self):
        t = track(123)
        entry = self.engine.clear_tracks(self.core, self.sp, [t], "manual", "manual-clear")[0]
        self.assertEqual(entry["position"], 123)
        restored = self.engine.undo(self.core, self.sp, entry["seq"], now_ms=entry["ts_ms"] + 1000)
        self.assertTrue(restored["undone"])
        self.assertEqual(self.sp.add_calls, [([t["uri"]], 123)])
        self.assertEqual(self.index.position_of(t["id"]), 123)
        self.assertEqual(self.index.status()["total"], 250)
        self.assertEqual(self.core.stats["removed_total"], 0)
        self.assertEqual(self.core.history[-1][0], "restored")
        with self.assertRaises(ClearError) as ctx:
            self.engine.undo(self.core, self.sp, entry["seq"])
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ClearError) as ctx:
            self.engine.undo(self.core, self.sp, 424242)
        self.assertEqual(ctx.exception.status, 404)
        e2 = self.engine.clear_tracks(self.core, self.sp, [track(5)], "manual", "manual-clear")[0]
        with self.assertRaises(ClearError) as ctx:
            self.engine.undo(self.core, self.sp, e2["seq"], now_ms=e2["ts_ms"] + 31 * 60 * 1000)
        self.assertEqual(ctx.exception.status, 410)

    def test_next_wait_wakes_right_after_the_threshold(self):
        fate = {"state": "in_backlog", "clears_in_s": 0.7}
        self.assertAlmostEqual(self.engine.next_wait(2.0, fate, True), 0.85)
        self.assertEqual(self.engine.next_wait(2.0, fate, False), 2.0)
        self.assertEqual(self.engine.next_wait(2.0, {"state": "kept"}, True), 2.0)


class LegacyTypeMapTests(unittest.TestCase):
    def test_event_constants_exist(self):
        from src.persistence import events
        self.assertEqual(events.THRESHOLD_REACHED, "THRESHOLD_REACHED")
        self.assertEqual(events.TRACK_RESTORED, "TRACK_RESTORED")


if __name__ == "__main__":
    unittest.main()
