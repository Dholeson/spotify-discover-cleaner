"""Small endpoints for the Now view.

POST /api/v2/backlog/play {shuffle?: bool}
    Start playing the backlog playlist (the one that gets cleared) on the user's Spotify, so an
    idle "Nothing playing" screen has a one-click way back into clearing the backlog. With
    shuffle, playback starts at a random backlog track and Spotify's shuffle is switched on
    (best effort: the random start already gives a shuffled feel when that call fails).
    Everything goes through core.get_sp(), i.e. the Spotify guard.
"""

from __future__ import annotations

import random

from flask import Blueprint, jsonify, request

from src.web import api_error

bp = Blueprint("now_api", __name__)


def _core():
    import app as core
    return core


def _pick_device(sp):
    """The active device, else the first one Spotify lists, else None (let Spotify decide).
    A failed lookup is not fatal: start_playback without a device still works when one is active."""
    try:
        devices = (sp.devices() or {}).get("devices") or []
    except Exception:
        return None
    active = next((d for d in devices if d.get("is_active")), None)
    chosen = active or (devices[0] if devices else None)
    return (chosen or {}).get("id")


def _retry_after(exc, default=5):
    ra = getattr(exc, "retry_after", None)
    if ra is None:
        try:
            ra = int((getattr(exc, "headers", None) or {}).get("Retry-After", default))
        except (TypeError, ValueError):
            ra = default
    try:
        return max(1, int(round(float(ra))))
    except (TypeError, ValueError):
        return default


def _playback_error(core, exc):
    blocked = getattr(core, "SpotifyGuardBlocked", None)
    # The guard's local block subclasses SpotifyException(429); check it first: nothing was sent.
    if blocked is not None and isinstance(exc, blocked):
        return api_error("spotify_busy", "Spotify is pacing requests, so playback didn't start. Try again in a moment.",
                         503, retry_after=_retry_after(exc))
    status = getattr(exc, "http_status", None)
    text = f"{getattr(exc, 'reason', '') or ''} {getattr(exc, 'msg', '') or ''} {exc}"
    if status == 429:
        return api_error("rate_limited", "Spotify is rate-limiting playback right now. Try again in a bit.",
                         429, retry_after=_retry_after(exc, 30))
    if status == 404 or "NO_ACTIVE_DEVICE" in text:
        return api_error("no_device", "No Spotify device is available. Open Spotify on your phone or computer, "
                         "then try again.", 409)
    if status == 403:
        return api_error("playback_forbidden", "Spotify refused to start playback. Controlling playback needs "
                         "Spotify Premium.", 403)
    log = getattr(core, "_log_app_error", None)
    if callable(log):
        try:
            log(exc, "backlog play")
        except Exception:
            pass
    return api_error("spotify_error", "Spotify couldn't start playback. Try again, or start it in Spotify.", 502)


@bp.route("/api/v2/backlog/play", methods=["POST"])
def backlog_play():
    core = _core()
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return api_error("invalid_body", "The request body must be a JSON object.", 400)
    shuffle = data.get("shuffle", False)
    if not isinstance(shuffle, bool):
        return api_error("invalid_shuffle", "'shuffle' must be true or false.", 400, field="shuffle")

    sp = core.get_sp()
    if not sp:
        return api_error("signed_out", "Spotify is not connected. Connect it to play your backlog.", 401)

    index = core._backlog_index()
    pid = getattr(index, "pid", None)
    if not pid:
        with core.state_lock:
            pid = core.state.get("playlist_id")
    if not pid:
        return api_error("no_backlog", "Choose the playlist to clear in Settings first.", 409)

    try:
        items = [e for e in (index.items() if getattr(index, "pid", None) == pid else [])
                 if e.get("uri") and not e.get("is_local")]
    except Exception:
        items = []
    start = (random.choice(items) if shuffle else items[0]) if items else None
    kwargs = {"context_uri": f"spotify:playlist:{pid}"}
    if start is not None:
        # An explicit first track: "in order" really starts at the top, "shuffle" at a random track.
        kwargs["offset"] = {"uri": start["uri"]}

    shuffled = False
    try:
        device_id = _pick_device(sp)
        sp.start_playback(device_id=device_id, **kwargs)
    except Exception as exc:
        return _playback_error(core, exc)
    if shuffle:
        try:
            sp.shuffle(True, device_id=device_id)
            shuffled = True
        except Exception:
            shuffled = False  # playback already started at a random track; that is enough

    changed = getattr(core, "_playback_changed", None)
    if callable(changed):
        changed(is_playing=True, progress_ms=0)
    name = getattr(index, "name", "") or ""
    if not name:
        with core.state_lock:
            name = core.state.get("playlist_name") or ""
    return jsonify({
        "playing": True,
        "playlist_id": pid,
        "name": name,
        "shuffle": shuffle,
        "shuffle_state": shuffled,
        "started_with": ({"track_id": start.get("track_id"), "name": start.get("name") or "",
                          "artists": start.get("artists") or ""} if start is not None else None),
    })
