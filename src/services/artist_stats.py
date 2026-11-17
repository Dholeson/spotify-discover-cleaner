"""Personal artist statistics and library queries over the canonical ``plays`` ledger.

Every number here is computed from ``plays WHERE superseded_by IS NULL`` (one row per real
listen: Last.fm scrobbles plus live plays that no scrobble covers), keyed by the normalized
``artist_key`` / ``song_key`` / ``album_key`` columns. That is what fixes the old artist page:
no double counting of live + scrobbled listens, no comma splitting of band names such as
'Woe, Is Me', and no full-table ``LIKE`` scans.

The key helpers come from ``src.persistence.plays`` (owned by the history package). They are
imported lazily with a local fallback that implements the same rules, so this module also
works against a scratch ledger built before that package lands.

Callers pass an open sqlite3 connection (any row_factory; rows are read positionally).
"""

import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime
from types import SimpleNamespace

log = logging.getLogger(__name__)

TOP_LIMIT = 20
HISTORY_START_GRACE_MS = 30 * 86400 * 1000  # "first heard" this close to the start of history is not a real date
_CACHE_TTL_S = 120.0
WARM_PROFILES = 5  # top artists whose profiles the background worker keeps precomputed
HIDDEN_META_KEY = "hidden_artist_keys"  # durable copy of the hide flags (artist_stats can be rebuilt)


class LedgerNotReady(RuntimeError):
    """The plays ledger has not been built yet (the history migration is still running)."""


# ---------------------------------------------------------------------------
# Key helpers: src.persistence.plays when available, else an identical local fallback
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
_FEAT = re.compile(r"\s*[\(\[](?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.I)
_FEAT_DASH = re.compile(r"\s+-\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
_FEAT_BARE = re.compile(r"\s+(?:feat\.|ft\.)\s.*$", re.I)
_REC_NOISE = re.compile(
    r"\s*(?:[\(\[](?:explicit|clean|remaster(?:ed)?(?: \d{4})?(?: version)?|\d{4} remaster(?:ed)?|single version|radio edit|album version|mono|stereo)[\)\]]"
    r"|\s-\s(?:explicit|remaster(?:ed)?(?: \d{4})?(?: version)?|\d{4} remaster(?:ed)?|single version|radio edit|album version|mono|stereo))\s*$", re.I)
_SONG_NOISE = re.compile(
    r"\s*(?:[\(\[](?:live(?: [^\)\]]*)?|tree city sessions(?: \d)?|acoustic(?: version)?|instrumental|heavy edit|(?:lost )?demo|stripped|unplugged)[\)\]]"
    r"|\s-\s(?:live(?: .*)?|tree city sessions(?: \d)?|acoustic(?: version)?|instrumental|heavy edit|(?:lost )?demo|stripped|unplugged))\s*$", re.I)
_ALB_NOISE = re.compile(
    r"\s*(?:[\(\[](?:deluxe(?: edition| version)?|expanded edition|bonus (?:track )?edition|remaster(?:ed)?(?: \d{4})?|\d{4} remaster(?:ed)?|explicit|special edition|anniversary edition|definitive)[\)\]]"
    r"|\s-\s(?:ep|single|deluxe(?: edition)?|remaster(?:ed)?(?: \d{4})?))\s*$", re.I)


def _fold(s):
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES).casefold().strip()
    return _WS.sub(" ", s)


def _recording_part(title):
    s = title or ""
    for _ in range(3):
        s2 = _REC_NOISE.sub("", _FEAT_BARE.sub("", _FEAT_DASH.sub("", _FEAT.sub("", s))))
        if s2 == s:
            break
        s = s2
    return _fold(s)


def _song_part(title):
    s = _recording_part(title)
    for _ in range(3):
        s2 = _REC_NOISE.sub("", _SONG_NOISE.sub("", s))
        if s2 == s:
            break
        s = s2
    return s.strip()


def _album_part(album):
    s = album or ""
    for _ in range(3):
        s2 = _ALB_NOISE.sub("", s)
        if s2 == s:
            break
        s = s2
    return _fold(s)


def _fb_artist_key(name):
    return _fold(name)


def _fb_track_key(artist_key, title):
    return f"{artist_key}|{_recording_part(title)}"


def _fb_song_key(artist_key, title):
    return f"{artist_key}|{_song_part(title)}"


def _fb_album_key(artist_key, album):
    if not (album or "").strip():
        return None
    return f"{artist_key}|{_album_part(album)}"


def _fb_ensure_plays_ready(conn):
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plays'").fetchone():
            return False
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plays)").fetchall()}
    except Exception:
        return False
    return {"artist_key", "song_key", "superseded_by", "ts_quality"} <= cols


_FALLBACK = SimpleNamespace(
    artist_key=_fb_artist_key,
    track_key=_fb_track_key,
    song_key=_fb_song_key,
    album_key=_fb_album_key,
    ensure_plays_ready=_fb_ensure_plays_ready,
    is_fallback=True,
)

_keys_lock = threading.Lock()
_keys_state = {"mod": None, "checked": 0.0}


