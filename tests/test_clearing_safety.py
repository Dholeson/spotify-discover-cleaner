"""Clearing-engine safety regressions (from the clearing safety review).

Each test reproduces one reviewer scenario with fakes: an in-memory index synced from a fake
playlist, an in-memory SQLite outbox and a fake "core". Nothing here touches real data.
"""

import os
import sqlite3
import sys
import threading
import time
import types
import unittest
from unittest import mock

from src.services.backlog_index import IndexSyncWorker, PlaylistIndex
from src.services.clearing import (
    PENDING_MAX_AGE_S,
    ClearError,
    ClearingEngine,
    PendingRemovals,
    PlayTracker,
)

PID = "P" * 22
OTHER_PID = "Q" * 22
POLL = 2.0
ADDED_AT = "2026-09-20T12:00:00Z"


def track(i):
    tid = f"trk{i:019d}"
    return {"id": tid, "uri": f"spotify:track:{tid}", "type": "track", "name": f"Song {i}",
            "artists": [{"id": "a1", "name": "Artist"}], "album": {"id": "al", "name": "Al", "images": []},
            "duration_ms": 200000}


class HttpError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"http {status}")
        self.http_status = status
        self.retry_after = retry_after


class FakeSp:
    def __init__(self, tracks):
        self.tracks = list(tracks)
        self.snapshot = 1
        self.remove_calls = []
        self.add_calls = []
        self.fail_with = None
        self.add_fail_with = None
        self.remove_delay_s = 0.0
        self.recent_items = []

    def playlist(self, pid, fields=None):
        return {"name": "B", "snapshot_id": f"s{self.snapshot}", "tracks": {"total": len(self.tracks)}}

    def playlist_items(self, pid, fields=None, limit=100, offset=0):
        chunk = self.tracks[offset: offset + limit]
        return {"items": [{"track": t, "added_at": ADDED_AT} for t in chunk], "total": len(self.tracks),
                "next": "n" if offset + limit < len(self.tracks) else None}

    def playlist_remove_all_occurrences_of_items(self, pid, ids):
        if self.remove_delay_s:
            time.sleep(self.remove_delay_s)
        if self.fail_with is not None:
            raise self.fail_with
        self.remove_calls.append((pid, list(ids)))
        self.tracks = [t for t in self.tracks if t["id"] not in set(ids)]
        self.snapshot += 1

    def playlist_add_items(self, pid, uris, position=None):
        if self.add_fail_with is not None:
            raise self.add_fail_with
        self.add_calls.append((pid, list(uris), position))
        tid = uris[0].split(":")[-1]
        self.tracks.insert(len(self.tracks) if position is None else position,
                           {"id": tid, "uri": uris[0], "type": "track", "name": tid, "artists": [], "album": {}})
        self.snapshot += 1

    def current_user_recently_played(self, limit=50):
        return {"items": list(self.recent_items)}


class FakeCore:
    _SQLITE_ENABLED = False
    _events = types.SimpleNamespace(_LEGACY_TYPE_MAP={"removed": "TRACK_REMOVED_FROM_QUEUE",
                                                      "restored": "TRACK_RESTORED"})

    def __init__(self, index):
        self.index = index
        self.removed_log = []
        self.stats = {"removed_total": 0}
        self.errors = []
        self.history = []
        self.note_fails = False
        self.config = {}
        self.config_ok = True
        self.state = {"threshold_seconds": 15, "current_track_id": None}
        self.state_lock = threading.Lock()

    def remove_from_playlist(self, sp, ids, pid, raise_errors=False):
        sp.playlist_remove_all_occurrences_of_items(pid, ids)
        if pid == self.index.pid:
            self.index.apply_removal(ids, playlist_id=pid)
        return True

    def note_recent_removal(self, track_info, listened, reason, source, extra=None):
        if self.note_fails:
            raise sqlite3.OperationalError("database is locked")
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

    def update_config(self, mutator):
        if not self.config_ok:
            return None
        mutator(self.config)
        return self.config


