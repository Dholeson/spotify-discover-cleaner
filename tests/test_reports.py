"""Reports & insights (WP-06): period bounds, period reports, overview, year in music, today
counts, backlog insights and the v2 routes, all over small synthetic ledgers.

Every fixture lives in an in-memory SQLite database; the real app, DB and config are never
touched (the routes run against a stub ``app`` module).
"""

import json
import sqlite3
import sys
import threading
import types
import unittest
from datetime import date, datetime, timedelta

from src.services import backlog_insights as B
from src.services import reports as R

DAY_MS = 86_400_000

SPEC_DDL = """
CREATE TABLE plays (
  id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE, source TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  day_local TEXT NOT NULL, hour_local INTEGER NOT NULL, dow INTEGER NOT NULL, ym TEXT NOT NULL, year INTEGER NOT NULL,
  artist_name TEXT NOT NULL, artist_key TEXT NOT NULL, track_name TEXT NOT NULL, track_key TEXT NOT NULL,
  song_key TEXT NOT NULL, album_name TEXT, album_key TEXT, spotify_track_id TEXT, listened_s REAL,
  linked_live_event_id INTEGER, is_backlog INTEGER NOT NULL DEFAULT 0, ts_quality TEXT NOT NULL DEFAULT 'ok',
  superseded_by INTEGER);
CREATE INDEX plays_ts ON plays(ts_ms);
CREATE TABLE artist_stats (artist_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, first_ts INTEGER, last_ts INTEGER,
  plays INTEGER NOT NULL DEFAULT 0, days_listened INTEGER, peak_year INTEGER, is_music INTEGER NOT NULL DEFAULT 1,
  spotify_artist_id TEXT, lfm_listeners INTEGER, lfm_fetched_at INTEGER);
CREATE TABLE track_stats (track_key TEXT PRIMARY KEY, song_key TEXT, artist_key TEXT, display_name TEXT NOT NULL,
  first_ts INTEGER, last_ts INTEGER, plays INTEGER NOT NULL DEFAULT 0, est_duration_s REAL, peak7 INTEGER,
  peak7_start INTEGER);
CREATE TABLE album_stats (album_key TEXT PRIMARY KEY, artist_key TEXT, display_name TEXT NOT NULL,
  first_ts INTEGER, last_ts INTEGER, plays INTEGER NOT NULL DEFAULT 0);
"""

EVENTS_DDL = """
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  day TEXT NOT NULL, track_id TEXT, artist_id TEXT, album_id TEXT, payload TEXT, source TEXT DEFAULT 'live');
CREATE INDEX events_ts ON events(ts_ms);
"""


_OPEN = []   # fixture connections, closed after each test


def ms(y, m, d, h=12, mi=0, s=0):
    """Epoch ms of a LOCAL wall-clock time (the same way the ledger computes local days)."""
    return int(datetime(y, m, d, h, mi, s).timestamp() * 1000)


NOW = ms(2026, 9, 25, 15, 4)   # a Friday afternoon