def keys():
    """The shared key helpers. Retries the real module every 30 s while it is missing or broken."""
    now = time.monotonic()
    with _keys_lock:
        mod = _keys_state["mod"]
        if mod is not None and (mod is not _FALLBACK or now - _keys_state["checked"] < 30):
            return mod
        _keys_state["checked"] = now
        try:
            from src.persistence import plays as real  # noqa: WPS433 (lazy on purpose)
            needed = ("artist_key", "track_key", "song_key", "album_key")
            mod = real if all(callable(getattr(real, n, None)) for n in needed) else _FALLBACK
        except Exception:
            mod = _FALLBACK
        _keys_state["mod"] = mod
        return mod


def artist_key(name):
    return keys().artist_key((name or "").strip())


def song_key_for(artist_name, title):
    k = keys()
    return k.song_key(k.artist_key((artist_name or "").strip()), title or "")


def ledger_ready(conn):
    """True when the plays ledger is built and can be queried."""
    mod = keys()
    fn = getattr(mod, "ensure_plays_ready", None)
    if mod is _FALLBACK or not callable(fn):
        return _fb_ensure_plays_ready(conn)
    try:
        return bool(fn(conn))
    except Exception:
        log.exception("ensure_plays_ready failed")
        return False


def require_ledger(conn):
    if conn is None or not ledger_ready(conn):
        raise LedgerNotReady("The listening history is still being prepared.")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _table_exists(conn, name):
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except Exception:
        return False


def _db_identity(conn):
    """Cache namespace: the database file (shared by per-thread connections) or the connection itself."""
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        path = ""
    return path or f"conn:{id(conn)}"


def _meta_get(conn, key):
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _local_now(now_ms=None):
    return datetime.fromtimestamp((now_ms if now_ms is not None else time.time() * 1000) / 1000)


def _pick_name(counter, fallback=""):
    """Most frequent variant; ties go to the shorter (least decorated) spelling."""
    if not counter:
        return fallback
    return min(counter.items(), key=lambda kv: (-kv[1], len(kv[0] or ""), kv[0] or ""))[0] or fallback


def _longest_streak(days):
    ords = sorted({date.fromisoformat(d).toordinal() for d in days if d})
    best = None
    run = 0
    start = prev = None
    for o in ords:
        if prev is not None and o == prev + 1:
            run += 1
        else:
            run, start = 1, o
        if best is None or run >= best[0]:  # ties: prefer the most recent streak
            best = (run, start, o)
        prev = o
    if not best:
        return None
    return {"days": best[0], "start": date.fromordinal(best[1]).isoformat(), "end": date.fromordinal(best[2]).isoformat()}


def _last_months(now_dt, n):
    y, m = now_dt.year, now_dt.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Hidden (non-music) artists
# ---------------------------------------------------------------------------

def hidden_keys(conn):
    """Artist keys flagged as not music (podcasts, comedy)."""
    out = set()
    if _table_exists(conn, "artist_stats"):
        try:
            out.update(r[0] for r in conn.execute("SELECT artist_key FROM artist_stats WHERE is_music=0").fetchall())
        except Exception:
            log.exception("reading artist_stats.is_music failed")
    raw = _meta_get(conn, HIDDEN_META_KEY)
    if raw:
        try:
            meta = set(json.loads(raw) or [])
        except Exception:
            meta = set()
        missing = meta - out
        if missing and not conn.in_transaction and _table_exists(conn, "artist_stats"):
            # artist_stats may have been rebuilt since the user hid these: re-apply the flag.
            try:
                conn.executemany("UPDATE artist_stats SET is_music=0 WHERE artist_key=?", [(k,) for k in missing])
                conn.commit()
            except Exception:
                log.debug("could not re-apply hidden flags", exc_info=True)
        out |= meta
    return out


def set_hidden(conn, name, hidden):
    """Hide/unhide an artist from reports. Writes artist_stats.is_music plus a durable app_meta copy."""
    require_ledger(conn)
    key = artist_key(name)
    if not key:
        raise ValueError("name")
    display = (name or "").strip()
    row = conn.execute(
        "SELECT artist_name, COUNT(*) FROM plays WHERE artist_key=? AND superseded_by IS NULL "
        "GROUP BY artist_name ORDER BY 2 DESC LIMIT 1", (key,)).fetchone()
    if row and row[0]:
        display = row[0]
    conn.execute(
        "INSERT INTO artist_stats (artist_key, display_name, is_music) VALUES (?, ?, ?) "
        "ON CONFLICT(artist_key) DO UPDATE SET is_music=excluded.is_music",
        (key, display, 0 if hidden else 1))
    try:
        meta = set(json.loads(_meta_get(conn, HIDDEN_META_KEY) or "[]") or [])
    except Exception:
        meta = set()
    if hidden:
        meta.add(key)
    else:
        meta.discard(key)
    conn.execute("INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (HIDDEN_META_KEY, json.dumps(sorted(meta))))
    conn.commit()
    invalidate_caches()
    return bool(hidden)


# ---------------------------------------------------------------------------
# Cached whole-ledger aggregates (artist totals, library search index)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_caches = {}  # (db identity, kind) -> {"sig", "at", "data"}


def invalidate_caches():
    with _cache_lock:
        _caches.clear()
    with _profile_lock:
        _profile_cache.clear()


def _signature(conn):
    row = conn.execute("SELECT COUNT(*), MAX(id) FROM plays").fetchone()
    return (row[0], row[1], _meta_get(conn, "plays_rules_version"))


_STALE_MAX_S = 600.0


def _refresh_in_background(ident, path, kind, builder):
    def run():
        conn = None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            sig = _signature(conn)
            data = builder(conn)
            with _cache_lock:
                _caches[ident] = {"sig": sig, "at": time.monotonic(), "data": data}
        except Exception:
            log.exception("background refresh of %s failed", kind)
            with _cache_lock:
                entry = _caches.get(ident)
                if entry:
                    entry["refreshing"] = False
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=run, name=f"artist-stats-{kind}", daemon=True).start()


