"""Listening reports over the canonical ``plays`` ledger (Last.fm-Pro-style insights).

Pure functions over a sqlite3 connection; nothing here touches Flask or app state.

Rules every number follows (they are also spelled out in each response's ``definitions``):
- Only canonical plays count: ``superseded_by IS NULL`` (a live play later replaced by its
  scrobble is not counted twice).
- Artists hidden from stats (``artist_stats.is_music = 0``: podcasts, comedy) are left out.
- Plays scrobbled in bulk (``ts_quality = 'bulk'``: runs of >=5 plays <10 s apart, i.e. offline
  imports whose timestamps are fake) count in totals and top lists, but not in anything that
  depends on *when* a play happened: clock, weekday, daily/monthly series, busiest day, streaks,
  averages per day.
- Days, hours and weekdays are LOCAL (the ledger stores ``day_local``/``hour_local`` computed with
  ``datetime.fromtimestamp``); period bounds are local midnights built the same way, so they are
  DST-safe without tzdata.
- "Tracks" are songs: versions of one composition (live, acoustic, remaster, feat.) are merged by
  ``song_key`` and shown under their most-played name.

The canonical ledger is read once per ledger version (one ordered scan, ~0.5 s for 50K plays,
done by a background pre-warm worker) into column arrays plus whole-history aggregates
(first-play tables, estimated durations, per-day/hour counts, 7-day peaks); the version is a
cheap signature (MAX(id), COUNT(*), plays_* app_meta values, hidden artists, DB path). A report
is then slices and Counters over those arrays: a few ms, no SQL beyond the signature.
"""

from __future__ import annotations

import bisect
import gc
import threading
import time
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta
from itertools import accumulate

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
WARMUP_DAYS = 90                 # everything is "new" at the start of the history
DEFAULT_TRACK_S = 221.0          # the user's median gap between scrobbles
GAP_MIN_MS, GAP_MAX_MS = 30_000, 720_000   # gaps that plausibly equal one song's length
STALE_AFTER_MS = 2 * DAY_MS
MIN_PREV_FOR_PCT = 5             # below this a % change is noise ("+9800%")
REDISCOVER_MIN_PEAK = 40
REDISCOVER_QUIET_DAYS = 90
RETURN_WINDOW_DAYS = (30, 180)
OBSESSION_MIN_PLAYS = 15
YIM_TOP_N = 5                    # top artists / albums / songs listed in a year in music
PERIODS = ("week", "month", "year", "all", "last7", "last30")
DOW_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")


class HistoryNotReady(Exception):
    """The plays ledger does not exist yet (or is being rebuilt)."""


class BadRequest(ValueError):
    """Invalid input; ``field`` names the offending parameter."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now_ms():
    return int(time.time() * 1000)


def _rows(conn, sql, params=()):
    """Plain tuples regardless of the connection's row_factory (Row objects are slower)."""
    cur = conn.cursor()
    cur.row_factory = None
    try:
        return cur.execute(sql, params).fetchall()
    finally:
        cur.close()