class RateLimited(Exception):
    http_status = 429
    retry_after = 20


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.sp = FakeSp([track(i) for i in range(50)])
        self.index = PlaylistIndex()
        self.index.switch(PID)
        IndexSyncWorker(self.index, get_sp=lambda: self.sp, sleep=lambda s: None).sync(self.sp)
        self.assertTrue(self.index.status()["complete"])
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.now = {"ms": 10_000_000}
        self.pending = PendingRemovals(lambda: self.conn, clock_ms=lambda: self.now["ms"])
        self.engine = ClearingEngine(self.index, pending=self.pending, poll_interval_s=POLL)
        self.core = FakeCore(self.index)
        self.mono = 1000.0

    def observe(self, t, progress_s, playing=True, advance=POLL, membership="live"):
        """One poll of track t, like poll_loop does (membership from the index by default)."""
        self.mono += advance
        if membership == "live":
            membership = self.index.contains(t["id"])
        tr = self.engine.tracker
        fin, started = tr.observe(self.mono, int(self.mono * 1000), t["id"], playing, int(progress_s * 1000),
                                  200000, membership=membership, track=t)
        if started:
            self.engine.on_play_started(tr.play)
        return tr.play

    def play_for(self, t, seconds, start=0.5):
        self.observe(t, start, advance=0)
        steps = int(seconds / POLL)
        for k in range(1, steps + 1):
            self.observe(t, start + POLL * k)
        return self.engine.tracker.play

    def fate(self, t, listened=None):
        play = self.engine.tracker.play
        return self.engine.fate(t["id"], threshold_s=15, duration_ms=200000,
                                listened_s=play.total_listened_s if listened is None else listened)

    def poll_decision(self, t):
        """poll_loop's auto-clear rule; returns the entry when it cleared."""
        play = self.engine.tracker.play
        fate = self.fate(t)
        if (fate["state"] == "in_backlog" and self.engine.auto_clear_allowed(play)
                and play.total_listened_s >= 15):
            return self.engine.auto_clear(self.core, self.sp, play, t, 15)
        return None


# ---------------------------------------------------------------------------
# 1. A track added to the backlog mid-play must not be removed in that play
# ---------------------------------------------------------------------------

class AddedDuringPlayTests(EngineCase):
    def add_to_backlog(self, t):
        self.sp.tracks.append(t)
        self.index.apply_add({"track_id": t["id"], "uri": t["uri"], "added_at": ADDED_AT, "name": t["name"],
                              "artists": "Artist", "artist_list": [], "album": "", "album_id": None,
                              "image": None, "duration_ms": 200000, "is_local": False}, None, playlist_id=PID)

    def test_track_added_during_play_is_kept_not_removed(self):
        t = track(900)
        play = self.play_for(t, 20)
        self.assertIs(play.started_in_backlog, False)
        self.assertEqual(self.fate(t)["state"], "not_in_backlog")
        self.add_to_backlog(t)                     # user adds it in Spotify; the index learns it
        self.observe(t, 22.5)
        self.assertIs(self.index.contains(t["id"]), True)
        fate = self.fate(t)
        self.assertEqual((fate["state"], fate["reason"]), ("kept", "added_during_play"))
        self.assertIsNone(self.poll_decision(t))
        self.assertEqual(self.sp.remove_calls, [])
        self.assertIn(t, self.sp.tracks)

    def test_it_clears_the_next_time_it_plays(self):
        t = track(901)
        self.play_for(t, 6)
        self.add_to_backlog(t)
        other = track(3)
        self.observe(other, 1.0)                   # a different track in between
        play = self.play_for(t, 16)
        self.assertIs(play.started_in_backlog, True)
        self.assertEqual(self.fate(t)["state"], "in_backlog")
        self.assertIsNotNone(self.poll_decision(t))
        self.assertEqual(len(self.sp.remove_calls), 1)

    def test_unkeep_lets_this_play_clear(self):
        t = track(902)
        self.play_for(t, 4)
        self.add_to_backlog(t)
        self.play_for_more(t, 16)
        self.assertEqual(self.fate(t)["state"], "kept")
        self.engine.set_kept(t["id"], False)       # the player's Keep toggle pressed again
        self.assertEqual(self.fate(t)["state"], "in_backlog")
        self.assertIsNotNone(self.poll_decision(t))

    def play_for_more(self, t, seconds):
        play = self.engine.tracker.play
        base = play.last_progress_ms / 1000.0
        for k in range(1, int(seconds / POLL) + 1):
            self.observe(t, base + POLL * k)

    def test_unknown_at_start_then_member_still_clears(self):
        t = track(7)                               # in the backlog, but the index was syncing
        self.observe(t, 0.5, advance=0, membership=None)
        self.observe(t, 2.5, membership=None)
        for k in range(2, 10):
            self.observe(t, 0.5 + POLL * k)
        self.assertIs(self.engine.tracker.play.started_in_backlog, True)
        self.assertIsNotNone(self.poll_decision(t))

    def test_continuation_after_long_pause_keeps_start_membership(self):
        t = track(903)
        self.play_for(t, 10)
        self.observe(t, 10.5, playing=False)
        self.add_to_backlog(t)
        for _ in range(14):                        # > 120 s paused: the play is flushed
            self.observe(t, 10.5, playing=False, advance=10)
        cont = self.engine.tracker.play
        self.assertTrue(cont.continuation)
        self.assertIs(cont.started_in_backlog, False)
        for k in range(1, 6):
            self.observe(t, 10.5 + POLL * k)
        self.assertEqual(self.fate(t)["state"], "kept")
        self.assertIsNone(self.poll_decision(t))