def _cached(conn, kind, builder):
    """Whole-ledger aggregates, rebuilt when the ledger changes.

    A new play changes the signature; for file databases the previous snapshot keeps being
    served (for up to 10 min) while one background thread rebuilds it, so page loads never
    pay the ~100-200 ms rebuild. Only the very first build runs inline.
    """
    ident = (_db_identity(conn), kind)
    sig = _signature(conn)
    now = time.monotonic()
    with _cache_lock:
        entry = _caches.get(ident)
        if entry and entry["sig"] == sig and now - entry["at"] < _CACHE_TTL_S:
            return entry["data"]
        path = ident[0]
        if entry and not path.startswith("conn:") and now - entry["at"] < _STALE_MAX_S:
            if not entry.get("refreshing"):
                entry["refreshing"] = True
                _refresh_in_background(ident, path, kind, builder)
            return entry["data"]
    data = builder(conn)
    with _cache_lock:
        _caches[ident] = {"sig": sig, "at": now, "data": data}
    return data


def _build_artist_totals(conn):
    # One Python pass is ~2x faster than GROUP BY here (the grouping needs a temp b-tree).
    names = defaultdict(Counter)
    plays = Counter()
    first, last = {}, {}
    for k, name, ts in conn.execute(
            "SELECT artist_key, artist_name, ts_ms FROM plays WHERE superseded_by IS NULL"):
        names[k][name] += 1
        plays[k] += 1
        f = first.get(k)
        if f is None or ts < f:
            first[k] = ts
        l = last.get(k)
        if l is None or ts > l:
            last[k] = ts
    items = {}
    for k in plays:
        name = _pick_name(names[k], k)
        items[k] = {"key": k, "name": name, "fold": _fold(name), "plays": plays[k],
                    "first_ts": first[k], "last_ts": last[k]}
    desc_counts = sorted((-v for v in plays.values()))  # ascending negatives == descending plays
    return {"items": items, "neg_sorted": desc_counts, "total_plays": sum(plays.values())}


def artist_totals(conn):
    return _cached(conn, "artists", _build_artist_totals)


def warm_caches(conn):
    """Rebuild stale whole-ledger aggregates synchronously (background worker), so page
    requests find them current. Returns the kinds rebuilt."""
    rebuilt = []
    ident_db = _db_identity(conn)
    sig = _signature(conn)
    for kind, builder in (("artists", _build_artist_totals), ("library", _build_library_index)):
        with _cache_lock:
            entry = _caches.get((ident_db, kind))
            current = bool(entry and entry["sig"] == sig and time.monotonic() - entry["at"] < _CACHE_TTL_S / 2)
        if current:
            continue
        data = builder(conn)
        with _cache_lock:
            _caches[(ident_db, kind)] = {"sig": sig, "at": time.monotonic(), "data": data}
        rebuilt.append(kind)
    # The biggest artists are the slowest profiles (~60 ms for 8K plays) and the most visited:
    # keep them precomputed so their pages stay fast after each new play.
    totals = artist_totals(conn)
    for a in sorted(totals["items"].values(), key=lambda a: -a["plays"])[:WARM_PROFILES]:
        artist_profile(conn, a["name"], max_age_s=_PROFILE_TTL_S / 2)
    return rebuilt


def _rank_info(totals, hidden, key, plays):
    """Competition rank among music artists (hidden ones never take a slot)."""
    neg = totals["neg_sorted"]
    higher = bisect_left(neg, -plays)  # artists with strictly more plays
    hidden_higher = sum(1 for hk in hidden if hk != key and hk in totals["items"] and totals["items"][hk]["plays"] > plays)
    hidden_known = [hk for hk in hidden if hk in totals["items"]]
    music_artists = len(totals["items"]) - len([hk for hk in hidden_known if hk != key])
    music_plays = totals["total_plays"] - sum(totals["items"][hk]["plays"] for hk in hidden_known if hk != key)
    return higher - hidden_higher + 1, music_artists, music_plays


# ---------------------------------------------------------------------------
# Artist profile (the artist page)
# ---------------------------------------------------------------------------

_PROFILE_SQL = """
    SELECT ts_ms, day_local, ym, year, source, artist_name, track_name, track_key, song_key,
           album_name, album_key, spotify_track_id, ts_quality, linked_live_event_id
    FROM plays WHERE artist_key=? AND superseded_by IS NULL
"""


