"""Offline stand-in for spotipy.Spotify, for development, tests and the offline demo.

Simulates a listener playing through their backlog playlist, backed by the ``tracks``
table and the live events of a music_explorer.db, so the real poll loop, clearing engine
and UI can run without touching the Spotify API. Every write (remove/add/like/playback)
only mutates this in-memory simulation.

Knobs (environment variables):
  FAKE_PL_SIZE         synthesize extra backlog tracks until the playlist has this many
  FAKE_TRACK_SECONDS   real seconds each simulated track "plays" before advancing (default 40)
  FAKE_API_MODE        "legacy" (default): the Web API as apps created before 2026 see it.
                       "2026": a Development Mode app created after 2026-02-11 (see API_2026 below).

The 2026 mode emulates Spotify's restricted Web API for new Development Mode apps:
  * playlist objects carry ``items`` (not ``tracks``); playlist entries carry ``item`` (not ``track``);
  * ``fields=`` filters are applied, and a filter naming a field the object does not have is
    rejected with 400 (pessimistic: callers must survive a rejected filter);
  * removed endpoints (batch ``artists``, ``artist_top_tracks``, ``/browse/*``, ``/users/{id}*``,
    ``/markets``) raise SpotifyException 403;
  * ``search`` with limit > 10 raises 400 "Invalid limit";
  * removed fields are absent: track/artist ``popularity``, artist ``followers``, user
    ``country``/``email``/``product``/``explicit_content``/``followers``.
"""
import hashlib
import json
import os
import random
import sqlite3
import threading
import time

import spotipy

SIM_TRACK_SECONDS = int(os.environ.get("FAKE_TRACK_SECONDS", "40") or 40)
BACKLOG_NAME = "Discover Backlog (demo)"
OTHER_PLAYLIST_ID = "5bxGraveyardSandbox002"   # 22 chars, like a real playlist id
OTHER_PLAYLIST_NAME = "Graveyard (demo)"
API_LEGACY = "legacy"
API_2026 = "2026"
SEARCH_MAX_LIMIT_2026 = 10


def _artist_id(name):
    return "fakeartist" + "".join(ch for ch in name.lower() if ch.isalnum())[:14]


def _api_mode(value):
    raw = str(value if value is not None else os.environ.get("FAKE_API_MODE", "") or "").strip().lower()
    return API_2026 if raw == API_2026 else API_LEGACY


class _InvalidFields(ValueError):
    pass


def parse_fields(spec):
    """Spotify's ``fields`` filter as a tree {name: subtree | None}: ``a,b``, ``a.b``, ``a(b,c(d))``."""
    spec = str(spec or "")
    n = len(spec)
    pos = 0

    def merge(tree, name, sub):
        if name not in tree:
            tree[name] = sub
        elif tree[name] is None or sub is None:
            tree[name] = None  # the whole field wins over a partial selection
        else:
            for k, v in sub.items():
                merge(tree[name], k, v)

    def element():
        nonlocal pos
        start = pos
        while pos < n and spec[pos] not in ",.()":
            pos += 1
        name = spec[start:pos].strip()
        if not name:
            raise _InvalidFields(f"empty field name at {start}")
        sub = None
        if pos < n and spec[pos] == ".":
            pos += 1
            k, v = element()
            sub = {k: v}
        elif pos < n and spec[pos] == "(":
            pos += 1
            sub = group()
            if pos >= n or spec[pos] != ")":
                raise _InvalidFields("unbalanced parentheses")
            pos += 1
        return name, sub

    def group():
        nonlocal pos
        tree = {}
        while True:
            name, sub = element()
            merge(tree, name, sub)
            if pos < n and spec[pos] == ",":
                pos += 1
                continue
            return tree

    tree = group()
    if pos != n:
        raise _InvalidFields(f"unexpected {spec[pos]!r} at {pos}")
    return tree