def transitions(year):
    """Local dates in ``year`` whose midnight-to-midnight length isn't 24 h (DST changes)."""
    out = []
    d = date(year, 1, 1)
    while d.year == year:
        n = d + timedelta(days=1)
        if R.midnight_ms(n) - R.midnight_ms(d) != DAY_MS:
            out.append((d, (R.midnight_ms(n) - R.midnight_ms(d)) // 3_600_000))
        d = n
    return out


class Ledger:
    """A tiny plays ledger (SPEC DDL) plus an events table."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        _OPEN.append(self.conn)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SPEC_DDL + EVENTS_DDL)
        self._id = 0

    def play(self, ts, artist, title, album=None, source="lastfm", quality="ok", listened_s=None,
             superseded_by=None, song=None, spotify_id=None):
        self._id += 1
        dt = datetime.fromtimestamp(ts / 1000)
        ak = artist.casefold()
        alk = f"{ak}|{album.casefold()}" if album else None
        self.conn.execute(
            "INSERT INTO plays (id, event_id, source, ts_ms, day_local, hour_local, dow, ym, year, artist_name, "
            "artist_key, track_name, track_key, song_key, album_name, album_key, spotify_track_id, listened_s, "
            "ts_quality, superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self._id, self._id, source, ts, dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday(), dt.strftime("%Y-%m"),
             dt.year, artist, ak, title, f"{ak}|{title.casefold()}", f"{ak}|{(song or title).casefold()}",
             album, alk, spotify_id, listened_s, quality, superseded_by))
        return ts

    def run(self, n, start, artist, title, step_s=240, **kw):
        for i in range(n):
            self.play(start + i * step_s * 1000, artist, title, **kw)

    def hide(self, artist):
        self.conn.execute("INSERT OR REPLACE INTO artist_stats (artist_key, display_name, is_music) VALUES (?, ?, 0)",
                          (artist.casefold(), artist))

    def event(self, etype, ts, track_id, source="live", **payload):
        self.conn.execute("INSERT INTO events (event_type, ts_ms, day, track_id, payload, source) VALUES (?,?,?,?,?,?)",
                          (etype, ts, datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d"), track_id,
                           json.dumps(payload), source))


def standard_ledger():
    """History starts 2025-01-01 (warm-up ends 2025-04-01). March 2026 vs February 2026:

    Alpha  Feb 10 x "Hit"; Mar 12: 7 "Hit" + 5 "Hit - Live" (same song) + 20 BULK "Bulk Song" on Mar 21
    Gamma  4 in 2025, Feb 2, Mar 6 (prev < 5 -> re-entry)
    Beta   first ever play in March (5)            Delta  Feb only (8)
    Podcast (hidden) 20 plays in March; one superseded Alpha play in March.
    """
    L = Ledger()
    L.play(ms(2025, 1, 1, 10), "Old", "First")
    L.run(3, ms(2025, 6, 10, 9), "Alpha", "Hit", album="A-Album")
    L.run(4, ms(2025, 7, 1, 9), "Gamma", "G1")
    # February 2026
    L.run(10, ms(2026, 2, 10, 14), "Alpha", "Hit", album="A-Album")
    L.run(2, ms(2026, 2, 11, 14), "Gamma", "G1")
    L.run(8, ms(2026, 2, 12, 14), "Delta", "D1")
    # March 2026
    L.run(7, ms(2026, 3, 12, 14), "Alpha", "Hit", album="A-Album")
    L.run(5, ms(2026, 3, 12, 14, 28), "Alpha", "Hit - Live", album="A-Album", song="Hit")
    L.run(5, ms(2026, 3, 20, 10), "Beta", "B1", album="B-Album")
    L.run(20, ms(2026, 3, 21, 3), "Alpha", "Bulk Song", step_s=5, quality="bulk")
    L.run(6, ms(2026, 3, 22, 20), "Gamma", "G1")
    L.run(20, ms(2026, 3, 20, 8), "Podcast", "Episode", step_s=60)
    L.hide("Podcast")
    L.play(ms(2026, 3, 12, 16), "Alpha", "Hit", source="live", listened_s=200, superseded_by=999)
    L.conn.commit()
    return L


class Base(unittest.TestCase):
    def setUp(self):
        R.reset_caches()

    def tearDown(self):
        R.reset_caches()
        while _OPEN:
            _OPEN.pop().close()


# ---------------------------------------------------------------------------
# period bounds
# ---------------------------------------------------------------------------

class PeriodBoundsTests(Base):
    def local(self, ms_):
        return datetime.fromtimestamp(ms_ / 1000)

    def test_calendar_units_and_previous_period(self):
        b = R.period_bounds("week", "2026-03-18", NOW)
        self.assertEqual(self.local(b["start_ms"]), datetime(2026, 3, 16))          # ISO Monday
        self.assertEqual(self.local(b["end_ms"]), datetime(2026, 3, 23))
        self.assertEqual(self.local(b["prev_start_ms"]), datetime(2026, 3, 9))
        self.assertEqual(b["prev_end_ms"], b["start_ms"])
        self.assertTrue(b["complete"])
        self.assertFalse(b["is_current"])
        self.assertEqual(b["label"], "Mar 16 – 22, 2026")
        self.assertEqual(b["prev_label"], "Mar 9 – 15, 2026")

        b = R.period_bounds("month", "2026-04-15", NOW)
        self.assertEqual(self.local(b["start_ms"]), datetime(2026, 4, 1))
        self.assertEqual(self.local(b["end_ms"]), datetime(2026, 5, 1))
        self.assertEqual(self.local(b["prev_start_ms"]), datetime(2026, 3, 1))
        self.assertEqual((b["label"], b["prev_label"]), ("April 2026", "March 2026"))
        self.assertEqual((b["prev_anchor"], b["next_anchor"]), ("2026-03-01", "2026-05-01"))

        b = R.period_bounds("year", "2025-06-01", NOW)
        self.assertEqual(self.local(b["start_ms"]), datetime(2025, 1, 1))
        self.assertEqual(self.local(b["prev_start_ms"]), datetime(2024, 1, 1))
        self.assertEqual((b["label"], b["prev_label"]), ("2025", "2024"))

        b = R.period_bounds("last30", "2026-05-10", NOW)
        self.assertEqual(self.local(b["start_ms"]), datetime(2026, 4, 11))
        self.assertEqual(self.local(b["end_ms"]), datetime(2026, 5, 11))
        self.assertEqual(self.local(b["prev_start_ms"]), datetime(2026, 3, 12))  # same length, just before
        self.assertEqual(b["days_total"], 30)

        b = R.period_bounds("all", None, NOW, history_start_ms=ms(2021, 8, 18, 6))
        self.assertEqual(self.local(b["start_ms"]), datetime(2021, 8, 18))
        self.assertIsNone(b["prev_start_ms"])
        self.assertIsNone(b["prev_label"])

    def test_current_period_compares_same_elapsed_stretch(self):
        b = R.period_bounds("month", None, NOW)
        self.assertTrue(b["is_current"])
        self.assertFalse(b["complete"])
        self.assertTrue(b["prev_truncated"])
        self.assertEqual(self.local(b["prev_start_ms"]), datetime(2026, 8, 1))
        self.assertEqual(self.local(b["prev_end_ms"]), datetime(2026, 8, 25, 15, 4))
        self.assertEqual(b["prev_label"], "Aug 1 – 25, 2026")
        self.assertIsNone(b["next_anchor"])
        self.assertEqual(b["days_elapsed"], 25)

        b = R.period_bounds("week", None, NOW)                     # Mon Sep 21 .. now (Fri)
        self.assertEqual(self.local(b["start_ms"]), datetime(2026, 9, 21))
        self.assertEqual(self.local(b["prev_end_ms"]), datetime(2026, 9, 18, 15, 4))
        self.assertEqual(b["prev_label"], "Sep 14 – 18, 2026")

        b = R.period_bounds("year", None, NOW)
        self.assertEqual(self.local(b["prev_end_ms"]), datetime(2025, 9, 25, 15, 4))

    def test_errors_name_the_field(self):
        for period, anchor, field in (("decade", None, "period"), ("month", "2026-13-01", "anchor"),
                                      ("month", "yesterday", "anchor"), ("month", "2026-10-01", "anchor")):
            with self.assertRaises(R.BadRequest) as cm:
                R.period_bounds(period, anchor, NOW)
            self.assertEqual(cm.exception.field, field)

    def test_warmup_flag(self):
        start = ms(2025, 1, 1, 10)
        self.assertTrue(R.period_bounds("month", "2025-03-15", NOW, start)["warmup"])
        self.assertFalse(R.period_bounds("month", "2025-05-15", NOW, start)["warmup"])
        # the warm-up ends at local midnight 90 calendar days after the first day (Apr 1, 2025)
        self.assertEqual(R.warmup_end_ms(start), ms(2025, 4, 1, 0))
        self.assertFalse(R.period_bounds("month", "2025-04-10", NOW, start)["warmup"])
        self.assertTrue(R.period_bounds("week", "2025-03-31", NOW, start)["warmup"])   # starts Mon Mar 31

    def test_dst_bounds_are_local_midnights(self):
        changes = transitions(2025)
        if not changes:
            self.skipTest("the local time zone has no DST")
        for d, hours in changes:
            b = R.period_bounds("week", d.isoformat(), NOW)
            start, end = self.local(b["start_ms"]), self.local(b["end_ms"])
            self.assertEqual((start.hour, start.minute), (0, 0))
            self.assertEqual((end.hour, end.minute), (0, 0))
            self.assertEqual((end.date() - start.date()).days, 7)
            # the week really is an hour shorter/longer than 7 x 24 h
            self.assertEqual(b["end_ms"] - b["start_ms"], 7 * DAY_MS + (hours - 24) * 3_600_000)
            m = R.period_bounds("month", d.isoformat(), NOW)
            self.assertEqual(self.local(m["end_ms"]).day, 1)
            self.assertEqual(self.local(m["end_ms"]).hour, 0)

    def test_dst_current_period_keeps_wall_clock_time(self):
        changes = [d for d, h in transitions(2026) if h == 23]
        if not changes:
            self.skipTest("the local time zone has no spring-forward change")
        d = changes[0]
        now_d = d + timedelta(days=2)                           # two days after clocks moved forward
        if now_d.month != d.month or now_d.day > 28:
            self.skipTest("the change is too close to a month end for this check")
        b = R.period_bounds("month", None, ms(now_d.year, now_d.month, now_d.day, 12))
        prev_month = R._add_months(date(d.year, d.month, 1), -1)
        # the previous month is cut at the same wall-clock time, not 1 h off
        self.assertEqual(self.local(b["prev_end_ms"]), datetime(prev_month.year, prev_month.month, now_d.day, 12))


# ---------------------------------------------------------------------------
# period report
# ---------------------------------------------------------------------------

class ReportTests(Base):
    def setUp(self):
        super().setUp()
        self.L = standard_ledger()
        self.rep = R.report(self.L.conn, "month", "2026-03-15", 10, now_ms=NOW)

    def test_totals_and_deltas(self):
        t = self.rep["totals"]
        self.assertEqual(t["plays"], 43)               # 32 Alpha (12 + 20 bulk) + 5 Beta + 6 Gamma
        self.assertEqual(t["prev_plays"], 20)
        self.assertEqual(t["pct_change"], 115)
        self.assertEqual((t["artists"], t["prev_artists"]), (3, 3))
        self.assertEqual((t["new_artists"], t["pct_new_artists"]), (1, 33.3))
        self.assertEqual((t["tracks"], t["new_tracks"], t["pct_new_tracks"]), (4, 2, 50.0))
        self.assertEqual((t["albums"], t["new_albums"], t["pct_new_albums"]), (2, 1, 50.0))
        self.assertEqual(t["pct_plays_on_new_tracks"], 58.1)   # (5 + 20) / 43
        self.assertEqual(t["bulk_plays"], 20)
        self.assertEqual(t["active_days"], 3)
        self.assertEqual(t["days_in_period"], 31)
        self.assertEqual(t["avg_plays_per_active_day"], 7.7)   # 23 plays with real times / 3 days
        self.assertEqual(t["avg_plays_per_day"], 0.7)
        self.assertEqual((t["prev_active_days"], t["prev_avg_plays_per_active_day"]), (3, 6.7))   # Feb 10-12
        self.assertTrue(self.rep["period"]["complete"])
        self.assertFalse(self.rep["period"]["warmup"])

    def test_previous_active_days_stop_at_the_same_moment(self):
        L = Ledger()
        L.play(ms(2025, 1, 1), "Seed", "S")
        L.play(ms(2026, 9, 16, 9), "A", "x")      # last week, Wednesday
        L.play(ms(2026, 9, 18, 9), "A", "x")      # last week, Friday morning (before "now" o'clock)
        L.play(ms(2026, 9, 18, 16), "A", "x")     # last week, Friday after 15:04: outside the same stretch
        L.play(ms(2026, 9, 17, 9), "A", "x", quality="bulk")   # fake time: never an active day
        L.play(ms(2026, 9, 22, 9), "A", "x")
        L.conn.commit()
        t = R.report(L.conn, "week", None, 10, now_ms=NOW)["totals"]
        self.assertEqual((t["prev_plays"], t["prev_active_days"], t["prev_avg_plays_per_active_day"]), (3, 2, 1.0))

    def test_top_lists_carry_local_album_art(self):
        L = standard_ledger()
        L.play(ms(2026, 3, 13, 9), "Alpha", "Hit", album="A-Album", spotify_id="sp-hit")
        L.conn.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT, album TEXT, album_id TEXT, "
                       "image_url TEXT, spotify_url TEXT, duration_ms INTEGER, created_at TEXT)")
        L.conn.execute("INSERT INTO tracks (id, name, image_url) VALUES ('sp-hit', 'Hit', 'https://i.scdn.co/image/hit'),"
                       " ('sp-bad', 'B1', 'javascript:alert(1)')")
        L.conn.commit()
        R.reset_caches()
        top = R.report(L.conn, "month", "2026-03-15", 10, now_ms=NOW)["top"]
        by_name = {r["name"]: r for r in top["tracks"]}
        self.assertEqual(by_name["Hit"]["image"], "https://i.scdn.co/image/hit")
        self.assertIsNone(by_name["B1"]["image"])
        albums = {r["name"]: r.get("image") for r in top["albums"]}
        self.assertEqual(albums, {"A-Album": "https://i.scdn.co/image/hit", "B-Album": None})
        # no tracks table at all: no art, no error
        self.assertIsNone(self.rep["top"]["tracks"][0]["image"])
        # the year's top artist borrows their top album's cover (there is no artist photo)
        y = R.year_in_music(L.conn, 2026, now_ms=NOW)
        self.assertEqual(y["top_artist"]["name"], "Alpha")
        self.assertEqual(y["top_artist"]["image"], "https://i.scdn.co/image/hit")
        self.assertEqual(y["top_artist"]["image_from"], {"kind": "album", "name": "A-Album"})

    def test_year_top_artist_cover_beyond_the_top_lists(self):
        # Alpha's only song with art is not in the year's top 5 lists: found by the fallback query
        L = standard_ledger()
        L.play(ms(2026, 2, 3, 9), "Alpha", "Deep Cut", album="Rare EP", spotify_id="sp-deep")
        L.conn.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT, album TEXT, album_id TEXT, "
                       "image_url TEXT, spotify_url TEXT, duration_ms INTEGER, created_at TEXT)")
        L.conn.execute("INSERT INTO tracks (id, name, image_url) VALUES ('sp-deep', 'Deep Cut', 'https://i.scdn.co/image/deep')")
        L.conn.commit()
        R.reset_caches()
        top = R.year_in_music(L.conn, 2026, now_ms=NOW)["top_artist"]
        self.assertEqual(top["image"], "https://i.scdn.co/image/deep")
        self.assertEqual(top["image_from"], {"kind": "album", "name": "Rare EP"})

    def test_top_lists_badges_and_ranks(self):
        a = self.rep["top"]["artists"]
        self.assertEqual([r["name"] for r in a], ["Alpha", "Gamma", "Beta"])
        self.assertEqual((a[0]["plays"], a[0]["prev_plays"], a[0]["pct_change"], a[0]["prev_rank"], a[0]["badge"]),
                         (32, 10, 220, 1, None))
        # fewer than 5 plays last month: no % (it would be noise), a re-entry badge instead
        self.assertEqual((a[1]["prev_plays"], a[1]["pct_change"], a[1]["prev_rank"], a[1]["badge"]),
                         (2, None, 3, "re_entry"))
        self.assertEqual((a[2]["prev_plays"], a[2]["pct_change"], a[2]["prev_rank"], a[2]["badge"]),
                         (0, None, None, "new_ever"))
        tracks = self.rep["top"]["tracks"]
        self.assertEqual(tracks[0]["name"], "Bulk Song")                 # bulk plays count in tops
        self.assertEqual((tracks[1]["name"], tracks[1]["plays"]), ("Hit", 12))  # live version merged
        self.assertEqual(tracks[1]["artist"], "Alpha")
        self.assertEqual(tracks[1]["album"], "A-Album")
        albums = self.rep["top"]["albums"]
        self.assertEqual([(r["name"], r["artist"], r["plays"], r["badge"]) for r in albums],
                         [("A-Album", "Alpha", 12, None), ("B-Album", "Beta", 5, "new_ever")])
        self.assertEqual(self.rep["discoveries"][0]["name"], "Beta")

    def test_bulk_hidden_and_superseded_left_out_of_time_charts(self):
        clock = {c["hour"]: c["plays"] for c in self.rep["clock"]}
        self.assertEqual(len(self.rep["clock"]), 24)
        self.assertEqual((clock[14], clock[10], clock[20]), (12, 5, 6))
        self.assertEqual(clock[3], 0)     # the bulk batch at 03:00
        self.assertEqual(clock[8], 0)     # hidden podcast
        self.assertEqual(clock[16], 0)    # superseded live play
        wd = {w["dow"]: w for w in self.rep["weekday"]}
        self.assertEqual((wd[3]["label"], wd[3]["plays"], wd[3]["per_active_day"]), ("Thu", 12, 12.0))
        self.assertEqual(wd[5]["plays"], 0)                # Saturday Mar 21 only had bulk plays
        self.assertIsNone(wd[5]["per_active_day"])
        series = self.rep["series"]
        self.assertEqual(series["granularity"], "day")
        self.assertEqual(len(series["points"]), 31)
        pts = {p["key"]: p["plays"] for p in series["points"]}
        self.assertEqual((pts["2026-03-12"], pts["2026-03-21"]), (12, 0))
        q = self.rep["quick_facts"]
        self.assertEqual(q["most_active_day"], {"day": "2026-03-12", "plays": 12})
        self.assertEqual(q["busiest_hour"], {"hour": 14, "plays": 12})
        # Mar 20-22 would be a 3-day streak if the bulk day counted
        self.assertEqual(q["longest_streak"], {"days": 1, "start": "2026-03-12", "end": "2026-03-12"})
        self.assertEqual(q["top_new_artist"], {"name": "Beta", "key": "beta", "plays": 5})

    def test_contract_shape(self):
        rep = self.rep
        for k in ("type", "anchor", "label", "prev_label", "start_ms", "end_ms", "prev_start_ms", "prev_end_ms",
                  "complete", "is_current", "warmup", "prev_anchor", "next_anchor"):
            self.assertIn(k, rep["period"])
        for k in ("plays", "prev_plays", "pct_change", "listening_s_est", "prev_listening_s_est", "active_days",
                  "days_in_period", "avg_plays_per_day", "avg_plays_per_active_day", "artists", "prev_artists",
                  "albums", "prev_albums", "tracks", "prev_tracks", "new_artists", "new_albums", "new_tracks",
                  "pct_new_artists", "pct_new_albums", "pct_new_tracks", "pct_plays_on_new_tracks"):
            self.assertIn(k, rep["totals"])
        row_keys = {"rank", "key", "name", "plays", "prev_plays", "pct_change", "prev_rank", "badge"}
        self.assertTrue(row_keys <= set(rep["top"]["artists"][0]))
        self.assertTrue(row_keys | {"artist"} <= set(rep["top"]["albums"][0]))
        self.assertTrue(row_keys | {"artist", "album", "spotify_track_id"} <= set(rep["top"]["tracks"][0]))
        self.assertEqual(set(rep["quick_facts"]), {"most_active_day", "busiest_hour", "longest_streak",
                                                   "top_new_artist"})
        self.assertTrue({"newest_play_ts", "newest_lastfm_ts", "stale", "note"} <= set(rep["coverage"]))
        for k in ("plays", "listening_time", "pct_new", "active_days", "badge_new_ever", "badge_re_entry",
                  "bulk_note", "previous_period"):
            self.assertIsInstance(rep["definitions"].get(k), str)
        json.dumps(rep)  # everything is JSON-serialisable

    def test_limit_and_errors(self):
        rep = R.report(self.L.conn, "month", "2026-03-15", 1, now_ms=NOW)
        self.assertEqual(len(rep["top"]["artists"]), 1)
        for bad in (0, 51, "x"):
            with self.assertRaises(R.BadRequest) as cm:
                R.report(self.L.conn, "month", "2026-03-15", bad, now_ms=NOW)
            self.assertEqual(cm.exception.field, "limit")

    def test_current_partial_period_math(self):
        L = Ledger()
        L.play(ms(2026, 1, 1, 10), "Seed", "S")
        L.play(ms(2026, 8, 24, 12), "A", "x")
        L.play(ms(2026, 8, 25, 15, 0), "A", "x")        # before the cut (Aug 25 15:04)
        L.play(ms(2026, 8, 25, 16, 0), "A", "x")        # after it: not part of the comparison
        L.play(ms(2026, 8, 30, 12), "A", "x")
        L.run(3, ms(2026, 9, 2, 12), "A", "x")
        rep = R.report(L.conn, "month", None, now_ms=NOW)
        self.assertEqual(rep["totals"]["plays"], 3)
        self.assertEqual(rep["totals"]["prev_plays"], 2)
        self.assertEqual(rep["period"]["prev_label"], "Aug 1 – 25, 2026")
        self.assertIn("same stretch", rep["definitions"]["previous_period"])
        self.assertEqual(rep["totals"]["days_in_period"], 25)
        self.assertEqual(len(rep["series"]["points"]), 25)   # no empty future days

    def test_all_time_and_year_series(self):
        rep = R.report(self.L.conn, "all", None, now_ms=NOW)
        self.assertEqual(rep["totals"]["plays"], 71)
        self.assertIsNone(rep["totals"]["prev_plays"])
        self.assertIsNone(rep["top"]["artists"][0]["badge"])
        self.assertEqual(rep["series"]["granularity"], "year")
        self.assertEqual([p["key"] for p in rep["series"]["points"]], ["2025", "2026"])
        yr = R.report(self.L.conn, "year", "2026-01-01", now_ms=NOW)
        self.assertEqual(yr["series"]["granularity"], "month")
        self.assertEqual(len(yr["series"]["points"]), 9)     # Jan..Sep so far
        self.assertEqual({p["key"]: p["plays"] for p in yr["series"]["points"]}["2026-03"], 23)

    def test_warmup_hides_new_figures(self):
        rep = R.report(self.L.conn, "month", "2025-01-15", now_ms=NOW)
        self.assertTrue(rep["period"]["warmup"])
        self.assertIsNone(rep["totals"]["pct_new_artists"])
        self.assertIsNone(rep["totals"]["pct_plays_on_new_tracks"])
        self.assertEqual(rep["discoveries"], [])
        self.assertIsNone(rep["quick_facts"]["top_new_artist"])

    def test_hiding_an_artist_rebuilds(self):
        self.L.hide("Alpha")
        self.L.conn.commit()
        rep = R.report(self.L.conn, "month", "2026-03-15", now_ms=NOW)
        self.assertEqual(rep["totals"]["plays"], 11)
        self.assertNotIn("Alpha", [r["name"] for r in rep["top"]["artists"]])

    def test_listening_time_estimate(self):
        L = Ledger()
        t0 = ms(2026, 6, 1, 12)
        L.play(t0, "A", "x")                                     # next play 200 s later -> x lasts 200 s
        L.play(t0 + 200_000, "A", "y")                           # next gap > 12 min -> 221 s default
        L.play(t0 + 10_000_000, "A", "z", source="live", listened_s=50)   # heard in the app: 50 s
        L.conn.commit()
        rep = R.report(L.conn, "month", "2026-06-15", now_ms=NOW)
        self.assertEqual(rep["totals"]["listening_s_est"], 200 + 221 + 50)
        # a stored duration (e.g. from Spotify) wins over the gap estimate
        L.conn.execute("INSERT INTO track_stats (track_key, display_name, est_duration_s) VALUES ('a|y', 'y', 300)")
        L.conn.commit()
        R.reset_caches()
        rep = R.report(L.conn, "month", "2026-06-15", now_ms=NOW)
        self.assertEqual(rep["totals"]["listening_s_est"], 200 + 300 + 50)

    def test_dst_days(self):
        changes = transitions(2025)
        if not changes:
            self.skipTest("the local time zone has no DST")
        for d, _hours in changes:
            R.reset_caches()
            L = Ledger()
            prev_d, next_d = d - timedelta(days=1), d + timedelta(days=1)
            L.play(ms(2025, 1, 1), "Seed", "S")
            L.play(ms(prev_d.year, prev_d.month, prev_d.day, 23, 59), "A", "x")
            L.play(ms(d.year, d.month, d.day, 0, 0, 30), "A", "x")
            L.play(ms(d.year, d.month, d.day, 12), "A", "x")
            L.play(ms(d.year, d.month, d.day, 23, 59), "A", "x")
            L.play(ms(next_d.year, next_d.month, next_d.day, 0, 0, 30), "A", "x")
            L.conn.commit()
            rep = R.report(L.conn, "last7", d.isoformat(), now_ms=NOW)
            pts = {p["key"]: p["plays"] for p in rep["series"]["points"]}
            self.assertEqual(len(pts), 7)
            self.assertEqual(pts[d.isoformat()], 3, d)
            self.assertEqual(pts[prev_d.isoformat()], 1, d)
            self.assertEqual(rep["totals"]["plays"], 4, d)     # the next day's 00:00:30 is outside
            clock = {c["hour"]: c["plays"] for c in rep["clock"]}
            self.assertEqual((clock[0], clock[12], clock[23]), (1, 1, 2))

    def test_history_schema_compatibility(self):
        """The history package's own schema (extra columns, app_meta build state) works too."""
        try:
            from src.persistence import plays as P
        except Exception:  # pragma: no cover - the ledger module is optional for this package
            self.skipTest("src.persistence.plays not available")
        conn = sqlite3.connect(":memory:")
        _OPEN.append(conn)
        P.ensure_schema(conn)
        conn.commit()
        self.assertFalse(R.plays_ready(conn))       # schema exists but was never built
        L = Ledger.__new__(Ledger)
        L.conn, L._id = conn, 0
        L.play(ms(2026, 3, 3, 12), "A", "x")
        P.mark_ready(conn)
        self.assertTrue(R.plays_ready(conn))
        self.assertEqual(R.report(conn, "month", "2026-03-15", now_ms=NOW)["totals"]["plays"], 1)


class CoverageTests(Base):
    def build(self, live_after=True, lastfm_last=ms(2026, 5, 11, 20)):
        L = Ledger()
        L.play(ms(2025, 1, 1), "Seed", "S")
        L.run(10, ms(2026, 3, 5, 12), "A", "x")
        L.run(10, ms(2026, 4, 5, 12), "A", "x")
        L.run(10, ms(2026, 5, 5, 12), "A", "x")
        L.play(lastfm_last, "A", "x")
        if live_after:
            L.run(6, ms(2026, 6, 3, 12), "A", "x", source="live", listened_s=180)
        L.conn.commit()
        return L

    def test_stale_lastfm_is_explained(self):
        L = self.build()
        rep = R.report(L.conn, "month", "2026-06-10", now_ms=NOW)
        cov = rep["coverage"]
        self.assertTrue(cov["stale"])
        self.assertEqual(cov["lastfm_through_day"], "2026-05-11")
        self.assertIn("May 11, 2026", cov["note"])
        self.assertIn("Sync Last.fm", cov["note"])
        self.assertEqual(rep["totals"]["plays"], 6)
        self.assertEqual(rep["totals"]["prev_plays"], 11)
        self.assertIsNone(rep["totals"]["pct_change"])      # an undercounted month isn't compared
        # a month that ended before the cut-off is complete: no note
        april = R.report(L.conn, "month", "2026-04-10", now_ms=NOW)
        self.assertIsNone(april["coverage"]["note"])
        self.assertEqual(april["totals"]["pct_change"], 0)

    def test_not_stale_without_later_app_plays_or_when_recent(self):
        L = self.build(live_after=False)
        self.assertFalse(R.report(L.conn, "month", "2026-06-10", now_ms=NOW)["coverage"]["stale"])
        R.reset_caches()
        L = self.build(lastfm_last=NOW - DAY_MS)
        self.assertFalse(R.report(L.conn, "month", None, now_ms=NOW)["coverage"]["stale"])


# ---------------------------------------------------------------------------
# overview + year in music
# ---------------------------------------------------------------------------

class OverviewTests(Base):
    def setUp(self):
        super().setUp()
        self.L = standard_ledger()

    def test_overview(self):
        o = R.overview(self.L.conn, now_ms=NOW)
        t = o["totals"]
        self.assertEqual((t["plays"], t["artists"], t["tracks"], t["bulk_plays"]), (71, 5, 6, 20))
        self.assertEqual(t["first_ts"], ms(2025, 1, 1, 10))
        self.assertEqual(o["streaks"]["longest"], {"days": 3, "start": "2026-02-10", "end": "2026-02-12"})
        self.assertIsNone(o["streaks"]["current"])
        self.assertEqual(o["top_artists"][0]["name"], "Alpha")
        self.assertEqual(o["top_artists"][0]["plays"], 45)
        c = o["concentration"]
        self.assertEqual((c["top1_pct"], c["one_play_artists"], c["one_play_artists_pct"]), (63.4, 1, 20.0))
        years = {y["year"]: y for y in o["plays_by_year"]}
        self.assertEqual((years[2025]["plays"], years[2026]["plays"]), (8, 63))
        self.assertTrue(years[2025]["warmup"])
        self.assertIsNone(years[2025]["pct_new_artists"])
        self.assertEqual((years[2026]["new_artists"], years[2026]["artists"]), (2, 4))
        self.assertEqual(years[2026]["top_artist"]["name"], "Alpha")
        # the only song with 15+ plays: 12 of them on one afternoon
        self.assertEqual([(x["track"], x["peak7"], x["lifetime"]) for x in o["obsessions"]], [("Hit", 12, 25)])
        self.assertEqual({d["year"] for d in o["discovery_by_year"]}, {2026})
        # first heard after the 90-day warm-up (Alpha: June 2025), ranked by lifetime plays
        self.assertEqual([b["name"] for b in o["best_discoveries"]], ["Alpha", "Gamma", "Delta", "Beta"])
        for k in ("totals", "plays_by_year", "streaks", "top_artists", "concentration", "discovery_by_year",
                  "best_discoveries", "obsessions", "rediscover", "clock_by_year", "coverage", "definitions"):
            self.assertIn(k, o)
        json.dumps(o)

    def test_current_streak_and_rediscover(self):
        L = Ledger()
        L.play(ms(2024, 1, 1), "Seed", "S")
        L.run(45, ms(2024, 3, 1, 12), "Old Love", "o", step_s=3600)     # 45 plays in 2024, then silence
        for i in range(3):                                                # yesterday, today, and 2 days ago
            day = datetime.fromtimestamp(NOW / 1000).date() - timedelta(days=i)
            L.play(ms(day.year, day.month, day.day, 9), "New", "n")
        L.conn.commit()
        o = R.overview(L.conn, now_ms=NOW)
        self.assertEqual(o["streaks"]["current"]["days"], 3)
        self.assertTrue(o["streaks"]["current"]["includes_today"])
        self.assertEqual([(r["name"], r["peak_year"], r["peak_plays"]) for r in o["rediscover"]],
                         [("Old Love", 2024, 45)])
        # something to press play on: the artist's most-played song
        self.assertEqual(o["rediscover"][0]["top_song"], {"name": "o", "plays": 45, "spotify_track_id": None})

    def test_year_in_music(self):
        y = R.year_in_music(self.L.conn, 2026, now_ms=NOW)
        self.assertFalse(y["complete"])
        self.assertEqual((y["plays"], y["prev_plays"], y["pct_change"]), (63, 8, 688))
        self.assertEqual(y["top_artist"], {"name": "Alpha", "key": "alpha", "plays": 42, "image": None})
        self.assertEqual((y["top_track"]["name"], y["top_track"]["plays"]), ("Hit", 22))
        self.assertEqual(y["top_album"]["artist"], "Alpha")
        self.assertEqual(y["top_new_artist"]["name"], "Delta")
        self.assertEqual(y["peak_month"], {"ym": "2026-03", "label": "March", "plays": 23})
        for k in ("year", "complete", "plays", "prev_plays", "pct_change", "hours_est", "active_days", "days",
                  "artists", "new_artists", "pct_new_artists", "tracks", "pct_new_tracks", "top_artist", "top_album",
                  "top_track", "top_new_artist", "peak_month", "most_active_day", "busiest_hour", "longest_streak",
                  "coverage"):
            self.assertIn(k, y)
        with self.assertRaises(R.BadRequest):
            R.year_in_music(self.L.conn, 2031, now_ms=NOW)

    def test_year_in_music_lists_and_months(self):
        y = R.year_in_music(self.L.conn, 2026, now_ms=NOW)
        self.assertEqual([r["name"] for r in y["top_artists"]], ["Alpha", "Delta", "Gamma", "Beta"])
        self.assertEqual([r["rank"] for r in y["top_artists"]], [1, 2, 2, 4])   # Delta and Gamma tie on 8
        self.assertLessEqual(len(y["top_tracks"]), R.YIM_TOP_N)
        self.assertEqual(y["top_tracks"][0]["artist"], "Alpha")
        self.assertIn("spotify_track_id", y["top_track"])
        self.assertEqual(len(y["months"]), 9)          # a running year: January to September so far
        self.assertEqual(y["months"][2], {"ym": "2026-03", "label": "Mar", "plays": 23})
        empty = R.year_in_music(self.L.conn, 2020, now_ms=NOW)
        self.assertEqual(empty["plays"], 0)
        self.assertIsNone(empty["top_artist"])


# ---------------------------------------------------------------------------
# today counts (events-based, rolls over at local midnight)
# ---------------------------------------------------------------------------

class TodayTests(Base):
    def setUp(self):
        super().setUp()
        L = self.L = Ledger()
        today = datetime.fromtimestamp(NOW / 1000).date()
        y = today - timedelta(days=1)

        def at(d, h, mi=0, s=0):
            return ms(d.year, d.month, d.day, h, mi, s)
        self.at, self.today, self.yesterday = at, today, y
        L.event("SONG_PLAYED", at(today, 9), "t1", listened_s=100)
        L.event("SONG_PLAYED", at(today, 10), "t1", listened_s=100)
        L.event("SONG_PLAYED", at(today, 0, 0, 1), "t4", listened_s=40)
        L.event("SONG_PLAYED", at(y, 23, 59, 59), "t2", listened_s=40)
        L.event("SONG_PLAYED", at(today - timedelta(days=8), 12), "t5", listened_s=40)
        L.event("SONG_PLAYED", at(today, 11), "lf1", source="lastfm")
        L.event("TRACK_REMOVED_FROM_QUEUE", at(today, 9, 5), "c1", reason="threshold")
        L.event("TRACK_REMOVED_FROM_QUEUE", at(today, 9, 5, 2), "c1", reason="threshold")   # logged twice
        L.event("TRACK_REMOVED_FROM_QUEUE", at(today, 11), "c2", reason="manual")
        L.event("TRACK_RESTORED", at(today, 11, 10), "c2")                                  # undone
        L.event("TRACK_REMOVED_FROM_QUEUE", at(today - timedelta(days=3), 12), "c3")
        L.event("TRACK_REMOVED_FROM_QUEUE", at(y, 23, 50), "c4")
        L.event("TRACK_RESTORED", at(today, 0, 5), "c4")
        L.event("TRACK_REMOVED_FROM_QUEUE", at(today - timedelta(days=8), 12), "c5")
        L.event("TRACK_LIKED", at(today, 9), "l1")
        L.event("TRACK_LIKED", at(today, 9, 30), "l1")
        L.event("TRACK_LIKED", at(y, 9), "l2")
        L.play(at(today, 8), "A", "x")
        L.play(at(today, 9), "A", "y")
        L.play(at(y, 22), "A", "z")
        L.play(at(today, 10), "A", "x", source="live", superseded_by=2)
        L.play(at(today, 10), "Pod", "e")
        L.hide("Pod")
        L.conn.commit()

    def test_counts(self):
        c = R.today_counts(self.L.conn, NOW)
        self.assertEqual(c["plays_today"], 2)
        self.assertEqual(c["heard_in_app"], 2)          # t1 (twice) + t4; not yesterday's t2, not scrobbles
        self.assertEqual(c["heard_in_app_7d"], 3)
        self.assertEqual(c["cleared_today"], 1)         # c1 once; c2 and c4 were undone
        self.assertEqual(c["cleared_7d"], 2)            # + c3
        self.assertEqual(c["liked_today"], 1)
        # the playing track counts once it has played long enough
        self.assertEqual(R.today_counts(self.L.conn, NOW, "t9", 20, 10)["heard_in_app"], 3)
        self.assertEqual(R.today_counts(self.L.conn, NOW, "t9", 5, 10)["heard_in_app"], 2)
        self.assertEqual(R.today_counts(self.L.conn, NOW, "t1", 20, 10)["heard_in_app"], 2)

    def test_short_skips_are_not_heard(self):
        # "Heard in app" uses the play rule (30 s), so it can't exceed Plays because of skips
        self.L.event("SONG_PLAYED", self.at(self.today, 12), "skip1", listened_s=4)
        self.L.event("SONG_PLAYED", self.at(self.today, 12, 5), "old_payload")   # no listened_s
        self.L.conn.commit()
        self.assertEqual(R.today_counts(self.L.conn, NOW)["heard_in_app"], 2)
        # the playing track needs 30 s by default too
        self.assertEqual(R.today_counts(self.L.conn, NOW, "t9", 20)["heard_in_app"], 2)
        self.assertEqual(R.today_counts(self.L.conn, NOW, "t9", 31)["heard_in_app"], 3)

    def test_cleared_today_matches_sql_distinct_count(self):
        t0 = R.midnight_ms(self.today)
        sql = self.L.conn.execute(
            "SELECT COUNT(DISTINCT track_id) FROM events WHERE event_type='TRACK_REMOVED_FROM_QUEUE' AND ts_ms >= ? "
            "AND track_id NOT IN (SELECT track_id FROM events WHERE event_type='TRACK_RESTORED' AND ts_ms >= ?)",
            (t0, t0)).fetchone()[0]
        self.assertEqual(R.today_counts(self.L.conn, NOW)["cleared_today"], sql)

    def test_rolls_over_at_local_midnight(self):
        just_before = R.midnight_ms(self.today) - 1
        c = R.today_counts(self.L.conn, just_before)
        self.assertEqual(c["heard_in_app"], 1)          # yesterday: only t2
        self.assertEqual(c["cleared_today"], 1)         # c4, restored only after midnight
        self.assertEqual(c["liked_today"], 1)
        self.assertEqual(c["plays_today"], 1)


# ---------------------------------------------------------------------------
# backlog insights
# ---------------------------------------------------------------------------

class BacklogInsightTests(Base):
    def test_windows_pace_funnel(self):
        L = Ledger()
        now = NOW
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 5, 10), "c1", reason="threshold", genres=["metalcore"])
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 5, 10, 0, 5), "c1", reason="threshold", genres=["metalcore"])
        L.event("SONG_PLAYED", ms(2026, 9, 5, 10, 2), "c1", listened_s=150)
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 12, 10), "c2", reason="manual", genres=["metalcore"])
        L.event("SONG_PLAYED", ms(2026, 9, 12, 9, 59), "c2", listened_s=40)
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 20, 10), "c3", reason="threshold", genres=["metalcore"])
        L.event("TRACK_RESTORED", ms(2026, 9, 20, 10, 5), "c3")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 21, 10), "c4", reason="threshold", genres=["metalcore"])
        L.conn.commit()
        hist = [{"day": "2026-09-01", "ts_ms": ms(2026, 9, 1), "total": 100},
                {"day": "2026-09-10", "ts_ms": ms(2026, 9, 10), "total": 110}]
        out = B.backlog_insights(L.conn, hist, 105, None, now_ms=now)
        self.assertEqual(out["current_total"], 105)
        self.assertEqual(out["trajectory"][-1], {"day": "2026-09-25", "total": 105})
        ss = out["windows"]["since_start"]
        self.assertEqual((ss["start_day"], ss["cleared"], ss["net"], ss["inflow"], ss["days"]),
                         ("2026-09-01", 3, 5, 8, 24))
        f = out["funnel"]
        self.assertEqual((f["cleared_distinct"], f["deep"], f["partial"], f["skip"], f["unknown"]), (3, 1, 1, 0, 1))
        self.assertEqual(f["median_listen_s"], 95.0)
        self.assertEqual(out["by_genre"], [{"genre": "metalcore", "cleared": 3, "deep": 1, "known": 2,
                                            "deep_pct": 50.0}])
        p = out["pace"]
        self.assertEqual((p["cleared_per_week"], p["inflow_per_week"]), (round(3 * 7 / 24, 1), round(8 * 7 / 24, 1)))
        self.assertTrue(p["growing"])
        self.assertIsNone(p["weeks_to_empty"])
        self.assertEqual(p["needed_per_day_12w"], round((105 / 12 + 8 * 7 / 24) / 7, 1))
        self.assertEqual(p["best_week"]["cleared"], 1)
        self.assertFalse(out["kept"]["known"])
        for k in ("trajectory", "current_total", "windows", "pace", "funnel", "by_genre", "kept", "definitions"):
            self.assertIn(k, out)
        for k in ("inflow", "cleared", "net", "measured", "exact", "start_day", "days"):
            self.assertIn(k, out["windows"]["last_7"])
        json.dumps(out)
        # definitions are shown verbatim in info tooltips: plain words, no API field names
        for key, text in out["definitions"].items():
            self.assertNotIn("_", text, key)
            for word in ("exact", "measured'", "since_start", "start_day"):
                self.assertNotIn(word, text, key)

    def _off_fixture(self):
        """Clears on Sep 5, 12, 21 (+ one undone on Sep 20); sizes recorded Sep 1 and 10 only."""
        L = Ledger()
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 5, 10), "c1", reason="threshold")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 12, 10), "c2", reason="manual")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 20, 10), "c3", reason="threshold")
        L.event("TRACK_RESTORED", ms(2026, 9, 20, 10, 5), "c3")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 21, 10), "c4", reason="threshold")
        L.conn.commit()
        hist = [{"day": "2026-09-01", "ts_ms": ms(2026, 9, 1), "total": 100},
                {"day": "2026-09-10", "ts_ms": ms(2026, 9, 10), "total": 110}]
        return B.backlog_insights(L.conn, hist, 105, None, now_ms=NOW)

    def test_window_is_never_silently_widened(self):
        w = self._off_fixture()["windows"]["last_7"]
        # the window is still "the last 7 days": its own start day, and cleared counts only those days
        self.assertEqual((w["start_day"], w["days"], w["exact"], w["cleared"]), ("2026-09-18", 7, False, 1))
        # no size was recorded on Sep 18, so added/net for the week are unknown, not a 15-day figure
        self.assertIsNone(w["inflow"])
        self.assertIsNone(w["net"])
        self.assertIsNone(w["start_total"])
        # the closest measurable span is labelled with its own start (nothing recorded inside the week)
        m = w["measured"]
        self.assertEqual((m["start_day"], m["days"], m["start_total"], m["end_total"]), ("2026-09-10", 15, 110, 105))
        self.assertEqual((m["cleared"], m["net"], m["inflow"]), (2, -5, 0))

    def test_window_prefers_a_recorded_day_inside_it(self):
        w = self._off_fixture()["windows"]["last_30"]
        self.assertEqual((w["start_day"], w["exact"], w["cleared"]), ("2026-08-26", False, 3))
        m = w["measured"]
        # Sep 1 lies inside the 30 days: measure from there instead of reaching further back
        self.assertEqual((m["start_day"], m["days"], m["cleared"], m["net"], m["inflow"]), ("2026-09-01", 24, 3, 5, 8))

    def test_exact_window_and_weekly_series(self):
        L = Ledger()
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 15, 10), "a1", reason="threshold")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 19, 10), "a2", reason="threshold")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 23, 10), "a3", reason="threshold")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 25, 9), "a4", reason="manual")
        L.conn.commit()
        sizes = {13: 50, 14: 50, 15: 49, 16: 52, 17: 52, 18: 54, 19: 53, 20: 60, 21: 60, 22: 61, 23: 60, 24: 60}
        hist = [{"day": f"2026-09-{d:02d}", "ts_ms": ms(2026, 9, d, 23), "total": t} for d, t in sizes.items()]
        out = B.backlog_insights(L.conn, hist, 59, None, now_ms=NOW)
        w = out["windows"]["last_7"]
        self.assertTrue(w["exact"])
        self.assertEqual((w["start_day"], w["start_total"], w["cleared"], w["net"], w["inflow"]),
                         ("2026-09-18", 54, 3, 5, 8))
        self.assertEqual(w["measured"]["start_day"], "2026-09-18")
        weeks = {r["week_start"]: r for r in out["weekly"]}
        self.assertEqual(list(weeks), ["2026-09-07", "2026-09-14", "2026-09-21"])
        # Sep 7-13: only Sep 13 recorded (no Sunday before it) -> not measured
        self.assertIsNone(weeks["2026-09-07"]["added"])
        # Sep 14-20: from Sunday Sep 13 (50) to Sunday Sep 20 (60), with a1 + a2 cleared -> 12 came in
        wk = weeks["2026-09-14"]
        self.assertEqual((wk["cleared"], wk["added"], wk["added_days"], wk["days"]), (2, 12, 7, 7))
        # this week so far: Sep 20 (60) -> now (59), a3 + a4 cleared -> 1 came in, over 5 days
        wk = weeks["2026-09-21"]
        self.assertEqual((wk["cleared"], wk["added"], wk["added_days"], wk["current"], wk["days"]), (2, 1, 5, True, 5))

    def test_weekly_marks_weeks_the_app_was_off(self):
        out = self._off_fixture()
        weeks = out["weekly"]
        self.assertEqual([w["week_start"] for w in weeks], ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21"])
        self.assertEqual([w["cleared"] for w in weeks], [1, 1, 0, 1])
        self.assertEqual([w["added"] for w in weeks], [None, None, None, None])

    def test_kept_needs_synced_liked_songs(self):
        L = Ledger()
        L.conn.execute("CREATE TABLE saved_tracks (track_id TEXT PRIMARY KEY, added_at TEXT)")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 20, 10), "k1", reason="threshold")
        L.event("TRACK_REMOVED_FROM_QUEUE", ms(2026, 9, 21, 10), "k2", reason="threshold")
        L.conn.commit()
        hist = [{"day": "2026-09-19", "ts_ms": ms(2026, 9, 19), "total": 10}]
        kept = B.backlog_insights(L.conn, hist, 8, None, now_ms=NOW)["kept"]
        self.assertFalse(kept["known"])            # an empty table means "never synced", not "0% kept"
        self.assertIsNone(kept["pct"])
        L.conn.execute("INSERT INTO saved_tracks VALUES ('k1', '2026-09-20'), ('zz', '2026-01-01')")
        L.conn.commit()
        kept = B.backlog_insights(L.conn, hist, 8, None, now_ms=NOW)["kept"]
        self.assertEqual((kept["known"], kept["kept"], kept["pct"]), (True, 1, 50.0))

    def test_empty(self):
        L = Ledger()
        out = B.backlog_insights(L.conn, [], None, None, now_ms=NOW)
        self.assertEqual(out["trajectory"], [])
        self.assertIsNone(out["windows"]["last_7"])
        self.assertEqual(out["funnel"]["cleared_distinct"], 0)