_profile_lock = threading.Lock()
_profile_cache = {}  # (db, name, key, month, limit, ledger signature, hidden) -> (monotonic, profile)
_PROFILE_CACHE_MAX = 64
_PROFILE_TTL_S = 60.0


def artist_profile(conn, name, now_ms=None, top_limit=TOP_LIMIT, max_age_s=None):
    """Personal stats for one artist. Raises LedgerNotReady when the ledger is not built.

    The returned dict follows the /api/v2/artist/<name>/stats contract. It also carries an
    internal ``heard`` entry ({"song_plays": {song_key: n}, "track_ids": {spotify_id: song_key}})
    that callers must pop before serializing (and must not mutate: profiles are cached until
    the ledger changes).
    """
    require_ledger(conn)
    display = (name or "").strip()
    key = keys().artist_key(display)
    now_dt = _local_now(now_ms)
    hidden_set = hidden_keys(conn)
    ck = (_db_identity(conn), display, key, now_dt.strftime("%Y-%m"), top_limit, _signature(conn),
          key in hidden_set)
    now = time.monotonic()
    with _profile_lock:
        hit = _profile_cache.get(ck)
        if hit and now - hit[0] < (_PROFILE_TTL_S if max_age_s is None else max_age_s):
            return dict(hit[1])
    profile = _compute_profile(conn, display, key, now_dt, hidden_set, top_limit)
    with _profile_lock:
        if len(_profile_cache) >= _PROFILE_CACHE_MAX:
            for old in sorted(_profile_cache, key=lambda k: _profile_cache[k][0])[:_PROFILE_CACHE_MAX // 4]:
                _profile_cache.pop(old, None)
        _profile_cache[ck] = (now, profile)
    return dict(profile)


def _compute_profile(conn, display, key, now_dt, hidden_set, top_limit):
    rows = conn.execute(_PROFILE_SQL, (key,)).fetchall() if key else []
    hidden = bool(key) and key in hidden_set

    names = Counter()
    split = Counter()
    heard_in_app = 0
    by_year = Counter()
    by_ym = Counter()
    ok_by_ym = Counter()
    ok_days = set()
    recordings = set()
    albums = set()
    songs = {}
    album_agg = {}
    track_ids = {}
    first_ts = last_ts = None

    for (ts, day, ym, year, source, a_name, t_name, t_key, s_key, al_name, al_key, sp_id, quality, linked) in rows:
        names[a_name] += 1
        split[source] += 1
        if source == "live" or linked is not None:
            heard_in_app += 1
        by_year[int(year)] += 1
        by_ym[ym] += 1
        if quality == "ok":
            ok_days.add(day)
            ok_by_ym[ym] += 1
        first_ts = ts if first_ts is None or ts < first_ts else first_ts
        last_ts = ts if last_ts is None or ts > last_ts else last_ts
        recordings.add(t_key)
        s = songs.get(s_key)
        if s is None:
            s = songs[s_key] = {"plays": 0, "names": Counter(), "variants": set(), "albums": Counter(),
                                "sp_id": None, "sp_ts": -1}
        s["plays"] += 1
        s["names"][t_name] += 1
        s["variants"].add(t_key)
        if al_name:
            s["albums"][al_name] += 1
        if sp_id:
            track_ids[sp_id] = s_key
            if ts > s["sp_ts"]:
                s["sp_id"], s["sp_ts"] = sp_id, ts
        if al_key:
            albums.add(al_key)
            a = album_agg.get(al_key)
            if a is None:
                a = album_agg[al_key] = {"plays": 0, "names": Counter(), "tracks": set()}
            a["plays"] += 1
            a["names"][al_name] += 1
            a["tracks"].add(t_key)

    plays = len(rows)
    found = plays > 0
    if found:
        display = _pick_name(names, display)

    totals = artist_totals(conn)
    rank = None
    share_pct = 0.0
    if found:
        rank, total_artists, music_plays = _rank_info(totals, hidden_set, key, plays)
        share_pct = round(100.0 * plays / music_plays, 2) if music_plays else 0.0
        if hidden:
            rank = None  # hidden artists take no slot in the ranking (same as the artist index)
    else:
        total_artists = len(totals["items"]) - len([k for k in hidden_set if k in totals["items"]])

    history_start = None
    if found:
        row = conn.execute("SELECT MIN(ts_ms) FROM plays").fetchone()
        history_start = row[0] if row else None

    top_tracks = []
    for s_key, s in sorted(songs.items(), key=lambda kv: (-kv[1]["plays"], _pick_name(kv[1]["names"]).casefold()))[:top_limit]:
        top_tracks.append({
            "name": _pick_name(s["names"]),
            "song_key": s_key,
            "plays": s["plays"],
            "variants": len(s["variants"]),
            "spotify_track_id": s["sp_id"],
            "album": _pick_name(s["albums"]) or None,
        })
    top_albums = []
    for al_key, a in sorted(album_agg.items(), key=lambda kv: (-kv[1]["plays"], _pick_name(kv[1]["names"]).casefold()))[:top_limit]:
        top_albums.append({"name": _pick_name(a["names"]), "album_key": al_key, "plays": a["plays"],
                           "tracks_heard": len(a["tracks"])})

    plays_by_year = []
    if found:
        y0 = min(by_year)
        y1 = max(max(by_year), now_dt.year)
        plays_by_year = [{"year": y, "plays": by_year.get(y, 0)} for y in range(y0, y1 + 1)]
    plays_by_month = [{"ym": ym, "plays": by_ym.get(ym, 0)} for ym in _last_months(now_dt, 24)]
    peak_month = None
    if ok_by_ym:
        ym, n = max(ok_by_ym.items(), key=lambda kv: (kv[1], kv[0]))
        peak_month = {"ym": ym, "plays": n}

    events = _artist_event_counts(conn, key, display) if key else {"cleared": 0, "liked": 0}

    return {
        "key": key,
        "name": display,
        "found": found,
        "hidden": hidden,
        "plays": plays,
        "rank": rank,
        "total_artists": total_artists,
        "share_pct": share_pct,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "first_is_history_start": bool(found and history_start is not None
                                       and first_ts - history_start <= HISTORY_START_GRACE_MS),
        "days_listened": len(ok_days),
        "songs": len(songs),
        "recordings": len(recordings),
        "albums": len(albums),
        "plays_by_year": plays_by_year,
        "plays_by_month": plays_by_month,
        "top_tracks": top_tracks,
        "top_albums": top_albums,
        "longest_streak": _longest_streak(ok_days),
        "peak_month": peak_month,
        "split": {
            "lastfm": split.get("lastfm", 0),
            "app": split.get("live", 0),
            "spotify_import": sum(v for s, v in split.items() if s not in ("lastfm", "live")),
            "heard_in_app": heard_in_app,
        },
        "backlog": {"cleared_count": events["cleared"]},
        "liked_count": events["liked"],
        "heard": {
            "song_plays": {k: v["plays"] for k, v in songs.items()},
            "track_ids": track_ids,
        },
    }


def public_profile(profile):
    """The profile without internal fields, ready to jsonify."""
    out = dict(profile)
    out.pop("heard", None)
    return out


def _event_payload(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def _known_artist(conn, key, cache):
    if key not in cache:
        cache[key] = conn.execute("SELECT 1 FROM plays WHERE artist_key=? LIMIT 1", (key,)).fetchone() is not None
    return cache[key]


def primary_artist_key(conn, payload, cache=None):
    """Primary artist key of an app event payload.

    New payloads carry artist arrays; legacy ones only a comma-joined credit string, which is
    resolved to its longest comma-prefix that is a known artist ('Woe, Is Me, X' -> 'woe, is me').
    """
    cache = {} if cache is None else cache
    K = keys()
    names = payload.get("artist_names")
    if not names and isinstance(payload.get("artist_list"), list):
        names = [a.get("name") for a in payload["artist_list"] if isinstance(a, dict)]
    if isinstance(names, list) and names and names[0]:
        return K.artist_key(str(names[0]))
    credit = str(payload.get("artists") or payload.get("artist_name") or "").strip()
    if not credit:
        return ""
    toks = [t.strip() for t in credit.split(",")]
    for i in range(len(toks), 0, -1):
        cand = K.artist_key(", ".join(toks[:i]))
        if cand and _known_artist(conn, cand, cache):
            return cand
    return K.artist_key(toks[0])


def _artist_event_counts(conn, key, display):
    """Distinct backlog tracks cleared and liked for this artist (app events, deduped by track)."""
    out = {"cleared": 0, "liked": 0}
    first_word = (display or key).split(",")[0].strip()
    if not first_word:
        return out
    pat = f"%{first_word}%"
    try:
        rows = conn.execute(
            "SELECT event_type, track_id, payload FROM events "
            "WHERE event_type IN ('TRACK_REMOVED_FROM_QUEUE', 'TRACK_LIKED') "
            "AND (json_extract(payload, '$.artists') LIKE ? OR json_extract(payload, '$.artist_names') LIKE ? "
            "     OR json_extract(payload, '$.artist_name') LIKE ?)",
            (pat, pat, pat)).fetchall()
    except Exception:
        log.debug("event counts unavailable", exc_info=True)
        return out
    cache = {}
    cleared, liked = set(), set()
    for etype, track_id, raw in rows:
        payload = _event_payload(raw)
        if primary_artist_key(conn, payload, cache) != key:
            continue
        ident = track_id or payload.get("track_name") or payload.get("name")
        if not ident:
            continue
        (cleared if etype == "TRACK_REMOVED_FROM_QUEUE" else liked).add(ident)
    out["cleared"], out["liked"] = len(cleared), len(liked)
    return out


def legacy_stats(conn, name):
    """The legacy /api/artist-info ``listen_stats`` shape, computed from the same profile."""
    p = artist_profile(conn, name)
    return legacy_from_profile(p)


def legacy_from_profile(p):
    days = int(p.get("days_listened") or 0)
    split = p.get("split") or {}
    return {
        "listen_count": p["plays"],
        "imported_listens": int(split.get("lastfm", 0)) + int(split.get("spotify_import", 0)),
        "app_listens": int(split.get("app", 0)),
        "liked_count": int(p.get("liked_count") or 0),
        "removed_count": int((p.get("backlog") or {}).get("cleared_count") or 0),
        "distinct_tracks": p["songs"],
        "distinct_recordings": p["recordings"],
        "distinct_albums": p["albums"],
        "distinct_days": days,
        "revisit_days": max(0, days - 1),
        "first_heard_at": p["first_ts"],
        "last_heard_at": p["last_ts"],
        "first_is_history_start": p["first_is_history_start"],
        "rank": p["rank"],
        "share_pct": p["share_pct"],
        "hidden": p["hidden"],
        "top_tracks": [{"name": t["name"], "count": t["plays"], "song_key": t["song_key"],
                        "spotify_track_id": t["spotify_track_id"]} for t in p["top_tracks"][:5]],
        "top_albums": [{"name": a["name"], "count": a["plays"]} for a in p["top_albums"][:5]],
    }


# ---------------------------------------------------------------------------
# Play counts for things shown on the artist page (Spotify/Last.fm entities)
# ---------------------------------------------------------------------------

def artist_play_counts(conn, names):
    """{name: canonical plays} for a list of artist names (similar artists, etc.)."""
    names = [n for n in (names or []) if n]
    if not names:
        return {}
    by_key = {}
    for n in names:
        by_key.setdefault(artist_key(n), []).append(n)
    by_key.pop("", None)
    counts = {}
    ks = list(by_key)
    for i in range(0, len(ks), 400):
        chunk = ks[i:i + 400]
        q = ",".join("?" * len(chunk))
        for k, n in conn.execute(
                f"SELECT artist_key, COUNT(*) FROM plays WHERE artist_key IN ({q}) AND superseded_by IS NULL "
                "GROUP BY artist_key", chunk).fetchall():
            counts[k] = n
    return {n: counts.get(k, 0) for k, ns in by_key.items() for n in ns}


def song_play_counts(conn, entries):
    """Plays for each entry {artist, title, id?, artists?:[names]} -> list of ints (same order).

    A song counts as played when its song_key (under the page artist or its primary credited
    artist) has canonical plays, or when its Spotify track id appears on any play.
    """
    K = keys()
    cand = []
    wanted_keys, wanted_ids = set(), set()
    for e in entries or []:
        title = (e.get("title") or e.get("name") or "").strip()
        artists = [a for a in ([e.get("artist")] + list(e.get("artists_list") or [])) if a]
        sks = {K.song_key(K.artist_key(a), title) for a in artists if title}
        tid = (e.get("id") or "").strip() or None
        cand.append((sks, tid))
        wanted_keys |= sks
        if tid:
            wanted_ids.add(tid)
    counts = {}
    ks = list(wanted_keys)
    for i in range(0, len(ks), 400):
        chunk = ks[i:i + 400]
        q = ",".join("?" * len(chunk))
        for sk, n in conn.execute(
                f"SELECT song_key, COUNT(*) FROM plays WHERE song_key IN ({q}) AND superseded_by IS NULL "
                "GROUP BY song_key", chunk).fetchall():
            counts[sk] = n
    id_song = {}
    if wanted_ids:
        ids = list(wanted_ids)
        q = ",".join("?" * len(ids))
        for tid, sk in conn.execute(
                f"SELECT spotify_track_id, song_key FROM plays WHERE spotify_track_id IN ({q})", ids).fetchall():
            id_song.setdefault(tid, set()).add(sk)
        extra = {sk for s in id_song.values() for sk in s} - set(counts)
        if extra:
            ex = list(extra)
            q = ",".join("?" * len(ex))
            for sk, n in conn.execute(
                    f"SELECT song_key, COUNT(*) FROM plays WHERE song_key IN ({q}) AND superseded_by IS NULL "
                    "GROUP BY song_key", ex).fetchall():
                counts[sk] = n
    out = []
    for sks, tid in cand:
        best = max([counts.get(sk, 0) for sk in sks] + [0])
        if tid and tid in id_song:
            best = max([best] + [max(counts.get(sk, 0), 1) for sk in id_song[tid]])
        out.append(best)
    return out


def annotate_artist_info(conn, info, artist_name=None):
    """Add ``my_plays`` to an artist-info payload (top_tracks, related, recommendations) and set
    ``unheard_top_tracks`` (top tracks with no plays, by song key or Spotify id).

    Returns False (payload untouched) when the ledger is not ready.
    """
    if conn is None or not isinstance(info, dict) or not ledger_ready(conn):
        return False
    name = (artist_name or info.get("name") or "").strip()

    def credits(item):
        out = [a.get("name") for a in (item.get("artist_list") or [])[:1] if isinstance(a, dict) and a.get("name")]
        joined = (item.get("lastfm_artist") or item.get("artists") or "").strip()
        if joined:
            out += [joined, joined.split(",")[0].strip()]
        return [c for c in out if c]

    tops = [t for t in (info.get("top_tracks") or []) if isinstance(t, dict)]
    counts = song_play_counts(conn, [{"artist": name, "artists_list": credits(t), "title": t.get("name"),
                                      "id": t.get("id")} for t in tops])
    for t, n in zip(tops, counts):
        t["my_plays"] = n
    info["unheard_top_tracks"] = [t for t in tops if not t.get("my_plays")][:10]

    recs = [r for r in (info.get("recommendations") or []) if isinstance(r, dict)]
    rec_counts = song_play_counts(conn, [{"artist": (credits(r) or [""])[0], "artists_list": credits(r),
                                          "title": r.get("name"), "id": r.get("id")} for r in recs])
    for r, n in zip(recs, rec_counts):
        r["my_plays"] = n

    related = [r for r in (info.get("related") or []) if isinstance(r, dict)]
    rel_counts = artist_play_counts(conn, [r.get("name") for r in related])
    for r in related:
        r["my_plays"] = rel_counts.get(r.get("name"), 0)
    info["my_plays_available"] = True
    return True


def artist_heard(conn, name):
    """Heard sets for one artist (discography): song keys, Spotify ids and per-album-key detail."""
    K = keys()
    key = K.artist_key(name or "")
    songs, ids = set(), set()
    albums = {}
    if not key:
        return {"key": key, "song_keys": songs, "track_ids": ids, "albums": albums}
    for s_key, t_key, sp_id, al_key, ts in conn.execute(
            "SELECT song_key, track_key, spotify_track_id, album_key, ts_ms FROM plays "
            "WHERE artist_key=? AND superseded_by IS NULL", (key,)).fetchall():
        songs.add(s_key)
        if sp_id:
            ids.add(sp_id)
        if al_key:
            a = albums.get(al_key)
            if a is None:
                a = albums[al_key] = {"song_keys": set(), "track_keys": set(), "first": ts, "last": ts, "plays": 0}
            a["song_keys"].add(s_key)
            a["track_keys"].add(t_key)
            a["plays"] += 1
            a["first"] = min(a["first"], ts)
            a["last"] = max(a["last"], ts)
    return {"key": key, "song_keys": songs, "track_ids": ids, "albums": albums}


def track_ids_heard(conn, track_ids):
    """Subset of Spotify track ids that appear on any canonical play."""
    ids = [t for t in set(track_ids or []) if t]
    if not ids:
        return set()
    q = ",".join("?" * len(ids))
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT spotify_track_id FROM plays WHERE spotify_track_id IN ({q}) AND superseded_by IS NULL",
        ids).fetchall()}


