"""Offline stand-in for spotipy.Spotify, for development, tests and the offline demo.

Simulates a listener playing through their backlog playlist, backed by the ``tracks``
table and the live events of a music_explorer.db, so the real poll loop, clearing engine
and UI can run without touching the Spotify API. Every write (remove/add/like/playback)
only mutates this in-memory simulation.

Knobs (environment variables):
  FAKE_PL_SIZE         synthesize extra backlog tracks until the playlist has this many
  FAKE_TRACK_SECONDS   real seconds each simulated track "plays" before advancing (default 40)
"""
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


def _artist_id(name):
    return "fakeartist" + "".join(ch for ch in name.lower() if ch.isalnum())[:14]


class FakeSpotify:
    def __init__(self, db_path, playlist_id):
        self._lock = threading.RLock()
        self.playlist_id = playlist_id
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
        self.pl = list(self.tracks)
        rnd.shuffle(self.pl)
        self.liked = set(t["id"] for t in self.tracks[: max(1, len(self.tracks) // 5)])
        self.idx = 0
        self.started = time.time()
        self.paused_at = None
        self.snapshot = 1

    # ---------- object builders ----------
    def _track_obj(self, r, artist_names):
        artists = [{"id": _artist_id(n.strip()), "name": n.strip(), "uri": f"spotify:artist:{_artist_id(n.strip())}",
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
        return {"id": aid, "name": name, "genres": self.genres_by_artist.get(name, []), "images": img,
                "followers": {"total": 12345}, "popularity": 42, "uri": f"spotify:artist:{aid}",
                "external_urls": {"spotify": ""}, "type": "artist"}

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
        pls = [
            {"id": self.playlist_id, "name": BACKLOG_NAME, "tracks": {"total": len(self.pl)}, "owner": owner,
             "images": [], "collaborative": False, "public": False, "snapshot_id": str(self.snapshot)},
            {"id": OTHER_PLAYLIST_ID, "name": OTHER_PLAYLIST_NAME, "tracks": {"total": 0}, "owner": owner,
             "images": [], "collaborative": False, "public": False, "snapshot_id": "1"},
        ]
        return self._page(pls[offset: offset + limit], offset, limit, len(pls), "playlists")

    # ---------- playlists ----------
    def playlist(self, playlist_id, fields=None, **k):
        self._record("playlist")
        name = BACKLOG_NAME if playlist_id == self.playlist_id else OTHER_PLAYLIST_NAME
        total = len(self.pl) if playlist_id == self.playlist_id else 0
        return {"id": playlist_id, "name": name, "snapshot_id": str(self.snapshot),
                "tracks": {"total": total, "items": [], "next": None},
                "owner": {"id": "demo_user", "display_name": "Demo Listener"}, "images": [],
                "collaborative": False, "public": False, "description": ""}

    def playlist_items(self, playlist_id, fields=None, limit=100, offset=0, **k):
        self._record("playlist_items")
        src = self.pl if playlist_id == self.playlist_id else []
        items = [{"added_at": "2026-05-01T00:00:00Z", "track": t, "is_local": False} for t in src[offset: offset + limit]]
        return self._page(items, offset, limit, len(src), f"pl-{playlist_id}")

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
            return self.playlist_items(kind[3:], limit=limit, offset=offset)
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
        return {"artists": [self._artist_obj(a.split(":")[-1]) for a in artists]}

    def artist_top_tracks(self, artist_id, country="US", **k):
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