# ---------------------------------------------------------------------------
# 2. Listening credit: frozen progress and mid-track starts
# ---------------------------------------------------------------------------

class CreditTests(unittest.TestCase):
    def test_frozen_progress_while_playing_earns_nothing(self):
        tr = PlayTracker(poll_interval_s=POLL)
        mono = 100.0
        tr.observe(mono, 0, "a", True, 20000, 200000, membership=True)
        tr.observe(mono + 2, 0, "a", True, 22000, 200000, membership=True)
        base = tr.play.listened_s
        for i in range(20):
            mono += 2 if i else 4
            tr.observe(mono, 0, "a", True, 22000, 200000, membership=True)
        self.assertAlmostEqual(tr.play.listened_s, base, places=3)

    def test_crawling_progress_earns_at_most_twice_the_progress(self):
        tr = PlayTracker(poll_interval_s=POLL)
        tr.observe(100.0, 0, "a", True, 20000, 200000)
        tr.observe(102.0, 0, "a", True, 20200, 200000)    # 0.2 s of progress in 2 s
        self.assertAlmostEqual(tr.play.listened_s, 0.4, places=3)

    def test_mid_track_start_earns_no_head_credit(self):
        tr = PlayTracker(poll_interval_s=POLL)
        tr.observe(100.0, 0, "a", True, 50000, 200000)
        # after a paused stretch, 'b' shows up already 60 s in (resumed session / scrub)
        tr.observe(100.0 + 30, 0, "b", True, 60000, 200000)
        self.assertEqual(tr.play.track_id, "b")
        self.assertEqual(tr.play.listened_s, 0.0)
        tr.observe(132.0, 0, "b", True, 62000, 200000)    # then credited as it advances
        self.assertAlmostEqual(tr.play.listened_s, 2.0, places=3)

    def test_head_credit_when_it_fits_the_gap(self):
        tr = PlayTracker(poll_interval_s=POLL)
        tr.observe(100.0, 0, "a", True, 50000, 200000)
        tr.observe(102.0, 0, "b", True, 800, 200000)
        self.assertAlmostEqual(tr.play.listened_s, 0.8, places=3)


# ---------------------------------------------------------------------------
# 3. Two clears racing for one track remove it once
# ---------------------------------------------------------------------------