# ---------------------------------------------------------------------------
# Artist index and library (history + local search)
# ---------------------------------------------------------------------------

ARTIST_SORTS = ("plays", "recent", "first", "name")
HIDDEN_FILTERS = ("include", "exclude", "only")


def list_artists(conn, q="", sort="plays", limit=50, offset=0, hidden_filter="include"):
    """One page of the artist index. hidden_filter: 'include' (default, every artist),
    'exclude' (music only, what the index shows by default) or 'only' (the hidden ones).
    ``total`` counts the filtered list; ``hidden_total`` counts all hidden artists (for the toggle)."""
    require_ledger(conn)
    totals = artist_totals(conn)
    hidden = hidden_keys(conn)
    items = list(totals["items"].values())
    hidden_total = sum(1 for a in items if a["key"] in hidden)
    if hidden_filter == "exclude":
        items = [a for a in items if a["key"] not in hidden]
    elif hidden_filter == "only":
        items = [a for a in items if a["key"] in hidden]
    fq = _fold(q)
    if fq:
        items = [a for a in items if fq in a["key"] or fq in a["fold"]]
    if sort == "recent":
        items.sort(key=lambda a: (-(a["last_ts"] or 0), a["key"]))
    elif sort == "first":
        items.sort(key=lambda a: (-(a["first_ts"] or 0), a["key"]))
    elif sort == "name":
        items.sort(key=lambda a: (a["fold"], a["key"]))
    else:
        items.sort(key=lambda a: (-a["plays"], a["fold"]))
    page = items[offset: offset + limit]
    out = []
    for a in page:
        is_hidden = a["key"] in hidden
        rank = None if is_hidden else _rank_info(totals, hidden, a["key"], a["plays"])[0]
        out.append({"key": a["key"], "name": a["name"], "plays": a["plays"], "rank": rank,
                    "first_ts": a["first_ts"], "last_ts": a["last_ts"], "hidden": is_hidden})
    # max_plays lets the index draw every page's bars on one scale.
    return {"total": len(items), "hidden_total": hidden_total,
            "max_plays": max((a["plays"] for a in items), default=0), "items": out}


