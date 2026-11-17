"""Backlog insights: how the "to listen" playlist grows and shrinks, and how deep listens go.

Pure functions over a sqlite3 connection plus the backlog snapshot list the app keeps in
``state["backlog_history"]`` ([{day, ts_ms, total, playlist_id?}], one entry per local day).

Everything is *measured*, never assumed:
- cleared = distinct tracks removed from the backlog (TRACK_REMOVED_FROM_QUEUE), minus removals
  that were undone (a later TRACK_RESTORED for the same track). The raw events over-count: a
  poll loop once wrote 210 removal events for 17 tracks, so we always count distinct tracks.
- inflow = change in backlog size + tracks cleared over the same days (what must have come in).
- listen depth = the longest SONG_PLAYED ``listened_s`` of the track within an hour of its clear.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta

LISTEN_MATCH_MS = 3_600_000
SKIP_S, DEEP_S = 30.0, 120.0
MIN_GENRE_CLEARED = 3
MIN_PACE_DAYS = 7
TARGET_WEEKS = 12
UNDO_WINDOW_MS = 35 * 60 * 1000   # an undo is only offered for 30 min after a clear


def _rows(conn, sql, params=()):
    cur = conn.cursor()
    cur.row_factory = None
    try:
        return cur.execute(sql, params).fetchall()
    finally:
        cur.close()


def _table_exists(conn, name):
    return bool(_rows(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _loads(payload):
    if not payload:
        return {}
    try:
        v = json.loads(payload)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _local_day(ms):
    return datetime.fromtimestamp(ms / 1000).date()


def _midnight_ms(d):
    return int(datetime(d.year, d.month, d.day).timestamp() * 1000)


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def _round1(x):
    return None if x is None else round(x, 1)


def snapshots(backlog_history, playlist_id=None, current_total=None, today=None):
    """{date: total}, last value per local day, for the active playlist (entries without a
    playlist_id predate multi-playlist tracking and are kept)."""
    out = {}
    items = [h for h in (backlog_history or []) if isinstance(h, dict)]
    items.sort(key=lambda h: (str(h.get("day") or ""), int(h.get("ts_ms") or 0)))
    for h in items:
        pid = h.get("playlist_id")
        if playlist_id and pid and pid != playlist_id:
            continue
        try:
            d = date.fromisoformat(str(h.get("day")))
            total = int(h.get("total"))
        except (TypeError, ValueError):
            continue
        if total < 0:
            continue
        out[d] = total
    if current_total is not None and today is not None:
        out[today] = int(current_total)
    return dict(sorted(out.items()))


def effective_clears(conn, playlist_id=None):
    """Removals that were not undone, oldest first: [(track_id, ts_ms, payload dict)].

    A TRACK_RESTORED cancels every removal of that track logged in the undo window before it
    (one clear is sometimes logged several times), but not an older, separate clear.
    """
    rows = _rows(conn, """SELECT event_type, track_id, ts_ms, payload FROM events
        WHERE event_type IN ('TRACK_REMOVED_FROM_QUEUE', 'TRACK_RESTORED') AND track_id IS NOT NULL
        ORDER BY ts_ms, id""")
    pending = {}   # track_id -> list of (ts, payload) not yet undone
    out = []
    for etype, tid, ts, payload in rows:
        p = _loads(payload)
        pid = p.get("playlist_id")
        if playlist_id and pid and pid != playlist_id:
            continue
        if etype == "TRACK_RESTORED":
            stack = pending.get(tid)
            if stack:
                pending[tid] = [(t, pl) for t, pl in stack if t < ts - UNDO_WINDOW_MS]
            continue
        pending.setdefault(tid, []).append((ts, p))
    for tid, stack in pending.items():
        for ts, p in stack:
            out.append((tid, ts, p))
    out.sort(key=lambda t: t[1])
    return out


def _cleared_between(clears, start_d, end_ms):
    """Distinct tracks cleared after the end of day start_d, up to end_ms."""
    lo = _midnight_ms(start_d + timedelta(days=1))
    return len({tid for tid, ts, _ in clears if lo <= ts <= end_ms})


def _span(snaps, clears, start_d, today, now_ms, end_total):
    """Size-based figures from the recorded size at the end of start_d to now."""
    start_total = snaps[start_d]
    cleared = _cleared_between(clears, start_d, now_ms)
    net = end_total - start_total
    return {
        "start_day": start_d.isoformat(),
        "days": (today - start_d).days,
        "start_total": start_total,
        "end_total": end_total,
        "cleared": cleared,
        "net": net,
        # tracks removed outside the app (in Spotify) shrink the backlog without a clear event,
        # which would make this negative; it can't be, so it is floored at 0
        "inflow": max(0, net + cleared),
    }


def _measure_start(snaps, start_d, today):
    """The recorded day to measure a window from when start_d itself has no recorded size.

    Prefer the earliest recorded day inside the window (the figure stays within the period the
    window promises, just shorter); only when the app recorded nothing inside it, fall back to
    the latest recorded day before it (a longer span that the UI must label with its own date).
    """
    if start_d in snaps:
        return start_d
    inside = [d for d in snaps if start_d < d < today]
    if inside:
        return inside[0]
    before = [d for d in snaps if d < start_d]
    return before[-1] if before else None


def _window(snaps, clears, start_d, today, now_ms, end_total):
    """The window from the end of start_d to now.

    ``cleared`` always covers exactly that window (clears are timestamped). ``inflow`` and ``net``
    need the backlog size at the window's start, which only exists when the app recorded it that
    day; otherwise they are None rather than quietly covering some other span. ``measured`` then
    holds the closest span that can be measured, with its own start_day ("since Aug 8"), so a
    48-day figure is never presented as "this week".
    """
    if not snaps or end_total is None:
        return None
    exact = start_d in snaps
    m_start = _measure_start(snaps, start_d, today)
    measured = _span(snaps, clears, m_start, today, now_ms, end_total) if m_start is not None else None
    whole = measured if exact else None
    return {
        "start_day": start_d.isoformat(),
        "requested_start_day": start_d.isoformat(),   # kept for older callers; same as start_day
        "days": (today - start_d).days,
        # True when the backlog size was recorded at the window start, so inflow/net are known
        "exact": exact,
        "start_total": whole["start_total"] if whole else None,
        "end_total": end_total,
        "cleared": _cleared_between(clears, start_d, now_ms),
        "net": whole["net"] if whole else None,
        "inflow": whole["inflow"] if whole else None,
        "measured": measured,
    }


def _weekly(snaps, clears, today, now_ms, max_weeks=52):
    """Calendar weeks (Mon-Sun) from the first recorded size to this week.

    cleared is exact for every week. added needs a recorded size at both ends: the week is
    measured from the size at the end of the previous Sunday (or, failing that, the first size
    recorded inside the week) to the last size recorded inside it; weeks the app never ran
    have added=None ("not measured"), and added_days says how much of the week was measured.
    """
    if not snaps:
        return []
    first = next(iter(snaps))
    this_monday = today - timedelta(days=today.weekday())
    monday = max(first - timedelta(days=first.weekday()), this_monday - timedelta(weeks=max_weeks - 1))
    by_week = {}
    for tid, ts, _ in clears:
        d = _local_day(ts)
        by_week.setdefault(d - timedelta(days=d.weekday()), set()).add(tid)
    days = list(snaps)
    out = []
    while monday <= this_monday:
        sunday = monday + timedelta(days=6)
        end_d = min(sunday, today)
        inside = [d for d in days if monday <= d <= end_d]
        prev_sunday = monday - timedelta(days=1)
        base = prev_sunday if prev_sunday in snaps else (inside[0] if inside else None)
        last = inside[-1] if inside else None
        added, added_days = None, 0
        if base is not None and last is not None and last > base:
            hi = now_ms if last == today else _midnight_ms(last + timedelta(days=1)) - 1
            n = _cleared_between(clears, base, hi)
            added = max(0, snaps[last] - snaps[base] + n)
            added_days = (last - base).days
        out.append({
            "week_start": monday.isoformat(),
            "week_end": sunday.isoformat(),
            "days": (end_d - monday).days + 1,
            "current": monday == this_monday,
            "cleared": len(by_week.get(monday, ())),
            "added": added,
            "added_days": added_days,
            "end_total": snaps[last] if last is not None else None,
        })
        monday += timedelta(weeks=1)
    return out


def _median(values):
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _bucket(listened):
    if listened is None:
        return "unknown"
    if listened < SKIP_S:
        return "skip"
    if listened < DEEP_S:
        return "partial"
    return "deep"


def backlog_insights(conn, backlog_history, current_total=None, playlist_id=None, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    today = _local_day(now_ms)
    snaps = snapshots(backlog_history, playlist_id, current_total, today)
    clears = effective_clears(conn, playlist_id)
    end_total = current_total if current_total is not None else (list(snaps.values())[-1] if snaps else None)

    trajectory = [{"day": d.isoformat(), "total": t} for d, t in snaps.items()]
    first_day = next(iter(snaps)) if snaps else None
    windows = {
        "last_7": _window(snaps, clears, today - timedelta(days=7), today, now_ms, end_total),
        "last_30": _window(snaps, clears, today - timedelta(days=30), today, now_ms, end_total),
        "since_start": (_window(snaps, clears, first_day, today, now_ms, end_total) if first_day else None),
    }
    weekly = _weekly(snaps, clears, today, now_ms) if end_total is not None else []

    # pace over everything measured so far (the app is used in bursts, so short windows swing wildly)
    pace = {"basis": "since_start", "basis_days": None, "cleared_per_week": None, "inflow_per_week": None,
            "net_per_week": None, "weeks_to_empty": None, "empty_by_day": None, "needed_per_day_12w": None,
            "growing": None, "best_week": None, "at_best_week_pace": None}
    ss = windows["since_start"]
    if ss and ss["days"] >= MIN_PACE_DAYS:
        wk = 7.0 / ss["days"]
        cleared_pw, inflow_pw = ss["cleared"] * wk, ss["inflow"] * wk
        pace.update({
            "basis_days": ss["days"],
            "cleared_per_week": _round1(cleared_pw),
            "inflow_per_week": _round1(inflow_pw),
            "net_per_week": _round1(inflow_pw - cleared_pw),
            "growing": inflow_pw >= cleared_pw,
        })
        if end_total is not None:
            if end_total == 0:
                pace.update(weeks_to_empty=0.0, empty_by_day=today.isoformat())
            elif cleared_pw > inflow_pw:
                weeks = end_total / (cleared_pw - inflow_pw)
                pace.update(weeks_to_empty=_round1(weeks),
                            empty_by_day=(today + timedelta(days=math.ceil(weeks * 7))).isoformat())
            pace["needed_per_day_12w"] = _round1((end_total / TARGET_WEEKS + inflow_pw) / 7.0)
    # best calendar week (Mon-Sun) by distinct tracks cleared
    weeks = {}
    for tid, ts, _ in clears:
        d = _local_day(ts)
        monday = d - timedelta(days=d.weekday())
        weeks.setdefault(monday, set()).add(tid)
    if weeks:
        monday, tids = max(weeks.items(), key=lambda kv: (len(kv[1]), kv[0]))
        pace["best_week"] = {"start": monday.isoformat(), "cleared": len(tids)}
        if pace["inflow_per_week"] is not None and end_total:
            best_pw = float(len(tids))
            inflow_pw = pace["inflow_per_week"]
            if best_pw > inflow_pw:
                weeks_n = end_total / (best_pw - inflow_pw)
                pace["at_best_week_pace"] = {
                    "weeks_to_empty": _round1(weeks_n),
                    "empty_by_day": (today + timedelta(days=math.ceil(weeks_n * 7))).isoformat()}

    # listen depth of each distinct cleared track (its first clear that wasn't undone)
    first_clear = {}
    for tid, ts, p in clears:
        if tid not in first_clear:
            first_clear[tid] = (ts, p)
    listens = {}
    if first_clear:
        lo = min(ts for ts, _ in first_clear.values()) - LISTEN_MATCH_MS
        for tid, ts, payload in _rows(conn, """SELECT track_id, ts_ms, payload FROM events
                WHERE event_type='SONG_PLAYED' AND track_id IS NOT NULL AND ts_ms >= ?""", (lo,)):
            fc = first_clear.get(tid)
            if fc is None or abs(ts - fc[0]) > LISTEN_MATCH_MS:
                continue
            p = _loads(payload)
            try:
                ls = float(p.get("listened_s"))
            except (TypeError, ValueError):
                continue
            if ls > listens.get(tid, (-1.0, None))[0]:
                listens[tid] = (ls, p)
    funnel = {"cleared_distinct": len(first_clear), "deep": 0, "partial": 0, "skip": 0, "unknown": 0}
    genres = {}
    reasons = {}
    known_ls = []
    for tid, (ts, p) in first_clear.items():
        ls = listens.get(tid, (None, None))[0]
        b = _bucket(ls)
        funnel[b] += 1
        if ls is not None:
            known_ls.append(ls)
        reason = p.get("reason") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
        g = p.get("genres") or (listens.get(tid, (None, {}))[1] or {}).get("genres") or []
        genre = g[0] if isinstance(g, list) and g else None
        if genre:
            rec = genres.setdefault(genre, {"cleared": 0, "deep": 0, "known": 0})
            rec["cleared"] += 1
            if ls is not None:
                rec["known"] += 1
                if b == "deep":
                    rec["deep"] += 1
    known_n = len(known_ls)
    funnel.update({
        "known": known_n,
        "deep_pct": _pct(funnel["deep"], known_n),
        "partial_pct": _pct(funnel["partial"], known_n),
        "skip_pct": _pct(funnel["skip"], known_n),
        "median_listen_s": _round1(_median(known_ls)),
        "by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
    })
    by_genre = [{"genre": g, "cleared": r["cleared"], "deep": r["deep"], "known": r["known"],
                 "deep_pct": _pct(r["deep"], r["known"])}
                for g, r in genres.items() if r["cleared"] >= MIN_GENRE_CLEARED]
    by_genre.sort(key=lambda r: (-r["cleared"], r["genre"]))

    # kept: only knowable once Liked Songs ids are stored (saved_tracks); until then, app likes
    kept = {"known": False, "kept": None, "pct": None, "source": None}
    if first_clear:
        ids = list(first_clear)
        # an empty saved_tracks table means Liked Songs were never synced, not that nothing was kept
        if _table_exists(conn, "saved_tracks") and _rows(conn, "SELECT 1 FROM saved_tracks LIMIT 1"):
            n = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                n += _rows(conn, f"SELECT COUNT(*) FROM saved_tracks WHERE track_id IN ({','.join('?' * len(chunk))})",
                           tuple(chunk))[0][0]
            kept = {"known": True, "kept": n, "pct": _pct(n, len(ids)), "source": "liked_songs"}
        else:
            n = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                n += _rows(conn, f"""SELECT COUNT(DISTINCT track_id) FROM events WHERE event_type='TRACK_LIKED'
                    AND track_id IN ({','.join('?' * len(chunk))})""", tuple(chunk))[0][0]
            kept = {"known": False, "kept": n, "pct": None, "source": "app_likes"}

    return {
        "trajectory": trajectory,
        "current_total": end_total,
        "windows": windows,
        "weekly": weekly,
        "pace": pace,
        "funnel": funnel,
        "by_genre": by_genre,
        "kept": kept,
        "definitions": {
            "trajectory": "Backlog size at the end of each day the app was running (days it was off are skipped).",
            "cleared": ("Different tracks cleared from the backlog: automatically after the listen threshold, "
                        "or with Clear now, Like & clear, or catch-up. Clears you undid don't count."),
            "inflow": ("Tracks that came into the backlog: the change in its size plus the tracks cleared over the "
                       "same days. Tracks removed directly in Spotify make this an undercount."),
            "net": "Change in backlog size over the window (positive means it grew).",
            # Shown in the UI's info tooltips, so plain words only (the field-level rules are in
            # _window's docstring).
            "windows": ("Cleared counts every track removed from your backlog in these days. Added and the "
                        "change in size need the backlog size from the day before; if the app wasn't running "
                        "that day, they show the closest span that can be measured instead, with its own dates."),
            "weekly": ("Tracks cleared and added in each calendar week (Monday to Sunday). Added is only known for "
                       "weeks the app recorded the backlog size; weeks it wasn't running show as not measured."),
            "pace": ("Weekly rates over everything recorded since the first backlog snapshot. The app is used in "
                     "bursts, so this long view is steadier than the last week."),
            "weeks_to_empty": ("Weeks until the backlog is empty if you keep clearing at this pace and tracks keep "
                               "arriving at this rate. Empty when the backlog grows faster than you clear it."),
            "needed_per_day_12w": f"Tracks you'd need to clear per day to empty the backlog in {TARGET_WEEKS} weeks.",
            "best_week": "Your best calendar week (Monday to Sunday) by tracks cleared.",
            "at_best_week_pace": "When the backlog would be empty if every week matched your best week.",
            "funnel": ("How long you listened to each cleared track (its longest play within an hour of clearing): "
                       f"skip under {int(SKIP_S)} s, partial {int(SKIP_S)} s to {int(DEEP_S // 60)} min, deep "
                       f"{int(DEEP_S // 60)} min or more; unknown when the app has no play for it."),
            "median_listen_s": "The middle listening time of cleared tracks with a known play.",
            "by_genre": (f"Deep-listen share by the track's first listed genre (genres with at least "
                         f"{MIN_GENRE_CLEARED} cleared tracks)."),
            "kept": ("Cleared tracks you saved to Liked Songs. Until the app stores your Liked Songs, only likes "
                     "made in this app are counted, so no percentage is shown."),
        },
    }
