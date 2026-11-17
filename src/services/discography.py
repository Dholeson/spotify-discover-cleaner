"""
Discography service.

Fetches an artist's releases from Spotify, normalises them (dedup remasters / deluxe, classify
album / EP / single / live / compilation) and marks what the user has heard.

"Heard" always comes from the canonical plays ledger (src.services.artist_stats), matched per
artist: a release is explored through the plays of *this* artist whose normalized album key
matches the release, and a tracklist entry is heard when its song key (or Spotify id) has plays.
Spotify metadata is cacheable; heard state is recomputed on every request so a new play shows
up immediately.
"""

import logging
import re
import time
from collections import Counter, defaultdict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_REMASTER_RE = re.compile(
    r'\s*[\(\[](\d{4}\s+)?(remaster|remastered|deluxe|expanded|anniversary|special|'
    r'super|platinum|bonus|edition|version|reissue|re-?issue|re-?master|'
    r'collector|limited)[^\)\]]*[\)\]]',
    re.IGNORECASE,
)
_REMASTER_DASH_RE = re.compile(
    r'\s*-\s*(remaster(ed)?|deluxe|expanded|anniversary|special edition)\s*$',
    re.IGNORECASE,
)
# Only explicit live markers: '(Live ...)', 'Live at/from/in', 'in concert', ' - Live'.
# A bare word match used to file albums such as 'Live Forever' or 'Live Life' under live.
_LIVE_RE = re.compile(
    r'[\(\[]\s*live\b[^\)\]]*[\)\]]|\blive\s+(?:at|from|in)\b|\bin concert\b|\s-\s*live\b',
    re.IGNORECASE,
)
_COMP_RE = re.compile(
    r'greatest hits|best of|the collection|anthology|the essential|'
    r'singles collection|complete collection',
    re.IGNORECASE,
)
_EP_NAME_RE = re.compile(r'(?:\s-\s*|\s|\()EP\)?\s*$')


def _canonical_name(name: str) -> str:
    name = _REMASTER_RE.sub('', name or '')
    name = _REMASTER_DASH_RE.sub('', name)
    return name.strip()


def _classify(album: dict) -> str:
    name = album.get('name', '') or ''
    atype = (album.get('album_type') or 'album').lower()
    total = int(album.get('total_tracks') or 0)
    if _LIVE_RE.search(name):
        return 'live'
    if _COMP_RE.search(name) or atype == 'compilation':
        return 'compilation'
    if atype == 'ep' or _EP_NAME_RE.search(name):
        return 'ep'
    if atype == 'single':
        # Spotify files EPs under "single"; its own rule is 4-6 tracks = EP.
        return 'ep' if total >= 4 else 'single'
    return 'album'


def normalize_albums(raw_albums: list) -> list:
    """
    Given a raw list of Spotify album objects, return annotated + deduplicated list (copies).

    Added fields per album:
      canonical_name  : str   — name with remaster/deluxe suffixes stripped
      album_class     : str   — 'album' | 'ep' | 'single' | 'live' | 'compilation'
      is_variant      : bool  — True if this is a remaster/deluxe of an earlier release
      original_id     : str   — id of the kept original (only set when is_variant=True)
      release_year    : int
    """
    albums = [dict(a) for a in raw_albums or [] if a and a.get('id')]
    seen_ids = set()
    unique = []
    for a in albums:  # Spotify repeats a release when it appears in several include_groups
        if a['id'] in seen_ids:
            continue
        seen_ids.add(a['id'])
        unique.append(a)
    for a in unique:
        a['canonical_name'] = _canonical_name(a.get('name', ''))
        a['album_class'] = _classify(a)
        rd = a.get('release_date', '') or ''
        a['release_year'] = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else 0

    # Oldest first so the original is kept when deduplicating
    seen: dict = {}
    for a in sorted(unique, key=lambda x: x.get('release_date', '') or ''):
        key = a['album_class'] + '::' + a['canonical_name'].lower()
        if key in seen:
            a['is_variant'] = True
            a['original_id'] = seen[key]
        else:
            a['is_variant'] = False
            a['original_id'] = None
            seen[key] = a['id']

    unique.sort(key=lambda x: x.get('release_date', '') or '', reverse=True)  # newest first for display
    return unique