def history_page(conn, before_ms=None, limit=50, artist=None, q=None):
    """Newest-first canonical plays. Rows sharing the boundary timestamp are kept on one page so
    ``before_ms`` paging never drops scrobbles that share a second."""
    require_ledger(conn)
    base = ["superseded_by IS NULL"]
    base_params = []
    if artist:
        base.append("artist_key = ?")
        base_params.append(artist_key(artist))
    if q:
        # Literal text search: '%' and '_' typed by the user are not wildcards.
        esc = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        base.append("(track_name LIKE ? ESCAPE '\\' OR artist_name LIKE ? ESCAPE '\\' OR album_name LIKE ? ESCAPE '\\')")
        base_params += [like, like, like]
    where, params = list(base), list(base_params)
    if before_ms is not None:
        where.append("ts_ms < ?")
        params.append(int(before_ms))
    cols = ("id, ts_ms, day_local, source, artist_name, artist_key, track_name, album_name, spotify_track_id, "
            "listened_s, is_backlog, ts_quality, song_key")
    sql = f"SELECT {cols} FROM plays WHERE {' AND '.join(where)} ORDER BY ts_ms DESC, id DESC LIMIT ?"
    rows = conn.execute(sql, params + [int(limit) + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    if more and rows:
        last_ts, last_id = rows[-1][1], rows[-1][0]
        tie_sql = (f"SELECT {cols} FROM plays WHERE {' AND '.join(base + ['ts_ms = ?', 'id < ?'])} "
                   "ORDER BY id DESC")
        rows += conn.execute(tie_sql, base_params + [last_ts, last_id]).fetchall()
        more = conn.execute(
            f"SELECT 1 FROM plays WHERE {' AND '.join(base + ['ts_ms < ?'])} LIMIT 1",
            base_params + [last_ts]).fetchone() is not None
    hidden = hidden_keys(conn)
    items = [{
        "id": r[0], "ts_ms": r[1], "day": r[2], "source": r[3], "artist": r[4], "artist_key": r[5],
        "track": r[6], "album": r[7], "spotify_track_id": r[8], "listened_s": r[9], "is_backlog": bool(r[10]),
        "bulk": r[11] == "bulk", "song_key": r[12], "hidden": r[5] in hidden,
    } for r in rows]
    return {"items": items, "next_before_ms": items[-1]["ts_ms"] if (more and items) else None}


def _build_library_index(conn):
    tracks = {}  # song_key -> [artist_key, Counter(names), plays, spotify id]
    albums = {}  # album_key -> [artist_key, Counter(names), plays]
    for a_key, s_key, t_name, sp_id, al_key, al_name in conn.execute(
            "SELECT artist_key, song_key, track_name, spotify_track_id, album_key, album_name "
            "FROM plays WHERE superseded_by IS NULL"):
        t = tracks.get(s_key)
        if t is None:
            t = tracks[s_key] = [a_key, Counter(), 0, None]
        t[1][t_name] += 1
        t[2] += 1
        if sp_id:
            t[3] = sp_id
        if al_key:
            a = albums.get(al_key)
            if a is None:
                a = albums[al_key] = [a_key, Counter(), 0]
            a[1][al_name] += 1
            a[2] += 1
    track_list = []
    for s_key, (a_key, names, plays, sp_id) in tracks.items():
        name = _pick_name(names)
        track_list.append((_fold(name), name, a_key, plays, sp_id, s_key))
    album_list = []
    for al_key, (a_key, names, plays) in albums.items():
        name = _pick_name(names)
        album_list.append((_fold(name), name, a_key, plays, al_key))
    track_list.sort(key=lambda x: -x[3])
    album_list.sort(key=lambda x: -x[3])
    return {"tracks": track_list, "albums": album_list}


def _match_rank(folded, fq):
    if folded == fq:
        return 0
    if folded.startswith(fq):
        return 1
    if f" {fq}" in f" {folded}":
        return 2
    return 3 if fq in folded else None


def search_local(conn, q, limit=8):
    require_ledger(conn)
    fq = _fold(q)
    if not fq:
        return {"artists": [], "tracks": [], "albums": []}
    totals = artist_totals(conn)
    idx = _cached(conn, "library", _build_library_index)
    names = totals["items"]

    arts = []
    for a in names.values():
        r = _match_rank(a["fold"], fq)
        if r is None and fq in a["key"]:
            r = 3
        if r is not None:
            arts.append((r, -a["plays"], a))
    arts.sort(key=lambda x: (x[0], x[1], x[2]["key"]))

    def artist_name(k):
        return (names.get(k) or {}).get("name") or k

    trs = []
    for folded, name, a_key, plays, sp_id, s_key in idx["tracks"]:
        r = _match_rank(folded, fq)
        if r is not None:
            trs.append((r, -plays, name, a_key, plays, sp_id, s_key))
    trs.sort(key=lambda x: (x[0], x[1], x[2]))
    als = []
    for folded, name, a_key, plays, al_key in idx["albums"]:
        r = _match_rank(folded, fq)
        if r is not None:
            als.append((r, -plays, name, a_key, plays, al_key))
    als.sort(key=lambda x: (x[0], x[1], x[2]))
    return {
        "artists": [{"name": a["name"], "key": a["key"], "plays": a["plays"]} for _, _, a in arts[:limit]],
        "tracks": [{"name": t[2], "artist": artist_name(t[3]), "artist_key": t[3], "plays": t[4],
                    "spotify_track_id": t[5], "song_key": t[6]} for t in trs[:limit]],
        "albums": [{"name": a[2], "artist": artist_name(a[3]), "artist_key": a[3], "plays": a[4],
                    "album_key": a[5]} for a in als[:limit]],
    }