class ConcurrentClearTests(EngineCase):
    def test_two_threads_remove_once_log_once(self):
        t = track(11)
        self.sp.remove_delay_s = 0.15
        results = []
        barrier = threading.Barrier(2)

        def clear(reason):
            barrier.wait()
            results.append(self.engine.clear_tracks(self.core, self.sp, [t], reason, reason))

        threads = [threading.Thread(target=clear, args=(r,)) for r in ("threshold", "manual")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(self.sp.remove_calls), 1)
        self.assertEqual(len(self.core.removed_log), 1)
        self.assertEqual(self.core.stats["removed_total"], 1)
        self.assertEqual(len(self.engine.recent.list(50)), 1)
        seqs = {r[0]["seq"] for r in results}
        self.assertEqual(len(seqs), 1)             # the loser got the winner's entry back

    def test_undo_of_an_already_restored_track_does_not_duplicate_it(self):
        t = track(12)
        entry = self.engine.clear_tracks(self.core, self.sp, [t], "manual", "manual")[0]
        # the listener re-added it in Spotify, and the index saw it
        self.sp.tracks.append(t)
        self.index.apply_add({"track_id": t["id"], "uri": t["uri"], "added_at": ADDED_AT, "name": "x",
                              "artists": "", "artist_list": [], "album": "", "album_id": None, "image": None,
                              "duration_ms": 0, "is_local": False}, None, playlist_id=PID)
        restored = self.engine.undo(self.core, self.sp, entry["seq"], now_ms=entry["ts_ms"] + 1000)
        self.assertTrue(restored["undone"])
        self.assertEqual(self.sp.add_calls, [])
        self.assertEqual(sum(1 for x in self.sp.tracks if x["id"] == t["id"]), 1)

    def test_undo_keeps_the_original_added_date_and_details(self):
        t = track(15)
        before = self.index.entry(t["id"])
        entry = self.engine.clear_tracks(self.core, self.sp, [t], "manual", "manual")[0]
        self.assertEqual(entry["added_at"], ADDED_AT)
        now = entry["ts_ms"] + 1000
        self.engine.undo(self.core, self.sp, entry["seq"], now_ms=now)
        back = self.index.entry(t["id"])
        # the index keeps Spotify's stamp (catch-up relies on it), album and length come back
        self.assertNotEqual(back["added_at"], ADDED_AT)
        self.assertEqual((back["album"], back["duration_ms"]), (before["album"], before["duration_ms"]))
        # lists show the date it was first added, for the copy Undo put back only
        dates = self.engine.restored_dates
        self.assertEqual(dates.display_added_at(t["id"], back["added_at"]), ADDED_AT)
        self.assertEqual(dates.display_added_at(t["id"], "2027-01-01T00:00:00Z"), "2027-01-01T00:00:00Z")
        self.assertEqual(dates.display_added_at(track(16)["id"], ADDED_AT), ADDED_AT)
        from src.web.backlog_api import _entry_row
        self.assertEqual(_entry_row(3, back, dates)["added_at"], ADDED_AT)

    def test_restored_dates_survive_a_restart(self):
        from src.services.clearing import RestoredDates
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        RestoredDates(lambda: conn).remember("t1", ADDED_AT, restored_ms=1_800_000_000_000)
        again = RestoredDates(lambda: conn)
        self.assertEqual(again.display_added_at("t1", "2027-01-15T08:01:00Z"), ADDED_AT)

    def test_undo_for_a_previous_playlist_appends(self):
        t = track(13)
        entry = self.engine.clear_tracks(self.core, self.sp, [t], "manual", "manual", playlist_id=OTHER_PID)[0]
        self.assertIsNone(entry["position"])
        self.engine.undo(self.core, self.sp, entry["seq"], now_ms=entry["ts_ms"] + 1000)
        self.assertEqual(self.sp.add_calls, [(OTHER_PID, [t["uri"]], None)])

    def test_undo_rejected_for_good_is_a_409(self):
        t = track(14)
        entry = self.engine.clear_tracks(self.core, self.sp, [t], "manual", "manual")[0]
        self.sp.add_fail_with = HttpError(403)
        with self.assertRaises(ClearError) as ctx:
            self.engine.undo(self.core, self.sp, entry["seq"], now_ms=entry["ts_ms"] + 1000)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "restore_rejected"))
        self.assertFalse(self.engine.recent.get(entry["seq"])["undone"])


# ---------------------------------------------------------------------------
# 4. Keep vs queued removals; auto-clean off stops queued automatic clears
# ---------------------------------------------------------------------------

