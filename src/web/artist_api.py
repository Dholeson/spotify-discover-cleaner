"""Artist endpoints for the v2 UI: personal stats, the artist index, hide-from-stats and lazy
Spotify resolution for play buttons.

All personal numbers come from the canonical plays ledger via src.services.artist_stats.
"""

import re
import time

from flask import Blueprint, jsonify, request

from src.web import api_error

bp = Blueprint("artist_api", __name__)

_MAX_NAME = 300


def _core():
    import app as core  # lazy: app.py is the live module (see src/web/__init__.py)
    return core


_WARM_INTERVAL_S = 45


def _warm_worker():
    """Keep the whole-ledger aggregates (artist ranks, local search index) current so artist
    pages and search never pay the rebuild on the request path. Never dies."""
    from src.services import artist_stats as AS

    time.sleep(3)
    warmed = False
    while True:
        try:
            core = _core()
            if getattr(core, "_SQLITE_ENABLED", False):
                conn = core._db.get_conn()
                if AS.ledger_ready(conn):
                    AS.warm_caches(conn)
                    warmed = True
        except Exception as e:
            try:
                _core()._log_app_error(e, "artist_stats_warm")
            except Exception:
                pass
        # Poll quickly until the ledger's first build lands, then settle into the slow cadence.
        time.sleep(_WARM_INTERVAL_S if warmed else 2)


def init(core):
    register = getattr(core, "register_background_worker", None)
    if callable(register):
        register("artist-stats-warm", _warm_worker)


def _ledger_conn():
    """(conn, error_response). Error when SQLite is off or the plays ledger is not built yet."""
    from src.services import artist_stats as AS

    core = _core()
    if not getattr(core, "_SQLITE_ENABLED", False):
        return None, api_error("history_unavailable", "Listening history storage is not available.", 503)
    conn = core._db.get_conn()
    if not AS.ledger_ready(conn):
        return None, api_error("history_not_ready",
                               "Your listening history is still being prepared. Try again in a minute.", 503)
    return conn, None


def _clean_name(name):
    name = (name or "").strip()
    if not name:
        return None, api_error("invalid_name", "Artist name is required.", 400, field="name")
    if len(name) > _MAX_NAME:
        return None, api_error("invalid_name", "Artist name is too long.", 400, field="name")
    return name, None


def _int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, api_error("invalid_" + name, f"'{name}' must be a whole number.", 400, field=name)
    if val < lo or val > hi:
        return None, api_error("invalid_" + name, f"'{name}' must be between {lo} and {hi}.", 400, field=name)
    return val, None


@bp.route("/api/v2/artist/<path:name>/stats")
def artist_stats(name):
    from src.services import artist_stats as AS

    name, err = _clean_name(name)
    if err:
        return err
    conn, err = _ledger_conn()
    if err:
        return err
    started = time.perf_counter()
    try:
        profile = AS.artist_profile(conn, name)
    except AS.LedgerNotReady:
        return api_error("history_not_ready", "Your listening history is still being prepared.", 503)
    except Exception as e:
        _core()._log_app_error(e, "api_v2_artist_stats")
        return api_error("artist_stats_failed", "Could not compute this artist's stats.", 500)
    out = AS.public_profile(profile)
    _attach_top_art(conn, out)
    out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return jsonify(out)


def _attach_top_art(conn, out):
    """Cover art for the top tracks and albums, from art the app already stored (like the
    Insights top lists): no Spotify call. Rows are copied, so the cached profile stays as is."""
    from src.services.reports import local_art
    tracks = out.get("top_tracks") or []
    albums = out.get("top_albums") or []
    songs, album_art = local_art(conn, [t.get("song_key") for t in tracks], [a.get("album_key") for a in albums])
    out["top_tracks"] = [dict(t, image=songs.get(t.get("song_key"))) for t in tracks]
    out["top_albums"] = [dict(a, image=album_art.get(a.get("album_key"))) for a in albums]


@bp.route("/api/v2/artists")
def artists_index():
    from src.services import artist_stats as AS

    q = (request.args.get("q") or "").strip()
    if len(q) > 200:
        return api_error("invalid_q", "Search text is too long.", 400, field="q")
    sort = (request.args.get("sort") or "plays").strip().lower()
    if sort not in AS.ARTIST_SORTS:
        return api_error("invalid_sort", "sort must be one of: " + ", ".join(AS.ARTIST_SORTS) + ".", 400, field="sort")
    hidden = (request.args.get("hidden") or "include").strip().lower()
    if hidden not in AS.HIDDEN_FILTERS:
        return api_error("invalid_hidden", "hidden must be one of: " + ", ".join(AS.HIDDEN_FILTERS) + ".", 400,
                         field="hidden")
    limit, err = _int_arg("limit", 50, 1, 500)
    if err:
        return err
    offset, err = _int_arg("offset", 0, 0, 10_000_000)
    if err:
        return err
    conn, err = _ledger_conn()
    if err:
        return err
    try:
        return jsonify(AS.list_artists(conn, q=q, sort=sort, limit=limit, offset=offset, hidden_filter=hidden))
    except AS.LedgerNotReady:
        return api_error("history_not_ready", "Your listening history is still being prepared.", 503)
    except Exception as e:
        _core()._log_app_error(e, "api_v2_artists")
        return api_error("artists_failed", "Could not list your artists.", 500)


