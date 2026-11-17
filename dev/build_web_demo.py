"""Build the static, in-browser demo: dist/web-demo/ (the live demo on dhole.dev).

    python dev/build_web_demo.py [--out dist/web-demo] [--lastfm-cache DIR]

The result is the real interface (templates/index.html + static/js + static/css) plus JSON
fixtures and an in-browser mock of the API (static/js/demo/). It needs no server: any static
host works, under any base path, with a strict Content-Security-Policy (no inline scripts; the
only external requests are album covers from Spotify's image CDN, i.scdn.co).

How it is made, in one process and without any network access to Spotify:

1. The clock is frozen at DEMO_NOW, a few minutes after the newest play in demo/snapshot/
   (the author's history, frozen on 2026-10-04), so "today", "this week" and "this month" are the
   snapshot's. The browser runs on the same clock (static/js/demo/clock.js). Unlike
   ``app.py --demo``, dates are NOT moved forward: the web demo shows the real dates.
2. A temporary data folder is seeded from the snapshot, app.py is imported in demo mode
   (FakeSpotify; *.spotify.com blocked), the plays ledger and the backlog index are built, and
   the API is crawled in-process with Flask's test client (no HTTP server, no poll loop).
3. The crawl becomes fixtures/ (reports for every period reachable with the < > buttons, year in
   music for every year, the artist index, artist pages, recent plays, library, settings, ...).
   Live parts (what's playing, the backlog, clearing, undo, likes) are simulated in the browser
   from these fixtures by static/js/demo/sim.js.

Last.fm (artist bios, tags, similar artists) is optional: with LASTFM_API_KEY in the environment
the top artists' Last.fm data is fetched once (bounded timeouts, polite pacing) and cached in
--lastfm-cache, so rebuilds are reproducible and never refetch. Without a key, or when Last.fm
can't be reached, the demo is built without it and the summary says so.

The time zone matters (the plays ledger files plays under local days): the snapshot is from
America/Denver. On macOS/Linux the build switches to it; on Windows the machine's zone must
match (the build stops otherwise, unless --any-timezone).

Output is deterministic: the same snapshot and code give byte-identical files.
"""

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = APP_DIR / "demo" / "snapshot"
DEFAULT_OUT = APP_DIR / "dist" / "web-demo"
DEFAULT_LASTFM_CACHE = APP_DIR / "dist" / ".cache" / "lastfm"

DEMO_TZ = "America/Denver"
NOW_PAD_MS = 4 * 60_000            # DEMO_NOW = newest play + 4 min, rounded up to the minute
SIM_TRACK_SECONDS = 45             # how long each simulated track plays in the browser
SIZE_BUDGET = 15_000_000           # bytes on disk (fixtures are stored gzipped)
FILE_BUDGET = 1500
TOP_STATS = 0                      # artists with a full artist page: 0 = all (gzipped, ~0.7 MB);
                                   # else the top N + every artist a list, the backlog or recent
                                   # plays shows, and the mock builds a short page for the rest
TOP_INFO = 40                      # artists with artist-info (Spotify genres, Last.fm bio/tags)
HISTORY_PLAYS = 2000               # recent plays in Library -> Recent plays
LIBRARY_TRACKS = 5000              # local search index: most played tracks / albums
LIBRARY_ALBUMS = 2500
REPORT_LIMIT = 25                  # the Reports tab always asks for the top 25
ARTIST_SHARD = 64                  # artist pages per fixture file (fewer, still small, files)
MAX_PERIODS = 0                    # reports per period type: 0 = every reachable one (--sample: a few)
REPO_URL = "https://github.com/Dholeson/spotify-discover-cleaner"
ABOUT_URL = "../discover-cleaner"
STATIC_SKIP = ("classic",)         # the old interface is not part of the demo
VOLATILE_KEYS = {"elapsed_ms", "plays_build_ms", "build_ms"}
PLACEHOLDER_IDS = ("fakeartist", "fakealbum", "spotify:artist:fake")   # FakeSpotify's made-up ids

H = {"Host": "127.0.0.1:8888"}


def log(msg):
    print(f"  {msg}", flush=True)


