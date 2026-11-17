"""Backlog index: complete, resumable, fail-safe membership (WP-03).

Runs entirely in memory: a fake Spotify playlist source, an in-memory SQLite store and a
fake sleep. Never imports app.py and never touches the real DB/config.
"""

import sqlite3
import threading
import unittest

from src.services.backlog_index import (
    IndexSyncWorker,
    PlaylistIndex,
    SQLiteIndexStore,
    entry_from_item,
    entry_from_track,
)

PID = "A" * 22


class GuardBlock(Exception):
    """Stands in for SpotifyGuardBlocked: http_status 429 + retry_after, no request sent."""

    http_status = 429

    def __init__(self, retry_after):
        super().__init__(f"blocked {retry_after}s")
        self.retry_after = retry_after


def make_track(i, prefix="t"):
    tid = f"{prefix}{i:020d}"
    return {"id": tid, "uri": f"spotify:track:{tid}", "type": "track", "is_local": False,
            "name": f"Song {i}", "duration_ms": 180000 + i,
            "artists": [{"id": f"ar{i % 7}", "name": f"Artist {i % 7}"}],
            "album": {"id": f"al{i % 11}", "name": f"Album {i % 11}", "images": [
                {"url": f"https://img/{i}/640", "width": 640}, {"url": f"https://img/{i}/64", "width": 64}]}}


class FakePlaylistSource:
    """Minimal spotipy-like playlist API with limit/offset paging and snapshot ids."""

    def __init__(self, n, prefix="t"):
        self.tracks = [make_track(i, prefix) for i in range(n)]
        self.snapshot = 1
        self.meta_calls = 0
        self.page_offsets = []
        self.block_plan = {}      # offset -> number of guard blocks still to raise
        self.on_page = None       # hook(offset) called before serving a page
        self.fail_plan = {}       # offset -> number of generic errors still to raise

    def playlist(self, pid, fields=None):
        self.meta_calls += 1
        return {"name": "Backlog", "snapshot_id": f"snap{self.snapshot}",
                "tracks": {"total": len(self.tracks)}}

    def playlist_items(self, pid, fields=None, limit=100, offset=0):
        if self.block_plan.get(offset):
            self.block_plan[offset] -= 1
            raise GuardBlock(retry_after=75)
        if self.fail_plan.get(offset):
            self.fail_plan[offset] -= 1
            raise RuntimeError("network")
        self.page_offsets.append(offset)
        if self.on_page:
            self.on_page(offset)
        chunk = self.tracks[offset: offset + limit]
        items = [{"added_at": "2026-01-01T00:00:00Z", "track": t} for t in chunk]
        nxt = "next" if offset + limit < len(self.tracks) else None
        return {"items": items, "total": len(self.tracks), "next": nxt}

    # simulated writes
    def remove(self, ids):
        self.tracks = [t for t in self.tracks if t["id"] not in set(ids)]
        self.snapshot += 1

    def add(self, track, position=None):
        pos = len(self.tracks) if position is None else position
        self.tracks.insert(pos, track)
        self.snapshot += 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def build(n=1200, store=None):
    clock = Clock()
    src = FakePlaylistSource(n)
    idx = PlaylistIndex(store=store, clock=clock, wall_ms=lambda: int(clock.t * 1000))
    idx.switch(PID)
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock.t += s

    worker = IndexSyncWorker(idx, get_sp=lambda: src, sleep=fake_sleep, clock=clock)
    return idx, src, worker, clock, sleeps


class EntryParsingTests(unittest.TestCase):
    def test_track_and_item_shapes_and_locals(self):
        t = make_track(3)
        e1 = entry_from_item({"added_at": "x", "track": t})
        e2 = entry_from_item({"added_at": "x", "item": t})
        self.assertEqual(e1["track_id"], t["id"])
        self.assertEqual(e2["track_id"], t["id"])
        self.assertEqual(e1["artist_list"], [{"id": "ar3", "name": "Artist 3"}])
        self.assertEqual(e1["image"], "https://img/3/64")  # smallest image for list rows
        local = entry_from_item({"is_local": True, "track": dict(t, id=None, is_local=True)})
        self.assertIsNone(local["track_id"])
        episode = entry_from_item({"track": dict(t, type="episode")})
        self.assertIsNone(episode["track_id"])
        self.assertIsNone(entry_from_item(None)["track_id"])

    def test_entry_from_app_track_info(self):
        e = entry_from_track({"id": "x" * 22, "name": "N", "artists": "A, B",
                              "artist_list": [{"id": "1", "name": "A"}], "album": "Al", "image": "i"})
        self.assertEqual(e["uri"], "spotify:track:" + "x" * 22)
        self.assertEqual(e["artists"], "A, B")


