""".config.json IO: atomic writes, one lock for read-modify-write, .bak fallback, and no
save ever replacing the user's data with {} or a partial file.

Runs against an isolated copy of app.py (tests/platform_harness.py); never the real config.
"""

import json
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_harness  # noqa: E402

HISTORY_LEN = 300


def _big_config():
    return {
        "threshold_seconds": 45,
        "playlist_id": "X" * 22,
        "lyrics_offset_ms": 2500,
        "favorite_artists": ["a", "b"],
        "stats": {"removed_total": 5},
        "history": [{"type": "listen", "track_name": "n" * 40, "artists": "x" * 30, "i": i} for i in range(HISTORY_LEN)],
    }


class ConfigIOTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def setUp(self):
        a = self.app
        self.path = Path(a.CONFIG_PATH)
        self.bak = self.path.with_name(self.path.name + ".bak")
        for p in [self.path, self.bak] + list(self.path.parent.glob(".config.json.corrupt-*")):
            if p.exists():
                p.unlink()
        a._config_corrupt = None
        a._config_main_corrupt = False

    def tearDown(self):
        self.app._config_corrupt = None
        self.app._config_main_corrupt = False

    # -- basics ---------------------------------------------------------------------
    def test_missing_file_loads_empty_and_first_save_creates_it(self):
        a = self.app
        self.assertEqual(a.load_config_strict(), {})
        saved = a.update_config(lambda c: c.update(threshold_seconds=20))
        self.assertEqual(saved, {"threshold_seconds": 20})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"threshold_seconds": 20})
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_every_save_keeps_previous_version_as_bak(self):
        a = self.app
        a.update_config(lambda c: c.update(v=1))
        a.update_config(lambda c: c.update(v=2))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["v"], 2)
        self.assertEqual(json.loads(self.bak.read_text(encoding="utf-8"))["v"], 1)

    def test_mutator_may_return_replacement_dict(self):
        a = self.app
        a.update_config(lambda c: c.update(keep=1))
        a.update_config(lambda c: {"only": True})
        self.assertEqual(a.load_config(), {"only": True})

    def test_mutator_exception_leaves_file_untouched(self):
        a = self.app
        a.update_config(lambda c: c.update(v=1))
        before = self.path.read_bytes()

        def boom(cfg):
            cfg["v"] = 99
            raise RuntimeError("mutator failed")

        with self.assertRaises(RuntimeError):
            a.update_config(boom)
        self.assertEqual(self.path.read_bytes(), before)

    # -- corruption -------------------------------------------------------------------
    def test_truncated_config_falls_back_to_bak_and_never_writes_partial_data(self):
        a = self.app
        good = _big_config()
        a.save_config(good)
        a.save_config(good)  # now .bak == good as well
        full_text = self.path.read_text(encoding="utf-8")
        self.path.write_text(full_text[: len(full_text) // 2], encoding="utf-8")  # crash mid-write

        loaded = a.load_config_strict()
        self.assertEqual(loaded["history"], good["history"])
        self.assertIsNone(a.config_error_message())

        saved = a.update_config(lambda c: c.update(threshold_seconds=30))
        self.assertIsNotNone(saved)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["threshold_seconds"], 30)
        self.assertEqual(len(on_disk["history"]), HISTORY_LEN)
        # The unreadable file is kept aside, not rotated over the good .bak.
        self.assertEqual(len(list(self.path.parent.glob(".config.json.corrupt-*"))), 1)
        self.assertEqual(len(json.loads(self.bak.read_text(encoding="utf-8"))["history"]), HISTORY_LEN)

    def test_main_missing_uses_bak(self):
        a = self.app
        a.save_config({"x": 1})
        a.save_config({"x": 2})
        self.path.unlink()  # crash between the rotate and the final rename
        self.assertEqual(a.load_config_strict(), {"x": 1})

    def test_both_unreadable_disables_saving_and_reports_it(self):
        a = self.app
        self.path.write_text('{"history": [1, 2', encoding="utf-8")
        self.bak.write_text("not json", encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(a.ConfigCorrupt):
            a.load_config_strict()
        self.assertEqual(a.load_config(), {})
        self.assertIsNone(a.update_config(lambda c: c.update(favorite_artists=["x"])))
        self.assertFalse(a.save_config({"favorite_artists": ["x"]}))
        a.save_favorites()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(a.config_error_message(), a.CONFIG_UNREADABLE_MESSAGE)
        status = a.app.test_client().get("/api/status").get_json()
        self.assertEqual(status["config_error"], a.CONFIG_UNREADABLE_MESSAGE)
        self.assertEqual(status["error"], a.CONFIG_UNREADABLE_MESSAGE)
        # Once the file is fixed, loading and saving work again.
        self.path.write_text('{"ok": 1}', encoding="utf-8")
        self.assertEqual(a.load_config_strict(), {"ok": 1})
        self.assertIsNotNone(a.update_config(lambda c: c.update(ok=2)))
        self.assertIsNone(a.config_error_message())

    def test_empty_file_is_treated_as_new(self):
        a = self.app
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(a.load_config_strict(), {})

    def test_non_object_json_is_unreadable(self):
        a = self.app
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(a.ConfigCorrupt):
            a.load_config_strict()

    # -- converted writers ----------------------------------------------------------
    def test_writers_only_touch_their_own_keys(self):
        a = self.app
        a.save_config(_big_config())
        with a.state_lock:
            saved_favs = set(a.state["favorite_artists"])
            a.state["favorite_artists"] = {"zeta", "alpha"}
        try:
            a.save_favorites()
        finally:
            with a.state_lock:
                a.state["favorite_artists"] = saved_favs
        a.cache_track_genres_bulk([{"track_id": "t1", "artist": "A", "genres": ["rock"]}])
        cfg = a.load_config()
        self.assertEqual(cfg["favorite_artists"], ["alpha", "zeta"])
        self.assertIn("t1", cfg["track_genre_cache"])
        self.assertEqual(len(cfg["history"]), HISTORY_LEN)
        self.assertEqual(cfg["threshold_seconds"], 45)

    def test_save_analytics_state_keeps_other_keys(self):
        a = self.app
        a.save_config(_big_config())
        a.save_analytics_state()
        cfg = a.load_config()
        self.assertEqual(cfg["threshold_seconds"], 45)
        self.assertEqual(cfg["favorite_artists"], ["a", "b"])

    # -- concurrency (WP-01 ACCEPT 6) -----------------------------------------------
    def test_concurrent_writers_and_readers(self):
        a = self.app
        base = _big_config()
        a.save_config(base)
        writers, per_writer, readers = 8, 200, 4
        bad_reads = []
        errors = []
        stop = threading.Event()
        reads = [0]

        def writer(i):
            try:
                for n in range(per_writer):
                    a.update_config(lambda c, i=i, n=n: c.__setitem__(f"w{i}", n))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        def reader():
            while not stop.is_set():
                c = a.load_config()
                reads[0] += 1
                if len(c.get("history") or []) != HISTORY_LEN or c.get("threshold_seconds") != 45:
                    bad_reads.append(sorted(c.keys()))

        rs = [threading.Thread(target=reader) for _ in range(readers)]
        ws = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
        for t in rs + ws:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        for t in rs:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(bad_reads, [], f"{len(bad_reads)} of {reads[0]} reads were empty or partial")
        final = json.loads(self.path.read_text(encoding="utf-8"))
        for i in range(writers):
            self.assertEqual(final[f"w{i}"], per_writer - 1, "an update was lost")
        self.assertEqual(len(final["history"]), HISTORY_LEN)
        self.assertGreater(reads[0], 0)

    def test_lock_order_state_lock_then_config_lock_does_not_deadlock(self):
        a = self.app
        done = []

        def holder_of_state_lock():
            for _ in range(50):
                with a.state_lock:
                    a.update_config(lambda c: c.update(x=1))
            done.append(1)

        def plain_writer():
            for _ in range(50):
                a.update_config(lambda c: c.update(y=1))
            done.append(1)

        ts = [threading.Thread(target=holder_of_state_lock), threading.Thread(target=plain_writer)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        self.assertEqual(len(done), 2)


class EnvFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = platform_harness.load_app()

    def tearDown(self):
        platform_harness.reset_env_file(self.app)

    def test_write_env_updates_is_atomic_and_preserves_comments(self):
        a = self.app
        a.write_env_updates({"LASTFM_USERNAME": "someone", "POLL_INTERVAL": "4"})
        text = Path(a.ENV_PATH).read_text(encoding="utf-8")
        self.assertIn("# test .env", text)
        self.assertIn("LASTFM_USERNAME=someone", text)
        self.assertTrue(text.rstrip().endswith("POLL_INTERVAL=4"))
        self.assertFalse(Path(a.ENV_PATH).with_name(".env.tmp").exists())
        self.assertEqual(a.read_env_file()["LASTFM_USERNAME"], "someone")

    def test_write_env_updates_refuses_line_breaks(self):
        a = self.app
        before = Path(a.ENV_PATH).read_bytes()
        for bad in ("a\nb", "a\rb", "a\x00b"):
            with self.assertRaises(ValueError):
                a.write_env_updates({"LASTFM_USERNAME": bad})
        with self.assertRaises(ValueError):
            a.write_env_updates({"BAD KEY": "x"})
        self.assertEqual(Path(a.ENV_PATH).read_bytes(), before)

    def test_env_num_falls_back_and_clamps(self):
        a = self.app
        cases = {"nan": 3.0, "abc": 3.0, "": 3.0, "0.5": 1.5, "100": 30.0, "2.5": 2.5}
        for raw, want in cases.items():
            os.environ["DC_TEST_NUM"] = raw
            self.assertEqual(a._env_num("DC_TEST_NUM", 3.0, float, 1.5, 30), want, raw)
        os.environ["DC_TEST_NUM"] = "12.5"
        self.assertEqual(a._env_num("DC_TEST_NUM", 30, int, 3, 600), 12)
        os.environ.pop("DC_TEST_NUM", None)
        self.assertEqual(a._env_num("DC_TEST_NUM", 30, int, 3, 600), 30)

    def test_debug_mode_defaults_off(self):
        self.assertFalse(self.app.DEBUG_MODE)


if __name__ == "__main__":
    unittest.main()