# ---------------------------------------------------------------------------- snapshot + clock
def newest_event_ms(snapshot_dir):
    with gzip.open(Path(snapshot_dir) / "events.jsonl.gz", "rt", encoding="utf-8") as fh:
        cols = json.loads(fh.readline())["columns"]
        i = cols.index("ts_ms")
        return max((json.loads(line)[i] or 0) for line in fh if line.strip())


def demo_now_ms(newest_ms):
    return -(-(newest_ms + NOW_PAD_MS) // 60_000) * 60_000


class FrozenClock:
    """time.time()/time_ns(), datetime.now()/today() and date.today() return DEMO_NOW.

    Installed before app.py or src/ are imported, so their ``from datetime import datetime``
    binds the frozen classes. time.monotonic() and time.sleep() stay real (pacing, timeouts).
    """

    def __init__(self, now_ms):
        self.now_ms = int(now_ms)

    def install(self):
        now_s = self.now_ms / 1000.0
        now_ns = self.now_ms * 1_000_000
        base_dt, base_d = _dt.datetime, _dt.date

        class FrozenDateTime(base_dt):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now_s, tz)

            @classmethod
            def today(cls):
                return cls.fromtimestamp(now_s)

            @classmethod
            def utcnow(cls):
                return cls.fromtimestamp(now_s, _dt.timezone.utc).replace(tzinfo=None)

        class FrozenDate(base_d):
            @classmethod
            def today(cls):
                return cls.fromtimestamp(now_s)

        time.time = lambda: now_s
        time.time_ns = lambda: now_ns
        _dt.datetime = FrozenDateTime
        _dt.date = FrozenDate
        return self


def use_demo_timezone(now_ms, allow_any=False):
    """Make local time America/Denver (POSIX) or check that it already is (Windows)."""
    if hasattr(time, "tzset"):
        os.environ["TZ"] = DEMO_TZ
        time.tzset()
    off = time.localtime(now_ms / 1000).tm_gmtoff
    jan = time.localtime(time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, -1))).tm_gmtoff
    if (off, jan) != (-6 * 3600, -7 * 3600) and not allow_any:
        raise SystemExit(f"  x The local time zone isn't {DEMO_TZ} (UTC offset {off / 3600:+.0f} h on the "
                         "demo day). Set the machine's zone to Mountain Time, or pass --any-timezone "
                         "(days in the reports would then be the local ones).")