# ---------------------------------------------------------------------------
# Spotify fetch (metadata only: safe to cache)
# ---------------------------------------------------------------------------

def fetch_artist_albums(sp, artist_id: str,
                        include_groups: str = 'album,single,compilation') -> list:
    """
    Fetch all albums for an artist, handling pagination.
    Returns raw Spotify album objects (slim fields).
    """
    albums = []
    offset = 0
    limit = 20   # Spotify max is 50 but some spotipy versions choke at that
    # Spotify's include_groups does not support "ep"; EPs come back under album/single
    # and are classified locally.
    valid_groups = []
    for raw in str(include_groups or "").split(","):
        g = (raw or "").strip().lower()
        if g == "ep":
            g = "album"
        if g in {"album", "single", "compilation", "appears_on"} and g not in valid_groups:
            valid_groups.append(g)
    include_groups = ",".join(valid_groups or ["album", "single", "compilation"])

    while True:
        try:
            # Bypass spotipy.artist_albums() here because this spotipy build
            # always injects country=None into the request params, and Spotify
            # rejects that for this endpoint with a misleading "Invalid limit".
            page = sp._get(
                f"artists/{artist_id}/albums",
                include_groups=include_groups,
                limit=limit,
                offset=offset,
            )
        except Exception as _e:
            if "400" in str(_e) and limit > 10:
                limit = 10   # fall back to smaller page on Bad Request
                continue
            raise
        items = (page or {}).get('items') or []
        albums.extend(items)
        if len(items) < limit or not (page or {}).get('next'):
            break
        offset += limit
        time.sleep(0.05)  # stay well under rate limit
    return albums


def fetch_album(sp, album_id: str) -> dict:
    """A release with its complete track list (all pages)."""
    album = dict(sp.album(album_id) or {})
    page = album.get("tracks") or {}
    items = list(page.get("items") or [])
    while page.get("next"):
        page = sp.next(page) or {}
        items.extend(page.get("items") or [])
    album["tracks"] = {"items": items, "total": len(items), "next": None}
    return album


def infer_artist_name(raw_albums: list, artist_id: str = None) -> str:
    """The artist's display name from the releases' credits (no extra Spotify call)."""
    names = Counter()
    for a in raw_albums or []:
        for ar in (a or {}).get('artists') or []:
            if artist_id and ar.get('id') == artist_id and ar.get('name'):
                return ar['name']
        first = ((a or {}).get('artists') or [{}])[0].get('name')
        if first:
            names[first] += 1
    return names.most_common(1)[0][0] if names else ""


# ---------------------------------------------------------------------------
# Exploration state (heard) from the plays ledger
# ---------------------------------------------------------------------------

def _stats_module():
    from src.services import artist_stats as AS
    return AS


def _ledger_ok(conn) -> bool:
    if conn is None:
        return False
    try:
        return _stats_module().ledger_ready(conn)
    except Exception:
        log.exception("ledger readiness check failed")
        return False


def album_owner_map(albums: list, artist_key: str) -> dict:
    """album_key -> release id that owns the plays filed under that album name.

    Several releases can share a normalized name (the album and its title-track single, a
    deluxe reissue). Plays go to one owner, preferring the original album/EP over singles,
    compilations and live records; ties go to the earliest release.
    """
    K = _stats_module().keys()
    rank = {'album': 0, 'ep': 0, 'single': 1, 'compilation': 2, 'live': 3}
    owner = {}
    for a in sorted(albums, key=lambda a: (bool(a.get('is_variant')), rank.get(a.get('album_class'), 4),
                                           a.get('release_date') or '')):
        for name in {a.get('name') or '', a.get('canonical_name') or ''}:
            k = K.album_key(artist_key, name) if name else None
            if k:
                owner.setdefault(k, a['id'])
    return owner