class QueuedRemovalTests(EngineCase):
    def queue_threshold(self, t):
        play = self.play_for(t, 16)
        self.sp.fail_with = RateLimited()
        self.assertIsNone(self.poll_decision(t))
        self.sp.fail_with = None
        self.assertEqual(self.fate(t)["state"], "queued")
        return play

    def test_keep_cancels_a_queued_automatic_clear(self):
        t = track(20)
        play = self.queue_threshold(t)
        self.engine.set_kept(t["id"], True)
        self.assertEqual(self.fate(t)["state"], "kept")
        self.assertFalse(play.removal_queued)
        self.now["ms"] += 10 * 60 * 1000
        self.assertEqual(self.engine.drain_pending(self.core, self.sp), 0)
        self.assertEqual(self.sp.remove_calls, [])
        self.assertIn(t["id"], [x["id"] for x in self.sp.tracks])

    def test_keep_does_not_cancel_an_explicit_clear(self):
        t = track(21)
        self.pending.add(t["id"], PID, "manual", 3.0, t)
        self.engine.set_kept(t["id"], True)
        self.assertEqual(self.pending.ids(), {t["id"]})

    def test_paused_auto_clean_holds_queued_threshold_rows(self):
        t = track(22)
        self.queue_threshold(t)
        self.engine.auto_clean_enabled = False
        self.now["ms"] += 10 * 60 * 1000
        self.assertEqual(self.engine.drain_pending(self.core, self.sp), 0)
        self.assertEqual(self.sp.remove_calls, [])
        self.engine.auto_clean_enabled = True
        self.assertEqual(self.engine.drain_pending(self.core, self.sp), 1)

    def test_queued_play_is_not_resent_every_poll(self):
        t = track(23)
        play = self.queue_threshold(t)
        self.pending.delete([t["id"]])               # e.g. dropped as permanently failing
        self.assertEqual(self.fate(t)["state"], "in_backlog")
        self.assertFalse(self.engine.auto_clear_allowed(play))
        self.assertIsNone(self.poll_decision(t))

    def test_permanent_error_is_not_queued_or_retried(self):
        t = track(24)
        play = self.play_for(t, 16)
        self.sp.fail_with = HttpError(403)
        self.assertIsNone(self.poll_decision(t))
        self.assertEqual(self.pending.ids(), set())
        self.assertIsNotNone(play.removal_error)
        self.assertIsNone(self.poll_decision(t))     # no retry on the next poll

    def test_drain_drops_rows_spotify_refuses_for_good(self):
        t = track(25)
        self.pending.add(t["id"], PID, "manual", 3.0, t)
        self.now["ms"] += 60_000
        self.sp.fail_with = HttpError(404)
        self.engine.drain_pending(self.core, self.sp)
        self.assertEqual(self.pending.ids(), set())

    def test_drain_expires_old_rows_and_caps_attempts(self):
        t, u = track(26), track(27)
        self.pending.add(t["id"], PID, "manual", 3.0, t)
        self.now["ms"] += (PENDING_MAX_AGE_S + 60) * 1000
        self.assertEqual(self.engine.drain_pending(self.core, self.sp), 0)
        self.assertEqual(self.pending.ids(), set())
        self.assertEqual(self.sp.remove_calls, [])
        self.pending.add(u["id"], PID, "manual", 3.0, u)
        dropped = self.pending.failed([u["id"]], "5xx", max_attempts=3)
        self.assertEqual(dropped, [])
        dropped = self.pending.failed([u["id"]], "5xx", max_attempts=3)
        self.assertEqual([d["track_id"] for d in dropped], [u["id"]])
        self.assertEqual(self.pending.ids(), set())

    def test_requeue_follows_the_newest_playlist(self):
        t = track(28)
        self.pending.add(t["id"], OTHER_PID, "threshold", 16.0, t)
        self.pending.add(t["id"], PID, "manual", None, t)
        row = self.conn.execute("SELECT playlist_id, reason, listened_s FROM pending_removals").fetchone()
        self.assertEqual(row, (PID, "manual", 16.0))


# ---------------------------------------------------------------------------
# 5. Keep survives a restart of the same track; auto-clean re-enabled mid-play
# ---------------------------------------------------------------------------