class FullLoadTests(unittest.TestCase):
    def test_1200_track_playlist_loads_completely(self):
        idx, src, worker, _, _ = build(1200)
        self.assertIsNone(idx.contains(src.tracks[-1]["id"]))  # unknown before sync: fail safe
        self.assertEqual(worker.sync(src), "committed")
        st = idx.status()
        self.assertTrue(st["complete"])
        self.assertEqual(st["known"], 1200)
        self.assertEqual(st["total"], 1200)
        self.assertEqual(src.page_offsets, list(range(0, 1200, 100)))
        self.assertIs(idx.contains(src.tracks[1150]["id"]), True)
        self.assertIs(idx.contains("nope" * 5), False)
        self.assertEqual(idx.position_of(src.tracks[1101]["id"]), 1101)

    def test_users_real_size_1187(self):
        idx, src, worker, _, _ = build(1187)
        worker.sync(src)
        self.assertEqual(idx.status()["known"], 1187)
        self.assertEqual(idx.status()["total"], 1187)
        self.assertEqual(len(idx.ids_snapshot()), 1187)

    def test_nothing_is_committed_before_the_last_page(self):
        idx, src, worker, _, _ = build(350)
        seen = []

        def on_page(offset):
            seen.append((offset, idx.status()["complete"], idx.contains(src.tracks[-1]["id"])))
        src.on_page = on_page
        worker.sync(src)
        # While paging: never complete, and a not-yet-loaded track is unknown (None), not False.
        self.assertTrue(all(not complete for _, complete, _ in seen))
        self.assertEqual([m for _, _, m in seen], [None, None, None, None])
        self.assertTrue(idx.status()["complete"])

    def test_partial_pages_confirm_membership_while_loading(self):
        idx, src, worker, _, _ = build(300)
        probe = []
        src.on_page = lambda off: probe.append(idx.contains(src.tracks[5]["id"])) if off == 200 else None
        worker.sync(src)
        self.assertEqual(probe, [True])  # found on page 1 -> True even before completion


class GuardAndErrorTests(unittest.TestCase):
    def test_guard_block_sleeps_capped_and_retries_same_page(self):
        idx, src, worker, _, sleeps = build(1200)
        src.block_plan = {500: 2}
        self.assertEqual(worker.sync(src), "committed")
        self.assertEqual(src.page_offsets, list(range(0, 1200, 100)))  # no page skipped or repeated
        self.assertEqual(round(sum(sleeps)), 120)                       # 2 x min(75, 60)
        self.assertEqual(idx.status()["known"], 1200)

    def test_generic_errors_retry_then_abort_keeping_old_index(self):
        idx, src, worker, _, _ = build(200)
        worker.sync(src)
        src.add(make_track(999))
        src.fail_plan = {0: 10}
        self.assertEqual(worker.sync(src), "error")
        st = idx.status()
        self.assertTrue(st["complete"])        # the previous complete index is still in force
        self.assertEqual(st["known"], 200)
        self.assertIsNotNone(st["error"])

    def test_rejected_fields_filter_falls_back_to_unfiltered_pages(self):
        idx, src, worker, _, _ = build(250)
        plain = src.playlist_items
        seen = []

        class BadRequest(Exception):
            http_status = 400

        def strict(pid, fields=None, limit=100, offset=0):
            seen.append(fields)
            if fields is not None:
                raise BadRequest("fields")
            return plain(pid, limit=limit, offset=offset)

        src.playlist_items = strict
        self.assertEqual(worker.sync(src), "committed")
        self.assertEqual(idx.status()["known"], 250)
        self.assertIsNotNone(seen[0])
        self.assertEqual(seen[1:], [None, None, None])

    def test_worker_step_waits_for_real_rate_limit_lock(self):
        idx, src, _, clock, _ = build(100)
        worker = IndexSyncWorker(idx, get_sp=lambda: src, lock_remaining=lambda: 3600, clock=clock,
                                 sleep=lambda s: None)
        self.assertEqual(worker.step(), "locked")
        self.assertEqual(src.meta_calls, 0)
        self.assertGreater(idx.meta_due_in(), 0)