def get_exploration_state(conn, albums: list, artist_name: str) -> dict:
    """{album_id: {heard_tracks, first_heard_at, last_heard_at}} for normalized releases.

    heard_tracks never exceeds the release's total_tracks.
    """
    AS = _stats_module()
    K = AS.keys()
    out = {}
    if not albums or not artist_name or not _ledger_ok(conn):
        return out
    heard = AS.artist_heard(conn, artist_name)
    a_key = heard["key"]
    owner = album_owner_map(albums, a_key)
    by_id = {a['id']: a for a in albums}
    for a in albums:
        owner_id = a['original_id'] if a.get('is_variant') and a.get('original_id') in by_id else a['id']
        songs = set()  # distinct recordings filed under this release (plus a single's title song)
        first = last = None
        for name in {a.get('name') or '', a.get('canonical_name') or ''}:
            k = K.album_key(a_key, name) if name else None
            if not k or owner.get(k) not in (owner_id, a['id']):
                continue
            detail = heard["albums"].get(k)
            if not detail:
                continue
            songs |= detail.get("track_keys") or detail["song_keys"]
            first = detail["first"] if first is None else min(first, detail["first"])
            last = detail["last"] if last is None else max(last, detail["last"])
        if a.get('album_class') == 'single':
            # A single's plays are often filed under the parent album: count its title song.
            title_song = K.song_key(a_key, a.get('canonical_name') or a.get('name') or '')
            if title_song in heard["song_keys"]:
                songs.add(title_song)
        total = int(a.get('total_tracks') or 0)
        heard_n = min(len(songs), total) if total else len(songs)
        if heard_n:
            out[a['id']] = {"heard_tracks": heard_n, "first_heard_at": first, "last_heard_at": last}
    return out


# ---------------------------------------------------------------------------
# Era analysis
# ---------------------------------------------------------------------------

def preferred_eras(albums: list, exploration: dict) -> list:
    """
    Returns a list of {era, albums_explored, heard_tracks} sorted by engagement,
    where era is a decade string like '2010s'.
    """
    decade_stats: dict = defaultdict(lambda: {'albums_explored': 0, 'heard_tracks': 0})
    for a in albums:
        if a.get('is_variant') or a['album_class'] in ('live', 'compilation'):
            continue
        year = a.get('release_year', 0)
        if not year:
            continue
        era = f"{(year // 10) * 10}s"
        ht = (exploration.get(a['id']) or {}).get('heard_tracks', 0)
        if ht > 0:
            decade_stats[era]['albums_explored'] += 1
        decade_stats[era]['heard_tracks'] += ht

    result = [{'era': era, **stats} for era, stats in decade_stats.items() if stats['heard_tracks'] > 0]
    result.sort(key=lambda x: (-x['heard_tracks'], x['era']))
    return result


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def build_discography_from_albums(raw_albums: list, conn, artist_name: str = None, artist_id: str = None) -> dict:
    """Normalise cached Spotify releases and attach fresh heard state."""
    albums = normalize_albums(raw_albums)
    name = (artist_name or "").strip() or infer_artist_name(raw_albums, artist_id)
    history_ready = _ledger_ok(conn)
    exploration = get_exploration_state(conn, albums, name) if history_ready else {}

    for a in albums:
        exp = exploration.get(a['id']) or {}
        total = int(a.get('total_tracks') or 0)
        heard = min(int(exp.get('heard_tracks') or 0), total) if total else int(exp.get('heard_tracks') or 0)
        a['heard_tracks'] = heard
        a['first_heard_at'] = exp.get('first_heard_at')
        a['last_heard_at'] = exp.get('last_heard_at')
        pct = min(100, round(heard / total * 100)) if total else (100 if heard else 0)
        a['exploration_pct'] = pct
        a['exploration_state'] = 'completed' if pct >= 80 else 'partial' if pct > 0 else 'unheard'

    eras = preferred_eras(albums, exploration)

    # Summary stats (non-variant albums/eps only)
    canon = [a for a in albums if not a['is_variant'] and a['album_class'] in ('album', 'ep')]
    explored_count = sum(1 for a in canon if a['heard_tracks'] > 0)
    heard_track_total = sum(int(a.get('heard_tracks') or 0) for a in canon)
    total_track_total = sum(int(a.get('total_tracks') or 0) for a in canon)
    unheard_releases = [
        {
            "id": a.get("id") or "",
            "name": a.get("canonical_name") or a.get("name") or "",
            "release_year": a.get("release_year") or None,
            "album_class": a.get("album_class") or "album",
            "total_tracks": int(a.get("total_tracks") or 0),
        }
        for a in canon if int(a.get('heard_tracks') or 0) <= 0
    ]

    return {
        'artist_id': artist_id,
        'artist_name': name,
        'history_ready': history_ready,
        'albums': albums,
        'eras': eras,
        'total_albums': len(canon),
        'explored_albums': explored_count,
        'top_era': eras[0]['era'] if eras else None,
        'catalog_tracks_total': total_track_total,
        'catalog_tracks_heard': heard_track_total,
        'catalog_coverage_pct': min(100, round((heard_track_total / total_track_total) * 100)) if total_track_total else 0,
        'unheard_releases': unheard_releases,
    }


