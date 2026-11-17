"""The static web demo: dev/build_web_demo.py -> dist/web-demo, served by static/js/demo/.

- Build helpers: the frozen clock, DEMO_NOW, the scrub of FakeSpotify placeholders, the Last.fm
  cache that never hangs a build.
- The mock API answers every API path the interface uses (or refuses it as read-only on purpose).
- Fixture coverage, on a small ``--sample`` build made in a subprocess: every report period the
  < > buttons reach, year in music for every year with plays, an artist page for every artist a
  list shows, the live templates; a relative-URL, script-free index.html; no local paths.
  When a full build exists in dist/web-demo, the same checks run on it with the strict rules
  (report chains reach back to where the history starts).
"""

import gzip
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
BUILD = PROJECT / "dev" / "build_web_demo.py"
SNAPSHOT = PROJECT / "demo" / "snapshot"
FULL_BUNDLE = PROJECT / "dist" / "web-demo"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("dc_build_web_demo_for_test", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # stdlib imports only; nothing is patched until main() runs
    return mod


def _fixture(root, rel):
    return json.loads(gzip.decompress((root / "fixtures" / (rel + ".gz")).read_bytes()))


class BuildHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _load_build_module()

    def test_artist_info_drops_email_addresses_from_lastfm_text(self):
        info = {"name": "Band", "bio": "Booking: band.mgmt+tour@gmail.com\nMembers: A, B",
                "similar": [{"name": "Other", "bio": "mail x@y.co.uk"}], "artist_id": "fakeartistband"}
        out = self.b.sanitize_artist_info(info)
        self.assertEqual(out["bio"], "Booking: \nMembers: A, B")
        self.assertNotIn("@", json.dumps(out))
        self.assertNotIn("artist_id", out)

    def test_demo_now_is_a_few_minutes_after_the_newest_play(self):
        newest = 1_791_164_166_526
        now = self.b.demo_now_ms(newest)
        self.assertEqual(now % 60_000, 0)
        self.assertTrue(newest + 4 * 60_000 <= now < newest + 5 * 60_000)

    def test_frozen_clock_freezes_time_and_datetime(self):
        code = (
            "import importlib.util, sys, time, datetime\n"
            f"spec = importlib.util.spec_from_file_location('b', r'{BUILD}')\n"
            "b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)\n"
            "b.FrozenClock(1791164460000).install()\n"
            "from datetime import datetime as D, date as d\n"
            "time.sleep(0.05)\n"
            "print(time.time(), time.time_ns() // 1_000_000, int(D.now().timestamp() * 1000),"
            " d.today() == D.fromtimestamp(1791164.46e3).date())\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        t, ns_ms, dt_ms, same_day = out.stdout.split()
        self.assertEqual(float(t), 1791164460.0)
        self.assertEqual(int(ns_ms), 1791164460000)
        self.assertEqual(int(dt_ms), 1791164460000)
        self.assertEqual(same_day, "True")

    def test_scrub_drops_timings_and_placeholder_ids(self):
        out = self.b.scrub({"elapsed_ms": 3, "a": [{"id": "fakeartistdancegavindanc", "name": "X",
                                                     "url": "https://open.spotify.com/artist/fakeartistx"}],
                            "album_id": "fakealbum", "keep": "real"})
        self.assertEqual(out, {"a": [{"id": None, "name": "X", "url": ""}], "album_id": None, "keep": "real"})

    def test_placeholders_become_unknown_not_fake(self):
        item = {"id": "t1", "duration_ms": 200000, "added_at": "2026-05-01T00:00:00Z"}
        fx = {
            "live.json": {"backlog": {"items": [dict(item)]},
                          "templates": {"clear": {"cleared": dict(item)}, "undo": {"restored": dict(item)},
                                        "status_after_clear": {"recently_cleared": [dict(item)]}}},
            "library/liked.json": {"tracks": [{"id": "t2", "duration_ms": 200000, "liked_at": "2026-09-01T12:00:00Z"}]},
            "library/favorites.json": {"release_feed": [{"release_date": "2020-01-01"}], "release_total": 1},
        }
        self.b.drop_placeholders(fx)
        row = fx["live.json"]["backlog"]["items"][0]
        self.assertIsNone(row["added_at"])
        self.assertEqual(row["duration_ms"], self.b.SIM_TRACK_SECONDS * 1000)
        self.assertIsNone(fx["library/liked.json"]["tracks"][0]["liked_at"])
        fav = fx["library/favorites.json"]
        self.assertEqual(fav["release_feed"], [])
        self.assertTrue(fav["release_demo"] and fav["release_error"])

    def test_artist_info_keeps_only_real_fields(self):
        info = {"name": "A", "spotify_followers": 12345, "artist_image": "https://i.scdn.co/image/x",
                "artist_image_kind": "artist", "top_tracks": [{"name": "t"}], "spotify_genres": ["rock"],
                "notes": ["set LASTFM_API_KEY"], "spotify_url": "https://open.spotify.com/artist/fakeartista"}
        out = self.b.sanitize_artist_info(info)
        self.assertNotIn("spotify_followers", out)
        self.assertNotIn("top_tracks", out)
        self.assertNotIn("notes", out)
        self.assertNotIn("spotify_url", out)
        self.assertEqual(out["artist_image_kind"], "album")   # FakeSpotify's "photo" is an album cover
        self.assertEqual(out["spotify_genres"], ["rock"])

    def test_lastfm_cache_never_hangs_and_never_refetches(self):
        b = self.b

        class Core:
            _LFM_FAIL = object()
            _LFM_MISSING = object()

            def __init__(self):
                self.calls = 0
                self.result = self._LFM_FAIL

            def lastfm_api_key(self):
                return "k"

            def lastfm_call(self, method, **params):
                self.calls += 1
                return self.result

        with tempfile.TemporaryDirectory() as tmp:
            core = Core()
            cache = b.LastfmCache(tmp, core)
            for i in range(6):
                self.assertIs(cache("artist.getInfo", artist=f"A{i}"), core._LFM_FAIL)
            self.assertEqual(core.calls, 3, "after 3 failures in a row Last.fm counts as unreachable")
            self.assertTrue(cache.unreachable)
            self.assertIn("unreachable", cache.summary())

            core2 = Core()
            core2.result = {"artist": {"name": "B"}}
            cache2 = b.LastfmCache(tmp, core2)
            self.assertEqual(cache2("artist.getInfo", artist="B"), {"artist": {"name": "B"}})
            core3 = Core()                          # a rebuild: answered from the cache, no call
            self.assertEqual(b.LastfmCache(tmp, core3)("artist.getInfo", artist="B"), {"artist": {"name": "B"}})
            self.assertEqual(core3.calls, 0)

            class NoKey(Core):
                def lastfm_api_key(self):
                    return None
            nokey = NoKey()
            self.assertIs(b.LastfmCache(tmp, nokey)("artist.getInfo", artist="C"), nokey._LFM_FAIL)
            self.assertEqual(nokey.calls, 0)


class MockCoversTheInterfaceTest(unittest.TestCase):
    """Every API path the interface uses is answered by static/js/demo/mock-api.js."""

    def test_every_api_path_has_a_mock_route(self):
        mock = (PROJECT / "static" / "js" / "demo" / "mock-api.js").read_text(encoding="utf-8")
        def block(start):
            return mock.split(start, 1)[1].split("\n};", 1)[0]
        get = set(re.findall(r"^  '(/api/[^']+)': ", block("const GET = {"), re.M))
        post = set(re.findall(r"^  '(/api/[^']+)': ", block("const POST = {"), re.M))
        readonly = set(re.findall(r"'(/api/[^']+)'", mock.split("export const READONLY = [", 1)[1].split("];", 1)[0]))
        self.assertTrue(get and post and readonly)
        known = get | post | {p.split("<", 1)[0] for p in readonly} | {"/api/v2/artist/"}
        used = set()
        for js in (PROJECT / "static" / "js").rglob("*.js"):
            if "demo" in js.relative_to(PROJECT / "static" / "js").parts:
                continue
            used |= set(re.findall(r"""['`"](/api/[A-Za-z0-9_/\-]*)""", js.read_text(encoding="utf-8")))
        missing = sorted(u for u in used
                         if u not in known and not (u.endswith("/") and any(k.startswith(u) for k in known)))
        self.assertEqual(missing, [], "add these to static/js/demo/mock-api.js (GET, POST or READONLY)")


class FixtureCoverage:
    """Checks shared by the sample build and a full build in dist/web-demo."""

    strict = False
    root = None

    def test_manifest_lists_existing_parseable_fixtures(self):
        m = json.loads((self.root / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
        for rel in m["files"]:
            p = self.root / "fixtures" / rel
            self.assertTrue(p.is_file(), rel)
            json.loads(gzip.decompress(p.read_bytes()))
        self.assertGreater(m["demo_now_ms"], 0)
        self.assertEqual(m["frozen_on"], json.loads((SNAPSHOT / "meta.json").read_text(encoding="utf-8"))["frozen_on"])

    def test_every_reachable_report_period_has_a_fixture(self):
        m = json.loads((self.root / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
        for period in ("week", "month", "year"):
            starts = m["reports"][period]
            self.assertTrue(starts, period)
            reps = [_fixture(self.root, f"reports/{period}/{s}.json") for s in starts]
            self.assertTrue(reps[-1]["period"]["is_current"], f"{period}: the newest is the current one")
            for older, newer in zip(reps, reps[1:]):   # the < button of each leads to the one before
                self.assertEqual(newer["period"]["prev_anchor"], older["period"]["start_day"], period)
            if self.strict:
                first = reps[0]
                self.assertTrue(first["totals"]["prev_plays"] is None or not first["period"]["prev_anchor"],
                                f"{period}: the chain must reach the period where the history starts")
        for period in ("all", "last7", "last30"):
            self.assertEqual(_fixture(self.root, f"reports/{period}.json")["period"]["type"], period)

    def test_year_in_music_for_every_year_with_plays(self):
        overview = _fixture(self.root, "insights/overview.json")
        years = [r["year"] for r in overview["plays_by_year"] if r.get("plays")]
        self.assertTrue(years)
        for y in years:
            self.assertEqual(_fixture(self.root, f"insights/year/{y}.json")["year"], y)

    def test_listed_artists_have_artist_pages(self):
        m = json.loads((self.root / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
        index = _fixture(self.root, "artists.json")
        col = {c: i for i, c in enumerate(index["columns"])}
        by_key = {r[col["key"]]: r for r in index["rows"]}
        self.assertEqual(len(by_key), m["counts"]["artists"])
        listed = set()
        for period in ("week", "month", "year"):
            for s in m["reports"][period]:
                rep = _fixture(self.root, f"reports/{period}/{s}.json")
                listed |= {a["key"] for a in rep["top"]["artists"]}
                listed |= {a["key"] for a in rep.get("discoveries") or []}
        no_page = sorted(k for k in listed if not by_key.get(k, [0] * 9)[col["stats"]])
        self.assertEqual(no_page, [], "artists shown in a report list must have a full artist page")
        # a page is where the index says, and it is that artist's
        some = [r for r in index["rows"] if r[col["stats"]]][:5]
        for r in some:
            sid = r[col["stats"]]
            shard = _fixture(self.root, f"artist/{(sid - 1) // m['artist_shard']}.json")
            self.assertEqual(shard[str(sid)]["key"], r[col["key"]])

    def test_live_templates_match_the_status(self):
        live = _fixture(self.root, "live.json")
        st = live["status"]
        self.assertEqual(st["setup"], {"demo": True, "needs_credentials": False, "needs_playlist": False})
        self.assertEqual(len(live["backlog"]["items"]), st["backlog"]["total"])
        for k in ("clear", "undo", "auto_clean", "threshold", "catch_up"):
            self.assertIn(k, live["templates"])
        self.assertIn("seq", live["templates"]["clear"]["cleared"])
        self.assertFalse(live["artist_unknown"]["found"])

    def test_index_html_is_relative_and_script_free(self):
        page = (self.root / "index.html").read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", page):
            self.assertTrue('src="' in tag or 'type="application/json"' in tag, f"inline script: {tag}")
        refs = re.findall(r'(?:src|href)="([^"]+)"', page)
        self.assertTrue(refs)
        for ref in refs:
            self.assertFalse(ref.startswith("/"), ref)
            if ref.startswith(("http:", "https:", "#", "..")):
                continue
            self.assertTrue((self.root / ref.split("?", 1)[0]).is_file(), ref)
        cfg = json.loads(re.search(r'<script type="application/json" id="app-config">(.*?)</script>', page, re.S).group(1))
        self.assertTrue(cfg["demo"])
        self.assertGreater(cfg["webDemo"]["now"], 0)
        self.assertIn('src="static/js/demo/clock.js', page.split('src="static/js/boot.js', 1)[0],
                      "the demo clock must load before everything else")

    def test_js_imports_resolve_inside_the_bundle(self):
        js_root = self.root / "static" / "js"
        for js in js_root.rglob("*.js"):
            text = js.read_text(encoding="utf-8")
            for spec in re.findall(r"""(?:from\s+|import\s*\(\s*)['"](\.[^'"]+)['"]""", text):
                self.assertTrue((js.parent / spec).resolve().is_file(), f"{js.relative_to(self.root)} -> {spec}")

    def test_no_local_paths_or_placeholder_ids(self):
        for p in (self.root / "fixtures").rglob("*.gz"):
            text = gzip.decompress(p.read_bytes()).decode("utf-8")
            for bad in ("C:\\\\", "AppData", "/Users/", "/home/", "fakeartist", "fakealbum"):
                self.assertNotIn(bad, text, p.name)
        self.assertFalse((self.root / "static" / "classic").exists())


@unittest.skipUnless((SNAPSHOT / "meta.json").exists(), "demo/snapshot not built")
class SampleBuildCoverageTest(FixtureCoverage, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dc_web_demo_test_"))
        cls.root = cls.tmp / "web-demo"
        env = {k: v for k, v in os.environ.items() if k not in ("LASTFM_API_KEY", "DISCOVER_DATA_DIR",
                                                                  "DISCOVER_DEMO", "DISCOVER_DEMO_DIR")}
        out = subprocess.run(
            [sys.executable, str(BUILD), "--sample", "--any-timezone", "--out", str(cls.root),
             "--lastfm-cache", str(cls.tmp / "lastfm")],
            capture_output=True, text=True, timeout=600, env=env, cwd=str(PROJECT))
        cls.output = out.stdout + out.stderr
        if out.returncode != 0:
            raise AssertionError(f"sample build failed:\n{cls.output[-3000:]}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_build_reports_size_and_lastfm(self):
        self.assertRegex(self.output, r"\d+ files, [\d.]+ MB uncompressed")
        self.assertIn("Last.fm enrichment: skipped", self.output)
        self.assertIn("bundle checks passed", self.output)


@unittest.skipUnless((FULL_BUNDLE / "fixtures" / "manifest.json").exists(),
                     "no full build in dist/web-demo (python dev/build_web_demo.py)")
class FullBundleCoverageTest(FixtureCoverage, unittest.TestCase):
    strict = True
    root = FULL_BUNDLE

    def test_budget(self):
        files = [p for p in self.root.rglob("*") if p.is_file()]
        self.assertLessEqual(len(files), 1500)
        self.assertLessEqual(sum(p.stat().st_size for p in files), 15_000_000)


if __name__ == "__main__":
    unittest.main()