class SnapshotRuleTests(unittest.TestCase):
    def test_unchanged_snapshot_costs_one_meta_call_only(self):
        idx, src, worker, _, _ = build(400)
        worker.sync(src)
        pages = len(src.page_offsets)
        self.assertEqual(worker.sync(src), "unchanged")
        self.assertEqual(len(src.page_offsets), pages)

    def test_own_write_marks_dirty_and_is_adopted_without_repaging(self):
        idx, src, worker, _, _ = build(400)
        worker.sync(src)
        victim = src.tracks[123]["id"]
        src.remove([victim])                    # our own write (snapshot moves)
        self.assertEqual(idx.apply_removal([victim], playlist_id=PID), 1)
        self.assertTrue(idx.status()["dirty"])
        pages = len(src.page_offsets)
        self.assertEqual(worker.sync(src), "adopted")
        self.assertEqual(len(src.page_offsets), pages)  # no full re-page
        self.assertFalse(idx.status()["dirty"])
        self.assertEqual(idx.status()["total"], 399)
        self.assertIs(idx.contains(victim), False)

    def test_external_add_is_picked_up(self):
        idx, src, worker, _, _ = build(1200)
        worker.sync(src)
        newcomer = make_track(5000, prefix="x")
        src.add(newcomer)                      # added in the Spotify app
        self.assertEqual(worker.sync(src), "committed")
        self.assertIs(idx.contains(newcomer["id"]), True)
        st = idx.status()
        self.assertEqual((st["known"], st["total"]), (1201, 1201))

    def test_dirty_with_mismatched_total_repages(self):
        idx, src, worker, _, _ = build(300)
        worker.sync(src)
        victim = src.tracks[10]["id"]
        src.remove([victim])
        idx.apply_removal([victim], playlist_id=PID)
        src.add(make_track(7000, prefix="x"))
        src.add(make_track(7001, prefix="x"))   # external adds at the same time
        self.assertEqual(worker.sync(src), "committed")
        self.assertEqual(idx.status()["total"], 301)