def project_fields(obj, tree, strict=True, path=""):
    """Keep only the fields in ``tree``. With ``strict`` a requested field the object lacks is an
    error (lists are checked element by element; an empty list checks nothing)."""
    if isinstance(obj, list):
        return [project_fields(x, tree, strict, path) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for name, sub in tree.items():
        if name not in obj:
            if strict:
                raise _InvalidFields(f"unknown field {path}{name}")
            continue
        out[name] = obj[name] if sub is None else project_fields(obj[name], sub, strict, f"{path}{name}.")
    return out


def _forbidden(endpoint):
    return spotipy.SpotifyException(
        403, -1, f"fake: {endpoint} is not available to Development Mode apps created after 2026-02-11",
        reason="Forbidden", headers={})


class FakeSpotify:
    def __init__(self, db_path, playlist_id, api_mode=None):
        self._lock = threading.RLock()
        self.playlist_id = playlist_id
        self.api_mode = _api_mode(api_mode)
        self.modern = self.api_mode == API_2026
        self.calls = []
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        artists_by_track = {}
        genres_by_artist = {}
        for row in conn.execute(
            "select track_id, payload from events where source='live' and track_id is not null and payload like '%artists%'"
        ):
            try:
                p = json.loads(row["payload"] or "{}")
            except Exception:
                continue
            a = (p.get("artists") or "").strip()
            if a:
                artists_by_track.setdefault(row["track_id"], a)
                for name in [x.strip() for x in a.split(",") if x.strip()]:
                    if p.get("genres"):
                        genres_by_artist.setdefault(name, p["genres"][:5])
        self.genres_by_artist = genres_by_artist
        self.tracks = []
        for r in conn.execute("select * from tracks order by created_at"):
            names = artists_by_track.get(r["id"]) or "Unknown Artist"
            self.tracks.append(self._track_obj(r, names))
        conn.close()
        rnd = random.Random(7)
        want = int(os.environ.get("FAKE_PL_SIZE", "0") or 0)
        base = list(self.tracks)
        n = 0
        while want and len(self.tracks) < want and base:
            src = base[n % len(base)]
            n += 1
            tid = f"fk{n:020d}"
            self.tracks.append(dict(src, id=tid, uri=f"spotify:track:{tid}", name=f"{src['name']} (take {n})",
                                    external_urls={"spotify": f"https://open.spotify.com/track/{tid}"}))
        if self.modern:
            for t in self.tracks:   # removed for new apps (synthesized copies share no dicts with base)
                t.pop("popularity", None)
        self.pl = list(self.tracks)
        rnd.shuffle(self.pl)
        self.liked = set(t["id"] for t in self.tracks[: max(1, len(self.tracks) // 5)])
        self.idx = 0
        self.started = time.time()
        self.paused_at = None
        self.snapshot = 1

    # ---------- object builders ----------
    def _aid(self, name):
        """A stable fake artist id. Names sharing the 14-character slug prefix ("Dance Gavin Dance"
        and "Dance Gavin Dance Tribute Band") used to share one id; later names get a suffix."""
        ids = self.__dict__.setdefault("_ids_by_name", {})
        owners = self.__dict__.setdefault("_names_by_id", {})
        aid = ids.get(name)
        if aid is None:
            aid = _artist_id(name)
            if owners.get(aid, name) != name:
                aid += hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
            ids[name] = aid
            owners[aid] = name
        return aid

    def _track_obj(self, r, artist_names):
        artists = [{"id": self._aid(n.strip()), "name": n.strip(), "uri": f"spotify:artist:{self._aid(n.strip())}",
                    "external_urls": {"spotify": ""}} for n in artist_names.split(",") if n.strip()]
        img = r["image_url"] or ""
        return {
            "id": r["id"], "name": r["name"], "uri": f"spotify:track:{r['id']}",
            "duration_ms": r["duration_ms"] or 200000, "is_local": False, "explicit": False,
            "popularity": 40, "type": "track",
            "external_urls": {"spotify": r["spotify_url"] or f"https://open.spotify.com/track/{r['id']}"},
            "album": {"id": r["album_id"] or "fakealbum", "name": r["album"] or "Unknown Album",
                      "images": [{"url": img, "width": 640, "height": 640}] if img else [],
                      "release_date": "2020-01-01", "album_type": "album", "total_tracks": 10,
                      "artists": artists[:1], "external_urls": {"spotify": ""}},
            "artists": artists,
        }

    def _artist_obj(self, aid, name=None):
        if not name:
            for t in self.tracks:
                for a in t["artists"]:
                    if a["id"] == aid:
                        name = a["name"]
                        break
                if name:
                    break
        name = name or "Unknown Artist"
        img = next((t["album"]["images"] for t in self.tracks if any(a["id"] == aid for a in t["artists"])), [])
        obj = {"id": aid, "name": name, "genres": self.genres_by_artist.get(name, []), "images": img,
               "followers": {"total": 12345}, "popularity": 42, "uri": f"spotify:artist:{aid}",
               "external_urls": {"spotify": ""}, "type": "artist"}
        if self.modern:
            obj.pop("followers")
            obj.pop("popularity")
        return obj

    def _page(self, items, offset, limit, total, kind):
        nxt = f"sandbox:{kind}:{offset + limit}:{limit}" if offset + limit < total else None
        return {"items": items, "offset": offset, "limit": limit, "total": total, "next": nxt, "previous": None}

    def _record(self, name):
        with self._lock:
            self.calls.append((time.time(), name))
            self.calls = self.calls[-500:]

    # ---------- playback simulation ----------
    def _current(self):
        with self._lock:
            if not self.pl:
                return None, 0, False
            elapsed = (self.paused_at or time.time()) - self.started
            while elapsed >= SIM_TRACK_SECONDS:
                self.started += SIM_TRACK_SECONDS
                self.idx = (self.idx + 1) % len(self.pl)
                elapsed -= SIM_TRACK_SECONDS
            t = self.pl[self.idx % len(self.pl)]
            dur = t["duration_ms"] or 200000
            progress = min(dur - 1000, int(elapsed * 1000))
            return t, progress, self.paused_at is None

    def current_playback(self, *a, **k):
        self._record("current_playback")
        t, progress, playing = self._current()
        if not t:
            return None
        return {"item": t, "progress_ms": progress, "is_playing": playing, "currently_playing_type": "track",
                "shuffle_state": False, "repeat_state": "off", "timestamp": int(time.time() * 1000),
                "context": {"type": "playlist", "uri": f"spotify:playlist:{self.playlist_id}"},
                "device": {"id": "demo-device", "name": "DEMO", "type": "Computer", "volume_percent": 60,
                           "is_active": True}}

    current_user_playing_track = current_playback

    def devices(self, *a, **k):
        return {"devices": [{"id": "demo-device", "name": "DEMO", "type": "Computer", "is_active": True,
                             "volume_percent": 60}]}

    def queue(self, *a, **k):
        t, _, _ = self._current()
        with self._lock:
            upcoming = [self.pl[(self.idx + i) % len(self.pl)] for i in range(1, 21)] if self.pl else []
        return {"currently_playing": t, "queue": upcoming}

    def start_playback(self, *a, uris=None, context_uri=None, offset=None, **k):
        self._record("start_playback")
        with self._lock:
            if uris:
                tid = uris[0].split(":")[-1]
                for i, t in enumerate(self.pl):
                    if t["id"] == tid:
                        self.idx = i
                        break
                else:
                    extra = next((t for t in self.tracks if t["id"] == tid), None)
                    if extra:
                        self.pl.insert(self.idx + 1, extra)
                        self.idx += 1
            self.started = time.time()
            self.paused_at = None

    def pause_playback(self, *a, **k):
        with self._lock:
            if self.paused_at is None:
                self.paused_at = time.time()

    def next_track(self, *a, **k):
        with self._lock:
            self.idx = (self.idx + 1) % max(1, len(self.pl))
            self.started = time.time()
            self.paused_at = None

    def previous_track(self, *a, **k):
        with self._lock:
            self.idx = (self.idx - 1) % max(1, len(self.pl))
            self.started = time.time()
            self.paused_at = None

    def seek_track(self, position_ms, *a, **k):
        with self._lock:
            now = self.paused_at or time.time()
            self.started = now - position_ms / 1000.0

    def shuffle(self, state, *a, **k):
        return None

    # ---------- user / library ----------
    def current_user(self, *a, **k):
        if self.modern:
            return {"id": "demo_user", "display_name": "Demo Listener", "images": [], "type": "user",
                    "uri": "spotify:user:demo_user", "external_urls": {"spotify": ""}}
        return {"id": "demo_user", "display_name": "Demo Listener", "images": [], "country": "US",
                "product": "premium"}

    me = current_user

    def current_user_saved_tracks(self, limit=20, offset=0, **k):
        liked = [t for t in self.tracks if t["id"] in self.liked]
        items = [{"added_at": "2026-09-01T12:00:00Z", "track": t} for t in liked[offset: offset + limit]]
        return self._page(items, offset, limit, len(liked), "saved")

    def current_user_saved_tracks_contains(self, tracks=None, *a, **k):
        return [((x or "").split(":")[-1] in self.liked) for x in (tracks or [])]

    def current_user_saved_tracks_add(self, tracks=None, *a, **k):
        self._record("current_user_saved_tracks_add")
        for x in tracks or []:
            self.liked.add(x.split(":")[-1])

    def current_user_saved_tracks_delete(self, tracks=None, *a, **k):
        for x in tracks or []:
            self.liked.discard(x.split(":")[-1])

    def current_user_recently_played(self, limit=50, *a, **k):
        with self._lock:
            recent = [self.pl[(self.idx - i) % len(self.pl)] for i in range(1, min(limit, 30) + 1)] if self.pl else []
        now = time.time()
        items = [{"played_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 200 * (i + 1))), "track": t}
                 for i, t in enumerate(recent)]
        return {"items": items, "next": None, "cursors": None, "limit": limit}

    def current_user_top_tracks(self, limit=20, offset=0, time_range="medium_term", **k):
        return self._page(self.tracks[offset: offset + limit], offset, limit, len(self.tracks), "toptracks")

    def current_user_top_artists(self, limit=20, offset=0, time_range="medium_term", **k):
        seen, arts = set(), []
        for t in self.tracks:
            for a in t["artists"]:
                if a["id"] not in seen:
                    seen.add(a["id"])
                    arts.append(self._artist_obj(a["id"], a["name"]))
        return self._page(arts[offset: offset + limit], offset, limit, len(arts), "topartists")

    def current_user_playlists(self, limit=50, offset=0, **k):
        owner = {"id": "demo_user", "display_name": "Demo Listener"}
        key = "items" if self.modern else "tracks"   # 2026: the simplified playlist's {href,total} is "items"
        pls = [
            {"id": self.playlist_id, "name": BACKLOG_NAME, key: {"total": len(self.pl)}, "owner": owner,
             "images": [], "collaborative": False, "public": False, "snapshot_id": str(self.snapshot)},
            {"id": OTHER_PLAYLIST_ID, "name": OTHER_PLAYLIST_NAME, key: {"total": 0}, "owner": owner,
             "images": [], "collaborative": False, "public": False, "snapshot_id": "1"},
        ]
        return self._page(pls[offset: offset + limit], offset, limit, len(pls), "playlists")

    # ---------- playlists ----------
    def _apply_fields(self, obj, fields):
        """2026 mode applies fields= (strictly); legacy mode keeps ignoring it, as before."""
        if not self.modern or not fields:
            return obj
        try:
            return project_fields(obj, parse_fields(fields), strict=True)
        except _InvalidFields as e:
            raise spotipy.SpotifyException(400, -1, f"fake: Invalid fields filter ({e})",
                                           reason="Bad Request", headers={})

    def _entry(self, t):
        if self.modern:
            return {"added_at": "2026-05-01T00:00:00Z", "item": t, "is_local": False}
        return {"added_at": "2026-05-01T00:00:00Z", "track": t, "is_local": False}

    def playlist(self, playlist_id, fields=None, **k):
        self._record("playlist")
        name = BACKLOG_NAME if playlist_id == self.playlist_id else OTHER_PLAYLIST_NAME
        total = len(self.pl) if playlist_id == self.playlist_id else 0
        if self.modern:
            with self._lock:
                src = list(self.pl) if playlist_id == self.playlist_id else []
            first = self._page([self._entry(t) for t in src[:100]], 0, 100, len(src), f"pl-{playlist_id}")
            obj = {"id": playlist_id, "name": name, "snapshot_id": str(self.snapshot), "items": first,
                   "owner": {"id": "demo_user", "display_name": "Demo Listener"}, "images": [],
                   "collaborative": False, "public": False, "description": "", "type": "playlist",
                   "uri": f"spotify:playlist:{playlist_id}", "external_urls": {"spotify": ""}}
            return self._apply_fields(obj, fields)
        return {"id": playlist_id, "name": name, "snapshot_id": str(self.snapshot),
                "tracks": {"total": total, "items": [], "next": None},
                "owner": {"id": "demo_user", "display_name": "Demo Listener"}, "images": [],
                "collaborative": False, "public": False, "description": ""}

    def playlist_items(self, playlist_id, fields=None, limit=100, offset=0, **k):
        self._record("playlist_items")
        src = self.pl if playlist_id == self.playlist_id else []
        items = [self._entry(t) for t in src[offset: offset + limit]]
        # The next "URL" keeps the fields filter, like Spotify's does.
        kind = f"pl-{playlist_id}" + (f"|{fields}" if fields and self.modern else "")
        return self._apply_fields(self._page(items, offset, limit, len(src), kind), fields)

    playlist_tracks = playlist_items

    def playlist_remove_all_occurrences_of_items(self, playlist_id, items, snapshot_id=None):
        self._record("playlist_remove_all_occurrences_of_items")
        ids = {x.split(":")[-1] for x in items}
        with self._lock:
            if playlist_id == self.playlist_id:
                self.pl = [t for t in self.pl if t["id"] not in ids]
                if self.pl:
                    self.idx = min(self.idx, len(self.pl) - 1)
            self.snapshot += 1
        return {"snapshot_id": str(self.snapshot)}

    def playlist_add_items(self, playlist_id, items, position=None):
        self._record("playlist_add_items")
        with self._lock:
            if playlist_id == self.playlist_id:
                for x in items:
                    tid = x.split(":")[-1]
                    t = next((t for t in self.tracks if t["id"] == tid), None)
                    if t:
                        if position is None:
                            self.pl.append(t)
                        else:
                            self.pl.insert(max(0, min(int(position), len(self.pl))), t)
            self.snapshot += 1
        return {"snapshot_id": str(self.snapshot)}

    def next(self, page):
        nxt = (page or {}).get("next")
        if not nxt or not str(nxt).startswith("sandbox:"):
            return None
        _, kind, offset, limit = str(nxt).split(":", 3)
        offset, limit = int(offset), int(limit)
        if kind.startswith("pl-"):
            pid, _, fields = kind[3:].partition("|")
            return self.playlist_items(pid, fields=fields or None, limit=limit, offset=offset)
        if kind == "saved":
            return self.current_user_saved_tracks(limit=limit, offset=offset)
        if kind == "toptracks":
            return self.current_user_top_tracks(limit=limit, offset=offset)
        if kind == "topartists":
            return self.current_user_top_artists(limit=limit, offset=offset)
        if kind == "playlists":
            return self.current_user_playlists(limit=limit, offset=offset)
        return None

    # ---------- catalog ----------
    def artist(self, artist_id, **k):
        return self._artist_obj(artist_id)

    def artists(self, artists, **k):
        self._record("artists")
        if self.modern:
            raise _forbidden("GET /artists (several artists)")
        return {"artists": [self._artist_obj(a.split(":")[-1]) for a in artists]}

    def artist_top_tracks(self, artist_id, country="US", **k):
        self._record("artist_top_tracks")
        if self.modern:
            raise _forbidden("GET /artists/{id}/top-tracks")
        return {"tracks": [t for t in self.tracks if any(a["id"] == artist_id for a in t["artists"])][:10]}

    def artist_albums(self, artist_id, album_type=None, include_groups=None, country=None, limit=20, offset=0, **k):
        albums, seen = [], set()
        for t in self.tracks:
            if any(a["id"] == artist_id for a in t["artists"]) and t["album"]["id"] not in seen:
                seen.add(t["album"]["id"])
                albums.append(dict(t["album"], album_group="album"))
        return self._page(albums[offset: offset + limit], offset, limit, len(albums), f"albums-{artist_id}")

    def album(self, album_id, **k):
        ts = [t for t in self.tracks if t["album"]["id"] == album_id]
        base = ts[0]["album"] if ts else {"id": album_id, "name": "Unknown Album", "images": [], "release_date": "2020"}
        return dict(base, tracks={"items": [dict(t, track_number=i + 1) for i, t in enumerate(ts)], "total": len(ts),
                                  "next": None})

    def album_tracks(self, album_id, limit=50, offset=0, **k):
        a = self.album(album_id)
        return self._page(a["tracks"]["items"][offset: offset + limit], offset, limit, a["tracks"]["total"], "x")

    def search(self, q="", limit=10, offset=0, type="track", market=None, **k):
        self._record("search")
        if self.modern and int(limit or 0) > SEARCH_MAX_LIMIT_2026:
            raise spotipy.SpotifyException(400, -1, "fake: Invalid limit", reason="Bad Request", headers={})
        ql = (q or "").lower()
        words = [w for w in ql.replace("artist:", " ").replace("track:", " ").replace('"', " ").split() if w]
        match = [t for t in self.tracks
                 if all(w in (t["name"] + " " + " ".join(a["name"] for a in t["artists"])).lower() for w in words)]
        out = {}
        if "track" in type:
            out["tracks"] = self._page(match[:limit], 0, limit, len(match), "x")
        if "artist" in type:
            seen, arts = set(), []
            for t in match:
                for a in t["artists"]:
                    if a["id"] not in seen:
                        seen.add(a["id"])
                        arts.append(self._artist_obj(a["id"], a["name"]))
            out["artists"] = self._page(arts[:limit], 0, limit, len(arts), "x")
        return out

    # Endpoints Spotify no longer gives new developer apps: fail the way the real API does.
    def _gone(self, *a, **k):
        raise spotipy.SpotifyException(404, -1, "fake: endpoint not available", headers={})

    artist_related_artists = _gone
    recommendations = _gone
    audio_features = _gone
    _get = _gone

    # Removed for Development Mode apps created after 2026-02-11 (403 in 2026 mode). The app
    # does not call these; they exist so a regression that starts to would fail loudly.
    def _removed_2026(endpoint):
        def call(self, *a, **k):
            self._record(endpoint)
            if self.modern:
                raise _forbidden(endpoint)
            raise spotipy.SpotifyException(404, -1, f"fake: {endpoint} is not simulated", headers={})
        return call

    new_releases = _removed_2026("GET /browse/new-releases")
    featured_playlists = _removed_2026("GET /browse/featured-playlists")
    categories = _removed_2026("GET /browse/categories")
    category_playlists = _removed_2026("GET /browse/categories/{id}/playlists")
    user = _removed_2026("GET /users/{id}")
    user_playlists = _removed_2026("GET /users/{id}/playlists")
    user_playlist_create = _removed_2026("POST /users/{id}/playlists")
    available_markets = _removed_2026("GET /markets")
    del _removed_2026