class KeepTests(EngineCase):
    def test_keep_survives_previous_or_scrub_to_start(self):
        t = track(30)
        self.play_for(t, 10)
        self.engine.set_kept(t["id"], True)
        self.observe(t, 0.8)                         # restarted: a new play of the same track
        self.assertEqual(self.engine.tracker.play.listened_s, 0.8)
        for k in range(1, 10):
            self.observe(t, 0.8 + POLL * k)
        self.assertEqual(self.fate(t)["state"], "kept")
        self.assertIsNone(self.poll_decision(t))
        self.observe(track(31), 1.0)                 # a different track resets Keep
        self.assertIsNone(self.engine.kept_track_id)

    def test_reenabling_auto_clean_past_the_threshold_keeps_this_play(self):
        t = track(32)
        self.engine.set_auto_clean(self.core, False)
        self.play_for(t, 24)
        self.assertEqual(self.fate(t)["state"], "clearing_paused")
        self.assertIs(self.engine.set_auto_clean(self.core, True), True)
        self.assertEqual(self.core.config["auto_clean_enabled"], True)
        fate = self.fate(t)
        self.assertEqual((fate["state"], fate["reason"]), ("kept", "auto_clean_resumed"))
        self.assertIsNone(self.poll_decision(t))
        self.assertFalse(self.engine.was_kept(t["id"]))  # not a listener Keep...
        self.assertTrue(self.engine.heard_while_paused(t["id"]))  # ...but Now said it stays: no catch-up

    def test_play_past_the_threshold_while_paused_is_remembered(self):
        t, short = track(34), track(35)
        self.engine.set_auto_clean(self.core, False)
        self.play_for(t, 24)
        self.assertEqual(self.fate(t)["state"], "clearing_paused")
        self.assertTrue(self.engine.heard_while_paused(t["id"]))
        self.play_for(short, 6)                       # skipped before the threshold: not "heard"
        self.fate(short)
        self.assertFalse(self.engine.heard_while_paused(short["id"]))

    def test_reenabling_before_the_threshold_clears_normally(self):
        t = track(33)
        self.engine.set_auto_clean(self.core, False)
        self.play_for(t, 6)
        self.engine.set_auto_clean(self.core, True)
        for k in range(1, 7):
            self.observe(t, 6.5 + POLL * k)
        self.assertIsNotNone(self.poll_decision(t))

    def test_unsaved_setting_changes_nothing(self):
        self.core.config_ok = False
        with self.assertRaises(ClearError) as ctx:
            self.engine.set_auto_clean(self.core, False)
        self.assertEqual(ctx.exception.status, 500)
        self.assertTrue(self.engine.auto_clean_enabled)


# ---------------------------------------------------------------------------
# 6. Removal bookkeeping and removals for a previous backlog playlist
# ---------------------------------------------------------------------------

class BookkeepingTests(EngineCase):
    def test_logging_failure_after_a_removal_is_not_a_failed_removal(self):
        t = track(40)
        play = self.play_for(t, 16)
        self.core.note_fails = True
        entry = self.poll_decision(t)
        self.assertIsNotNone(entry)
        self.assertEqual(play.cleared_reason, "threshold")
        self.assertEqual(self.pending.ids(), set())
        self.assertEqual(self.fate(t)["state"], "cleared")
        self.assertTrue(any("bookkeeping" in c for c, _ in self.core.errors))

    def test_removal_for_another_playlist_leaves_the_current_play_alone(self):
        t = track(41)
        play = self.play_for(t, 6)
        entry = self.engine.clear_tracks(self.core, self.sp, [t], "threshold", "retry-queue",
                                         playlist_id=OTHER_PID)[0]
        self.assertIsNone(entry["position"])
        self.assertIsNone(play.cleared_reason)
        self.assertIs(self.index.contains(t["id"]), True)
        self.assertEqual(self.fate(t)["state"], "in_backlog")


# ---------------------------------------------------------------------------
# 7. Catch-up leaves alone plays from before the add, kept and restored tracks
# ---------------------------------------------------------------------------