class LocalMutationTests(unittest.TestCase):
    def test_apply_removal_only_counts_present_ids(self):
        idx, src, worker, _, _ = build(100)
        worker.sync(src)
        seq = idx.status()["removal_seq"]
        self.assertEqual(idx.apply_removal(["missing" * 3], playlist_id=PID), 0)
        self.assertEqual(idx.status()["total"], 100)
        self.assertEqual(idx.status()["removal_seq"], seq)
        tid = src.tracks[0]["id"]
        self.assertEqual(idx.apply_removal([tid], playlist_id=PID), 1)
        self.assertEqual(idx.apply_removal([tid], playlist_id=PID), 0)  # idempotent
        self.assertEqual(idx.status()["total"], 99)
        self.assertEqual(idx.position_of(src.tracks[1]["id"]), 0)       # positions shift
        self.assertEqual(idx.apply_removal([src.tracks[5]["id"]], playlist_id="B" * 22), 0)

    def test_duplicates_are_removed_together(self):
        idx, src, worker, _, _ = build(10)
        src.tracks.append(dict(src.tracks[2]))
        worker.sync(src)
        self.assertEqual(idx.status()["total"], 11)
        self.assertEqual(idx.apply_removal([src.tracks[2]["id"]], playlist_id=PID), 2)
        self.assertEqual(idx.status()["total"], 9)

    def test_apply_add_restores_position(self):
        idx, src, worker, _, _ = build(50)
        worker.sync(src)
        t = src.tracks[20]
        idx.apply_removal([t["id"]], playlist_id=PID)
        idx.apply_add(entry_from_track(t), 20, playlist_id=PID)
        self.assertEqual(idx.position_of(t["id"]), 20)
        self.assertEqual(idx.status()["total"], 50)
        self.assertIs(idx.contains(t["id"]), True)

    def test_our_own_removals_never_trigger_an_early_check(self):
        # Spotify keeps playing a track we just removed, from the backlog's context. That
        # must not look like an external add (it would cost a meta call every 20 s).
        idx, src, worker, clock, _ = build(30)
        worker.sync(src)
        gone = src.tracks[3]["id"]
        idx.apply_removal([gone], playlist_id=PID)
        clock.t += 5
        self.assertFalse(idx.hint_stale(gone))
        self.assertGreater(idx.meta_due_in(), 0)
        self.assertTrue(idx.hint_stale("unknown-external-track"))
        self.assertEqual(idx.meta_due_in(), 0)

    def test_removal_during_a_run_is_not_resurrected_and_nothing_is_skipped(self):
        idx, src, worker, _, _ = build(500)
        victim = src.tracks[50]["id"]
        state = {"done": False}

        def on_page(offset):
            # After page 2 was served, our auto-clear removes a track from page 1.
            if offset == 200 and not state["done"]:
                state["done"] = True
                src.remove([victim])
                idx.apply_removal([victim], playlist_id=PID)
        src.on_page = on_page
        self.assertEqual(worker.sync(src), "committed")
        st = idx.status()
        self.assertEqual(st["total"], 499)
        self.assertEqual(st["known"], 499)
        self.assertIs(idx.contains(victim), False)
        self.assertEqual(len(idx.ids_snapshot()), 499)      # every surviving track is known
        self.assertEqual(idx.ids_snapshot(), {t["id"] for t in src.tracks})

    def test_external_change_mid_run_restarts(self):
        idx, src, worker, _, _ = build(300)
        state = {"n": 0}

        def on_page(offset):
            if offset == 100 and state["n"] == 0:
                state["n"] = 1
                src.add(make_track(9000, prefix="x"), position=0)
        src.on_page = on_page
        self.assertEqual(worker.sync(src), "committed")
        self.assertEqual(idx.ids_snapshot(), {t["id"] for t in src.tracks})
        self.assertEqual(idx.status()["total"], 301)

    def test_switch_aborts_a_running_sync(self):
        idx, src, worker, _, _ = build(500)
        src.on_page = lambda off: idx.switch("B" * 22) if off == 200 else None
        self.assertEqual(worker.sync(src), "aborted")
        self.assertEqual(idx.pid, "B" * 22)
        self.assertFalse(idx.status()["complete"])