@bp.route("/api/v2/artist/<path:name>/hide", methods=["POST"])
def artist_hide(name):
    from src.services import artist_stats as AS

    name, err = _clean_name(name)
    if err:
        return err
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("hidden"), bool):
        return api_error("invalid_hidden", "Send {\"hidden\": true} or {\"hidden\": false}.", 400, field="hidden")
    conn, err = _ledger_conn()
    if err:
        return err
    try:
        hidden = AS.set_hidden(conn, name, body["hidden"])
    except AS.LedgerNotReady:
        return api_error("history_not_ready", "Your listening history is still being prepared.", 503)
    except Exception as e:
        _core()._log_app_error(e, "api_v2_artist_hide")
        return api_error("hide_failed", "Could not update this artist.", 500)
    return jsonify({"hidden": hidden, "key": AS.artist_key(name)})


# ---------------------------------------------------------------------------
# Lazy Spotify resolution (play buttons for Last.fm-only entries)
# ---------------------------------------------------------------------------

_TRACK_NOISE = re.compile(r"\s*[-(\[][^)\]]*?(remaster|remastered|version|edit|mix|live|stereo|mono)[^)\]]*[)\]]?",
                          re.IGNORECASE)


class _RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = int(retry_after or 30)


def _search(sp, **kwargs):
    """sp.search through the guard, turning local budget blocks and 429s into _RateLimited."""
    import spotipy

    core = _core()
    if core._sp_search_rate_limited():
        raise _RateLimited(core._sp_search_backoff_remaining_s())
    try:
        return sp.search(**kwargs) or {}
    except core.SpotifyGuardBlocked as e:  # local budget/cooldown: not a real Spotify 429
        raise _RateLimited(getattr(e, "retry_after", None) or 30)
    except spotipy.SpotifyException as e:
        if getattr(e, "http_status", None) == 429:
            try:
                core._note_sp_search_rate_limit(e)
            except Exception:
                pass
            raise _RateLimited(core._sp_search_backoff_remaining_s() or 30)
        raise


def _images(images):
    images = images or []
    return (images[-1].get("url") if images else None), (images[0].get("url") if images else None)


def _resolve_artist(sp, name):
    from src.services import artist_stats as AS

    res = _search(sp, q=f'artist:"{name}"', type="artist", limit=5)
    items = [a for a in ((res.get("artists") or {}).get("items") or []) if a and a.get("id")]
    if not items:
        return None
    want = AS.artist_key(name)
    best = next((a for a in items if AS.artist_key(a.get("name")) == want), items[0])
    small, large = _images(best.get("images"))
    return {"id": best["id"], "name": best.get("name"), "image": small, "image_large": large,
            "uri": best.get("uri") or f"spotify:artist:{best['id']}",
            "url": (best.get("external_urls") or {}).get("spotify"), "exact": AS.artist_key(best.get("name")) == want}


def _resolve_track(sp, artist, track):
    from src.services import artist_stats as AS

    want_artist = AS.artist_key(artist)
    want_song = AS.song_key_for(artist, track)
    clean = _TRACK_NOISE.sub("", track).strip() or track
    queries = [f'track:"{clean}" artist:"{artist}"']
    if clean != track:
        queries.append(f'track:"{track}" artist:"{artist}"')
    for q in queries:
        res = _search(sp, q=q, type="track", limit=5)
        items = [t for t in ((res.get("tracks") or {}).get("items") or []) if t and t.get("id")]
        if not items:
            continue

        def score(t):
            names = [a.get("name") or "" for a in (t.get("artists") or [])]
            by_artist = bool(names) and AS.artist_key(names[0]) == want_artist
            same_song = AS.song_key_for(names[0] if names else artist, t.get("name")) == want_song
            return (not (by_artist and same_song), not by_artist, not same_song)

        best = min(items, key=score)
        artists = best.get("artists") or []
        small, large = _images((best.get("album") or {}).get("images"))
        return {
            "id": best["id"],
            "uri": best.get("uri") or f"spotify:track:{best['id']}",
            "name": best.get("name"),
            "artists": ", ".join(a.get("name") or "" for a in artists),
            "artist_list": [{"id": a.get("id"), "name": a.get("name")} for a in artists],
            "album": (best.get("album") or {}).get("name"),
            "image": small,
            "image_large": large,
            "duration_ms": best.get("duration_ms"),
            "url": (best.get("external_urls") or {}).get("spotify"),
            "exact": score(best) == (False, False, False),
        }
    return None


@bp.route("/api/v2/resolve")
def resolve():
    artist = (request.args.get("artist") or "").strip()
    track = (request.args.get("track") or "").strip()
    if not artist:
        return api_error("invalid_artist", "artist is required.", 400, field="artist")
    if len(artist) > _MAX_NAME:
        return api_error("invalid_artist", "artist is too long.", 400, field="artist")
    if len(track) > _MAX_NAME:
        return api_error("invalid_track", "track is too long.", 400, field="track")
    core = _core()
    sp = core.get_sp()
    if not sp:
        return api_error("not_authenticated", "Sign in to Spotify to play this.", 401)
    from src.services import artist_stats as AS

    try:
        out = {"track": None, "artist": None}
        if track:
            out["track"] = _resolve_track(sp, artist, track)
            primary = ((out["track"] or {}).get("artist_list") or [{}])[0]
            if primary.get("id") and AS.artist_key(primary.get("name")) == AS.artist_key(artist):
                # Spend no second search (the budget is 3/min): the track already names the artist.
                out["artist"] = {"id": primary["id"], "name": primary.get("name"), "image": None,
                                 "image_large": None, "uri": f"spotify:artist:{primary['id']}", "url": None,
                                 "exact": True}
        if out["artist"] is None:
            out["artist"] = _resolve_artist(sp, artist)
        return jsonify(out)
    except _RateLimited as e:
        return api_error("rate_limited", f"Spotify search is cooling down. Try again in {e.retry_after}s.", 429,
                         retry_after=e.retry_after)
    except Exception as e:
        core._log_app_error(e, "api_v2_resolve")
        return api_error("resolve_failed", "Could not look this up on Spotify.", 502)