# ---------------------------------------------------------------------------
# v2 routes + status extension (a stub `app` module stands in for app.py)
# ---------------------------------------------------------------------------

class RouteTests(Base):
    def setUp(self):
        super().setUp()
        from flask import Flask

        self.L = standard_ledger()
        conn = self.L.conn
        self._saved_app = sys.modules.get("app")
        core = types.ModuleType("app")
        core._SQLITE_ENABLED = True
        core._db = types.SimpleNamespace(get_conn=lambda: self.conn_for_core)
        core.errors = []
        core._log_app_error = lambda e, ctx="": core.errors.append((ctx, e))
        core.state = {"current_track": {"id": "tNow"}, "current_listened": 30.0,
                      "backlog_history": [{"day": "2026-09-01", "total": 100}],
                      "playlist_id": "pl", "playlist_total": 0, "playlist_track_ids_loaded": False}
        core.state_lock = threading.Lock()
        core.LISTEN_EVENT_MIN_SECONDS = 10
        core.extensions, core.workers = [], []
        core.register_status_extension = core.extensions.append
        core.register_background_worker = lambda name, fn: core.workers.append(name)
        self.conn_for_core = conn
        sys.modules["app"] = core
        self.core = core
        from src.web import insights_api
        self.api = insights_api
        insights_api._today_cache.update(at=0.0, day=None, sets=None, last=None)
        insights_api._seen_today.update(day=None, ids=set())
        flask_app = Flask(__name__)
        flask_app.register_blueprint(insights_api.bp)
        self.client = flask_app.test_client()

    def tearDown(self):
        super().tearDown()
        if self._saved_app is not None:
            sys.modules["app"] = self._saved_app
        else:
            sys.modules.pop("app", None)

    def test_reports_route(self):
        r = self.client.get("/api/v2/reports?period=month&anchor=2026-03-15&limit=5")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["totals"]["plays"], 43)
        self.assertEqual(body["top"]["artists"][2]["badge"], "new_ever")
        self.assertEqual(len(body["clock"]), 24)

    def test_bad_input_is_400_naming_the_field(self):
        for url, field in (("/api/v2/reports?period=decade", "period"),
                           ("/api/v2/reports?period=month&anchor=2026-02-30", "anchor"),
                           ("/api/v2/reports?period=month&limit=0", "limit"),
                           ("/api/v2/insights/year-in-music?year=20x5", "year"),
                           ("/api/v2/insights/year-in-music?year=1900", "year")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 400, url)
            err = r.get_json()["error"]
            self.assertEqual((err["code"], err["field"]), ("bad_request", field), url)

    def test_not_ready_is_503(self):
        self.conn_for_core = sqlite3.connect(":memory:", check_same_thread=False)
        _OPEN.append(self.conn_for_core)
        self.conn_for_core.executescript(EVENTS_DDL)
        for url in ("/api/v2/reports", "/api/v2/insights/overview", "/api/v2/insights/year-in-music"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 503, url)
            self.assertEqual(r.get_json()["error"]["code"], "history_not_ready")
        # backlog insights only need events
        self.assertEqual(self.client.get("/api/v2/backlog/insights").status_code, 200)

    def test_overview_year_and_backlog_routes(self):
        o = self.client.get("/api/v2/insights/overview")
        self.assertEqual(o.status_code, 200)
        self.assertEqual(o.get_json()["totals"]["plays"], 71)
        y = self.client.get("/api/v2/insights/year-in-music?year=2026")
        self.assertEqual(y.status_code, 200)
        self.assertEqual(y.get_json()["top_artist"]["name"], "Alpha")
        b = self.client.get("/api/v2/backlog/insights")
        self.assertEqual(b.status_code, 200)
        body = b.get_json()
        # playlist_total 0 before the playlist was read is "unknown", not an empty backlog
        self.assertEqual(body["current_total"], 100)

    def test_status_extension(self):
        self.api.init(self.core)
        self.assertEqual(self.core.extensions, [self.api.today_status])
        self.assertIn("reports_prewarm", self.core.workers)
        out = self.api.today_status()
        self.assertEqual(set(out["today"]) >= {"plays_today", "heard_in_app", "cleared_today", "liked_today",
                                                "cleared_7d", "heard_in_app_7d"}, True)
        self.assertEqual(out["today"]["heard_in_app"], 1)       # the current track, played 30 s
        # cached: a new clear doesn't show until the cache expires (<= 10 s)
        self.L.event("TRACK_REMOVED_FROM_QUEUE", int(datetime.now().timestamp() * 1000), "cX")
        self.L.conn.commit()
        self.assertEqual(self.api.today_status()["today"]["cleared_today"], 0)
        self.api._today_cache["at"] = 0.0
        self.assertEqual(self.api.today_status()["today"]["cleared_today"], 1)

    def test_status_extension_never_raises(self):
        self.core._db = types.SimpleNamespace(get_conn=lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
        self.assertEqual(self.api.today_status(), {})


if __name__ == "__main__":
    unittest.main()