class CatchUpTests(EngineCase):
    def setUp(self):
        super().setUp()
        from src.web import backlog_api
        self.api = backlog_api
        self.core.state["current_track_id"] = None

    def recent(self, t, played_at):
        return {"track": t, "played_at": played_at}

    def test_filters(self):
        before_add, after_add, kept, restored, paused = track(1), track(2), track(3), track(4), track(5)
        self.sp.recent_items = [
            self.recent(after_add, "2026-09-24T10:00:00.000Z"),
            self.recent(before_add, "2026-09-10T10:00:00.000Z"),   # heard before it joined the backlog
            self.recent(kept, "2026-09-24T09:00:00.000Z"),
            self.recent(restored, "2026-09-24T08:00:00.000Z"),
            self.recent(paused, "2026-09-24T07:00:00.000Z"),       # heard in the app while auto-clear was off
        ]
        self.engine.set_auto_clean(self.core, False)
        self.play_for(paused, 24)
        self.fate(paused)
        self.engine.set_auto_clean(self.core, True)
        self.engine.set_kept(kept["id"], True)
        entry = self.engine.clear_tracks(self.core, self.sp, [restored], "manual", "manual")[0]
        self.engine.undo(self.core, self.sp, entry["seq"])
        ids = [c["id"] for c in self.api.catch_up_candidates(self.core, self.sp, self.index, self.engine)]
        self.assertEqual(ids, [after_add["id"]])
        entries, skipped = self.api.clear_catch_up(self.core, self.sp, self.index, self.engine,
                                                   [after_add["id"], kept["id"], restored["id"], paused["id"]])
        self.assertEqual([e["track_id"] for e in entries], [after_add["id"]])
        self.assertEqual(sorted(skipped), sorted([kept["id"], restored["id"], paused["id"]]))


# ---------------------------------------------------------------------------
# 8. Legacy Like / Skip+remove without track_id right after a track change
# ---------------------------------------------------------------------------

class UnpinnedLegacyActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import platform_harness
        cls.app = platform_harness.load_app()

    def _play(self, tid, prev, age_s):
        play = types.SimpleNamespace(track_id=tid, seen_mono=time.monotonic() - age_s, prev_track_id=prev)
        return types.SimpleNamespace(tracker=types.SimpleNamespace(play=play))

    def test_unpinned_like_right_after_a_change_is_refused(self):
        a = self.app
        sp = mock.Mock()
        with a.state_lock:
            saved = {k: a.state.get(k) for k in ("current_track", "current_track_id")}
            a.state["current_track"] = {"id": "B" * 22, "name": "New", "artists": "X"}
            a.state["current_track_id"] = "B" * 22
        try:
            with mock.patch.object(a, "get_sp", return_value=sp), \
                    mock.patch.object(a, "_backlog_engine", return_value=self._play("B" * 22, "A" * 22, 0.5)):
                r = a.app.test_client().post("/api/like", json={})
                self.assertEqual(r.status_code, 409)
                self.assertEqual(r.get_json()["code"], "track_changed")
                r = a.app.test_client().post("/api/skip-remove", json={})
                self.assertEqual(r.status_code, 409)
            sp.current_user_saved_tracks_add.assert_not_called()
            sp.next_track.assert_not_called()
        finally:
            with a.state_lock:
                a.state.update(saved)

    def test_refusal_window(self):
        a = self.app
        ctx = a.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        with mock.patch.object(a, "_backlog_engine", return_value=self._play("B", "A", 0.5)):
            self.assertIsNotNone(a._unpinned_action_refused("B"))
        with mock.patch.object(a, "_backlog_engine", return_value=self._play("B", "A", 30)):
            self.assertIsNone(a._unpinned_action_refused("B"))        # settled
        with mock.patch.object(a, "_backlog_engine", return_value=self._play("B", "B", 0.5)):
            self.assertIsNone(a._unpinned_action_refused("B"))        # same track restarted
        with mock.patch.object(a, "_backlog_engine", return_value=self._play("B", "A", 0.5)):
            self.assertIsNone(a._unpinned_action_refused("C"))        # not the tracked play


if __name__ == "__main__":
    unittest.main()