class PersistenceTests(unittest.TestCase):
    def test_persisted_index_is_usable_immediately_after_restart(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(conn.close)
        store = SQLiteIndexStore(lambda: conn)
        idx, src, worker, _, _ = build(1200, store=store)
        worker.sync(src)
        victim = src.tracks[7]["id"]
        idx.apply_removal([victim], playlist_id=PID)
        idx.flush_persist(force=True)
        # "restart": a brand-new index over the same store
        idx2 = PlaylistIndex(store=store)
        idx2.switch(PID)
        st = idx2.status()
        self.assertTrue(st["complete"])
        self.assertEqual(st["known"], 1199)
        self.assertTrue(st["dirty"])
        self.assertIs(idx2.contains(src.tracks[1199]["id"]), True)
        self.assertIs(idx2.contains(victim), False)
        self.assertEqual(idx2.meta_due_in(), 0)            # revalidates right away

    def test_switching_back_uses_the_other_playlists_copy(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(conn.close)
        store = SQLiteIndexStore(lambda: conn)
        idx, src, worker, _, _ = build(120, store=store)
        worker.sync(src)
        idx.switch("B" * 22)
        self.assertFalse(idx.status()["complete"])
        idx.switch(PID)
        self.assertTrue(idx.status()["complete"])
        self.assertEqual(idx.status()["known"], 120)


class ConcurrencyTests(unittest.TestCase):
    def test_contains_is_safe_while_syncing(self):
        idx, src, worker, _, _ = build(1200)
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    idx.contains(src.tracks[600]["id"])
                    idx.status()
                    idx.items()
            except Exception as e:  # pragma: no cover
                errors.append(e)
        th = threading.Thread(target=reader)
        th.start()
        worker.sync(src)
        stop.set()
        th.join()
        self.assertEqual(errors, [])


class SafetyReviewTests(unittest.TestCase):
    """Regressions from the clearing safety review (stale restart copy, page-run write race)."""

    def test_restored_copy_is_not_trusted_for_auto_clear_until_confirmed(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(conn.close)
        store = SQLiteIndexStore(lambda: conn)
        idx, src, worker, clock, _ = build(300, store=store)
        worker.sync(src)
        idx.flush_persist(force=True)
        self.assertIs(idx.clearable(src.tracks[5]["id"]), True)
        victim = src.tracks[5]["id"]
        src.remove([victim])                        # removed in Spotify while the app was off
        idx2 = PlaylistIndex(store=store, clock=clock, wall_ms=lambda: int(clock.t * 1000))
        idx2.switch(PID)
        self.assertIs(idx2.contains(victim), True)      # shown at once ...
        self.assertIs(idx2.clearable(victim), None)     # ... but not acted on
        self.assertFalse(idx2.status()["confirmed"])
        worker2 = IndexSyncWorker(idx2, get_sp=lambda: src, sleep=lambda s: None, clock=clock)
        self.assertEqual(worker2.sync(src), "committed")
        self.assertIs(idx2.clearable(victim), False)
        self.assertIs(idx2.clearable(src.tracks[6]["id"]), True)
        self.assertTrue(idx2.status()["confirmed"])

    def test_unchanged_restored_copy_is_confirmed_by_one_meta_call(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(conn.close)
        store = SQLiteIndexStore(lambda: conn)
        idx, src, worker, clock, _ = build(120, store=store)
        worker.sync(src)
        idx.flush_persist(force=True)
        idx2 = PlaylistIndex(store=store, clock=clock, wall_ms=lambda: int(clock.t * 1000))
        idx2.switch(PID)
        pages = len(src.page_offsets)
        self.assertEqual(IndexSyncWorker(idx2, get_sp=lambda: src, sleep=lambda s: None, clock=clock).sync(src),
                         "unchanged")
        self.assertEqual(len(src.page_offsets), pages)
        self.assertIs(idx2.clearable(src.tracks[3]["id"]), True)

    def test_confirmed_since_needs_a_completed_check_after_the_moment(self):
        idx, src, worker, clock, _ = build(50)
        self.assertFalse(idx.confirmed_since(0))           # nothing confirmed yet
        worker.sync(src)
        t = idx.synced_at_ms
        self.assertTrue(idx.confirmed_since(t))
        self.assertFalse(idx.confirmed_since(t + 1))       # a play that began after the last check
        clock.t += 5
        self.assertEqual(worker.sync(src), "unchanged")
        self.assertTrue(idx.confirmed_since(t + 1))

    def test_write_racing_a_page_is_repaged_not_adopted(self):
        idx, src, worker, _, _ = build(500)
        first, second = src.tracks[10]["id"], src.tracks[20]["id"]
        st = {"n": 0}

        def on_page(offset):
            if offset == 100 and st["n"] == 0:      # a local removal: the run is "touched"
                st["n"] = 1
                src.remove([first])
                idx.apply_removal([first], playlist_id=PID)
            elif offset >= 200 and st["n"] == 1:    # a second write lands on Spotify first ...
                st["n"] = 2
                src.remove([second])
        orig_add_page = idx.add_page

        def add_page(*a, **k):
            result = orig_add_page(*a, **k)
            if st["n"] == 2:                        # ... and reaches the index after the page
                st["n"] = 3
                idx.apply_removal([second], playlist_id=PID)
            return result
        idx.add_page = add_page
        src.on_page = on_page
        self.assertEqual(worker.sync(src), "committed")
        truth = [t["id"] for t in src.tracks]
        order = [e["track_id"] for e in idx.items()]
        self.assertNotEqual(order, truth)           # the race did corrupt the run (counts agree)
        self.assertEqual(len(order), len(truth))
        src.on_page = None
        self.assertEqual(worker.sync(src), "committed")   # re-paged instead of 'adopted'
        self.assertEqual([e["track_id"] for e in idx.items()], truth)


if __name__ == "__main__":
    unittest.main()