def _one(conn, sql, params=()):
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _table_exists(conn, name):
    return _one(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def _columns(conn, table):
    return {r[1] for r in _rows(conn, f"PRAGMA table_info({table})")}


def midnight_ms(d):
    """Epoch ms of local midnight starting date ``d`` (mktime-based, DST-safe)."""
    return int(datetime(d.year, d.month, d.day).timestamp() * 1000)


def local_dt(ms):
    return datetime.fromtimestamp(ms / 1000)


def local_day(ms):
    return local_dt(ms).date()


def _pct(part, whole, digits=1):
    if not whole:
        return None
    return round(100.0 * part / whole, digits)


def _pct_change(cur, prev):
    if prev is None or prev < MIN_PREV_FOR_PCT:
        return None
    return int(round(100.0 * (cur - prev) / prev))


def _fmt_day(d, with_year=True):
    s = f"{MONTH_ABBR[d.month - 1]} {d.day}"
    return f"{s}, {d.year}" if with_year else s


def _range_label(d0, d1):
    """Inclusive local-day range as 'Sep 22 – 28, 2026' / 'Sep 29 – Oct 5, 2026'."""
    if d0 == d1:
        return _fmt_day(d0)
    if d0.year == d1.year:
        if d0.month == d1.month:
            return f"{MONTH_ABBR[d0.month - 1]} {d0.day} – {d1.day}, {d0.year}"
        return f"{_fmt_day(d0, False)} – {_fmt_day(d1, False)}, {d0.year}"
    return f"{_fmt_day(d0)} – {_fmt_day(d1)}"


def warmup_end_ms(history_start_ms):
    """Local midnight WARMUP_DAYS calendar days after the history's first day (DST-safe)."""
    if history_start_ms is None:
        return None
    return midnight_ms(local_day(history_start_ms) + timedelta(days=WARMUP_DAYS))


def _parse_anchor(anchor):
    if anchor is None or anchor == "":
        return None
    if isinstance(anchor, date):
        return anchor
    try:
        return datetime.strptime(str(anchor), "%Y-%m-%d").date()
    except ValueError:
        raise BadRequest("anchor", "anchor must be a date like 2026-04-15.") from None


def _add_months(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _islands(days_sorted):
    """Runs of consecutive dates -> [(start, end, length)] (gaps-and-islands)."""
    out = []
    start = prev = None
    for d in days_sorted:
        if prev is not None and (d - prev).days == 1:
            prev = d
            continue
        if start is not None:
            out.append((start, prev, (prev - start).days + 1))
        start = prev = d
    if start is not None:
        out.append((start, prev, (prev - start).days + 1))
    return out


def _longest(islands):
    if not islands:
        return None
    s, e, n = max(islands, key=lambda t: (t[2], -t[0].toordinal()))  # earliest wins ties
    return {"days": n, "start": s.isoformat(), "end": e.isoformat()}


def _rank(counts, name_of):
    """[(key, plays, rank)] ordered by plays desc then name; competition ranking (1,1,3)."""
    items = sorted(counts.items(), key=lambda kv: (-kv[1], (name_of(kv[0]) or "").casefold(), kv[0]))
    out = []
    rank = 0
    prev_n = None
    for i, (k, n) in enumerate(items, 1):
        if n != prev_n:
            rank = i
            prev_n = n
        out.append((k, n, rank))
    return out


# ---------------------------------------------------------------------------
# readiness + ledger cache
# ---------------------------------------------------------------------------

_PLAYS_COLUMNS = {"superseded_by", "ts_quality", "day_local", "hour_local", "song_key", "album_key", "listened_s"}


def plays_ready(conn):
    """True when the plays ledger exists and is built.

    Prefers ``src.persistence.plays.ensure_plays_ready`` (owned by the history package); falls
    back to a schema check so this module also works against a scratch ledger.
    """
    try:
        from src.persistence import plays as _plays_mod  # lazy: may not exist yet
        fn = getattr(_plays_mod, "ensure_plays_ready", None)
    except Exception:
        fn = None
    if fn is not None:
        try:
            return bool(fn(conn))
        except Exception:
            return False
    if not _table_exists(conn, "plays") or not _PLAYS_COLUMNS <= _columns(conn, "plays"):
        return False
    if _one(conn, "SELECT 1 FROM plays LIMIT 1"):
        return True
    # An empty ledger is "ready" only when there is nothing to build it from.
    if _table_exists(conn, "events"):
        return _one(conn, "SELECT 1 FROM events WHERE event_type='SONG_PLAYED' LIMIT 1") is None
    return True


def _hidden_keys(conn):
    if not _table_exists(conn, "artist_stats"):
        return ()
    return tuple(sorted(r[0] for r in _rows(conn, "SELECT artist_key FROM artist_stats WHERE is_music=0")))


def ledger_signature(conn):
    """Cheap fingerprint of the ledger contents (a few ms)."""
    if not _table_exists(conn, "plays"):
        raise HistoryNotReady("plays table missing")
    max_id, n = _one(conn, "SELECT MAX(id), COUNT(*) FROM plays")
    meta = ()
    if _table_exists(conn, "app_meta"):
        # plays_rollups_dirty flips on every ingest and again when the rollups catch up; the rows
        # themselves are already covered by MAX(id)/COUNT(*) and the watermark, so it would only
        # cause a second rebuild per play
        meta = tuple(_rows(conn, "SELECT key, value FROM app_meta WHERE key LIKE 'plays%' "
                                 "AND key <> 'plays_rollups_dirty' ORDER BY key"))
    # the file path keeps two databases with equal counts (a test fixture, a sandbox) apart
    path = next((r[2] or "" for r in _rows(conn, "PRAGMA database_list") if r[1] == "main"), "")
    return (max_id, n, meta, _hidden_keys(conn), path)


class Ledger:
    """Whole-history aggregates for one ledger version (immutable once built)."""

    def __init__(self, sig):
        self.sig = sig
        self.hidden = frozenset(sig[3])
        self.start_ms = None           # first canonical play (any artist)
        self.newest_ts = None
        self.newest_lastfm_ts = None
        self.newest_nonlastfm_ts = None
        self.live_after_lastfm = 0
        self.total = 0                 # canonical, non-hidden plays
        self.bulk_total = 0
        self.bulk_days = {}            # day -> bulk plays
        self.artist = {}               # key -> [first_ts, last_ts, n, name]
        self.song = {}                 # key -> [first_ts, last_ts, n, name, artist_key, album, spotify_id]
        self.album = {}                # key -> [first_ts, last_ts, n, name, artist_key]
        self.recordings = 0
        self.ts = []                   # sorted ts of canonical non-hidden plays
        self.cum_s = [0.0]             # prefix sums of estimated seconds, len = len(ts)+1
        self.dayhour = {}              # date -> [24 ok-play counts]
        self.year_artist = {}          # (year, artist_key) -> plays (incl. bulk)
        self.ok_days = []              # sorted dates with >=1 ok play
        self._cols = None              # (ts, artist_key, song_key, ts_quality, album_key) columns
        self._derived = {}
        self._lock = threading.RLock()  # derived values may build on other derived values

    # -- queries over the cached arrays --
    def est_seconds(self, s, e):
        i = bisect.bisect_left(self.ts, s)
        j = bisect.bisect_left(self.ts, e)
        return self.cum_s[j] - self.cum_s[i]

    def counts_between(self, which, s, e):
        """{key: plays} for the plays in [s, e); ``which`` is artist, song or album.

        Served from the cached columns (a slice + Counter, ~1 ms for a year) rather than SQL, so
        a period's totals always agree with the first-play tables of the same ledger version.
        """
        if not self._cols:
            return {}
        i = bisect.bisect_left(self.ts, s)
        j = bisect.bisect_left(self.ts, e)
        counts = Counter(self._cols[{"artist": 1, "song": 2, "album": 4}[which]][i:j])
        counts.pop(None, None)   # plays without a known album
        return dict(counts)

    def artist_name(self, k):
        a = self.artist.get(k)
        return a[3] if a else k

    def song_name(self, k):
        v = self.song.get(k)
        return v[3] if v else k

    def album_name(self, k):
        v = self.album.get(k)
        return v[3] if v else k

    def warmup_end_ms(self):
        return warmup_end_ms(self.start_ms)

    def derived(self, name, fn):
        """Memoise an expensive derived aggregate on this ledger version."""
        with self._lock:
            if name not in self._derived:
                self._derived[name] = fn()
            return self._derived[name]

    def artist_ts(self):
        """artist_key -> sorted play timestamps (all canonical plays)."""
        def build():
            out = {}
            if self._cols:
                ts, ak = self._cols[0], self._cols[1]
                for k, t in zip(ak, ts):
                    lst = out.get(k)
                    if lst is None:
                        out[k] = [t]
                    else:
                        lst.append(t)
            return out
        return self.derived("artist_ts", build)

    def song_ok_ts(self):
        """song_key -> sorted play timestamps, bulk plays left out."""
        def build():
            out = {}
            if self._cols:
                ts, sk, q = self._cols[0], self._cols[2], self._cols[3]
                for k, t, qq in zip(sk, ts, q):
                    if qq == "bulk":
                        continue
                    lst = out.get(k)
                    if lst is None:
                        out[k] = [t]
                    else:
                        lst.append(t)
            return out
        return self.derived("song_ok_ts", build)


_LEDGER = None
_LEDGER_LOCK = threading.Lock()


def _variants(pairs):
    """{key: most frequent name} from an iterable of (key, name) pairs (None names ignored)."""
    best = {}
    for (k, name), n in Counter(pairs).items():
        if name is None or name == "":
            continue
        cur = best.get(k)
        if cur is None or n > cur[1] or (n == cur[1] and name < cur[0]):
            best[k] = (name, n)
    return {k: v[0] for k, v in best.items()}


def _build_ledger(conn, sig):
    """One ordered scan of the canonical ledger; aggregation is done column-wise in C
    (zip / dict / Counter) because a per-row Python loop over 50K plays costs ~250 ms."""
    led = Ledger(sig)
    hidden = led.hidden
    rows = _rows(conn, """
        SELECT ts_ms, artist_key, artist_name, song_key, track_name, track_key, album_key, album_name,
               listened_s, source, spotify_track_id, day_local, hour_local, ts_quality, year
        FROM plays WHERE superseded_by IS NULL ORDER BY ts_ms, id""")
    if not rows:
        return led
    ts_all = [r[0] for r in rows]
    led.start_ms = ts_all[0]
    led.newest_ts = ts_all[-1]

    # Coverage: newest scrobble and how many app-only plays came after it.
    newest_lastfm = newest_other = None
    after = 0
    for r in reversed(rows):
        if r[9] == "lastfm":
            newest_lastfm = r[0]
            break
        after += 1
        if newest_other is None:
            newest_other = r[0]
    led.newest_lastfm_ts = newest_lastfm
    led.newest_nonlastfm_ts = newest_other
    led.live_after_lastfm = after if newest_lastfm is not None else 0

    # Per-recording duration from the gap to the next play (of any artist: it is the physical
    # next play). A stored estimate in track_stats (e.g. a real Spotify duration) wins.
    gap_sum, gap_n = {}, {}
    tk_all = [r[5] for r in rows]
    for i, gap in enumerate(map(int.__sub__, ts_all[1:], ts_all[:-1])):
        if GAP_MIN_MS <= gap <= GAP_MAX_MS:
            tk = tk_all[i]
            gap_sum[tk] = gap_sum.get(tk, 0) + gap
            gap_n[tk] = gap_n.get(tk, 0) + 1
    est = {tk: gap_sum[tk] / gap_n[tk] / 1000.0 for tk in gap_sum}
    if _table_exists(conn, "track_stats") and "est_duration_s" in _columns(conn, "track_stats"):
        for tk, secs in _rows(conn, "SELECT track_key, est_duration_s FROM track_stats "
                                    "WHERE est_duration_s IS NOT NULL AND est_duration_s > 0"):
            est[tk] = float(secs)

    if hidden:
        rows = [r for r in rows if r[1] not in hidden]
    if not rows:
        return led
    (ts, ak, an, sk, tn, tk, alk, aln, ls, _src, sid, day, hour, q, yr) = zip(*rows)
    led.total = len(ts)
    led.ts = list(ts)
    est_get = est.get
    led.cum_s = [0.0] + list(accumulate(
        l if l is not None else est_get(t, DEFAULT_TRACK_S) for t, l in zip(tk, ls)))
    led.recordings = len(set(tk))

    # entity first/last/plays/name (rows are in time order: first write of reversed = earliest)
    a_first = dict(zip(reversed(ak), reversed(ts)))
    a_last = dict(zip(ak, ts))
    a_names = _variants(zip(ak, an))
    led.artist = {k: [a_first[k], a_last[k], n, a_names.get(k, k)] for k, n in Counter(ak).items()}

    s_first = dict(zip(reversed(sk), reversed(ts)))
    s_last = dict(zip(sk, ts))
    s_artist = dict(zip(sk, ak))
    s_names = _variants(zip(sk, tn))
    s_album = _variants(zip(sk, aln))
    s_sid = {k: v for k, v in zip(sk, sid) if v}          # latest known Spotify id
    led.song = {k: [s_first[k], s_last[k], n, s_names.get(k, k), s_artist[k], s_album.get(k), s_sid.get(k)]
                for k, n in Counter(sk).items()}

    al_rows = [(k, t, name, a) for k, t, name, a in zip(alk, ts, aln, ak) if k is not None]
    if al_rows:
        alk2, alt, aln2, ala = zip(*al_rows)
        al_first = dict(zip(reversed(alk2), reversed(alt)))
        al_last = dict(zip(alk2, alt))
        al_artist = dict(zip(alk2, ala))
        al_names = _variants(zip(alk2, aln2))
        led.album = {k: [al_first[k], al_last[k], n, al_names.get(k, k), al_artist[k]]
                     for k, n in Counter(alk2).items()}

    led.year_artist = dict(Counter(zip(yr, ak)))
    day_obj = {d: date.fromisoformat(d) for d in set(day)}
    for d, n in Counter(d for d, qq in zip(day, q) if qq == "bulk").items():
        led.bulk_days[day_obj[d]] = n
    led.bulk_total = sum(led.bulk_days.values())
    dayhour = led.dayhour
    for (d, h), n in Counter((d, h) for d, h, qq in zip(day, hour, q) if qq != "bulk").items():
        dd = day_obj[d]
        hours = dayhour.get(dd)
        if hours is None:
            hours = dayhour[dd] = [0] * 24
        hours[h] += n
    led.ok_days = sorted(dayhour)
    led._cols = (ts, ak, sk, q, alk)
    return led


def get_ledger(conn, wait=True):
    """Return the cached Ledger for the current ledger version, rebuilding when it changed.

    With ``wait=False`` (HTTP handlers) a request that arrives while another thread is already
    rebuilding gets the previous version instead of queueing behind the ~0.5 s build; the
    background pre-warm worker normally rebuilds before anyone asks.
    """
    global _LEDGER
    sig = ledger_signature(conn)
    led = _LEDGER
    if led is not None and led.sig == sig:
        return led
    if not _LEDGER_LOCK.acquire(blocking=wait or led is None):
        return led
    try:
        led = _LEDGER
        if led is not None and led.sig == sig:
            return led
        gc_was_on = gc.isenabled()
        gc.disable()  # the build allocates ~1M short-lived containers; GC passes only slow it down
        try:
            led = _build_ledger(conn, sig)
        finally:
            if gc_was_on:
                gc.enable()
        _LEDGER = led
        with _REPORT_CACHE_LOCK:
            _REPORT_CACHE.clear()
        return led
    finally:
        _LEDGER_LOCK.release()


def reset_caches():
    """Drop every in-memory cache (tests, or after a manual ledger rebuild)."""
    global _LEDGER
    with _LEDGER_LOCK:
        _LEDGER = None
        with _REPORT_CACHE_LOCK:
            _REPORT_CACHE.clear()


# small LRU for finished payloads: (kind, args..., ledger sig, time bucket) -> payload
_REPORT_CACHE = OrderedDict()
_REPORT_CACHE_MAX = 96
_REPORT_CACHE_LOCK = threading.Lock()


def _cached(key, fn):
    with _REPORT_CACHE_LOCK:
        hit = _REPORT_CACHE.get(key)
        if hit is not None:
            _REPORT_CACHE.move_to_end(key)
            return hit
    value = fn()
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE[key] = value
        while len(_REPORT_CACHE) > _REPORT_CACHE_MAX:
            _REPORT_CACHE.popitem(last=False)
    return value


# ---------------------------------------------------------------------------
# period bounds
# ---------------------------------------------------------------------------

def period_bounds(period, anchor=None, now_ms=None, history_start_ms=None):
    """Local, DST-safe bounds of a report period and of the period it is compared with.

    - week = ISO week (Mon–Sun) containing ``anchor``; month / year = the calendar unit;
      last7 / last30 = the N days ending on ``anchor`` (inclusive); all = history start → today.
    - The previous period is the previous unit of the same kind (previous week / month / year,
      the N days before). For the CURRENT, unfinished period it is cut to the same elapsed
      stretch (e.g. Sep 1–25 until 15:04 is compared with Aug 1–25 until 15:04), so a
      half-finished month is never compared with a whole one.
    """
    if period not in PERIODS:
        raise BadRequest("period", f"period must be one of: {', '.join(PERIODS)}.")
    now_ms = _now_ms() if now_ms is None else int(now_ms)
    now_dt = local_dt(now_ms)
    today = now_dt.date()
    a = _parse_anchor(anchor) or today
    if a > today:
        raise BadRequest("anchor", "anchor can't be in the future.")

    n_days = None
    if period == "week":
        start_d = a - timedelta(days=a.weekday())
        end_d = start_d + timedelta(days=7)
        prev_start_d = start_d - timedelta(days=7)
    elif period == "month":
        start_d = a.replace(day=1)
        end_d = _add_months(start_d, 1)
        prev_start_d = _add_months(start_d, -1)
    elif period == "year":
        start_d = date(a.year, 1, 1)
        end_d = date(a.year + 1, 1, 1)
        prev_start_d = date(a.year - 1, 1, 1)
    elif period in ("last7", "last30"):
        n_days = 7 if period == "last7" else 30
        end_d = a + timedelta(days=1)
        start_d = end_d - timedelta(days=n_days)
        prev_start_d = start_d - timedelta(days=n_days)
    else:  # all
        start_d = local_day(history_start_ms) if history_start_ms is not None else today
        start_d = min(start_d, today)
        end_d = today + timedelta(days=1)
        prev_start_d = None
        a = today

    start_ms, end_ms = midnight_ms(start_d), midnight_ms(end_d)
    is_current = start_ms <= now_ms < end_ms
    complete = now_ms >= end_ms
    prev_start_ms = prev_end_ms = None
    prev_last_d = None
    prev_truncated = False
    if prev_start_d is not None:
        prev_start_ms = midnight_ms(prev_start_d)
        prev_end_ms = start_ms
        prev_last_d = start_d - timedelta(days=1)
        if is_current:
            # same wall-clock stretch: naive datetime arithmetic keeps the time of day across DST
            elapsed = now_dt - datetime(start_d.year, start_d.month, start_d.day)
            cut = datetime(prev_start_d.year, prev_start_d.month, prev_start_d.day) + elapsed
            prev_end_ms = min(int(cut.timestamp() * 1000), start_ms)
            prev_last_d = min(cut.date(), prev_last_d)
            prev_truncated = prev_end_ms < start_ms

    last_d = end_d - timedelta(days=1)
    elapsed_end_d = min(end_d, today + timedelta(days=1))
    days_total = (end_d - start_d).days
    days_elapsed = max(0, (elapsed_end_d - start_d).days)

    if period == "week":
        label = _range_label(start_d, last_d)
        prev_label = _range_label(prev_start_d, prev_last_d) if prev_start_d else None
    elif period == "month":
        label = f"{MONTH_NAMES[start_d.month - 1]} {start_d.year}"
        prev_label = (_range_label(prev_start_d, prev_last_d) if prev_truncated
                      else f"{MONTH_NAMES[prev_start_d.month - 1]} {prev_start_d.year}")
    elif period == "year":
        label = str(start_d.year)
        prev_label = _range_label(prev_start_d, prev_last_d) if prev_truncated else str(prev_start_d.year)
    elif period in ("last7", "last30"):
        label = (f"Last {n_days} days" if a == today else _range_label(start_d, last_d))
        prev_label = _range_label(prev_start_d, prev_last_d)
    else:
        label = "All time"
        prev_label = None

    prev_anchor = next_anchor = None
    if period in ("week", "month", "year"):
        prev_anchor = prev_start_d.isoformat()
        next_anchor = end_d.isoformat() if end_d <= today else None
    elif n_days:
        prev_anchor = (a - timedelta(days=n_days)).isoformat()
        if a < today:
            next_anchor = min(a + timedelta(days=n_days), today).isoformat()

    warmup = False
    if history_start_ms is not None:
        warmup = start_ms < warmup_end_ms(history_start_ms)

    return {
        "type": period, "anchor": a.isoformat(), "label": label, "prev_label": prev_label,
        "start_ms": start_ms, "end_ms": end_ms, "prev_start_ms": prev_start_ms, "prev_end_ms": prev_end_ms,
        "start_day": start_d.isoformat(), "end_day": last_d.isoformat(),
        "complete": complete, "is_current": is_current, "warmup": warmup,
        "prev_truncated": prev_truncated, "prev_anchor": prev_anchor, "next_anchor": next_anchor,
        "days_total": days_total, "days_elapsed": days_elapsed, "as_of_ms": min(now_ms, end_ms),
    }


# ---------------------------------------------------------------------------
# coverage (is the history complete?)
# ---------------------------------------------------------------------------

def coverage(led, now_ms=None, period_end_ms=None, prev_end_ms=None):
    """Explain gaps in the data instead of letting a stalled Last.fm sync look like silence.

    stale = the newest Last.fm scrobble is more than 2 days old while plays heard by this app
    exist after it (so listening clearly continued but scrobbles never arrived).
    """
    now_ms = _now_ms() if now_ms is None else now_ms
    lfm = led.newest_lastfm_ts
    stale = bool(lfm is not None and now_ms - lfm > STALE_AFTER_MS and led.live_after_lastfm > 0)
    complete_through = lfm if stale else (led.newest_ts if led.newest_ts is not None else now_ms)
    affects = bool(stale and period_end_ms is not None and period_end_ms > lfm)
    # a period that runs past the cut-off is undercounted, so its % change isn't trustworthy
    comparison_affected = bool(affects and prev_end_ms is not None)
    note = None
    if lfm is None and led.total:
        note = ("Only plays heard by this app are included. Connect Last.fm in Settings to bring in "
                "your full listening history.")
    elif stale:
        through = _fmt_day(local_day(lfm))
        if affects or period_end_ms is None:
            note = (f"Your Last.fm history currently ends on {through}. After that, only plays heard by "
                    f"this app are counted, so {'this period is' if affects else 'recent weeks are'} "
                    f"missing plays from your other devices. Sync Last.fm in Settings to fill the gap.")
            if comparison_affected:
                note += " Changes against the previous period are hidden until then."
    return {
        "newest_play_ts": led.newest_ts,
        "newest_lastfm_ts": lfm,
        "lastfm_through_day": local_day(lfm).isoformat() if lfm is not None else None,
        "app_plays_after_lastfm": led.live_after_lastfm,
        "stale": stale,
        "complete_through_ts": complete_through,
        "affects_period": affects,
        "comparison_affected": comparison_affected,
        "note": note,
    }


# ---------------------------------------------------------------------------
# definitions (shown in the UI's info tooltips)
# ---------------------------------------------------------------------------

def _bulk_note(led):
    if not led.bulk_total:
        return "Plays that were scrobbled in bulk would be left out of time-of-day charts; there are none."
    big = sorted(d for d, n in led.bulk_days.items() if n >= 0.05 * led.bulk_total)
    where = ""
    if big:
        where = f" (mostly {_range_label(big[0], big[-1])})"
    return (f"{led.bulk_total:,} plays were scrobbled in bulk{where}, so their times aren't real. They count "
            f"in totals and top lists, but not in the clock, weekday, daily chart, busiest day, averages "
            f"per day or streaks.")


def _history_start_text(led):
    if led.start_ms is None:
        return "Your history is empty so far."
    s = local_day(led.start_ms)
    w = local_day(led.warmup_end_ms())
    return (f"Your history starts on {_fmt_day(s)}. In the first {WARMUP_DAYS} days everything looks new, "
            f"so 'new' figures aren't shown for periods that start before {_fmt_day(w)}.")


def _common_definitions(led):
    return {
        "plays": ("Each time a song was played: your Last.fm scrobbles plus plays this app heard itself. "
                  "A play heard by the app and also scrobbled is counted once."),
        "listening_time": (f"An estimate. Plays heard in this app use the seconds you actually listened; other "
                           f"plays use the song's usual length, worked out from the gap to your next scrobble; "
                           f"if that's unknown, {int(DEFAULT_TRACK_S // 60)} min {int(DEFAULT_TRACK_S % 60)} s "
                           f"(your typical gap between songs)."),
        "tracks": ("Different songs. Versions of the same song (live, acoustic, remastered, 'feat.') count as "
                   "one song and are shown under the name you played most."),
        "artists": "Different artists, by the main credited artist.",
        "albums": "Different albums (deluxe and remastered editions count as the same album).",
        "hidden_artists": ("Artists you hid from stats (for example podcasts or comedy) are left out of every "
                           "number here." if led.hidden else
                           "You can hide non-music artists (podcasts, comedy) from stats on their artist page."),
        "bulk_note": _bulk_note(led),
        "warmup": _history_start_text(led),
        "local_time": "Days, hours and weekdays use this computer's local time zone.",
    }


def report_definitions(led):
    d = _common_definitions(led)
    d.update({
        "pct_change": (f"Change against the previous period. Not shown when the previous period had fewer than "
                       f"{MIN_PREV_FOR_PCT} plays, because the percentage would be meaningless."),
        "previous_period": ("The previous week, month or year (or the previous 7 or 30 days). While a period is "
                            "still running it is compared with the same stretch of the previous one, up to the "
                            "same day and time, so a half-finished month isn't compared with a whole one."),
        "active_days": "Days with at least one play.",
        "days_in_period": "Days in the period so far (the whole period once it has ended).",
        "avg_plays_per_day": "Plays per day over the days in the period so far.",
        "avg_plays_per_active_day": "Plays per day, counting only the days you listened.",
        "pct_new": ("Share of the artists, albums or songs you played in this period that you had never played "
                    "before it (their first play in your whole history is in this period)."),
        "new_artists": "Artists you played for the first time ever in this period.",
        "pct_plays_on_new_tracks": "Share of this period's plays that went to songs you first heard in this period.",
        "rank_change": "The arrow compares the position with the previous period's chart.",
        "badge_new_ever": "NEW: you played it for the first time ever in this period.",
        "badge_re_entry": (f"BACK: you've played it before, but fewer than {MIN_PREV_FOR_PCT} times in the "
                           f"previous period."),
        "series": "Plays per day (per month for a year, per year for all time).",
        "clock": "Plays by the hour they started, in local time.",
        "weekday": "Plays on each weekday, and the average per day you listened on that weekday.",
        "most_active_day": "The day with the most plays in this period.",
        "busiest_hour": "The hour of the day with the most plays in this period.",
        "longest_streak": "Most days in a row with at least one play, within this period.",
        "top_new_artist": "The most-played artist you discovered in this period.",
        "discoveries": "Artists first heard in this period, by plays in the period.",
        "coverage": ("Where the history comes from and whether it's complete. If Last.fm hasn't synced for a "
                     "while, recent periods only include what this app heard."),
    })
    return d


# ---------------------------------------------------------------------------
# period report
# ---------------------------------------------------------------------------

def _all_counts(led, which):
    src = {"artist": led.artist, "song": led.song, "album": led.album}[which]
    return {k: v[2] for k, v in src.items()}


def _time_profile(led, start_d, end_d):
    """Clock, weekday and per-day counts of ok plays in [start_d, end_d)."""
    clock = [0] * 24
    wd_plays = [0] * 7
    wd_days = [0] * 7
    per_day = {}
    lo = bisect.bisect_left(led.ok_days, start_d)
    hi = bisect.bisect_left(led.ok_days, end_d)
    for d in led.ok_days[lo:hi]:
        hours = led.dayhour[d]
        n = 0
        for h in range(24):
            c = hours[h]
            if c:
                clock[h] += c
                n += c
        per_day[d] = n
        wd = d.weekday()
        wd_plays[wd] += n
        wd_days[wd] += 1
    return clock, wd_plays, wd_days, per_day


def _series(period, start_d, end_d, today, per_day):
    stop = min(end_d, today + timedelta(days=1))
    points = []
    if period in ("week", "month", "last7", "last30"):
        d = start_d
        while d < stop:
            points.append({"key": d.isoformat(), "label": _fmt_day(d, False), "dow": d.weekday(),
                           "plays": per_day.get(d, 0)})
            d += timedelta(days=1)
        return {"granularity": "day", "points": points}
    if period == "year":
        months = [0] * 12
        for d, n in per_day.items():
            months[d.month - 1] += n
        last_month = 12 if stop >= end_d else (stop - timedelta(days=1)).month
        for m in range(1, last_month + 1):
            points.append({"key": f"{start_d.year}-{m:02d}", "label": MONTH_ABBR[m - 1], "plays": months[m - 1]})
        return {"granularity": "month", "points": points}
    years = {}
    for d, n in per_day.items():
        years[d.year] = years.get(d.year, 0) + n
    for y in range(start_d.year, today.year + 1):
        points.append({"key": str(y), "label": str(y), "plays": years.get(y, 0)})
    return {"granularity": "year", "points": points}


def _top_rows(led, kind, cur, prev, s, e, limit, warmup, compare):
    name_of = {"artist": led.artist_name, "song": led.song_name, "album": led.album_name}[kind]
    first_of = {"artist": led.artist, "song": led.song, "album": led.album}[kind]
    prev_rank = {k: r for k, _, r in _rank(prev, name_of)} if prev is not None else {}
    out = []
    for k, n, rank in _rank(cur, name_of)[:limit]:
        p = prev.get(k, 0) if prev is not None else None
        ent = first_of.get(k)
        new_ever = bool(ent and s <= ent[0] < e)
        badge = None
        if prev is not None and p < MIN_PREV_FOR_PCT:
            if new_ever:
                badge = None if warmup else "new_ever"
            else:
                badge = "re_entry"
        row = {"rank": rank, "key": k, "name": name_of(k), "plays": n, "prev_plays": p,
               "pct_change": _pct_change(n, p) if compare else None,
               "prev_rank": prev_rank.get(k), "badge": badge,
               "first_ts": ent[0] if ent else None}
        if kind == "album":
            row["artist"] = led.artist_name(ent[4]) if ent else None
            row["artist_key"] = ent[4] if ent else None
        elif kind == "song":
            row["artist"] = led.artist_name(ent[4]) if ent else None
            row["artist_key"] = ent[4] if ent else None
            row["album"] = ent[5] if ent else None
            row["spotify_track_id"] = ent[6] if ent else None
        out.append(row)
    return out


def _ok_profile_between(led, s, e):
    """(plays with real timestamps, days with one) in [s, e).

    Per play rather than per day: a running period's previous period ends mid-day (same day and
    time), and whole days would count plays after that moment.
    """
    if not led._cols or s is None or e is None:
        return 0, 0
    i = bisect.bisect_left(led.ts, s)
    j = bisect.bisect_left(led.ts, e)
    ts, q = led._cols[0], led._cols[3]
    n, days = 0, set()
    for k in range(i, j):
        if q[k] == "bulk":
            continue
        n += 1
        days.add(local_day(ts[k]))
    return n, len(days)


def local_art(conn, song_keys=(), album_keys=()):
    """({song_key: image}, {album_key: image}) from album art the app already stored.

    The ``tracks`` table keeps the Spotify art of every track the app has seen, so top lists get
    covers without a single Spotify call. Keys without a known track simply get no image.
    """
    songs, albums = {}, {}
    try:
        if not _table_exists(conn, "tracks") or "image_url" not in _columns(conn, "tracks"):
            return songs, albums
        for col, keys, out in (("song_key", song_keys, songs), ("album_key", album_keys, albums)):
            keys = [k for k in dict.fromkeys(keys) if k]
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                out.update(_rows(conn, f"""SELECT p.{col}, MAX(t.image_url) FROM plays p
                    JOIN tracks t ON t.id = p.spotify_track_id
                    WHERE p.{col} IN ({','.join('?' * len(chunk))}) AND t.image_url LIKE 'http%'
                    GROUP BY p.{col}""", tuple(chunk)))
    except Exception:
        # art is decoration: a locked or odd DB must never cost the report itself
        return {}, {}
    return songs, albums


def _with_art(conn, payload):
    top = payload.get("top") or {}
    songs, albums = local_art(conn, [r["key"] for r in top.get("tracks") or ()],
                              [r["key"] for r in top.get("albums") or ()])
    for r in top.get("tracks") or ():
        r["image"] = songs.get(r["key"])
    for r in top.get("albums") or ():
        r["image"] = albums.get(r["key"])
    return payload


def _new_count(first_of, keys, s, e):
    n = 0
    for k in keys:
        ent = first_of.get(k)
        if ent and s <= ent[0] < e:
            n += 1
    return n


def _period_payload(led, b, limit, now_ms):
    s, e = b["start_ms"], b["end_ms"]
    ps, pe = b["prev_start_ms"], b["prev_end_ms"]
    start_d, end_d = date.fromisoformat(b["start_day"]), date.fromisoformat(b["end_day"]) + timedelta(days=1)
    today = local_day(now_ms)
    is_all = b["type"] == "all"
    warmup = b["warmup"]

    # a previous period that ends before the history starts has no data to compare with
    has_prev = ps is not None and led.start_ms is not None and pe > led.start_ms
    if is_all:
        cur_a, cur_s, cur_al = _all_counts(led, "artist"), _all_counts(led, "song"), _all_counts(led, "album")
    else:
        cur_a = led.counts_between("artist", s, e)
        cur_s = led.counts_between("song", s, e)
        cur_al = led.counts_between("album", s, e)
    prev_a = prev_s = prev_al = None
    if has_prev:
        prev_a = led.counts_between("artist", ps, pe)
        prev_s = led.counts_between("song", ps, pe)
        prev_al = led.counts_between("album", ps, pe)

    cov = coverage(led, now_ms, e, pe)
    compare = has_prev and not cov["comparison_affected"]

    plays = sum(cur_a.values())
    prev_plays = sum(prev_a.values()) if prev_a is not None else None
    clock, wd_plays, wd_days, per_day = _time_profile(led, start_d, end_d)
    ok_plays = sum(clock)
    active_days = len(per_day)
    days = b["days_elapsed"]
    prev_ok, prev_active = _ok_profile_between(led, ps, pe) if has_prev else (None, None)

    new_artists = _new_count(led.artist, cur_a, s, e)
    new_albums = _new_count(led.album, cur_al, s, e)
    new_tracks = _new_count(led.song, cur_s, s, e)
    plays_on_new = sum(n for k, n in cur_s.items() if led.song.get(k) and s <= led.song[k][0] < e)
    hide_new = warmup  # everything is "new" near the start of the history

    totals = {
        "plays": plays, "prev_plays": prev_plays,
        "pct_change": _pct_change(plays, prev_plays) if compare else None,
        "listening_s_est": round(led.est_seconds(s, e)),
        "prev_listening_s_est": round(led.est_seconds(ps, pe)) if has_prev else None,
        "active_days": active_days, "days_in_period": days, "days_total": b["days_total"],
        "avg_plays_per_day": round(ok_plays / days, 1) if days else None,
        "avg_plays_per_active_day": round(ok_plays / active_days, 1) if active_days else None,
        "prev_active_days": prev_active,
        "prev_avg_plays_per_active_day": round(prev_ok / prev_active, 1) if prev_active else None,
        "artists": len(cur_a), "prev_artists": len(prev_a) if prev_a is not None else None,
        "albums": len(cur_al), "prev_albums": len(prev_al) if prev_al is not None else None,
        "tracks": len(cur_s), "prev_tracks": len(prev_s) if prev_s is not None else None,
        "new_artists": None if hide_new else new_artists,
        "new_albums": None if hide_new else new_albums,
        "new_tracks": None if hide_new else new_tracks,
        "pct_new_artists": None if hide_new else _pct(new_artists, len(cur_a)),
        "pct_new_albums": None if hide_new else _pct(new_albums, len(cur_al)),
        "pct_new_tracks": None if hide_new else _pct(new_tracks, len(cur_s)),
        "pct_plays_on_new_tracks": None if hide_new else _pct(plays_on_new, plays),
        "bulk_plays": plays - ok_plays,
    }

    top = {
        "artists": _top_rows(led, "artist", cur_a, prev_a, s, e, limit, warmup, compare),
        "albums": _top_rows(led, "album", cur_al, prev_al, s, e, limit, warmup, compare),
        "tracks": _top_rows(led, "song", cur_s, prev_s, s, e, limit, warmup, compare),
    }

    most_active = None
    if per_day:
        d, n = min(per_day.items(), key=lambda kv: (-kv[1], kv[0]))
        most_active = {"day": d.isoformat(), "plays": n}
    busiest = None
    if ok_plays:
        h = max(range(24), key=lambda i: (clock[i], -i))
        busiest = {"hour": h, "plays": clock[h]}
    new_keys = [] if hide_new else [k for k in cur_a if led.artist.get(k) and s <= led.artist[k][0] < e]
    new_keys.sort(key=lambda k: (-cur_a[k], led.artist_name(k).casefold()))
    discoveries = [{"name": led.artist_name(k), "key": k, "first_ts": led.artist[k][0], "plays": cur_a[k]}
                   for k in new_keys[:10]]
    quick = {
        "most_active_day": most_active,
        "busiest_hour": busiest,
        "longest_streak": _longest(_islands(sorted(per_day))),
        "top_new_artist": ({"name": discoveries[0]["name"], "key": discoveries[0]["key"],
                            "plays": discoveries[0]["plays"]} if discoveries else None),
    }
    period = {k: b[k] for k in ("type", "anchor", "label", "prev_label", "start_ms", "end_ms", "prev_start_ms",
                                "prev_end_ms", "complete", "is_current", "warmup", "prev_anchor", "next_anchor",
                                "start_day", "end_day", "prev_truncated", "as_of_ms")}
    return {
        "period": period,
        "totals": totals,
        "top": top,
        "series": _series(b["type"], start_d, end_d, today, per_day),
        "clock": [{"hour": h, "plays": clock[h]} for h in range(24)],
        "weekday": [{"dow": i, "label": DOW_LABELS[i], "plays": wd_plays[i], "active_days": wd_days[i],
                     "per_active_day": round(wd_plays[i] / wd_days[i], 1) if wd_days[i] else None}
                    for i in range(7)],
        "quick_facts": quick,
        "discoveries": discoveries,
        "coverage": cov,
        "definitions": report_definitions(led),
    }


def _time_bucket(b, now_ms):
    # finished periods never change for one ledger version; running ones depend on "now"
    return None if b["complete"] else now_ms // 60_000


def report(conn, period="month", anchor=None, limit=10, now_ms=None, ledger_wait=True):
    """Full period report (see the module docstring for the counting rules)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise BadRequest("limit", "limit must be a whole number between 1 and 50.") from None
    if not 1 <= limit <= 50:
        raise BadRequest("limit", "limit must be a whole number between 1 and 50.")
    now_ms = _now_ms() if now_ms is None else int(now_ms)
    led = get_ledger(conn, wait=ledger_wait)
    b = period_bounds(period, anchor, now_ms, led.start_ms)
    key = ("report", period, b["anchor"], limit, led.sig, _time_bucket(b, now_ms))
    return _cached(key, lambda: _with_art(conn, _period_payload(led, b, limit, now_ms)))


# ---------------------------------------------------------------------------
# year in music
# ---------------------------------------------------------------------------

def _artist_cover(conn, top, artist_key, start_ms, end_ms):
    """Album art standing in for an artist photo (the app stores none): the cover of the artist's
    most played album in the period, else of their most played song with known art.

    Returns {"image", "image_from": {"kind": "album" | "track", "name"}} or {"image": None}.
    """
    for kind, label in (("albums", "album"), ("tracks", "track")):
        for r in top.get(kind) or ():
            if r.get("artist_key") == artist_key and r.get("image"):
                return {"image": r["image"], "image_from": {"kind": label, "name": r.get("name")}}
    try:
        for col, label in (("album_key", "album"), ("song_key", "track")):
            name_col = "album_name" if label == "album" else "track_name"
            rows = _rows(conn, f"""SELECT {col}, MAX({name_col}), COUNT(*) AS n FROM plays
                WHERE artist_key = ? AND ts_ms >= ? AND ts_ms < ? AND superseded_by IS NULL
                  AND {col} IS NOT NULL AND {col} != ''
                GROUP BY {col} ORDER BY n DESC LIMIT 10""", (artist_key, start_ms, end_ms))
            if not rows:
                continue
            art = local_art(conn, **{"song_keys" if label == "track" else "album_keys": [r[0] for r in rows]})
            found = art[0] if label == "track" else art[1]
            for key, name, _ in rows:
                if found.get(key):
                    return {"image": found[key], "image_from": {"kind": label, "name": name}}
    except Exception:
        pass   # art is decoration
    return {"image": None}


def year_in_music(conn, year=None, now_ms=None, ledger_wait=True):
    now_ms = _now_ms() if now_ms is None else int(now_ms)
    today = local_day(now_ms)
    if year is None or year == "":
        year = today.year - 1
    try:
        year = int(year)
    except (TypeError, ValueError):
        raise BadRequest("year", "year must be a four-digit year like 2025.") from None
    if not 1970 <= year <= today.year:
        raise BadRequest("year", f"year must be between 1970 and {today.year}.")
    anchor = date(year, 1, 1)
    led = get_ledger(conn, wait=ledger_wait)
    rep = report(conn, "year", anchor, YIM_TOP_N, now_ms, ledger_wait=ledger_wait)
    t, top, q = rep["totals"], rep["top"], rep["quick_facts"]

    def slim(r, with_artist):
        out = {"rank": r["rank"], "name": r["name"], "key": r["key"], "plays": r["plays"]}
        if with_artist:
            out["artist"] = r.get("artist")
            out["artist_key"] = r.get("artist_key")
            out["image"] = r.get("image")
        if "spotify_track_id" in r:
            out["spotify_track_id"] = r.get("spotify_track_id")
            out["album"] = r.get("album")
        return out

    def pick(rows, with_artist):
        if not rows:
            return None
        out = slim(rows[0], with_artist)
        out.pop("rank")   # the single top_* picks keep their original shape
        return out

    months = [p for p in rep["series"]["points"] if p["plays"]]
    peak = max(months, key=lambda p: (p["plays"], -int(p["key"][5:]))) if months else None
    top_artist = pick(top["artists"], False)
    if top_artist:
        top_artist.update(_artist_cover(conn, top, top_artist["key"], rep["period"]["start_ms"],
                                        rep["period"]["end_ms"]))
    return {
        "year": year,
        "complete": rep["period"]["complete"],
        "warmup": rep["period"]["warmup"],
        "plays": t["plays"], "prev_plays": t["prev_plays"], "pct_change": t["pct_change"],
        "prev_label": rep["period"]["prev_label"],
        "hours_est": round(t["listening_s_est"] / 3600) if t["listening_s_est"] is not None else None,
        "listening_s_est": t["listening_s_est"],
        "active_days": t["active_days"], "days": t["days_in_period"],
        "artists": t["artists"], "new_artists": t["new_artists"], "pct_new_artists": t["pct_new_artists"],
        "albums": t["albums"], "tracks": t["tracks"], "new_tracks": t["new_tracks"],
        "pct_new_tracks": t["pct_new_tracks"],
        "top_artist": top_artist,
        "top_artist_share_pct": _pct(top_artist["plays"], t["plays"]) if top_artist else None,
        "top_album": pick(top["albums"], True),
        "top_track": pick(top["tracks"], True),
        "top_artists": [slim(r, False) for r in top["artists"]],
        "top_albums": [slim(r, True) for r in top["albums"]],
        "top_tracks": [slim(r, True) for r in top["tracks"]],
        "months": [{"ym": p["key"], "label": p["label"], "plays": p["plays"]} for p in rep["series"]["points"]],
        "top_new_artist": q["top_new_artist"],
        "peak_month": ({"ym": peak["key"], "label": MONTH_NAMES[int(peak["key"][5:]) - 1], "plays": peak["plays"]}
                       if peak else None),
        "most_active_day": q["most_active_day"],
        "busiest_hour": q["busiest_hour"],
        "longest_streak": q["longest_streak"],
        "coverage": rep["coverage"],
        "definitions": dict(report_definitions(led), **{
            "peak_month": "The month with the most plays (plays with approximate bulk timestamps left out).",
            "hours_est": "Estimated hours of listening (see listening_time).",
        }),
    }


# ---------------------------------------------------------------------------
# all-time overview
# ---------------------------------------------------------------------------

def _peak7(led):
    """Most plays of one song inside any 7-day window (ok plays only), best 10 songs."""
    best = []
    for sk, ts in led.song_ok_ts().items():
        if len(ts) < OBSESSION_MIN_PLAYS:
            continue
        j = 0
        peak, peak_start = 0, None
        n = len(ts)
        for i in range(n):
            if j < i:
                j = i
            lim = ts[i] + WEEK_MS
            while j < n and ts[j] <= lim:
                j += 1
            if j - i > peak:
                peak, peak_start = j - i, ts[i]
        best.append((peak, led.song[sk][2], sk, peak_start))
    best.sort(key=lambda t: (-t[0], -t[1], t[2]))
    out = []
    for peak, life, sk, start in best[:10]:
        ent = led.song[sk]
        out.append({"track": ent[3], "song_key": sk, "artist": led.artist_name(ent[4]), "artist_key": ent[4],
                    "peak7": peak, "peak7_start": start, "peak7_start_day": local_day(start).isoformat(),
                    "lifetime": life, "spotify_track_id": ent[6]})
    return out


def _year_bounds_ms(y):
    return midnight_ms(date(y, 1, 1)), midnight_ms(date(y + 1, 1, 1))


def _overview_payload(conn, led, now_ms):
    today = local_day(now_ms)
    cov = coverage(led, now_ms)
    asof = cov["complete_through_ts"]
    warm_end = led.warmup_end_ms()
    total = led.total

    # per-year figures
    years = {}
    for (y, ak), n in led.year_artist.items():
        rec = years.setdefault(y, {"plays": 0, "artists": 0, "new": 0, "top": (0, None)})
        rec["plays"] += n
        rec["artists"] += 1
        if local_dt(led.artist[ak][0]).year == y:
            rec["new"] += 1
        top_n, top_k = rec["top"]
        if n > top_n or (n == top_n and led.artist_name(ak).casefold() < led.artist_name(top_k).casefold()):
            rec["top"] = (n, ak)
    ok_days_by_year = {}
    for d in led.ok_days:
        ok_days_by_year[d.year] = ok_days_by_year.get(d.year, 0) + 1
    plays_by_year = []
    for y in sorted(years):
        rec = years[y]
        ys, ye = _year_bounds_ms(y)
        warm = warm_end is not None and ys < warm_end
        n_top, k_top = rec["top"]
        plays_by_year.append({
            "year": y, "plays": rec["plays"], "hours_est": round(led.est_seconds(ys, ye) / 3600),
            "active_days": ok_days_by_year.get(y, 0), "complete": ye <= now_ms, "warmup": warm,
            "artists": rec["artists"], "new_artists": None if warm else rec["new"],
            "pct_new_artists": None if warm else _pct(rec["new"], rec["artists"]),
            "top_artist": ({"name": led.artist_name(k_top), "key": k_top, "plays": n_top} if k_top else None),
            "top_artist_share_pct": _pct(n_top, rec["plays"]) if k_top else None,
        })

    # streaks
    isl = _islands(led.ok_days)
    current = None
    if isl:
        s, e, n = isl[-1]
        if e >= today - timedelta(days=1):
            current = {"days": n, "start": s.isoformat(), "end": e.isoformat(), "includes_today": e == today}

    # concentration
    ranked = sorted((v[2] for v in led.artist.values()), reverse=True)

    def share(k):
        return _pct(sum(ranked[:k]), total)
    one_play = sum(1 for n in ranked if n == 1)

    # discovery + stickiness (warm-up and right-censoring guards)
    lo_d, hi_d = RETURN_WINDOW_DAYS
    artist_ts = led.artist_ts()
    disc = {}
    for ak, (first, _last, _n, _name) in led.artist.items():
        if warm_end is None or first < warm_end:
            continue
        y = local_dt(first).year
        rec = disc.setdefault(y, {"discovered": 0, "eligible": 0, "returned": 0})
        rec["discovered"] += 1
        if first + hi_d * DAY_MS <= asof:
            rec["eligible"] += 1
            ts = artist_ts[ak]
            i = bisect.bisect_left(ts, first + lo_d * DAY_MS)
            if i < len(ts) and ts[i] <= first + hi_d * DAY_MS:
                rec["returned"] += 1
    year_artists = {r["year"]: r["artists"] for r in plays_by_year}
    discovery_by_year = []
    for y in sorted(disc):
        ys, _ = _year_bounds_ms(y)
        if warm_end is not None and ys < warm_end:
            continue  # a year that starts inside the warm-up would compare a partial count
        rec = disc[y]
        discovery_by_year.append({
            "year": y, "discovered": rec["discovered"],
            "pct_of_year_artists": _pct(rec["discovered"], year_artists.get(y)),
            "eligible": rec["eligible"],
            "returned_pct": _pct(rec["returned"], rec["eligible"]) if rec["eligible"] >= 10 else None,
        })

    # best discoveries: post warm-up artists by lifetime plays
    rank_of = {}
    for i, (k, n, r) in enumerate(_rank(_all_counts(led, "artist"), led.artist_name)):
        rank_of[k] = r
    found = [(v[2], k) for k, v in led.artist.items() if warm_end is not None and v[0] >= warm_end]
    found.sort(key=lambda t: (-t[0], led.artist_name(t[1]).casefold()))
    best = []
    for n, k in found[:10]:
        first = led.artist[k][0]
        ts = artist_ts[k]
        after30 = len(ts) - bisect.bisect_left(ts, first + 30 * DAY_MS)
        best.append({"name": led.artist_name(k), "key": k, "first_ts": first, "plays": n,
                     "plays_after_30d": after30, "rank": rank_of.get(k)})

    # rediscover: loved in a peak year, quiet for 90+ days of *complete* data
    peak = {}
    for (y, ak), n in led.year_artist.items():
        cur = peak.get(ak)
        if cur is None or n > cur[1] or (n == cur[1] and y < cur[0]):
            peak[ak] = (y, n)
    quiet_before = asof - REDISCOVER_QUIET_DAYS * DAY_MS
    redis = []
    for ak, (y, n) in peak.items():
        if n < REDISCOVER_MIN_PEAK:
            continue
        last = led.artist[ak][1]
        if last >= quiet_before:
            continue
        redis.append({"name": led.artist_name(ak), "key": ak, "peak_year": y, "peak_plays": n,
                      "lifetime": led.artist[ak][2], "last_ts": last, "days_since": int((asof - last) // DAY_MS)})
    redis.sort(key=lambda r: (-r["peak_plays"], r["name"].casefold()))
    # each artist's most-played song, so a rediscover row can start playing something of theirs
    want = {r["key"] for r in redis[:10]}
    fav = {}
    for sk, v in led.song.items():
        ak = v[4]
        if ak in want and (ak not in fav or (v[2], v[1]) > (fav[ak][2], fav[ak][1])):
            fav[ak] = v
    for r in redis[:10]:
        v = fav.get(r["key"])
        r["top_song"] = {"name": v[3], "plays": v[2], "spotify_track_id": v[6]} if v else None

    # clock by year
    by_year_hours = {}
    for d in led.ok_days:
        hrs = by_year_hours.setdefault(d.year, [0] * 24)
        for h, c in enumerate(led.dayhour[d]):
            hrs[h] += c
    clock_by_year = []
    for y in sorted(by_year_hours):
        hrs = by_year_hours[y]
        tot = sum(hrs)
        if not tot:
            continue
        ph = max(range(24), key=lambda i: (hrs[i], -i))
        night = sum(hrs[h] for h in (22, 23, 0, 1, 2, 3))
        clock_by_year.append({"year": y, "peak_hour": ph, "peak_hour_pct": _pct(hrs[ph], tot),
                              "night_pct": _pct(night, tot), "plays": tot})
    all_clock = [0] * 24
    for hrs in by_year_hours.values():
        for h in range(24):
            all_clock[h] += hrs[h]

    first_d = local_day(led.start_ms) if led.start_ms is not None else None
    last_d = local_day(led.newest_ts) if led.newest_ts is not None else None
    definitions = _common_definitions(led)
    definitions.update({
        "hours_est": "Estimated hours of listening (see listening_time).",
        "active_days": "Days with at least one play.",
        "days_span": "Days from your first play to your latest one.",
        "streaks": ("Most days in a row with at least one play. The current streak counts if you listened today "
                    "or yesterday."),
        "concentration": ("How much of your listening goes to your favourites: the share of all plays held by your "
                          "top 1, 10 and 50 artists, and how many artists you played only once."),
        "discovery_by_year": (f"Artists first heard each year (after the first {WARMUP_DAYS} days of history), as a "
                              f"share of all artists played that year. 'Came back' is the share played again "
                              f"{lo_d}–{hi_d} days after the first play; only artists discovered at least {hi_d} "
                              f"days before your latest complete data are counted, so recent finds don't drag "
                              f"it down."),
        "best_discoveries": (f"Artists you found after your first {WARMUP_DAYS} days of history, ranked by all "
                             f"their plays since."),
        "obsessions": "The most plays of one song within any 7 days (plays with approximate bulk times left out).",
        "rediscover": (f"Artists with at least {REDISCOVER_MIN_PEAK} plays in their best year that you haven't "
                       f"played for {REDISCOVER_QUIET_DAYS}+ days. Counted up to your latest complete data, so a "
                       f"stalled Last.fm sync doesn't make everyone look forgotten."),
        "clock_by_year": "Your busiest hour each year, and the share of plays between 22:00 and 04:00.",
        "top_artists": "Your most-played artists of all time.",
    })
    return {
        "totals": {
            "plays": total, "artists": len(led.artist), "tracks": len(led.song), "recordings": led.recordings,
            "albums": len(led.album), "hours_est": round(led.cum_s[-1] / 3600),
            "first_ts": led.start_ms, "last_ts": led.newest_ts, "active_days": len(led.ok_days),
            "days_span": (last_d - first_d).days + 1 if first_d else 0,
            "avg_plays_per_active_day": round(sum(all_clock) / len(led.ok_days), 1) if led.ok_days else None,
            "bulk_plays": led.bulk_total,
        },
        "plays_by_year": plays_by_year,
        "streaks": {"longest": _longest(isl), "current": current},
        "top_artists": [{"rank": r, "key": k, "name": led.artist_name(k), "plays": n, "share_pct": _pct(n, total, 2)}
                        for k, n, r in _rank(_all_counts(led, "artist"), led.artist_name)[:20]],
        "concentration": {"top1_pct": share(1), "top10_pct": share(10), "top50_pct": share(50),
                          "top200_pct": share(200), "one_play_artists": one_play,
                          "one_play_artists_pct": _pct(one_play, len(ranked))},
        "discovery_by_year": discovery_by_year,
        "best_discoveries": best,
        "obsessions": led.derived("peak7", lambda: _peak7(led)),
        "rediscover": redis[:10],
        "rediscover_as_of_ts": asof,
        "clock": [{"hour": h, "plays": all_clock[h]} for h in range(24)],
        "clock_by_year": clock_by_year,
        "coverage": cov,
        "definitions": definitions,
    }


def overview(conn, now_ms=None, ledger_wait=True):
    now_ms = _now_ms() if now_ms is None else int(now_ms)
    led = get_ledger(conn, wait=ledger_wait)
    # "current streak" and "complete" flags depend on the date, not the minute
    key = ("overview", led.sig, local_day(now_ms).isoformat(), now_ms // 3_600_000)

    def build():
        out = _overview_payload(conn, led, now_ms)
        songs, _ = local_art(conn, [o.get("song_key") for o in out.get("obsessions") or ()])
        for o in out.get("obsessions") or ():
            o["image"] = songs.get(o.get("song_key"))
        return out
    return _cached(key, build)


# ---------------------------------------------------------------------------
# today (for /api/status): computed from events so it rolls over at local midnight
# ---------------------------------------------------------------------------

UNDO_WINDOW_MS = 35 * 60 * 1000   # an undo is only offered for 30 min after a clear
# "Heard in app" uses the rule that makes an app play count as a play in the ledger
# (plays.MIN_LIVE_S), so the Today card never shows more tracks heard than plays.
HEARD_MIN_S = 30.0


def _effective_clears(rows):
    """[(track_id, ts)] of removals that were not undone. rows: (event_type, track_id, ts) sorted by ts.

    A TRACK_RESTORED cancels every removal of that track logged in the undo window before it
    (one clear is sometimes logged several times), but not an older, separate clear.
    """
    pending = {}
    for etype, tid, ts in rows:
        if etype == "TRACK_RESTORED":
            lst = pending.get(tid)
            if lst:
                pending[tid] = [t for t in lst if t < ts - UNDO_WINDOW_MS]
        else:
            pending.setdefault(tid, []).append(ts)
    return [(tid, t) for tid, lst in pending.items() for t in lst]


def today_sets(conn, now_ms=None, include_plays=True):
    """The DB half of the 'today' counts (cacheable): distinct track-id sets + plays today."""
    now_ms = _now_ms() if now_ms is None else int(now_ms)
    today = local_day(now_ms)
    t0 = midnight_ms(today)
    t7 = midnight_ms(today - timedelta(days=6))
    t1 = midnight_ms(today + timedelta(days=1))   # bounded so a clock step back can't count "tomorrow"
    live = " AND (source IS NULL OR source='live')" if "source" in _columns(conn, "events") else ""
    heard = _rows(conn, f"""SELECT track_id, MAX(ts_ms) FROM events
        WHERE ts_ms >= ? AND ts_ms < ? AND event_type='SONG_PLAYED' AND track_id IS NOT NULL{live}
          AND CAST(json_extract(payload, '$.listened_s') AS REAL) >= ?
        GROUP BY track_id""", (t7, t1, HEARD_MIN_S))
    removal_rows = _rows(conn, """SELECT event_type, track_id, ts_ms FROM events
        WHERE ts_ms >= ? AND ts_ms < ? AND event_type IN ('TRACK_REMOVED_FROM_QUEUE','TRACK_RESTORED')
          AND track_id IS NOT NULL
        ORDER BY ts_ms, id""", (t7, t1))
    clears = _effective_clears(removal_rows)
    liked = _rows(conn, """SELECT DISTINCT track_id FROM events
        WHERE ts_ms >= ? AND ts_ms < ? AND event_type='TRACK_LIKED' AND track_id IS NOT NULL""", (t0, t1))
    plays_today = None
    if include_plays and _table_exists(conn, "plays"):
        hid = ""
        if _table_exists(conn, "artist_stats"):
            hid = " AND artist_key NOT IN (SELECT artist_key FROM artist_stats WHERE is_music=0)"
        plays_today = _one(conn, f"""SELECT COUNT(*) FROM plays
            WHERE ts_ms >= ? AND ts_ms < ? AND superseded_by IS NULL{hid}""", (t0, t1))[0]
    return {
        "day": today.isoformat(),
        "heard_today": {tid for tid, ts in heard if ts >= t0},
        "heard_7d": {tid for tid, _ in heard},
        "cleared_today": {tid for tid, ts in clears if ts >= t0},
        "cleared_7d": {tid for tid, _ in clears},
        "liked_today": {r[0] for r in liked},
        "plays_today": plays_today,
    }


def today_from_sets(sets, extra_heard=()):
    """Counts from ``today_sets`` plus tracks heard in the app that have no event yet."""
    extra = set(extra_heard or ())
    return {
        "day": sets["day"],
        "plays_today": sets["plays_today"],
        "heard_in_app": len(sets["heard_today"] | extra),
        "cleared_today": len(sets["cleared_today"]),
        "liked_today": len(sets["liked_today"]),
        "cleared_7d": len(sets["cleared_7d"]),
        "heard_in_app_7d": len(sets["heard_7d"] | extra),
    }


def today_counts(conn, now_ms=None, current_track_id=None, current_listened_s=0.0, min_listen_s=HEARD_MIN_S,
                 include_plays=True):
    """Counts for the 'today' strip. Everything is distinct per track and uses local midnight.

    - heard_in_app: distinct tracks this app saw played for HEARD_MIN_S seconds or more today
      (shorter skips are not "heard"), plus the track playing now once it has played
      ``min_listen_s`` seconds.
    - cleared_today: distinct tracks removed from the backlog today and not restored (undo).
    - liked_today: distinct tracks liked in the app today.
    - plays_today: canonical plays in the ledger since midnight (None while there is no ledger).
    - cleared_7d / heard_in_app_7d: the same over the last 7 local days, today included.
    """
    sets = today_sets(conn, now_ms, include_plays)
    extra = ()
    if current_track_id and float(current_listened_s or 0) >= float(min_listen_s):
        extra = (current_track_id,)
    return today_from_sets(sets, extra)