def build_discography(sp, artist_id: str, conn, artist_name: str = None) -> dict:
    """Fetch + normalise discography, attach exploration state, compute era analysis."""
    raw = fetch_artist_albums(sp, artist_id)
    return build_discography_from_albums(raw, conn, artist_name=artist_name, artist_id=artist_id)


def build_album_tracklist_from_album(album: dict, conn, artist_name: str = None) -> dict:
    """Tracklist of a (cached) Spotify release with heard flags and play counts from the ledger."""
    album_artists = album.get("artists") or []
    page_artist = (artist_name or "").strip() or ((album_artists[0] or {}).get("name") if album_artists else "") or ""
    items = list(((album.get("tracks") or {}).get("items")) or [])
    history_ready = _ledger_ok(conn)
    counts = [0] * len(items)
    if history_ready and items:
        AS = _stats_module()
        entries = []
        for t in items:
            credits = [a.get("name") for a in (t.get("artists") or []) if a.get("name")]
            entries.append({"artist": page_artist, "artists_list": credits[:1], "title": t.get("name") or "",
                            "id": t.get("id") or None})
        counts = AS.song_play_counts(conn, entries)

    tracks = []
    for idx, (t, plays) in enumerate(zip(items, counts), start=1):
        tracks.append({
            "id": t.get("id") or "",
            "uri": t.get("uri") or "",
            "name": t.get("name") or "",
            "artists": ", ".join(a.get("name") or "" for a in (t.get("artists") or [])),
            "duration_ms": t.get("duration_ms") or 0,
            "track_number": t.get("track_number") or idx,
            "disc_number": t.get("disc_number") or 1,
            "heard": plays > 0,
            "my_plays": plays,
            "preview_url": t.get("preview_url") or "",
        })

    return {
        "album_id": album.get("id") or "",
        "album_name": album.get("name") or "",
        "artist_name": page_artist,
        "history_ready": history_ready,
        "tracks": tracks,
        "heard_tracks": sum(1 for t in tracks if t["heard"]),
        "total_tracks": len(tracks),
    }


def build_album_tracklist(sp, album_id: str, conn, artist_name: str = None) -> dict:
    """Fetch a single release's tracklist and mark which tracks have been heard."""
    album = fetch_album(sp, album_id)
    album.setdefault("id", album_id)
    return build_album_tracklist_from_album(album, conn, artist_name=artist_name)