# ---------------------------------------------------------------------------- Last.fm cache
class LastfmCache:
    """Wraps app.lastfm_call: answers from a file cache, fetches (politely) only what's missing.

    Never hangs the build: every request has app.py's bounded timeout, and after a few network
    failures in a row Last.fm is treated as unreachable for the rest of the build.
    """

    def __init__(self, cache_dir, core):
        self.dir = Path(cache_dir)
        self.core = core
        self.real = core.lastfm_call
        self.key = bool(core.lastfm_api_key())
        self.hits = self.fetched = self.failed = 0
        self.unreachable = False
        self._fails_in_row = 0
        self._last_call = 0.0
        self._lock = threading.Lock()

    def path_for(self, method, params):
        ident = json.dumps([method, sorted((k, str(v)) for k, v in params.items())], ensure_ascii=False)
        return self.dir / f"{hashlib.sha1(ident.encode('utf-8')).hexdigest()[:20]}.json"

    def __call__(self, method, **params):
        core = self.core
        path = self.path_for(method, params)
        with self._lock:
            if path.exists():
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                    self.hits += 1
                    return core._LFM_MISSING if rec.get("missing") else rec["data"]
                except (ValueError, KeyError):
                    pass
            if not self.key or self.unreachable:
                return core._LFM_FAIL
            wait = 0.25 - (time.monotonic() - self._last_call)   # <= 4 requests/s
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            out = self.real(method, **params)
            if out is core._LFM_FAIL:
                self.failed += 1
                self._fails_in_row += 1
                if self._fails_in_row >= 3:
                    self.unreachable = True
                return out
            self._fails_in_row = 0
            self.fetched += 1
            self.dir.mkdir(parents=True, exist_ok=True)
            rec = {"method": method, "params": {k: str(v) for k, v in sorted(params.items())},
                   "missing": out is core._LFM_MISSING, "data": None if out is core._LFM_MISSING else out}
            path.write_text(json.dumps(rec, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            return out

    def summary(self):
        if not self.key and not self.hits:
            return "skipped (no LASTFM_API_KEY in the environment and nothing cached)"
        bits = [f"{self.hits} cached", f"{self.fetched} fetched"]
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.unreachable:
            bits.append("Last.fm unreachable: built without the rest")
        return ", ".join(bits)


# ---------------------------------------------------------------------------- the app
def boot_app(data_dir, frozen_on):
    """Seed data_dir (no date shift), import app.py in demo mode and get it ready to crawl."""
    os.environ.update(DISCOVER_DEMO="1", DISCOVER_DEMO_DIR=str(data_dir), DISCOVER_DATA_DIR=str(data_dir),
                      LASTFM_AUTO_SYNC="0", APP_DEBUG="0", APP_USE_RELOADER="0")
    os.environ.pop("FAKE_PL_SIZE", None)
    os.environ.pop("FAKE_API_MODE", None)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    from src.devtools import demo as demo_mod
    t0 = time.perf_counter()
    seeded = demo_mod.seed(data_dir, SNAPSHOT_DIR, today=_dt.date.fromisoformat(frozen_on))
    assert seeded["shift_days"] == 0, seeded
    log(f"seeded {seeded['rows'].get('events', 0):,} events (no date shift) in {time.perf_counter() - t0:.1f} s")

    import app as core
    assert core.DEMO_MODE and Path(core.DATA_DIR).resolve() == Path(data_dir).resolve(), core.DATA_DIR
    core.app.testing = True
    # FakeSpotify is local: no pacing or budgets needed while crawling.
    core._SPOTIFY_MIN_INTERVAL_S = 0.0
    for lane in list(core._SPOTIFY_LANE_MIN_INTERVAL_S):
        core._SPOTIFY_LANE_MIN_INTERVAL_S[lane] = 0.0
    for pol in core._SPOTIFY_GUARD_POLICIES.values():
        if pol.get("budget"):
            pol["budget"] = [1_000_000, 60]
    core._db.init_db()
    core._migrate.migrate_if_needed()

    t0 = time.perf_counter()
    from src.persistence import plays as P
    conn = core._db.get_conn()
    P.rebuild(conn)
    from src.services import backlog_index, reports, artist_stats
    outcome = backlog_index.get_worker().sync(core.get_sp())
    reports.get_ledger(conn, wait=True)
    artist_stats.warm_caches(conn)
    log(f"plays ledger + backlog index ready ({outcome}) in {time.perf_counter() - t0:.1f} s")
    return core


class Crawler:
    def __init__(self, core):
        self.core = core
        self.client = core.app.test_client()
        self.requests = 0

    def call(self, method, path, params=None, body=None, ok=(200,)):
        self.requests += 1
        kw = {"query_string": params or {}, "headers": dict(H)}
        if method != "GET":
            kw["headers"]["Origin"] = "http://127.0.0.1:8888"
            kw["json"] = body if body is not None else {}
        r = self.client.open(path, method=method, **kw)
        data = r.get_json(silent=True)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} {params or ''} -> {r.status_code}: {r.get_data(as_text=True)[:300]}")
        return scrub(data)

    def get(self, path, ok=(200,), **params):
        return self.call("GET", path, params, ok=ok)

    def post(self, path, body=None, ok=(200,)):
        return self.call("POST", path, body=body, ok=ok)


def _placeholder(v):
    return isinstance(v, str) and any(m in v for m in PLACEHOLDER_IDS)


def scrub(obj):
    """Drop timing fields that differ between runs (the output must be reproducible) and the
    simulated Spotify's placeholder artist/album ids (a link built from one would be dead)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in VOLATILE_KEYS:
                continue
            if _placeholder(v) and (k in ("id", "album_id", "artist_id", "uri", "url", "spotify_url")):
                v = "" if k in ("url", "spotify_url") else None
            out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def walk_artists(obj, out):
    """Artist names mentioned anywhere in a payload (top lists, discoveries, history rows...)."""
    if isinstance(obj, dict):
        for k in ("artist", "artist_name"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
        key = obj.get("key")
        if isinstance(obj.get("name"), str) and isinstance(key, str) and "|" not in key and key:
            out.add(obj["name"].strip())
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_artists(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_artists(v, out)
    return out


def compact_table(rows, columns):
    return {"columns": list(columns), "rows": [[r.get(c) for c in columns] for r in rows]}


# ---------------------------------------------------------------------------- crawl
def crawl(core, lfm):
    c = Crawler(core)
    from src.services import artist_stats as AS
    fx = {}                     # fixture path -> JSON object
    mentioned = set()
    conn = core._db.get_conn()

    # -- live templates: the state the simulator starts from
    status = c.get("/api/status")
    assert status["setup"] == {"demo": True, "needs_credentials": False, "needs_playlist": False}, status["setup"]
    backlog_items = []
    off = 0
    while True:
        page = c.get("/api/v2/backlog/tracks", offset=off, limit=200)
        backlog_items += page["items"]
        off += len(page["items"])
        if not page["items"] or off >= page["matched"]:
            break
    backlog_meta = {k: v for k, v in page.items() if k not in ("items", "offset", "limit", "q", "matched")}
    walk_artists(backlog_items, mentioned)

    # -- Insights: reports for every period the < > buttons reach
    reports_index = {}
    definitions = None
    for period in ("week", "month", "year"):
        starts = []
        anchor = None
        while True:
            params = {"period": period, "limit": REPORT_LIMIT}
            if anchor:
                params["anchor"] = anchor
            rep = c.get("/api/v2/reports", **params)
            definitions = definitions or rep.get("definitions")
            if rep.get("definitions") == definitions:
                rep.pop("definitions", None)
            p = rep["period"]
            fx[f"reports/{period}/{p['start_day']}.json"] = rep
            starts.append(p["start_day"])
            walk_artists(rep.get("top"), mentioned)
            walk_artists(rep.get("discoveries"), mentioned)
            # The UI stops at the period whose previous one has no figures (history starts there).
            if not p.get("prev_anchor") or (rep.get("totals") or {}).get("prev_plays") is None:
                break
            if MAX_PERIODS and len(starts) >= MAX_PERIODS:
                break
            anchor = p["prev_anchor"]
        reports_index[period] = sorted(starts)
    for period in ("all", "last7", "last30"):
        rep = c.get("/api/v2/reports", period=period, limit=REPORT_LIMIT)
        if rep.get("definitions") == definitions:
            rep.pop("definitions", None)
        fx[f"reports/{period}.json"] = rep
        walk_artists(rep.get("top"), mentioned)
    fx["reports/definitions.json"] = definitions or {}
    log("reports: " + ", ".join(f"{len(v)} {k}s" for k, v in reports_index.items()) + " + all, last 7/30 days")

    overview = c.get("/api/v2/insights/overview")
    fx["insights/overview.json"] = overview
    walk_artists(overview, mentioned)
    years = sorted(r["year"] for r in overview.get("plays_by_year") or [] if r.get("plays"))
    for y in years:
        yim = c.get("/api/v2/insights/year-in-music", year=str(y))
        fx[f"insights/year/{y}.json"] = yim
        walk_artists(yim, mentioned)
    fx["insights/backlog.json"] = c.get("/api/v2/backlog/insights")

    # -- recent plays
    history = []
    before = None
    while len(history) < HISTORY_PLAYS:
        params = {"limit": 200}
        if before:
            params["before_ms"] = before
        page = c.get("/api/v2/history", **params)
        history += page["items"]
        before = page.get("next_before_ms")
        if not before:
            break
    history = history[:HISTORY_PLAYS]
    hist_cols = ["id", "ts_ms", "day", "source", "artist", "artist_key", "track", "album", "spotify_track_id",
                 "listened_s", "is_backlog", "bulk", "song_key", "hidden", "image"]
    fx["library/history.json"] = compact_table(history, hist_cols)
    walk_artists(history, mentioned)

    # -- the artist index (the mock filters, sorts and pages it like the server)
    artists = []
    off = 0
    while True:
        page = c.get("/api/v2/artists", limit=500, offset=off, sort="plays", hidden="include")
        artists += page["items"]
        off += len(page["items"])
        if not page["items"] or off >= page["total"]:
            break
    by_key = {a["key"]: a for a in artists}

    # -- artist pages: the top artists + every artist a list, the backlog or recent plays shows
    want = {a["key"] for a in (artists[:TOP_STATS] if TOP_STATS else artists)}
    for name in mentioned:
        k = AS.artist_key(name)
        if k in by_key:
            want.add(k)
    stats_ids = {}
    stat_keys = sorted(want, key=lambda k: (-by_key[k]["plays"], k))
    for k in stat_keys:
        st = c.get("/api/v2/artist/" + urllib.parse.quote(by_key[k]["name"], safe="") + "/stats")
        if not st.get("found"):
            continue
        i = len(stats_ids) + 1
        stats_ids[k] = i
        fx.setdefault(f"artist/{(i - 1) // ARTIST_SHARD}.json", {})[str(i)] = st
    small = next((by_key[k] for k in reversed(stat_keys) if k in stats_ids), None)
    log(f"artist pages: {len(stats_ids)} full of {len(artists):,} artists")

    # -- artist info (Spotify genres from what the app saw + Last.fm when available)
    info_ids = {}
    infos = fx["artist-info.json"] = {}
    for i, k in enumerate([k for k in stat_keys if k in stats_ids][:TOP_INFO], 1):
        info = c.get("/api/artist-info", artist_name=by_key[k]["name"], source="page")
        infos[str(i)] = sanitize_artist_info(info)
        info_ids[k] = i

    fx["artists.json"] = {
        "columns": ["key", "name", "plays", "rank", "first_ts", "last_ts", "hidden", "stats", "info"],
        "rows": [[a["key"], a["name"], a["plays"], a["rank"], a["first_ts"], a["last_ts"], bool(a["hidden"]),
                  stats_ids.get(a["key"], 0), info_ids.get(a["key"], 0)] for a in artists],
    }

    # -- local search index (most played tracks and albums; artists come from artists.json)
    idx = AS._build_library_index(conn)
    fx["library/search.json"] = {
        "tracks": [[name, a_key, plays, sp_id, s_key] for _f, name, a_key, plays, sp_id, s_key in idx["tracks"][:LIBRARY_TRACKS]],
        "albums": [[name, a_key, plays, al_key] for _f, name, a_key, plays, al_key in idx["albums"][:LIBRARY_ALBUMS]],
        "tracks_total": len(idx["tracks"]), "albums_total": len(idx["albums"]),
    }

    # -- library tabs
    liked = None
    off = 0
    while True:
        page = c.get("/api/liked-songs", limit=50, offset=off)
        if liked is None:
            liked = dict(page)
        else:
            liked["tracks"] += page["tracks"]
        off += len(page["tracks"])
        if not page["tracks"] or off >= page.get("total", 0):
            break
    liked.update(offset=0, limit=len(liked["tracks"]), next_offset=None)
    fx["library/liked.json"] = liked
    fx["library/favorites.json"] = c.get("/api/favorites/overview")
    fx["library/watchlist.json"] = c.get("/api/v2/watchlist")
    research = {"all": c.get("/api/research"), "active": c.get("/api/research", status="active"),
                "seed_tags": c.get("/api/research/seed-tags")}
    fx["library/research.json"] = research
    entries = [{"name": t.get("name") or "", "artist": t.get("artist") or (t.get("artists") or "").split(", ")[0],
                "id": t.get("id")} for t in (liked.get("tracks") or [])]
    entries += [{"name": t["name"], "artist": (t.get("artist_list") or [{}])[0].get("name") or t.get("artists") or "",
                 "id": t["id"]} for t in backlog_items]
    counts = c.post("/api/v2/library/play-counts", {"tracks": entries[:500]})["tracks"]
    if len(entries) > 500:
        counts += c.post("/api/v2/library/play-counts", {"tracks": entries[500:1000]})["tracks"]
    fx["library/track-counts.json"] = {f"{e['artist'].lower()}|{e['name'].lower()}|{e['id'] or ''}": n
                                       for e, n in zip(entries, counts)}

    # -- settings and data
    settings = c.get("/api/settings")
    settings["env_path"] = ".env"     # a local path of the build machine otherwise
    fx["settings.json"] = {
        "settings": settings,
        "health": c.get("/api/v2/data/health"),
        "lastfm_import": c.get("/api/lastfm/import"),
        "spotify_import": c.get("/api/spotify/import"),
        "setup": c.get("/api/v2/setup"),
        "playlists": c.get("/api/playlists"),
        "dev_errors": {"errors": []},
    }
    calls = c.get("/api/dev/spotify-calls")
    calls["calls"] = []
    fx["settings.json"]["spotify_calls"] = calls

    # -- templates for the simulator: real responses of the live routes (shapes to copy)
    catch_up = c.get("/api/v2/backlog/catch-up")
    search_sample = c.get("/api/search", q="dance", type="track,artist")
    templates = {"catch_up": catch_up, "search": {k: v[:1] for k, v in search_sample.items() if isinstance(v, list)}}
    target = backlog_items[-1]
    cleared = c.post("/api/v2/backlog/clear", {"track_id": target["id"], "skip": False})
    templates["clear"] = cleared
    templates["undo"] = c.post("/api/v2/backlog/undo", {"seq": cleared["cleared"]["seq"]})
    templates["auto_clean"] = c.post("/api/v2/backlog/auto-clean", {"enabled": True})
    templates["threshold"] = c.post("/api/threshold", {"seconds": status.get("threshold_seconds") or 15})
    templates["status_after_clear"] = {k: c.get("/api/status").get(k) for k in ("recently_cleared", "stats", "today")}
    fx["live.json"] = {
        "status": status,
        "backlog": {"meta": backlog_meta, "items": backlog_items},
        "templates": templates,
        "artist_template": {"name": small["name"], "key": small["key"]} if small else None,
        # what the server says for a name that isn't in the history (the "new to you" page)
        "artist_unknown": c.get("/api/v2/artist/Nobody In This History/stats"),
    }
    drop_placeholders(fx)
    log(f"{c.requests} API requests crawled; Last.fm: {lfm.summary()}")
    return fx, {"reports": reports_index, "years": years, "artists": len(artists), "stats": len(stats_ids),
                "info": len(info_ids), "history": len(history)}


def drop_placeholders(fx):
    """FakeSpotify fills what the snapshot doesn't know with constants (every track 3:20, added on
    May 1, liked on Sep 1, releases on Jan 1 2020). A public demo must not present those as real:
    durations become the simulated play length, unknown dates become empty, and the release radar
    (which would be made up) is replaced by a note."""
    sim_ms = SIM_TRACK_SECONDS * 1000
    live = fx["live.json"]

    def fix_track(t, date_key):
        if isinstance(t, dict):
            if "duration_ms" in t:
                t["duration_ms"] = sim_ms
            if date_key in t:
                t[date_key] = None

    for t in live["backlog"]["items"]:
        fix_track(t, "added_at")
    tpl = live["templates"]
    fix_track(tpl["clear"]["cleared"], "added_at")
    fix_track(tpl["undo"]["restored"], "added_at")
    for e in tpl["status_after_clear"].get("recently_cleared") or []:
        fix_track(e, "added_at")
    for t in fx["library/liked.json"].get("tracks") or []:
        fix_track(t, "liked_at")
    fav = fx["library/favorites.json"]
    fav.update(release_feed=[], release_total=0, release_checked=0, release_partial=False, release_demo=True,
               release_error="New releases come from Spotify’s catalog, which the demo doesn’t call. "
                             "Run Discover Cleaner with your own Spotify to follow your favorites’ releases.")


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _strip_emails(value):
    """Last.fm bios sometimes end with a band's booking address; a public page shouldn't
    republish third-party email addresses for scrapers, so they are dropped from all text."""
    if isinstance(value, str):
        return _EMAIL_RE.sub("", value)
    if isinstance(value, list):
        return [_strip_emails(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_emails(v) for k, v in value.items()}
    return value


def sanitize_artist_info(info):
    """Keep what is real. FakeSpotify's artist objects carry placeholder ids, follower counts and
    an album cover as the "artist photo", and its "popular tracks" are just the backlog's tracks:
    none of that may be presented as Spotify data in a public demo."""
    keep = ("name", "bio", "lastfm_tags", "lastfm_url", "related", "similar", "stats", "listen_stats",
            "spotify_genres", "matches_taste", "my_plays_available", "artist_liked")
    out = {k: _strip_emails(info.get(k)) for k in keep if k in info}
    img = info.get("artist_image")
    if img:
        out.update(artist_image=img, artist_image_small=info.get("artist_image_small") or img,
                   artist_image_kind="album")
    out["spotify_limited"] = False
    out["demo"] = True
    return out


# ---------------------------------------------------------------------------- write
def file_hash(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:10]


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render_index(core, out_dir, config):
    static = APP_DIR / "static"

    def asset_url(rel):
        return f"static/{rel}?v={file_hash(static / rel)}"

    core.app.jinja_env.globals["asset_url"] = asset_url
    with core.app.test_request_context("/"):
        page = core.render_template("index.html", poll_interval=3, demo_mode=True)
    start = page.index('<script type="application/json" id="app-config">')
    end = page.index("</script>", start) + len("</script>")
    cfg = json.dumps(config, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = page[:start] + f'<script type="application/json" id="app-config">{cfg}</script>' + page[end:]
    head_add = (f'<link rel="stylesheet" href="{asset_url("css/demo.css")}">\n'
                '<meta name="description" content="Live demo of Discover Cleaner: a Spotify backlog cleaner and '
                'Last.fm-style listening reports, running in your browser on real listening history.">\n')
    page = page.replace("</head>", head_add + "</head>", 1)
    boot_tag = f'<script src="{asset_url("js/boot.js")}"></script>'
    main_tag = f'<script type="module" src="{asset_url("js/main.js")}"></script>'
    assert boot_tag in page and main_tag in page, "templates/index.html changed: update render_index()"
    page = page.replace(boot_tag, f'<script src="{asset_url("js/demo/clock.js")}"></script>\n{boot_tag}')
    page = page.replace(main_tag, f'<script type="module" src="{asset_url("js/demo/main.js")}"></script>')
    page = page.replace(' The classic view is at <a href="/classic">/classic</a>.', "")
    if "<script>" in page or "/static/" in page or 'href="/' in page or 'src="/' in page:
        raise SystemExit("  x index.html still has an inline script or a root-relative URL")
    (out_dir / "index.html").write_text(page, encoding="utf-8", newline="\n")


def write_bundle(core, out_dir, fixtures, info, now_ms, meta):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "static").mkdir(parents=True)
    src = APP_DIR / "static"
    for child in sorted(src.iterdir()):
        if child.name in STATIC_SKIP:
            continue
        if child.is_dir():
            shutil.copytree(child, out_dir / "static" / child.name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.map", ".DS_Store"))
        else:
            shutil.copy2(child, out_dir / "static" / child.name)
    fx_dir = out_dir / "fixtures"
    fingerprint = hashlib.sha1()
    for rel in sorted(fixtures):
        # Stored gzipped (deterministic: mtime 0); static/js/demo/fixtures.js inflates them with
        # the browser's DecompressionStream, so no host configuration is needed.
        data = dump(fixtures[rel])
        fingerprint.update(rel.encode() + b"\0" + data)
        p = fx_dir / (rel + ".gz")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    manifest = {
        "format": 1,
        "demo_now_ms": now_ms,
        "frozen_on": meta["frozen_on"],
        "time_zone": DEMO_TZ,
        "reports": info["reports"],
        "years": info["years"],
        "counts": {k: info[k] for k in ("artists", "stats", "info", "history")},
        "artist_shard": ARTIST_SHARD,
        "files": sorted(rel + ".gz" for rel in fixtures),
        "fingerprint": fingerprint.hexdigest()[:12],
    }
    (fx_dir / "manifest.json").write_bytes(dump(manifest))
    config = {
        "pollIntervalMs": 3000,
        "demo": True,
        "webDemo": {
            "now": now_ms, "frozenOn": meta["frozen_on"], "timeZone": DEMO_TZ, "user": meta.get("user_display_name"),
            "simSeconds": SIM_TRACK_SECONDS, "about": ABOUT_URL, "repo": REPO_URL,
            "fixtures": "fixtures/", "version": manifest["fingerprint"],
        },
    }
    render_index(core, out_dir, config)
    (out_dir / "README.txt").write_text(
        "Discover Cleaner - static in-browser demo. Built by dev/build_web_demo.py from demo/snapshot/;\n"
        "do not edit by hand, rebuild instead. Serve this folder as is (any base path, hash routing).\n",
        encoding="utf-8", newline="\n")


def bundle_stats(out_dir):
    files = [p for p in out_dir.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files), files


def check_bundle(out_dir):
    """Things that would break the demo on a static host with a strict CSP."""
    problems = []
    n, size, files = bundle_stats(out_dir)
    if n > FILE_BUDGET:
        problems.append(f"{n} files (budget {FILE_BUDGET})")
    if size > SIZE_BUDGET:
        problems.append(f"{size / 1e6:.1f} MB (budget {SIZE_BUDGET / 1e6:.0f} MB)")
    for p in files:
        if p.suffix in (".json", ".html", ".txt", ".gz"):
            raw = p.read_bytes()
            text = (gzip.decompress(raw) if p.suffix == ".gz" else raw).decode("utf-8")
            if p.suffix == ".gz":
                json.loads(text)
            for bad in ("C:\\", "C:/Users", "/home/", "/Users/", "AppData", "@gmail", "fakeartist", "fakealbum"):
                if bad in text:
                    problems.append(f"{p.relative_to(out_dir)} contains {bad!r}")
                    break
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder (default dist/web-demo)")
    ap.add_argument("--lastfm-cache", type=Path, default=Path(os.environ.get("WEB_DEMO_LASTFM_CACHE") or DEFAULT_LASTFM_CACHE),
                    help="where Last.fm answers are cached between builds (default dist/.cache/lastfm)")
    ap.add_argument("--any-timezone", action="store_true", help="build even if local time isn't America/Denver")
    ap.add_argument("--sample", action="store_true",
                    help="a small, quick build (a few periods and artists) for tests; not for publishing")
    args = ap.parse_args(argv)
    if args.sample:
        global MAX_PERIODS, TOP_STATS, TOP_INFO, HISTORY_PLAYS, LIBRARY_TRACKS, LIBRARY_ALBUMS
        MAX_PERIODS, TOP_STATS, TOP_INFO, HISTORY_PLAYS, LIBRARY_TRACKS, LIBRARY_ALBUMS = 4, 100, 3, 400, 500, 300
    out_dir = args.out.resolve()
    if out_dir == APP_DIR or out_dir in APP_DIR.parents:
        raise SystemExit(f"  x Refusing to write the demo into {out_dir}")
    if out_dir.exists() and any(out_dir.iterdir()) and not (out_dir / "fixtures" / "manifest.json").exists():
        raise SystemExit(f"  x {out_dir} isn't empty and isn't a previous demo build: choose another --out")
    t_start = time.perf_counter()
    meta = json.loads((SNAPSHOT_DIR / "meta.json").read_text(encoding="utf-8"))
    now_ms = demo_now_ms(newest_event_ms(SNAPSHOT_DIR))
    use_demo_timezone(now_ms, args.any_timezone)
    FrozenClock(now_ms).install()
    print(f"Building the web demo: snapshot frozen on {meta['frozen_on']}, "
          f"clock frozen at {_dt.datetime.fromtimestamp(now_ms / 1000):%Y-%m-%d %H:%M} ({DEMO_TZ})", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="dc_web_demo_"))
    try:
        core = boot_app(tmp / "data", meta["frozen_on"])
        lfm = LastfmCache(args.lastfm_cache, core)
        core.lastfm_call = lfm
        fixtures, info = crawl(core, lfm)
        write_bundle(core, out_dir, fixtures, info, now_ms, meta)
    finally:
        try:
            import atexit
            from src.services import backlog_index
            idx = backlog_index.get_index()
            idx.flush_persist(True)
            atexit.unregister(idx.flush_persist)
            from src.persistence import db as _db
            _db.get_conn().close()   # SQLite connections are per thread: this one did all the work
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    problems = check_bundle(out_dir)
    n, size, files = bundle_stats(out_dir)
    groups = {}
    for f in files:
        rel = f.relative_to(out_dir).parts
        g = "/".join(rel[:2]) if rel[0] in ("fixtures", "static") and len(rel) > 2 else rel[0]
        cnt, sz = groups.get(g, (0, 0))
        groups[g] = (cnt + 1, sz + f.stat().st_size)
    for g, (cnt, sz) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        print(f"    {g:<28} {cnt:>5} files {sz / 1e6:>7.2f} MB")
    print(f"\n  Wrote {out_dir}")
    print(f"  {n} files, {size / 1e6:.2f} MB uncompressed (budget {FILE_BUDGET} files, "
          f"{SIZE_BUDGET / 1e6:.0f} MB); {len(fixtures)} fixtures; {time.perf_counter() - t_start:.0f} s")
    print(f"  Last.fm enrichment: {lfm.summary()}")
    if problems:
        print("  x Problems:\n    - " + "\n    - ".join(problems))
        return 1
    print("  ok: bundle checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
